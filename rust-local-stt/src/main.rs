use anyhow::{anyhow, Context, Result};
use axum::{extract::{multipart::MultipartRejection, DefaultBodyLimit, Multipart, State}, http::StatusCode, response::{IntoResponse, Response}, routing::{get, post}, Json, Router};
use once_cell::sync::Lazy;
use serde::{Deserialize, Serialize};
use std::{collections::HashMap, env, io::Cursor, net::SocketAddr, path::{Path, PathBuf}, sync::Mutex};
use transcribe_rs::{onnx::{gigaam::GigaAMModel, parakeet::{ParakeetModel, ParakeetParams, TimestampGranularity}, Quantization}, vad::{SileroVad, SmoothedVad, Vad}, whisper_cpp::{WhisperEngine, WhisperInferenceParams}, SpeechModel, TranscribeOptions};

static ENGINE_CACHE: Lazy<Mutex<HashMap<String, LoadedEngine>>> = Lazy::new(|| Mutex::new(HashMap::new()));
const DEFAULT_MAX_UPLOAD_BYTES: usize = 256 * 1024 * 1024;

fn max_upload_bytes() -> usize {
    env::var("KWISPR_MAX_UPLOAD_BYTES")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .filter(|value| *value > 0)
        .unwrap_or(DEFAULT_MAX_UPLOAD_BYTES)
}

#[derive(Clone)] struct AppState { catalog: Catalog, model_dir: PathBuf, vad: VadConfig }
#[derive(Clone, Deserialize)] struct Catalog { models: Vec<ModelInfo> }
#[derive(Clone, Debug, Deserialize)] struct ModelInfo { id: String, name: String, engine_type: String, artifact: Artifact, #[serde(default)] supports_language_selection: bool }
#[derive(Clone, Debug, Deserialize)] struct Artifact { filename: String, #[serde(default)] is_directory: bool }
#[derive(Serialize)] struct Health { status: &'static str, vad: VadConfig }
#[derive(Serialize)] struct Transcription { text: String }
#[derive(Serialize)] struct ErrorBody { error: String }

#[derive(Clone, Debug, Serialize)]
struct VadConfig {
    enabled: bool,
    provider: VadProvider,
    model_path: Option<PathBuf>,
    threshold: f32,
    frame_ms: u32,
    min_speech_ms: u32,
    padding_ms: u32,
}

#[derive(Clone, Copy, Debug, Serialize, PartialEq)]
#[serde(rename_all = "kebab-case")]
enum VadProvider { Energy, Silero }

#[derive(Debug, PartialEq)]
enum VadDecision {
    Disabled,
    Trimmed { start: usize, end: usize },
    NoSpeech,
}

#[derive(Debug)]
struct PreprocessedAudio { samples: Vec<f32>, decision: VadDecision }
struct DecodedAudio { samples: Vec<f32>, sample_rate: u32 }

enum LoadedEngine { GigaAM(GigaAMModel), Parakeet(ParakeetModel), Whisper(WhisperEngine) }

#[tokio::main]
async fn main() -> Result<()> {
    let host = arg("--host").unwrap_or_else(|| "127.0.0.1".into());
    let port: u16 = arg("--port").unwrap_or_else(|| "9000".into()).parse()?;
    let catalog_path = PathBuf::from(arg("--catalog").unwrap_or_else(|| "models/local-stt-catalog.json".into()));
    let model_dir = env::var("KWISPR_MODEL_DIR").map(PathBuf::from).unwrap_or_else(|_| home_models_dir());
    let vad = VadConfig::from_env_and_args()?;
    let catalog: Catalog = serde_json::from_slice(&std::fs::read(&catalog_path).with_context(|| format!("read catalog {}", catalog_path.display()))?)?;
    let app_state = AppState { catalog, model_dir, vad: vad.clone() };
    let app = Router::new().route("/health", get(health))
        .route("/v1/audio/transcriptions", post(transcribe))
        .layer(DefaultBodyLimit::max(max_upload_bytes()))
        .with_state(app_state);
    let addr: SocketAddr = format!("{host}:{port}").parse()?;
    println!("kwispr local STT runtime listening on http://{addr} (vad_enabled={})", vad.enabled);
    let listener = tokio::net::TcpListener::bind(addr).await?;
    axum::serve(listener, app).await?;
    Ok(())
}

async fn health(State(state): State<AppState>) -> Json<Health> {
    Json(Health { status: "ok", vad: state.vad.clone() })
}

async fn transcribe(State(state): State<AppState>, mp: std::result::Result<Multipart, MultipartRejection>) -> std::result::Result<Json<Transcription>, ApiError> {
    let mut mp = mp.map_err(ApiError::multipart_rejection)?;
    let mut model = None; let mut lang = None; let mut format = "json".to_string(); let mut file = None;
    while let Some(field) = mp.next_field().await.map_err(ApiError::multipart_error)? {
        match field.name().unwrap_or("") {
            "model" => model = Some(field.text().await.map_err(ApiError::multipart_error)?),
            "language" => lang = Some(field.text().await.map_err(ApiError::multipart_error)?),
            "response_format" => format = field.text().await.map_err(ApiError::multipart_error)?,
            "file" => file = Some(field.bytes().await.map_err(ApiError::multipart_error)?.to_vec()),
            _ => {}
        }
    }
    if format != "json" { return Err(ApiError::bad_request(anyhow!("only response_format=json is supported"))); }
    let model_id = model.ok_or_else(|| ApiError::bad_request(anyhow!("missing model field")))?;
    let bytes = file.ok_or_else(|| ApiError::bad_request(anyhow!("missing audio file field: file")))?;
    let info = resolve_model(&state.catalog, &model_id)?;
    let audio = decode_wav(&bytes).map_err(ApiError::bad_request)?;
    let preprocessed = preprocess_audio(audio, &state.vad).map_err(ApiError::bad_request)?;
    if preprocessed.decision == VadDecision::NoSpeech { return Ok(Json(Transcription { text: String::new() })); }
    let text = tokio::task::spawn_blocking(move || transcribe_blocking(&state.model_dir, &info, preprocessed.samples, lang)).await.map_err(|e| ApiError::internal(anyhow!(e)))??;
    Ok(Json(Transcription { text }))
}

fn resolve_model(catalog: &Catalog, model_id: &str) -> std::result::Result<ModelInfo, ApiError> {
    catalog.models.iter().find(|m| m.id == model_id).cloned().ok_or_else(|| ApiError::not_found(anyhow!("unknown model: {model_id}")))
}

fn transcribe_blocking(model_dir: &Path, info: &ModelInfo, audio: Vec<f32>, language: Option<String>) -> std::result::Result<String, ApiError> {
    let mut cache = ENGINE_CACHE.lock().map_err(|_| ApiError::internal(anyhow!("engine cache lock poisoned")))?;
    if !cache.contains_key(&info.id) { cache.insert(info.id.clone(), load_engine(model_dir, info).map_err(ApiError::runtime)?); }
    let engine = cache.get_mut(&info.id).unwrap();
    let result = match engine {
        LoadedEngine::GigaAM(e) => e.transcribe(&audio, &TranscribeOptions::default()).map_err(|e| ApiError::runtime(anyhow!("GigaAM transcription failed: {e}")))?,
        LoadedEngine::Parakeet(e) => e.transcribe_with(&audio, &ParakeetParams { timestamp_granularity: Some(TimestampGranularity::Segment), ..Default::default() }).map_err(|e| ApiError::runtime(anyhow!("Parakeet transcription failed: {e}")))?,
        LoadedEngine::Whisper(e) => e.transcribe_with(&audio, &WhisperInferenceParams { language: if info.supports_language_selection { language } else { None }, ..Default::default() }).map_err(|e| ApiError::runtime(anyhow!("Whisper transcription failed: {e}")))?,
    };
    Ok(result.text.trim().to_string())
}

fn load_engine(model_dir: &Path, info: &ModelInfo) -> Result<LoadedEngine> {
    let path = model_path(model_dir, info);
    if !path.exists() { return Err(anyhow!("model '{}' ({}) is not installed at {}", info.id, info.name, path.display())); }
    match info.engine_type.as_str() {
        "gigaam" => Ok(LoadedEngine::GigaAM(GigaAMModel::load(&path, &Quantization::Int8)?)),
        "parakeet" => Ok(LoadedEngine::Parakeet(ParakeetModel::load(&path, &Quantization::Int8)?)),
        "whisper" => Ok(LoadedEngine::Whisper(WhisperEngine::load(&path)?)),
        other => Err(anyhow!("unsupported engine_type '{other}' for model {}", info.id)),
    }
}

fn model_path(model_dir: &Path, info: &ModelInfo) -> PathBuf {
    if !info.artifact.is_directory {
        return model_dir.join(&info.artifact.filename);
    }
    let base = model_dir.join(&info.id);
    let nested = base.join(&info.artifact.filename);
    if nested.is_dir() { nested } else { base }
}

fn decode_wav(bytes: &[u8]) -> Result<DecodedAudio> {
    let mut r = hound::WavReader::new(Cursor::new(bytes)).context("expected WAV audio")?;
    let spec = r.spec();
    if spec.channels == 0 { return Err(anyhow!("WAV has zero channels")); }
    let mut out = Vec::new();
    match spec.sample_format {
        hound::SampleFormat::Float => { for s in r.samples::<f32>() { out.push(s?); } },
        hound::SampleFormat::Int => { let max = (1_i64 << (spec.bits_per_sample.saturating_sub(1) as i64)) as f32; for s in r.samples::<i32>() { out.push(s? as f32 / max); } }
    }
    if spec.channels > 1 { out = out.chunks(spec.channels as usize).map(|c| c.iter().sum::<f32>() / c.len() as f32).collect(); }
    Ok(DecodedAudio { samples: out, sample_rate: spec.sample_rate })
}

fn preprocess_audio(audio: DecodedAudio, vad: &VadConfig) -> Result<PreprocessedAudio> {
    vad.validate()?;
    if !vad.enabled { return Ok(PreprocessedAudio { samples: audio.samples, decision: VadDecision::Disabled }); }
    match vad.provider {
        VadProvider::Energy => preprocess_energy_audio(audio, vad),
        VadProvider::Silero => preprocess_silero_audio(audio, vad),
    }
}

fn preprocess_energy_audio(audio: DecodedAudio, vad: &VadConfig) -> Result<PreprocessedAudio> {
    let frame = samples_for_ms(audio.sample_rate, vad.frame_ms).max(1);
    let min_speech_frames = frames_for_ms(vad.min_speech_ms, vad.frame_ms).max(1);
    let padding = samples_for_ms(audio.sample_rate, vad.padding_ms);
    let mut voiced = Vec::new();
    for (i, chunk) in audio.samples.chunks(frame).enumerate() {
        let rms = (chunk.iter().map(|s| s * s).sum::<f32>() / chunk.len() as f32).sqrt();
        if rms >= vad.threshold { voiced.push(i); }
    }
    trim_from_voiced_frames(audio.samples, frame, padding, min_speech_frames, voiced)
}

fn preprocess_silero_audio(audio: DecodedAudio, vad: &VadConfig) -> Result<PreprocessedAudio> {
    if audio.sample_rate != 16_000 { return Err(anyhow!("Silero VAD requires 16 kHz WAV audio, got {} Hz", audio.sample_rate)); }
    let model_path = vad.model_path.as_ref().ok_or_else(|| anyhow!("Silero VAD requires KWISPR_VAD_MODEL=/path/to/silero_vad_v4.onnx or --vad-model"))?;
    let frame = 480;
    let prefill = frames_for_ms(vad.padding_ms, 30);
    let hangover = frames_for_ms(vad.padding_ms, 30);
    let onset = frames_for_ms(vad.min_speech_ms, 30).max(1);
    let mut detector = SmoothedVad::new(Box::new(SileroVad::new(model_path, vad.threshold)?), prefill, hangover, onset);
    let mut voiced = Vec::new();
    for (i, chunk) in audio.samples.chunks(frame).enumerate() {
        if chunk.len() != frame { break; }
        if detector.is_speech(chunk)? { voiced.push(i); }
    }
    trim_from_voiced_frames(audio.samples, frame, 0, 1, voiced)
}

fn trim_from_voiced_frames(samples: Vec<f32>, frame: usize, padding: usize, min_speech_frames: usize, voiced: Vec<usize>) -> Result<PreprocessedAudio> {
    if voiced.len() < min_speech_frames { return Ok(PreprocessedAudio { samples: Vec::new(), decision: VadDecision::NoSpeech }); }
    let first = voiced[0] * frame;
    let last = ((voiced[voiced.len() - 1] + 1) * frame).min(samples.len());
    let start = first.saturating_sub(padding);
    let end = (last + padding).min(samples.len());
    Ok(PreprocessedAudio { samples: samples[start..end].to_vec(), decision: VadDecision::Trimmed { start, end } })
}

fn samples_for_ms(sample_rate: u32, ms: u32) -> usize { ((sample_rate as u64 * ms as u64) / 1000) as usize }
fn frames_for_ms(ms: u32, frame_ms: u32) -> usize { ms.div_ceil(frame_ms) as usize }
fn home_models_dir() -> PathBuf { env::var("HOME").map(PathBuf::from).unwrap_or_else(|_| PathBuf::from(".")).join(".local/share/kwispr/models") }
fn arg(name: &str) -> Option<String> { let mut args = env::args().skip(1); while let Some(a) = args.next() { if a == name { return args.next(); } } None }
fn env_or_arg(name: &str, var: &str) -> Option<String> { arg(name).or_else(|| env::var(var).ok()) }

impl VadConfig {
    fn from_env_and_args() -> Result<Self> {
        let provider = parse_vad_provider(env_or_arg("--vad-provider", "KWISPR_VAD_PROVIDER").as_deref())?;
        let config = Self {
            enabled: parse_bool(env_or_arg("--vad-enabled", "KWISPR_VAD_ENABLED").as_deref()).unwrap_or(false),
            provider,
            model_path: env_or_arg("--vad-model", "KWISPR_VAD_MODEL").map(PathBuf::from),
            threshold: env_or_arg("--vad-threshold", "KWISPR_VAD_THRESHOLD").unwrap_or_else(|| default_vad_threshold(provider).into()).parse().context("parse VAD threshold")?,
            frame_ms: env_or_arg("--vad-frame-ms", "KWISPR_VAD_FRAME_MS").unwrap_or_else(|| "30".into()).parse().context("parse VAD frame ms")?,
            min_speech_ms: env_or_arg("--vad-min-speech-ms", "KWISPR_VAD_MIN_SPEECH_MS").unwrap_or_else(|| "150".into()).parse().context("parse VAD min speech ms")?,
            padding_ms: env_or_arg("--vad-padding-ms", "KWISPR_VAD_PADDING_MS").unwrap_or_else(|| "120".into()).parse().context("parse VAD padding ms")?,
        };
        config.validate()?;
        Ok(config)
    }

    fn validate(&self) -> Result<()> {
        if self.enabled && self.frame_ms == 0 { return Err(anyhow!("VAD frame ms must be greater than 0")); }
        if self.enabled && !self.threshold.is_finite() { return Err(anyhow!("VAD threshold must be finite")); }
        if self.enabled && self.threshold < 0.0 { return Err(anyhow!("VAD threshold must be non-negative")); }
        if self.enabled && self.provider == VadProvider::Silero && self.model_path.is_none() { return Err(anyhow!("Silero VAD requires KWISPR_VAD_MODEL=/path/to/silero_vad_v4.onnx or --vad-model")); }
        Ok(())
    }
}

fn default_vad_threshold(provider: VadProvider) -> &'static str {
    match provider {
        VadProvider::Energy => "0.01",
        VadProvider::Silero => "0.3",
    }
}

fn parse_vad_provider(value: Option<&str>) -> Result<VadProvider> {
    match value.unwrap_or("energy").to_ascii_lowercase().as_str() {
        "energy" | "rms" => Ok(VadProvider::Energy),
        "silero" | "silero-onnx" => Ok(VadProvider::Silero),
        other => Err(anyhow!("unknown VAD provider: {other}")),
    }
}

fn parse_bool(value: Option<&str>) -> Option<bool> {
    match value?.to_ascii_lowercase().as_str() {
        "1" | "true" | "yes" | "on" => Some(true),
        "0" | "false" | "no" | "off" => Some(false),
        _ => None,
    }
}

#[derive(Debug)]
struct ApiError(StatusCode, String);
impl ApiError {
    fn bad_request(e: impl Into<anyhow::Error>) -> Self { Self(StatusCode::BAD_REQUEST, e.into().to_string()) }
    fn not_found(e: impl Into<anyhow::Error>) -> Self { Self(StatusCode::NOT_FOUND, e.into().to_string()) }
    fn runtime(e: impl Into<anyhow::Error>) -> Self { Self(StatusCode::UNPROCESSABLE_ENTITY, e.into().to_string()) }
    fn internal(e: impl Into<anyhow::Error>) -> Self { Self(StatusCode::INTERNAL_SERVER_ERROR, e.into().to_string()) }
    fn multipart_rejection(e: MultipartRejection) -> Self { Self(client_error_status(e.status()), e.body_text()) }
    fn multipart_error(e: axum::extract::multipart::MultipartError) -> Self { Self(client_error_status(e.status()), e.body_text()) }
}

fn client_error_status(status: StatusCode) -> StatusCode {
    if status.is_client_error() { status } else { StatusCode::BAD_REQUEST }
}
impl IntoResponse for ApiError { fn into_response(self) -> Response { (self.0, Json(ErrorBody { error: self.1 })).into_response() } }

#[cfg(test)]
mod tests {
    use super::*;

    fn test_vad() -> VadConfig { VadConfig { enabled: true, provider: VadProvider::Energy, model_path: None, threshold: 0.01, frame_ms: 10, min_speech_ms: 30, padding_ms: 10 } }

    #[test]
    fn directory_model_path_uses_nested_artifact_directory_when_present() {
        let tmp = std::env::temp_dir().join(format!("kwispr-model-path-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        std::fs::create_dir_all(tmp.join("model-id/model-artifact-dir")).unwrap();
        let info = ModelInfo {
            id: "model-id".into(),
            name: "Model".into(),
            engine_type: "parakeet".into(),
            artifact: Artifact { filename: "model-artifact-dir".into(), is_directory: true },
            supports_language_selection: false,
        };
        assert_eq!(model_path(&tmp, &info), tmp.join("model-id/model-artifact-dir"));
        std::fs::remove_dir_all(&tmp).unwrap();
    }

    #[test]
    fn vad_skips_silence() {
        let audio = DecodedAudio { samples: vec![0.0; 1600], sample_rate: 16_000 };
        let out = preprocess_audio(audio, &test_vad()).unwrap();
        assert_eq!(out.decision, VadDecision::NoSpeech);
        assert!(out.samples.is_empty());
    }

    #[test]
    fn vad_rejects_short_noise() {
        let mut samples = vec![0.0; 1600];
        for s in &mut samples[320..480] { *s = 0.2; }
        let out = preprocess_audio(DecodedAudio { samples, sample_rate: 16_000 }, &test_vad()).unwrap();
        assert_eq!(out.decision, VadDecision::NoSpeech);
    }

    #[test]
    fn vad_trims_leading_and_trailing_silence_with_padding() {
        let mut samples = vec![0.0; 3200];
        for s in &mut samples[800..1600] { *s = 0.2; }
        let out = preprocess_audio(DecodedAudio { samples, sample_rate: 16_000 }, &test_vad()).unwrap();
        assert_eq!(out.decision, VadDecision::Trimmed { start: 640, end: 1760 });
        assert_eq!(out.samples.len(), 1120);
    }

    #[test]
    fn vad_disabled_preserves_audio() {
        let audio = DecodedAudio { samples: vec![0.0; 1600], sample_rate: 16_000 };
        let out = preprocess_audio(audio, &VadConfig { enabled: false, ..test_vad() }).unwrap();
        assert_eq!(out.decision, VadDecision::Disabled);
        assert_eq!(out.samples.len(), 1600);
    }

    #[test]
    fn vad_rejects_zero_frame_ms_in_preprocess() {
        let audio = DecodedAudio { samples: vec![0.0; 1600], sample_rate: 16_000 };
        let err = preprocess_audio(audio, &VadConfig { frame_ms: 0, ..test_vad() }).unwrap_err();
        assert!(err.to_string().contains("VAD frame ms must be greater than 0"));
    }

    #[test]
    fn vad_rejects_invalid_threshold() {
        let err = VadConfig { threshold: f32::NAN, ..test_vad() }.validate().unwrap_err();
        assert!(err.to_string().contains("VAD threshold must be finite"));
    }

    #[test]
    fn silero_vad_requires_model_path_when_enabled() {
        let err = VadConfig { provider: VadProvider::Silero, model_path: None, threshold: 0.3, ..test_vad() }.validate().unwrap_err();
        assert!(err.to_string().contains("Silero VAD requires"));
    }

    #[test]
    fn parses_vad_provider_aliases() {
        assert_eq!(parse_vad_provider(None).unwrap(), VadProvider::Energy);
        assert_eq!(parse_vad_provider(Some("rms")).unwrap(), VadProvider::Energy);
        assert_eq!(parse_vad_provider(Some("silero-onnx")).unwrap(), VadProvider::Silero);
        assert!(parse_vad_provider(Some("bogus")).is_err());
    }

    #[test]
    fn unknown_model_is_rejected_before_silent_vad_skip() {
        let catalog = Catalog { models: vec![ModelInfo {
            id: "known-model".into(),
            name: "Known Model".into(),
            engine_type: "whisper".into(),
            artifact: Artifact { filename: "known.bin".into(), is_directory: false },
            supports_language_selection: false,
        }] };
        let err = resolve_model(&catalog, "missing-model").unwrap_err();
        assert_eq!(err.0, StatusCode::NOT_FOUND);
        assert!(err.1.contains("unknown model: missing-model"));

        let silent = preprocess_audio(DecodedAudio { samples: vec![0.0; 1600], sample_rate: 16_000 }, &test_vad()).unwrap();
        assert_eq!(silent.decision, VadDecision::NoSpeech);
    }

    #[test]
    fn silent_audio_with_valid_model_skips_before_model_load() {
        let catalog = Catalog { models: vec![ModelInfo {
            id: "known-model".into(),
            name: "Known Model".into(),
            engine_type: "whisper".into(),
            artifact: Artifact { filename: "known.bin".into(), is_directory: false },
            supports_language_selection: false,
        }] };
        let info = resolve_model(&catalog, "known-model").unwrap();
        assert_eq!(info.id, "known-model");

        let silent = preprocess_audio(DecodedAudio { samples: vec![0.0; 1600], sample_rate: 16_000 }, &test_vad()).unwrap();
        assert_eq!(silent.decision, VadDecision::NoSpeech);
    }
}
