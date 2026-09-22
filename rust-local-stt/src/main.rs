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
    net::{IpAddr, SocketAddr},
    path::{Path, PathBuf},
    process::Stdio,
    sync::Mutex,
    time::Duration,
};
use tokio::{io::AsyncWriteExt, process::Command, time::timeout};
use transcribe_cpp::{
    Backend, Model, ModelOptions, RunExtension, RunOptions, Session, Transcript,
    WhisperPromptCondition, WhisperRunOptions,
};
use transcribe_rs::vad::{SileroVad, SmoothedVad, Vad};

static SESSION_CACHE: Lazy<Mutex<HashMap<String, Session>>> =
    Lazy::new(|| Mutex::new(HashMap::new()));
const DEFAULT_MAX_UPLOAD_BYTES: usize = 256 * 1024 * 1024;
const DEFAULT_FFMPEG_TIMEOUT_SECONDS: u64 = 30;
const MAX_PROMPT_CHARS: usize = 4096;
const MAX_ALLOWED_LANGUAGES_BYTES: usize = 1024;

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
    capabilities: RuntimeCapabilities,
}
#[derive(Serialize)]
struct RuntimeCapabilities {
    whisper_prompt: bool,
    whisper_allowed_languages: bool,
    preserve_audio_tail: bool,
}
#[derive(Default, Serialize)]
struct Transcription {
    text: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    language: Option<String>,
}

impl Transcription {
    fn from_result(result: Transcript, fallback_language: Option<String>) -> Self {
        let text = result.text.trim().to_string();
        let language = if text.is_empty() {
            None
        } else {
            result
                .language
                .filter(|language| !language.trim().is_empty())
                .or(fallback_language)
        };
        Self { text, language }
    }
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

const DEFAULT_LOCAL_STT_HOST: &str = "127.0.0.1";
const DEFAULT_LOCAL_STT_PORT: u16 = 19650;

#[tokio::main]
async fn main() -> Result<()> {
    transcribe_cpp::init_logging();
    transcribe_cpp::init_backends_default()
        .context("initialize transcribe-cpp dynamic backends")?;
    let host = arg("--host").unwrap_or_else(|| DEFAULT_LOCAL_STT_HOST.into());
    let port: u16 = arg("--port")
        .map(|value| value.parse())
        .transpose()?
        .unwrap_or(DEFAULT_LOCAL_STT_PORT);
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
    let addr = SocketAddr::new(
        host.parse::<IpAddr>()
            .with_context(|| format!("invalid --host IP address: {host}"))?,
        port,
    );
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
        capabilities: RuntimeCapabilities {
            whisper_prompt: true,
            whisper_allowed_languages: true,
            preserve_audio_tail: true,
        },
    })
}

async fn transcribe(
    State(state): State<AppState>,
    mp: std::result::Result<Multipart, MultipartRejection>,
) -> std::result::Result<Json<Transcription>, ApiError> {
    let mut mp = mp.map_err(ApiError::multipart_rejection)?;
    let mut model = None;
    let mut lang = None;
    let mut allowed_languages_csv = None;
    let mut prompt = None;
    let mut preserve_audio_tail = false;
    let mut format = "json".to_string();
    let mut file = None;
    while let Some(field) = mp.next_field().await.map_err(ApiError::multipart_error)? {
        match field.name().unwrap_or("") {
            "model" => model = Some(field.text().await.map_err(ApiError::multipart_error)?),
            "language" => lang = Some(field.text().await.map_err(ApiError::multipart_error)?),
            "allowed_languages" => {
                allowed_languages_csv =
                    Some(field.text().await.map_err(ApiError::multipart_error)?);
            }
            "prompt" => {
                prompt = normalize_prompt(&field.text().await.map_err(ApiError::multipart_error)?)?;
            }
            "preserve_audio_tail" => {
                preserve_audio_tail = parse_preserve_audio_tail(
                    &field.text().await.map_err(ApiError::multipart_error)?,
                )?;
            }
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
    let allowed_languages = effective_allowed_languages(&info, allowed_languages_csv.as_deref())?;
    validate_prompt_support(&info, prompt.as_deref())?;
    let audio = decode_audio(&bytes).await.map_err(ApiError::bad_request)?;
    let preprocessed =
        preprocess_audio(audio, &state.vad, preserve_audio_tail).map_err(ApiError::bad_request)?;
    if preprocessed.decision == VadDecision::NoSpeech {
        return Ok(Json(Transcription::default()));
    }
    let transcription = tokio::task::spawn_blocking(move || {
        transcribe_blocking(
            &state.model_dir,
            &info,
            preprocessed.samples,
            language,
            prompt,
            allowed_languages,
        )
    })
    .await
    .map_err(|e| ApiError::internal(anyhow!(e)))??;
    Ok(Json(transcription))
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

fn effective_allowed_languages(
    info: &ModelInfo,
    requested: Option<&str>,
) -> std::result::Result<Option<Vec<String>>, ApiError> {
    let Some(csv) = requested else {
        return Ok(None);
    };
    if info.architecture != "whisper" {
        return Err(ApiError::bad_request(anyhow!(
            "allowed_languages is only supported by Whisper models"
        )));
    }
    if csv.len() > MAX_ALLOWED_LANGUAGES_BYTES {
        return Err(ApiError::bad_request(anyhow!(
            "allowed_languages must be at most {MAX_ALLOWED_LANGUAGES_BYTES} bytes"
        )));
    }
    let mut languages = Vec::new();
    for language in csv.split(',').map(str::trim) {
        let mut parts = language.split('-');
        let base = parts.next().unwrap_or_default();
        if !(2..=3).contains(&base.len())
            || !base.bytes().all(|byte| byte.is_ascii_alphabetic())
            || !parts.all(|part| {
                (1..=8).contains(&part.len())
                    && part.bytes().all(|byte| byte.is_ascii_alphanumeric())
            })
        {
            return Err(ApiError::bad_request(anyhow!(
                "allowed_languages must be a nonempty comma-separated list of language codes"
            )));
        }
        let canonical = effective_language(info, Some(language))?
            .expect("a validated language code is not auto");
        if !languages.contains(&canonical) {
            languages.push(canonical);
        }
    }
    Ok(Some(languages))
}

fn normalize_prompt(value: &str) -> std::result::Result<Option<String>, ApiError> {
    if value.contains('\0') {
        return Err(ApiError::bad_request(anyhow!(
            "prompt must not contain NUL characters"
        )));
    }
    if value.chars().count() > MAX_PROMPT_CHARS {
        return Err(ApiError::bad_request(anyhow!(
            "prompt must be at most {MAX_PROMPT_CHARS} characters"
        )));
    }
    let value = value.trim();
    Ok((!value.is_empty()).then(|| value.to_owned()))
}

fn validate_prompt_support(
    info: &ModelInfo,
    prompt: Option<&str>,
) -> std::result::Result<(), ApiError> {
    if prompt.is_some() && info.architecture != "whisper" {
        return Err(ApiError::bad_request(anyhow!(
            "prompt is only supported by Whisper models; model {} uses {}",
            info.slug,
            info.architecture
        )));
    }
    Ok(())
}

fn parse_preserve_audio_tail(value: &str) -> std::result::Result<bool, ApiError> {
    match value.trim() {
        "true" | "1" => Ok(true),
        "false" | "0" => Ok(false),
        _ => Err(ApiError::bad_request(anyhow!(
            "preserve_audio_tail must be true, false, 1, or 0"
        ))),
    }
}

fn transcribe_blocking(
    model_dir: &Path,
    info: &ModelInfo,
    audio: Vec<f32>,
    language: Option<String>,
    prompt: Option<String>,
    allowed_languages: Option<Vec<String>>,
) -> std::result::Result<Transcription, ApiError> {
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
    let options = run_options(language, prompt, allowed_languages);
    let result = session.run(&audio, &options).map_err(|error| {
        ApiError::runtime(anyhow!("transcribe-cpp transcription failed: {error}"))
    })?;
    Ok(Transcription::from_result(result, options.language))
}

fn run_options(
    language: Option<String>,
    prompt: Option<String>,
    allowed_languages: Option<Vec<String>>,
) -> RunOptions {
    let family = if prompt.is_none() && allowed_languages.is_none() {
        None
    } else {
        let mut options = WhisperRunOptions {
            allowed_languages,
            ..Default::default()
        };
        if let Some(prompt) = prompt {
            options.initial_prompt = Some(prompt);
            // Preserve caller context on every window without generated history.
            options.prompt_condition = Some(WhisperPromptCondition::AllSegments);
            options.condition_on_prev_tokens = Some(false);
        }
        Some(RunExtension::Whisper(options))
    };
    RunOptions {
        language,
        family,
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

fn preprocess_audio(
    audio: DecodedAudio,
    vad: &VadConfig,
    preserve_audio_tail: bool,
) -> Result<PreprocessedAudio> {
    vad.validate()?;
    if !vad.enabled {
        return Ok(PreprocessedAudio {
            samples: audio.samples,
            decision: VadDecision::Disabled,
        });
    }
    match vad.provider {
        VadProvider::Energy => preprocess_energy_audio(audio, vad, preserve_audio_tail),
        VadProvider::Silero => preprocess_silero_audio(audio, vad, preserve_audio_tail),
    }
}

fn preprocess_energy_audio(
    audio: DecodedAudio,
    vad: &VadConfig,
    preserve_audio_tail: bool,
) -> Result<PreprocessedAudio> {
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
    trim_from_voiced_frames(
        audio.samples,
        frame,
        padding,
        min_speech_frames,
        voiced,
        preserve_audio_tail,
    )
}

fn preprocess_silero_audio(
    audio: DecodedAudio,
    vad: &VadConfig,
    preserve_audio_tail: bool,
) -> Result<PreprocessedAudio> {
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
    let voiced = detect_complete_and_partial_frames(&audio.samples, frame, |chunk| {
        Ok(detector.is_speech(chunk)?)
    })?;
    trim_from_voiced_frames(audio.samples, frame, 0, 1, voiced, preserve_audio_tail)
}

fn detect_complete_and_partial_frames(
    samples: &[f32],
    frame: usize,
    mut is_speech: impl FnMut(&[f32]) -> Result<bool>,
) -> Result<Vec<usize>> {
    let mut voiced = Vec::new();
    for (i, chunk) in samples.chunks(frame).enumerate() {
        // Silero needs a complete frame, but a recording rarely ends exactly
        // on a frame boundary. Pad only the detector input, never the audio.
        let speech = if chunk.len() == frame {
            is_speech(chunk)?
        } else {
            let mut padded = vec![0.0; frame];
            padded[..chunk.len()].copy_from_slice(chunk);
            is_speech(&padded)?
        };
        if speech {
            voiced.push(i);
        }
    }
    Ok(voiced)
}

fn trim_from_voiced_frames(
    samples: Vec<f32>,
    frame: usize,
    padding: usize,
    min_speech_frames: usize,
    voiced: Vec<usize>,
    preserve_audio_tail: bool,
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
    let end = if preserve_audio_tail {
        samples.len()
    } else {
        (last + padding).min(samples.len())
    };
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

    #[test]
    fn fresh_bind_defaults_are_loopback_on_19650() {
        assert_eq!(DEFAULT_LOCAL_STT_HOST, "127.0.0.1");
        assert_eq!(DEFAULT_LOCAL_STT_PORT, 19650);
    }

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
            run_options(Some("zh".into()), None, None)
                .language
                .as_deref(),
            Some("zh")
        );
    }

    #[test]
    fn transcription_response_exposes_detected_language_and_preserves_text() {
        let response = Transcription::from_result(
            Transcript {
                text: "  Привет, друг!\n".into(),
                language: Some("ru".into()),
                ..Default::default()
            },
            Some("en".into()),
        );
        assert_eq!(
            serde_json::to_value(response).unwrap(),
            serde_json::json!({"text": "Привет, друг!", "language": "ru"})
        );
    }

    #[test]
    fn allowed_languages_normalize_catalog_codes_without_forcing_language() {
        let languages =
            effective_allowed_languages(&test_model(), Some(" RU , en-US,ru ")).unwrap();
        assert_eq!(languages, Some(vec!["ru".into(), "en".into()]));
        let options = run_options(None, None, languages.clone());
        assert_eq!(options.language, None);
        assert_eq!(options.task, transcribe_cpp::Task::Transcribe);
        assert_eq!(options.target_language, None);
        assert_eq!(
            options.family,
            Some(RunExtension::Whisper(WhisperRunOptions {
                allowed_languages: languages.clone(),
                ..Default::default()
            }))
        );
        // An explicit source language remains stronger than the auto candidates.
        let hinted = run_options(Some("en".into()), None, Some(vec!["ru".into()]));
        assert_eq!(hinted.language.as_deref(), Some("en"));
        let prompted = run_options(None, Some("Hello. Привет!".into()), languages);
        let Some(RunExtension::Whisper(whisper)) = prompted.family else {
            panic!("missing Whisper options")
        };
        assert_eq!(
            whisper.prompt_condition,
            Some(WhisperPromptCondition::AllSegments)
        );
        assert_eq!(whisper.condition_on_prev_tokens, Some(false));
        assert_eq!(
            whisper.allowed_languages,
            Some(vec!["ru".into(), "en".into()])
        );
    }

    #[test]
    fn allowed_languages_reject_invalid_empty_and_unsupported_requests() {
        for invalid in [
            "",
            " ",
            "ru,",
            ",en",
            "ru,,en",
            "ru;en",
            "r",
            "russ",
            "ru_zz",
            "en-",
            "en-123456789",
            "ru\0en",
            "xx",
        ] {
            let error = effective_allowed_languages(&test_model(), Some(invalid)).unwrap_err();
            assert_eq!(error.0, StatusCode::BAD_REQUEST, "{invalid:?}");
        }
        assert!(effective_allowed_languages(&test_model(), Some(&"r".repeat(1025))).is_err());
        assert_eq!(
            effective_allowed_languages(&test_model(), None).unwrap(),
            None
        );
        let mut other_model = test_model();
        other_model.architecture = "gigaam".into();
        assert_eq!(
            effective_allowed_languages(&other_model, None).unwrap(),
            None
        );
        let error = effective_allowed_languages(&other_model, Some("ru")).unwrap_err();
        assert_eq!(error.0, StatusCode::BAD_REQUEST);
        assert!(error.1.contains("only supported by Whisper"));
    }

    #[test]
    fn transcription_response_falls_back_to_effective_input_language() {
        // Whisper leaves its detected language unset when given a hint;
        // models without language detection also need this fallback.
        for detected_language in [None, Some(String::new()), Some(" \t".into())] {
            let response = Transcription::from_result(
                Transcript {
                    text: "Hello.".into(),
                    language: detected_language,
                    ..Default::default()
                },
                Some("en-US".into()),
            );
            assert_eq!(
                serde_json::to_value(response).unwrap(),
                serde_json::json!({"text": "Hello.", "language": "en-US"})
            );
        }
    }

    #[test]
    fn transcription_response_omits_unknown_language_for_existing_clients() {
        let response = Transcription::from_result(
            Transcript {
                text: "Hello.".into(),
                ..Default::default()
            },
            None,
        );
        assert_eq!(
            serde_json::to_value(response).unwrap(),
            serde_json::json!({"text": "Hello."})
        );
    }

    #[test]
    fn empty_transcription_never_establishes_a_language() {
        for text in ["", " \n\t"] {
            let response = Transcription::from_result(
                Transcript {
                    text: text.into(),
                    language: Some("ru".into()),
                    ..Default::default()
                },
                Some("en".into()),
            );
            assert_eq!(
                serde_json::to_value(response).unwrap(),
                serde_json::json!({"text": ""})
            );
        }
        // The pre-decoder VAD NoSpeech branch uses this same empty response.
        assert_eq!(
            serde_json::to_value(Transcription::default()).unwrap(),
            serde_json::json!({"text": ""})
        );
    }

    #[test]
    fn prompt_is_forwarded_to_every_window_without_generated_history() {
        let prompt = normalize_prompt("  Kwispr, Подман. Привет, друг!  ").unwrap();
        validate_prompt_support(&test_model(), prompt.as_deref()).unwrap();
        let options = run_options(Some("ru".into()), prompt, None);
        assert_eq!(options.language.as_deref(), Some("ru"));
        assert_eq!(
            options.family,
            Some(RunExtension::Whisper(WhisperRunOptions {
                initial_prompt: Some("Kwispr, Подман. Привет, друг!".into()),
                prompt_condition: Some(WhisperPromptCondition::AllSegments),
                condition_on_prev_tokens: Some(false),
                ..Default::default()
            }))
        );
        // A later prompt-free request must not inherit context from the
        // session cache or force family-specific defaults.
        assert_eq!(run_options(None, None, None).family, None);
        assert_eq!(normalize_prompt("  \n\t ").unwrap(), None);
    }

    #[test]
    fn prompt_validation_counts_unicode_characters_and_rejects_nul() {
        let unicode = "ё".repeat(MAX_PROMPT_CHARS);
        assert_eq!(normalize_prompt(&unicode).unwrap(), Some(unicode));
        for invalid in ["ё".repeat(MAX_PROMPT_CHARS + 1), "word\0ignored".into()] {
            let error = normalize_prompt(&invalid).unwrap_err();
            assert_eq!(error.0, StatusCode::BAD_REQUEST);
        }
    }

    #[test]
    fn unsupported_models_reject_prompt_instead_of_ignoring_it() {
        let mut model = test_model();
        model.architecture = "gigaam".into();
        validate_prompt_support(&model, None).unwrap();
        let error = validate_prompt_support(&model, Some("Kwispr")).unwrap_err();
        assert_eq!(error.0, StatusCode::BAD_REQUEST);
        assert!(error.1.contains("only supported by Whisper"));
    }

    #[test]
    fn preserve_audio_tail_flag_is_explicit_and_validated() {
        for value in ["true", "1", " 1 "] {
            assert!(parse_preserve_audio_tail(value).unwrap());
        }
        for value in ["false", "0"] {
            assert!(!parse_preserve_audio_tail(value).unwrap());
        }
        assert_eq!(
            parse_preserve_audio_tail("yes").unwrap_err().0,
            StatusCode::BAD_REQUEST
        );
    }

    #[test]
    fn preserving_tail_keeps_quiet_ending_after_vad_speech_gate() {
        let mut samples = vec![0.0; 3200];
        samples[800..1600].fill(0.2);
        samples[2400..].fill(0.005); // A quiet word below the energy threshold.
        let trimmed = preprocess_audio(
            DecodedAudio {
                samples: samples.clone(),
                sample_rate: 16_000,
            },
            &test_vad(),
            false,
        )
        .unwrap();
        assert_eq!(
            trimmed.decision,
            VadDecision::Trimmed {
                start: 640,
                end: 1760
            }
        );
        let preserved = preprocess_audio(
            DecodedAudio {
                samples: samples.clone(),
                sample_rate: 16_000,
            },
            &test_vad(),
            true,
        )
        .unwrap();
        assert_eq!(
            preserved.decision,
            VadDecision::Trimmed {
                start: 640,
                end: 3200
            }
        );
        assert_eq!(preserved.samples, samples[640..]);
        let silent = preprocess_audio(
            DecodedAudio {
                samples: vec![0.0; 3200],
                sample_rate: 16_000,
            },
            &test_vad(),
            true,
        )
        .unwrap();
        assert_eq!(silent.decision, VadDecision::NoSpeech);
    }

    #[test]
    fn partial_detector_frame_preserves_original_tail_without_adding_audio() {
        let mut samples = vec![0.0; 480];
        samples.extend([0.5; 137]);
        let mut seen = Vec::new();
        let voiced = detect_complete_and_partial_frames(&samples, 480, |chunk| {
            seen.push(chunk.to_vec());
            Ok(chunk.iter().any(|sample| *sample > 0.1))
        })
        .unwrap();
        assert_eq!(seen.len(), 2);
        assert_eq!(seen[1].len(), 480);
        assert_eq!(&seen[1][..137], &[0.5; 137]);
        assert!(seen[1][137..].iter().all(|sample| *sample == 0.0));
        let output = trim_from_voiced_frames(samples, 480, 0, 1, voiced, false).unwrap();
        assert_eq!(
            output.decision,
            VadDecision::Trimmed {
                start: 480,
                end: 617
            }
        );
        assert_eq!(output.samples, vec![0.5; 137]);
    }

    #[test]
    fn vad_skips_silence() {
        let audio = DecodedAudio {
            samples: vec![0.0; 1600],
            sample_rate: 16_000,
        };
        let out = preprocess_audio(audio, &test_vad(), false).unwrap();
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
            false,
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
            false,
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
            false,
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
            false,
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
            false,
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
            false,
        )
        .unwrap();
        assert_eq!(silent.decision, VadDecision::NoSpeech);
    }
}
