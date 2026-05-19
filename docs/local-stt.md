# Local STT roadmap

Kwispr includes two local STT server entry points:

- `kwispr-local-stt-server.py` remains a tiny Python stub for wiring smoke tests.
- `rust-local-stt/` is the real inference runtime scaffold. It exposes the same OpenAI-compatible endpoint, resolves models from `models/local-stt-catalog.json`, loads Handy-compatible artifacts from `$KWISPR_MODEL_DIR` or `~/.local/share/kwispr/models`, dispatches GigaAM / Parakeet / Whisper by `engine_type`, and caches loaded engines in-process.

Start the legacy stub server:

```bash
./kwispr-local-stt-server.py --host 127.0.0.1 --port 9000
```

Health check:

```bash
curl http://127.0.0.1:9000/health
```

Stub transcription request:

```bash
printf 'dummy audio bytes' >/tmp/kwispr-dummy.wav
curl -sS http://127.0.0.1:9000/v1/audio/transcriptions \
  -F model=gigaam-v3-e2e-ctc \
  -F response_format=json \
  -F language=ru \
  -F file=@/tmp/kwispr-dummy.wav
```

Kwispr can talk to the OpenAI-compatible local transcription endpoint through:

```env
KWISPR_BACKEND=openai-transcriptions
KWISPR_API_URL=http://127.0.0.1:9000/v1/audio/transcriptions
KWISPR_MODEL=gigaam-v3-e2e-ctc
KWISPR_API_KEY=
```

The repository now includes a machine-readable local model catalog at:

```text
models/local-stt-catalog.json
```

Use `kwispr-models.py` to list, download, and verify catalog artifacts. The Rust runtime uses these installed artifacts for local inference.

## Downloading models

```bash
# Show install status for all catalog entries
./kwispr-models.py list

# Download the Russian GigaAM model into ~/.local/share/kwispr/models
./kwispr-models.py download gigaam-v3-e2e-ctc

# Verify the installed model
./kwispr-models.py verify gigaam-v3-e2e-ctc
```

Set `KWISPR_MODEL_DIR=/path/to/models` or pass `--model-dir /path/to/models` to override the install directory. The helper verifies artifact SHA256 before installation, extracts `.tar.gz` directory artifacts safely, places single-file artifacts directly, and skips redownloading already-valid models.

## Rust runtime

Build and run when Rust/Cargo and native transcribe-rs dependencies are available:

```bash
cd rust-local-stt
# Native build dependencies include Rust/Cargo, CMake, Clang, libvulkan headers, and glslc.
cargo build --release
KWISPR_MODEL_DIR=~/.local/share/kwispr/models \
  ./target/release/kwispr-local-stt --host 127.0.0.1 --port 9000 \
  --catalog ../models/local-stt-catalog.json
```

Whisper builds with `whisper-vulkan` enabled. On a working Vulkan host, a successful GPU-backed Whisper load logs:

```text
ggml_vulkan: 0 = NVIDIA GeForce RTX 3080 Ti
whisper_backend_init_gpu: using Vulkan0 backend
```

Validation status: GigaAM v3, Whisper Large v3 Turbo, and Parakeet V3 have been validated with real local artifacts. Whisper Turbo was validated on an NVIDIA RTX 3080 Ti through Vulkan.

The endpoint is OpenAI-compatible. It accepts WAV and OGG/Opus audio only; Telegram voice messages are OGG/Opus. OGG/Opus decoding requires `ffmpeg` in `PATH` at runtime (`sudo pacman -S ffmpeg` on Arch/CachyOS):

```bash
curl -sS http://127.0.0.1:9000/v1/audio/transcriptions \
  -F model=gigaam-v3-e2e-ctc \
  -F response_format=json \
  -F language=ru \
  -F file=@sample.wav

curl -sS http://127.0.0.1:9000/v1/audio/transcriptions \
  -F model=gigaam-v3-e2e-ctc \
  -F response_format=json \
  -F language=ru \
  -F file=@voice.ogg
```

Expected success response:

```json
{"text":"..."}
```

Clear HTTP errors are returned as `{"error":"..."}`:

- `400` for malformed multipart, missing fields, unsupported response format, unsupported audio formats, invalid WAV input, or failed OGG/Opus decode
- `404` for unknown catalog model ids
- `422` for model resolution, load, or runtime transcription failures
- `500` for unexpected server failures

Loaded engines are cached by model id for the life of the process, so repeated requests to the same model do not reload the model.

## Optional VAD preprocessing

The Rust runtime has optional VAD preprocessing before local inference. It is disabled by default so existing local and cloud/OpenRouter/OpenAI behavior stays unchanged.

Energy/RMS provider, dependency-light and conservative:

```bash
KWISPR_VAD_ENABLED=1 \
KWISPR_VAD_PROVIDER=energy \
KWISPR_VAD_THRESHOLD=0.01 \
KWISPR_MODEL_DIR=~/.local/share/kwispr/models \
  ./target/release/kwispr-local-stt --host 127.0.0.1 --port 9000 \
  --catalog ../models/local-stt-catalog.json
```

Silero ONNX provider, closer to Handy-style neural VAD:

```bash
mkdir -p ~/.local/share/kwispr/models
curl -L -o ~/.local/share/kwispr/models/silero_vad_v4.onnx \
  https://blob.handy.computer/silero_vad_v4.onnx
KWISPR_VAD_ENABLED=1 \
KWISPR_VAD_PROVIDER=silero \
KWISPR_VAD_MODEL=~/.local/share/kwispr/models/silero_vad_v4.onnx \
KWISPR_VAD_THRESHOLD=0.3 \
KWISPR_MODEL_DIR=~/.local/share/kwispr/models \
  ./target/release/kwispr-local-stt --host 127.0.0.1 --port 9000 \
  --catalog ../models/local-stt-catalog.json
```

Equivalent CLI flags are available: `--vad-enabled true`, `--vad-provider energy|silero`, `--vad-model`, `--vad-threshold`, `--vad-frame-ms`, `--vad-min-speech-ms`, and `--vad-padding-ms`. `/health` reports the active VAD config. Silero uses 30 ms / 480-sample frames at 16 kHz and defaults to threshold `0.3`; energy VAD defaults to threshold `0.01`.

Both providers trim leading/trailing non-speech and return an empty transcript for no-speech/short-noise clips before STT model inference. `kwispr.sh` treats empty responses from a local `127.0.0.1` STT endpoint as a clean `No speech` skip rather than an API failure. This reduces junk audio sent to STT, lowers latency for padded recordings, and helps avoid hallucinated transcripts from silence/noise.

## Model recommendations

| Dictation need | Recommended model | Model id | Notes |
|---|---|---|---|
| Russian | GigaAM v3 | `gigaam-v3-e2e-ctc` | Best default for Russian-only dictation and the smallest current artifact. |
| Mixed Russian/English | Parakeet V3 or Whisper Large v3 Turbo | `parakeet-tdt-0.6b-v3` or `whisper-large-v3-turbo` | Parakeet covers ru/en and many European languages; Whisper Turbo is the broad fallback. |
| English low latency | Parakeet now; Moonshine-class models later | `parakeet-tdt-0.6b-v3` | The catalog does not include Moonshine yet, so mention it only as a future option. |

No cloud key is required for local mode. Keep `KWISPR_API_KEY=` empty when `KWISPR_API_URL` points at `127.0.0.1`. Cloud backends still require their usual OpenAI/OpenRouter key.

## Troubleshooting local mode

| Symptom | Likely cause | Fix |
|---|---|---|
| `curl: (7) Failed to connect` | Local server is not running or the port does not match `.env` | Start the Python stub or Rust runtime, then check `/health`. |
| `[stub transcript]` | You are using `kwispr-local-stt-server.py` | Use the Rust runtime for actual model inference. |
| `unknown model` | `KWISPR_MODEL` is not an id in `models/local-stt-catalog.json` | Run `./kwispr-models.py list` and copy an exact model id. |
| `model ... is not installed` | The catalog artifact has not been downloaded or `KWISPR_MODEL_DIR` points elsewhere | Run `./kwispr-models.py download <model-id>` and verify the same model dir is used by the runtime. |
| `unsupported audio format: expected WAV or OGG/Opus` | The upload is not WAV or OGG/Opus | Convert the input to WAV or OGG/Opus before POSTing. MP3, M4A, and WebM are not supported by the local endpoint. |
| `OGG/Opus input requires ffmpeg in PATH` | An OGG/Opus upload was received, but `ffmpeg` is not installed or not visible to the runtime | Install ffmpeg, for example `sudo pacman -S ffmpeg` on Arch/CachyOS, and restart the runtime. |
| `failed to decode OGG/Opus` / `ffmpeg timed out while decoding OGG/Opus` | The OGG/Opus input is malformed, unsupported by ffmpeg, or decoding exceeded `KWISPR_FFMPEG_TIMEOUT_SECONDS` (default 30) | Recreate the voice file or raise the timeout for unusually large inputs. |
| `unsupported engine_type` / model load failure | Native transcribe-rs dependency or engine support is unavailable for that model on this machine | Rebuild `rust-local-stt`, try another catalog model, or fall back to cloud mode. |
| Unsupported or incorrect language | Selected model does not support that language, or does not honor `language` selection | Use GigaAM for Russian; use Parakeet/Whisper Turbo for mixed ru/en; leave language empty for autodetect where supported. |
| Whisper is slow or logs `no GPU found` | The runtime did not get a usable Vulkan device | Build with the default `whisper-vulkan` feature, verify host `vulkaninfo` sees the GPU, and restart the local runtime. If Vulkan is unavailable, use GigaAM for Russian or a cloud backend. |

## Initial catalog slice

| Model | Engine | Best for | Artifact |
|---|---|---|---|
| GigaAM v3 | `gigaam` | Russian dictation | directory archive |
| Parakeet V3 | `parakeet` | mixed ru/en/uk and European languages | directory archive |
| Whisper Large v3 Turbo | `whisper` | general fallback | single GGML file |

The catalog records:

- stable model id
- display name and description
- engine type
- artifact URL
- SHA256 checksum
- approximate size
- archive/file layout
- supported languages
- intended use cases

## Why Handy-compatible metadata?

Handy already demonstrates a working local STT architecture with downloadable models, `transcribe-rs`, Whisper/ONNX engines, and VAD. Kwispr should stay small and script-first, but future local backends can reuse the same model metadata shape.

## Future slices

1. local OpenAI-compatible server skeleton
2. real `transcribe-rs` inference runtime
3. docs and integration polish
4. optional VAD preprocessing
5. future catalog expansion, such as Moonshine-class English low-latency models when suitable artifacts are selected

VAD now has an optional local-runtime preprocessing hook. The first implementation is a conservative energy/RMS gate; full Silero ONNX integration remains tracked in https://github.com/blockedby/kwispr/issues/8.
