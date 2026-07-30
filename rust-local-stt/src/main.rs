use anyhow::{anyhow, Context, Result};
use axum::{
    extract::{multipart::MultipartRejection, DefaultBodyLimit, Multipart, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use once_cell::sync::Lazy;
use serde::{Deserialize, Serialize};
use std::{
    collections::{HashMap, HashSet},
    env,
    io::Cursor,
    net::SocketAddr,
    path::{Path, PathBuf},
    process::Stdio,
    sync::Mutex,
    time::Duration,
};
use tokio::{io::AsyncWriteExt, process::Command, time::timeout};
use transcribe_cpp::{Backend, Model, ModelOptions, RunOptions, Session};
use transcribe_rs::vad::{SileroVad, SmoothedVad, Vad};

static SESSION_CACHE: Lazy<Mutex<HashMap<String, Session>>> =
    Lazy::new(|| Mutex::new(HashMap::new()));
const DEFAULT_MAX_UPLOAD_BYTES: usize = 256 * 1024 * 1024;
const DEFAULT_FFMPEG_TIMEOUT_SECONDS: u64 = 30;

fn max_upload_bytes() -> usize {
    env::var("KWISPR_MAX_UPLOAD_BYTES")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .filter(|value| *value > 0)
        .unwrap_or(DEFAULT_MAX_UPLOAD_BYTES)
}

fn ffmpeg_timeout() -> Duration {
    Duration::from_secs(
        env::var("KWISPR_FFMPEG_TIMEOUT_SECONDS")
            .ok()
            .and_then(|value| value.parse::<u64>().ok())
            .filter(|value| *value > 0)
            .unwrap_or(DEFAULT_FFMPEG_TIMEOUT_SECONDS),
    )
}

#[derive(Clone)]
struct AppState {
    catalog: Catalog,
    model_dir: PathBuf,
    vad: VadConfig,
}
#[derive(Clone, Deserialize)]
struct Catalog {
    catalog_version: u32,
    models: Vec<ModelInfo>,
}
#[derive(Clone, Debug, Deserialize)]
struct ModelInfo {
    id: String,
    revision: String,
    slug: String,
    name: String,
    architecture: String,
    languages: Vec<String>,
    capabilities: ModelCapabilities,
    files: Vec<QuantFile>,
    default_quant: String,
}
#[derive(Clone, Debug, Deserialize)]
struct ModelCapabilities {
    lang_detect: bool,
}
#[derive(Clone, Debug, Deserialize)]
struct QuantFile {
    filename: String,
    quant: String,
    size_bytes: u64,
    sha256: String,
}
#[derive(Serialize)]
struct Health {
    status: &'static str,
    vad: VadConfig,
}
#[derive(Serialize)]
struct Transcription {
    text: String,
}
#[derive(Serialize)]
struct ErrorBody {
    error: String,
}

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
enum VadProvider {
    Energy,
    Silero,
}

#[derive(Debug, PartialEq)]
enum VadDecision {
    Disabled,
    Trimmed { start: usize, end: usize },
    NoSpeech,
}

#[derive(Debug)]
struct PreprocessedAudio {
    samples: Vec<f32>,
    decision: VadDecision,
}
#[derive(Debug)]
struct DecodedAudio {
    samples: Vec<f32>,
    sample_rate: u32,
}

#[tokio::main]
async fn main() -> Result<()> {
    transcribe_cpp::init_logging();
    transcribe_cpp::init_backends_default()
        .context("initialize transcribe-cpp dynamic backends")?;
    let host = arg("--host").unwrap_or_else(|| "127.0.0.1".into());
    let port: u16 = arg("--port").unwrap_or_else(|| "9000".into()).parse()?;
    let catalog_path =
        PathBuf::from(arg("--catalog").unwrap_or_else(|| "models/local-stt-catalog.json".into()));
    let model_dir = env::var("KWISPR_MODEL_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| home_models_dir());
    let vad = VadConfig::from_env_and_args()?;
    let catalog: Catalog = serde_json::from_slice(
        &std::fs::read(&catalog_path)
            .with_context(|| format!("read catalog {}", catalog_path.display()))?,
    )?;
    validate_catalog(&catalog)?;
    let app_state = AppState {
        catalog,
        model_dir,
        vad: vad.clone(),
    };
    let app = Router::new()
        .route("/health", get(health))
        .route("/v1/audio/transcriptions", post(transcribe))
        .layer(DefaultBodyLimit::max(max_upload_bytes()))
        .with_state(app_state);
    let addr: SocketAddr = format!("{host}:{port}").parse()?;
    println!(
        "kwispr local STT runtime listening on http://{addr} (vad_enabled={})",
        vad.enabled
    );
    let listener = tokio::net::TcpListener::bind(addr).await?;
    axum::serve(listener, app).await?;
    Ok(())
}

async fn health(State(state): State<AppState>) -> Json<Health> {
    Json(Health {
        status: "ok",
        vad: state.vad.clone(),
    })
}

async fn transcribe(
    State(state): State<AppState>,
    mp: std::result::Result<Multipart, MultipartRejection>,
) -> std::result::Result<Json<Transcription>, ApiError> {
    let mut mp = mp.map_err(ApiError::multipart_rejection)?;
    let mut model = None;
    let mut lang = None;
    let mut format = "json".to_string();
    let mut file = None;
    while let Some(field) = mp.next_field().await.map_err(ApiError::multipart_error)? {
        match field.name().unwrap_or("") {
            "model" => model = Some(field.text().await.map_err(ApiError::multipart_error)?),
            "language" => lang = Some(field.text().await.map_err(ApiError::multipart_error)?),
            "response_format" => format = field.text().await.map_err(ApiError::multipart_error)?,
            "file" => {
                file = Some(
                    field
                        .bytes()
                        .await
                        .map_err(ApiError::multipart_error)?
                        .to_vec(),
                )
            }
            _ => {}
        }
    }
    if format != "json" {
        return Err(ApiError::bad_request(anyhow!(
            "only response_format=json is supported"
        )));
    }
    let model_id = model.ok_or_else(|| ApiError::bad_request(anyhow!("missing model field")))?;
    let bytes =
        file.ok_or_else(|| ApiError::bad_request(anyhow!("missing audio file field: file")))?;
    let info = resolve_model(&state.catalog, &model_id)?;
    let language = effective_language(&info, lang.as_deref())?;
    let audio = decode_audio(&bytes).await.map_err(ApiError::bad_request)?;
    let preprocessed = preprocess_audio(audio, &state.vad).map_err(ApiError::bad_request)?;
    if preprocessed.decision == VadDecision::NoSpeech {
        return Ok(Json(Transcription {
            text: String::new(),
        }));
    }
    let text = tokio::task::spawn_blocking(move || {
        transcribe_blocking(&state.model_dir, &info, preprocessed.samples, language)
    })
    .await
    .map_err(|e| ApiError::internal(anyhow!(e)))??;
    Ok(Json(Transcription { text }))
}

fn validate_catalog(catalog: &Catalog) -> Result<()> {
    if catalog.catalog_version != 2 {
        return Err(anyhow!("catalog_version must be 2"));
    }
    if catalog.models.is_empty() {
        return Err(anyhow!("catalog contains no models"));
    }
    let mut slugs = HashSet::new();
    for model in &catalog.models {
        if !slugs.insert(model.slug.as_str()) {
            return Err(anyhow!("duplicate catalog slug: {}", model.slug));
        }
        if model.id.split('/').count() != 2 {
            return Err(anyhow!("model {} has invalid Hugging Face id", model.slug));
        }
        if model.revision.len() != 40
            || !model.revision.bytes().all(|byte| byte.is_ascii_hexdigit())
        {
            return Err(anyhow!("model {} has invalid revision", model.slug));
        }
        if model.architecture.is_empty() || model.languages.is_empty() {
            return Err(anyhow!(
                "model {} lacks architecture or languages",
                model.slug
            ));
        }
        let file = default_file(model)?;
        if Path::new(&file.filename)
            .file_name()
            .and_then(|name| name.to_str())
            != Some(file.filename.as_str())
            || !file.filename.ends_with(".gguf")
        {
            return Err(anyhow!("model {} has unsafe default filename", model.slug));
        }
        if file.size_bytes == 0
            || file.sha256.len() != 64
            || !file.sha256.bytes().all(|byte| byte.is_ascii_hexdigit())
        {
            return Err(anyhow!(
                "model {} has invalid default file metadata",
                model.slug
            ));
        }
    }
    Ok(())
}

fn default_file(info: &ModelInfo) -> Result<&QuantFile> {
    let mut matches = info
        .files
        .iter()
        .filter(|file| file.quant == info.default_quant);
    let file = matches.next().ok_or_else(|| {
        anyhow!(
            "model {} has no file for default quant {}",
            info.slug,
            info.default_quant
        )
    })?;
    if matches.next().is_some() {
        return Err(anyhow!(
            "model {} has duplicate default quant files",
            info.slug
        ));
    }
    Ok(file)
}

fn resolve_model(catalog: &Catalog, model_slug: &str) -> std::result::Result<ModelInfo, ApiError> {
    catalog
        .models
        .iter()
        .find(|model| model.slug == model_slug)
        .cloned()
        .ok_or_else(|| ApiError::not_found(anyhow!("unknown model: {model_slug}")))
}

fn effective_language(
    info: &ModelInfo,
    requested: Option<&str>,
) -> std::result::Result<Option<String>, ApiError> {
    let requested = requested.map(str::trim).filter(|value| !value.is_empty());
    if requested.is_none_or(|value| value.eq_ignore_ascii_case("auto")) {
        if info.capabilities.lang_detect {
            return Ok(None);
        }
        let fallback = info
            .languages
            .iter()
            .find(|language| base_language(language).eq_ignore_ascii_case("en"))
            .or_else(|| info.languages.first())
            .expect("validated catalog languages");
        return Ok(Some(fallback.clone()));
    }

    let requested = requested.expect("non-auto language");
    let matched = info.languages.iter().find(|language| {
        language.eq_ignore_ascii_case(requested)
            || base_language(language).eq_ignore_ascii_case(base_language(requested))
    });
    matched.cloned().map(Some).ok_or_else(|| {
        ApiError::bad_request(anyhow!(
            "language '{requested}' is not supported by model {}",
            info.slug
        ))
    })
}

fn base_language(language: &str) -> &str {
    language.split_once('-').map_or(language, |(base, _)| base)
}

fn transcribe_blocking(
    model_dir: &Path,
    info: &ModelInfo,
    audio: Vec<f32>,
    language: Option<String>,
) -> std::result::Result<String, ApiError> {
    let mut cache = SESSION_CACHE
        .lock()
        .map_err(|_| ApiError::internal(anyhow!("session cache lock poisoned")))?;
    if !cache.contains_key(&info.slug) {
        cache.insert(
            info.slug.clone(),
            load_session(model_dir, info).map_err(ApiError::runtime)?,
        );
    }
    let session = cache.get_mut(&info.slug).expect("cached session");
    let result = session
        .run(&audio, &run_options(language))
        .map_err(|error| {
            ApiError::runtime(anyhow!("transcribe-cpp transcription failed: {error}"))
        })?;
    Ok(result.text.trim().to_string())
}

fn run_options(language: Option<String>) -> RunOptions {
    RunOptions {
        language,
        ..Default::default()
    }
}

fn load_session(model_dir: &Path, info: &ModelInfo) -> Result<Session> {
    let path = model_path(model_dir, info)?;
    if !path.is_file() {
        return Err(anyhow!(
            "model '{}' ({}) is not installed at {}",
            info.slug,
            info.name,
            path.display()
        ));
    }
    let model = Model::load_with(
        &path,
        &ModelOptions {
            backend: Backend::Auto,
            gpu_device: 0,
        },
    )
    .with_context(|| format!("load GGUF model {}", info.slug))?;
    model
        .session()
        .with_context(|| format!("create session for {}", info.slug))
}

fn model_path(model_dir: &Path, info: &ModelInfo) -> Result<PathBuf> {
    Ok(model_dir.join(&default_file(info)?.filename))
}

async fn decode_audio(bytes: &[u8]) -> Result<DecodedAudio> {
    if looks_like_wav(bytes) {
        return decode_wav(bytes);
    }
    if looks_like_ogg(bytes) {
        return decode_ogg_via_ffmpeg(bytes).await;
    }
    Err(anyhow!(
        "unsupported audio format: expected WAV or OGG/Opus"
    ))
}

fn looks_like_wav(bytes: &[u8]) -> bool {
    bytes.len() >= 12 && &bytes[0..4] == b"RIFF" && &bytes[8..12] == b"WAVE"
}

fn looks_like_ogg(bytes: &[u8]) -> bool {
    bytes.len() >= 4 && &bytes[0..4] == b"OggS"
}

async fn decode_ogg_via_ffmpeg(bytes: &[u8]) -> Result<DecodedAudio> {
    let mut command = Command::new("ffmpeg");
    command.kill_on_drop(true);
    let mut child = command
        .args([
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-f",
            "wav",
            "-ac",
            "1",
            "-ar",
            "16000",
            "pipe:1",
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .context("OGG/Opus input requires ffmpeg in PATH")?;

    let mut stdin = child.stdin.take().context("failed to open ffmpeg stdin")?;
    let input = bytes.to_vec();
    let writer = tokio::spawn(async move { stdin.write_all(&input).await });
    let output = match timeout(ffmpeg_timeout(), child.wait_with_output()).await {
        Ok(result) => result.context("failed to decode OGG/Opus")?,
        Err(_) => return Err(anyhow!("ffmpeg timed out while decoding OGG/Opus")),
    };
    let _ = writer.await;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        if stderr.is_empty() {
            return Err(anyhow!("failed to decode OGG/Opus"));
        }
        return Err(anyhow!("failed to decode OGG/Opus: {stderr}"));
    }
    let wav = normalize_streamed_wav_lengths(output.stdout);
    decode_wav(&wav).context("failed to decode OGG/Opus")
}

fn normalize_streamed_wav_lengths(mut bytes: Vec<u8>) -> Vec<u8> {
    if !looks_like_wav(&bytes) {
        return bytes;
    }

    let riff_size = bytes.len().saturating_sub(8).min(u32::MAX as usize) as u32;
    bytes[4..8].copy_from_slice(&riff_size.to_le_bytes());

    let mut offset = 12;
    while offset + 8 <= bytes.len() {
        let chunk_id = &bytes[offset..offset + 4];
        let chunk_size = u32::from_le_bytes([
            bytes[offset + 4],
            bytes[offset + 5],
            bytes[offset + 6],
            bytes[offset + 7],
        ]) as usize;

        if chunk_id == b"data" {
            let data_size = bytes
                .len()
                .saturating_sub(offset + 8)
                .min(u32::MAX as usize) as u32;
            bytes[offset + 4..offset + 8].copy_from_slice(&data_size.to_le_bytes());
            break;
        }

        if chunk_size == u32::MAX as usize {
            break;
        }
        let padded_size = chunk_size + (chunk_size % 2);
        let Some(next_offset) = offset
            .checked_add(8)
            .and_then(|value| value.checked_add(padded_size))
        else {
            break;
        };
        if next_offset <= offset || next_offset > bytes.len() {
            break;
        }
        offset = next_offset;
    }

    bytes
}

fn decode_wav(bytes: &[u8]) -> Result<DecodedAudio> {
    let mut r = hound::WavReader::new(Cursor::new(bytes)).context("expected WAV audio")?;
    let spec = r.spec();
    if spec.channels == 0 {
        return Err(anyhow!("WAV has zero channels"));
    }
    let mut out = Vec::new();
    match spec.sample_format {
        hound::SampleFormat::Float => {
            for s in r.samples::<f32>() {
                out.push(s?);
            }
        }
        hound::SampleFormat::Int => {
            let max = (1_i64 << (spec.bits_per_sample.saturating_sub(1) as i64)) as f32;
            for s in r.samples::<i32>() {
                out.push(s? as f32 / max);
            }
        }
    }
    if spec.channels > 1 {
        out = out
            .chunks(spec.channels as usize)
            .map(|c| c.iter().sum::<f32>() / c.len() as f32)
            .collect();
    }
    Ok(DecodedAudio {
        samples: out,
        sample_rate: spec.sample_rate,
    })
}

fn preprocess_audio(audio: DecodedAudio, vad: &VadConfig) -> Result<PreprocessedAudio> {
    vad.validate()?;
    if !vad.enabled {
        return Ok(PreprocessedAudio {
            samples: audio.samples,
            decision: VadDecision::Disabled,
        });
    }
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
        if rms >= vad.threshold {
            voiced.push(i);
        }
    }
    trim_from_voiced_frames(audio.samples, frame, padding, min_speech_frames, voiced)
}

fn preprocess_silero_audio(audio: DecodedAudio, vad: &VadConfig) -> Result<PreprocessedAudio> {
    if audio.sample_rate != 16_000 {
        return Err(anyhow!(
            "Silero VAD requires 16 kHz WAV audio, got {} Hz",
            audio.sample_rate
        ));
    }
    let model_path = vad.model_path.as_ref().ok_or_else(|| {
        anyhow!("Silero VAD requires KWISPR_VAD_MODEL=/path/to/silero_vad_v4.onnx or --vad-model")
    })?;
    let frame = 480;
    let prefill = frames_for_ms(vad.padding_ms, 30);
    let hangover = frames_for_ms(vad.padding_ms, 30);
    let onset = frames_for_ms(vad.min_speech_ms, 30).max(1);
    let mut detector = SmoothedVad::new(
        Box::new(SileroVad::new(model_path, vad.threshold)?),
        prefill,
        hangover,
        onset,
    );
    let mut voiced = Vec::new();
    for (i, chunk) in audio.samples.chunks(frame).enumerate() {
        if chunk.len() != frame {
            break;
        }
        if detector.is_speech(chunk)? {
            voiced.push(i);
        }
    }
    trim_from_voiced_frames(audio.samples, frame, 0, 1, voiced)
}

fn trim_from_voiced_frames(
    samples: Vec<f32>,
    frame: usize,
    padding: usize,
    min_speech_frames: usize,
    voiced: Vec<usize>,
) -> Result<PreprocessedAudio> {
    if voiced.len() < min_speech_frames {
        return Ok(PreprocessedAudio {
            samples: Vec::new(),
            decision: VadDecision::NoSpeech,
        });
    }
    let first = voiced[0] * frame;
    let last = ((voiced[voiced.len() - 1] + 1) * frame).min(samples.len());
    let start = first.saturating_sub(padding);
    let end = (last + padding).min(samples.len());
    Ok(PreprocessedAudio {
        samples: samples[start..end].to_vec(),
        decision: VadDecision::Trimmed { start, end },
    })
}

fn samples_for_ms(sample_rate: u32, ms: u32) -> usize {
    ((sample_rate as u64 * ms as u64) / 1000) as usize
}
fn frames_for_ms(ms: u32, frame_ms: u32) -> usize {
    ms.div_ceil(frame_ms) as usize
}
fn home_models_dir() -> PathBuf {
    env::var("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("."))
        .join(".local/share/kwispr/models")
}
fn arg(name: &str) -> Option<String> {
    let mut args = env::args().skip(1);
    while let Some(a) = args.next() {
        if a == name {
            return args.next();
        }
    }
    None
}
fn env_or_arg(name: &str, var: &str) -> Option<String> {
    arg(name).or_else(|| env::var(var).ok())
}

impl VadConfig {
    fn from_env_and_args() -> Result<Self> {
        let provider =
            parse_vad_provider(env_or_arg("--vad-provider", "KWISPR_VAD_PROVIDER").as_deref())?;
        let config = Self {
            enabled: parse_bool(env_or_arg("--vad-enabled", "KWISPR_VAD_ENABLED").as_deref())
                .unwrap_or(false),
            provider,
            model_path: env_or_arg("--vad-model", "KWISPR_VAD_MODEL").map(PathBuf::from),
            threshold: env_or_arg("--vad-threshold", "KWISPR_VAD_THRESHOLD")
                .unwrap_or_else(|| default_vad_threshold(provider).into())
                .parse()
                .context("parse VAD threshold")?,
            frame_ms: env_or_arg("--vad-frame-ms", "KWISPR_VAD_FRAME_MS")
                .unwrap_or_else(|| "30".into())
                .parse()
                .context("parse VAD frame ms")?,
            min_speech_ms: env_or_arg("--vad-min-speech-ms", "KWISPR_VAD_MIN_SPEECH_MS")
                .unwrap_or_else(|| "150".into())
                .parse()
                .context("parse VAD min speech ms")?,
            padding_ms: env_or_arg("--vad-padding-ms", "KWISPR_VAD_PADDING_MS")
                .unwrap_or_else(|| "120".into())
                .parse()
                .context("parse VAD padding ms")?,
        };
        config.validate()?;
        Ok(config)
    }

    fn validate(&self) -> Result<()> {
        if self.enabled && self.frame_ms == 0 {
            return Err(anyhow!("VAD frame ms must be greater than 0"));
        }
        if self.enabled && !self.threshold.is_finite() {
            return Err(anyhow!("VAD threshold must be finite"));
        }
        if self.enabled && self.threshold < 0.0 {
            return Err(anyhow!("VAD threshold must be non-negative"));
        }
        if self.enabled && self.provider == VadProvider::Silero && self.model_path.is_none() {
            return Err(anyhow!(
                "Silero VAD requires KWISPR_VAD_MODEL=/path/to/silero_vad_v4.onnx or --vad-model"
            ));
        }
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
    fn bad_request(e: impl Into<anyhow::Error>) -> Self {
        Self(StatusCode::BAD_REQUEST, e.into().to_string())
    }
    fn not_found(e: impl Into<anyhow::Error>) -> Self {
        Self(StatusCode::NOT_FOUND, e.into().to_string())
    }
    fn runtime(e: impl Into<anyhow::Error>) -> Self {
        Self(StatusCode::UNPROCESSABLE_ENTITY, e.into().to_string())
    }
    fn internal(e: impl Into<anyhow::Error>) -> Self {
        Self(StatusCode::INTERNAL_SERVER_ERROR, e.into().to_string())
    }
    fn multipart_rejection(e: MultipartRejection) -> Self {
        Self(client_error_status(e.status()), e.body_text())
    }
    fn multipart_error(e: axum::extract::multipart::MultipartError) -> Self {
        Self(client_error_status(e.status()), e.body_text())
    }
}

fn client_error_status(status: StatusCode) -> StatusCode {
    if status.is_client_error() {
        status
    } else {
        StatusCode::BAD_REQUEST
    }
}
impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.0, Json(ErrorBody { error: self.1 })).into_response()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_vad() -> VadConfig {
        VadConfig {
            enabled: true,
            provider: VadProvider::Energy,
            model_path: None,
            threshold: 0.01,
            frame_ms: 10,
            min_speech_ms: 30,
            padding_ms: 10,
        }
    }

    fn tiny_wav() -> Vec<u8> {
        let mut bytes = Vec::new();
        {
            let spec = hound::WavSpec {
                channels: 1,
                sample_rate: 16_000,
                bits_per_sample: 16,
                sample_format: hound::SampleFormat::Int,
            };
            let mut writer = hound::WavWriter::new(Cursor::new(&mut bytes), spec).unwrap();
            writer.write_sample::<i16>(0).unwrap();
            writer.write_sample::<i16>(16384).unwrap();
            writer.finalize().unwrap();
        }
        bytes
    }

    fn tone_wav(sample_count: usize) -> Vec<u8> {
        let mut bytes = Vec::new();
        {
            let spec = hound::WavSpec {
                channels: 1,
                sample_rate: 16_000,
                bits_per_sample: 16,
                sample_format: hound::SampleFormat::Int,
            };
            let mut writer = hound::WavWriter::new(Cursor::new(&mut bytes), spec).unwrap();
            for i in 0..sample_count {
                writer
                    .write_sample::<i16>(if i % 2 == 0 { 0 } else { 16384 })
                    .unwrap();
            }
            writer.finalize().unwrap();
        }
        bytes
    }

    fn ffmpeg_available() -> bool {
        std::process::Command::new("ffmpeg")
            .arg("-version")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map(|status| status.success())
            .unwrap_or(false)
    }

    async fn encode_ogg_opus_with_ffmpeg(wav: &[u8]) -> Vec<u8> {
        let mut child = Command::new("ffmpeg")
            .args([
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                "pipe:0",
                "-c:a",
                "libopus",
                "-f",
                "ogg",
                "pipe:1",
            ])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .expect("test requires ffmpeg in PATH");

        let mut stdin = child.stdin.take().unwrap();
        let input = wav.to_vec();
        let writer = tokio::spawn(async move { stdin.write_all(&input).await });
        let output = timeout(Duration::from_secs(10), child.wait_with_output())
            .await
            .expect("ffmpeg encode timed out")
            .expect("failed to encode OGG/Opus fixture");
        writer.await.unwrap().unwrap();
        assert!(
            output.status.success(),
            "ffmpeg encode failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert!(looks_like_ogg(&output.stdout));
        output.stdout
    }

    fn test_model() -> ModelInfo {
        ModelInfo {
            id: "handy-computer/known-model-gguf".into(),
            revision: "a".repeat(40),
            slug: "known-model".into(),
            name: "Known Model".into(),
            architecture: "whisper".into(),
            languages: vec!["en".into(), "ru".into()],
            capabilities: ModelCapabilities { lang_detect: true },
            files: vec![
                QuantFile {
                    filename: "known-Q4.gguf".into(),
                    quant: "Q4".into(),
                    size_bytes: 10,
                    sha256: "a".repeat(64),
                },
                QuantFile {
                    filename: "known-Q8.gguf".into(),
                    quant: "Q8_0".into(),
                    size_bytes: 20,
                    sha256: "b".repeat(64),
                },
            ],
            default_quant: "Q8_0".into(),
        }
    }

    #[test]
    fn looks_like_wav_detects_riff_wave() {
        let wav = tiny_wav();
        assert!(looks_like_wav(&wav));
        assert!(!looks_like_wav(b"RIFFxxxxNOPE"));
        assert!(!looks_like_wav(b"short"));
    }

    #[test]
    fn looks_like_ogg_detects_oggs() {
        assert!(looks_like_ogg(b"OggS\0payload"));
        assert!(!looks_like_ogg(b"oggS\0payload"));
        assert!(!looks_like_ogg(b"abc"));
    }

    #[tokio::test]
    async fn decode_audio_rejects_unknown_format() {
        let err = decode_audio(b"not wav or ogg").await.unwrap_err();
        assert_eq!(
            err.to_string(),
            "unsupported audio format: expected WAV or OGG/Opus"
        );
    }

    #[tokio::test]
    async fn decode_audio_preserves_wav_path() {
        let audio = decode_audio(&tiny_wav()).await.unwrap();
        assert_eq!(audio.sample_rate, 16_000);
        assert_eq!(audio.samples.len(), 2);
        assert_eq!(audio.samples[0], 0.0);
        assert!(audio.samples[1] > 0.49 && audio.samples[1] < 0.51);
    }

    #[tokio::test]
    async fn decode_ogg_via_ffmpeg_converts_sample_ogg() {
        if !ffmpeg_available() {
            eprintln!("skipping ffmpeg-gated OGG/Opus decode test: ffmpeg not in PATH");
            return;
        }
        let ogg = encode_ogg_opus_with_ffmpeg(&tone_wav(16_000)).await;
        let audio = decode_ogg_via_ffmpeg(&ogg).await.unwrap();
        assert_eq!(audio.sample_rate, 16_000);
        assert!(!audio.samples.is_empty());
    }

    #[test]
    fn normalize_streamed_wav_lengths_makes_ffmpeg_pipe_wav_decodable() {
        let mut wav = tiny_wav();
        wav[4..8].copy_from_slice(&u32::MAX.to_le_bytes());
        let data_offset = wav
            .windows(4)
            .position(|window| window == b"data")
            .expect("tiny WAV has data chunk");
        wav[data_offset + 4..data_offset + 8].copy_from_slice(&u32::MAX.to_le_bytes());

        let normalized = normalize_streamed_wav_lengths(wav);
        let audio = decode_wav(&normalized).unwrap();
        assert_eq!(audio.sample_rate, 16_000);
        assert_eq!(audio.samples.len(), 2);
    }

    #[test]
    fn bundled_v2_catalog_resolves_existing_ids_by_slug() {
        let catalog: Catalog =
            serde_json::from_str(include_str!("../../models/local-stt-catalog.json")).unwrap();
        validate_catalog(&catalog).unwrap();
        assert_eq!(catalog.catalog_version, 2);
        assert_eq!(catalog.models.len(), 67);
        for slug in [
            "gigaam-v3-e2e-ctc",
            "parakeet-tdt-0.6b-v3",
            "whisper-large-v3-turbo",
        ] {
            assert_eq!(resolve_model(&catalog, slug).unwrap().slug, slug);
        }
    }

    #[test]
    fn default_quant_selects_single_gguf_model_path() {
        let info = test_model();
        assert_eq!(default_file(&info).unwrap().quant, "Q8_0");
        assert_eq!(
            model_path(Path::new("/models"), &info).unwrap(),
            Path::new("/models/known-Q8.gguf")
        );
    }

    #[test]
    fn language_request_uses_catalog_capabilities() {
        let detecting = test_model();
        assert_eq!(effective_language(&detecting, None).unwrap(), None);
        assert_eq!(effective_language(&detecting, Some("auto")).unwrap(), None);
        assert_eq!(
            effective_language(&detecting, Some(" ru "))
                .unwrap()
                .as_deref(),
            Some("ru")
        );

        let mut fixed = test_model();
        fixed.capabilities.lang_detect = false;
        assert_eq!(
            effective_language(&fixed, None).unwrap().as_deref(),
            Some("en")
        );
        assert_eq!(
            effective_language(&fixed, Some("auto")).unwrap().as_deref(),
            Some("en")
        );

        let error = effective_language(&fixed, Some("de")).unwrap_err();
        assert_eq!(error.0, StatusCode::BAD_REQUEST);
        assert!(error.1.contains("language 'de' is not supported"));
    }

    #[test]
    fn language_request_matches_catalog_bcp47_base_code() {
        let mut info = test_model();
        info.languages = vec!["en-US".into(), "zh".into()];
        assert_eq!(
            effective_language(&info, Some("en")).unwrap().as_deref(),
            Some("en-US")
        );
        assert_eq!(
            effective_language(&info, Some("zh-Hant"))
                .unwrap()
                .as_deref(),
            Some("zh")
        );
        assert_eq!(
            run_options(Some("zh".into())).language.as_deref(),
            Some("zh")
        );
    }

    #[test]
    fn vad_skips_silence() {
        let audio = DecodedAudio {
            samples: vec![0.0; 1600],
            sample_rate: 16_000,
        };
        let out = preprocess_audio(audio, &test_vad()).unwrap();
        assert_eq!(out.decision, VadDecision::NoSpeech);
        assert!(out.samples.is_empty());
    }

    #[test]
    fn vad_rejects_short_noise() {
        let mut samples = vec![0.0; 1600];
        for s in &mut samples[320..480] {
            *s = 0.2;
        }
        let out = preprocess_audio(
            DecodedAudio {
                samples,
                sample_rate: 16_000,
            },
            &test_vad(),
        )
        .unwrap();
        assert_eq!(out.decision, VadDecision::NoSpeech);
    }

    #[test]
    fn vad_trims_leading_and_trailing_silence_with_padding() {
        let mut samples = vec![0.0; 3200];
        for s in &mut samples[800..1600] {
            *s = 0.2;
        }
        let out = preprocess_audio(
            DecodedAudio {
                samples,
                sample_rate: 16_000,
            },
            &test_vad(),
        )
        .unwrap();
        assert_eq!(
            out.decision,
            VadDecision::Trimmed {
                start: 640,
                end: 1760
            }
        );
        assert_eq!(out.samples.len(), 1120);
    }

    #[test]
    fn vad_disabled_preserves_audio() {
        let audio = DecodedAudio {
            samples: vec![0.0; 1600],
            sample_rate: 16_000,
        };
        let out = preprocess_audio(
            audio,
            &VadConfig {
                enabled: false,
                ..test_vad()
            },
        )
        .unwrap();
        assert_eq!(out.decision, VadDecision::Disabled);
        assert_eq!(out.samples.len(), 1600);
    }

    #[test]
    fn vad_rejects_zero_frame_ms_in_preprocess() {
        let audio = DecodedAudio {
            samples: vec![0.0; 1600],
            sample_rate: 16_000,
        };
        let err = preprocess_audio(
            audio,
            &VadConfig {
                frame_ms: 0,
                ..test_vad()
            },
        )
        .unwrap_err();
        assert!(err
            .to_string()
            .contains("VAD frame ms must be greater than 0"));
    }

    #[test]
    fn vad_rejects_invalid_threshold() {
        let err = VadConfig {
            threshold: f32::NAN,
            ..test_vad()
        }
        .validate()
        .unwrap_err();
        assert!(err.to_string().contains("VAD threshold must be finite"));
    }

    #[test]
    fn silero_vad_requires_model_path_when_enabled() {
        let err = VadConfig {
            provider: VadProvider::Silero,
            model_path: None,
            threshold: 0.3,
            ..test_vad()
        }
        .validate()
        .unwrap_err();
        assert!(err.to_string().contains("Silero VAD requires"));
    }

    #[test]
    fn parses_vad_provider_aliases() {
        assert_eq!(parse_vad_provider(None).unwrap(), VadProvider::Energy);
        assert_eq!(
            parse_vad_provider(Some("rms")).unwrap(),
            VadProvider::Energy
        );
        assert_eq!(
            parse_vad_provider(Some("silero-onnx")).unwrap(),
            VadProvider::Silero
        );
        assert!(parse_vad_provider(Some("bogus")).is_err());
    }

    #[test]
    fn unknown_model_is_rejected_before_silent_vad_skip() {
        let catalog = Catalog {
            catalog_version: 2,
            models: vec![test_model()],
        };
        let err = resolve_model(&catalog, "missing-model").unwrap_err();
        assert_eq!(err.0, StatusCode::NOT_FOUND);
        assert!(err.1.contains("unknown model: missing-model"));

        let silent = preprocess_audio(
            DecodedAudio {
                samples: vec![0.0; 1600],
                sample_rate: 16_000,
            },
            &test_vad(),
        )
        .unwrap();
        assert_eq!(silent.decision, VadDecision::NoSpeech);
    }

    #[test]
    fn silent_audio_with_valid_model_skips_before_model_load() {
        let catalog = Catalog {
            catalog_version: 2,
            models: vec![test_model()],
        };
        let info = resolve_model(&catalog, "known-model").unwrap();
        assert_eq!(info.slug, "known-model");

        let silent = preprocess_audio(
            DecodedAudio {
                samples: vec![0.0; 1600],
                sample_rate: 16_000,
            },
            &test_vad(),
        )
        .unwrap();
        assert_eq!(silent.decision, VadDecision::NoSpeech);
    }
}
