# Local STT

Kwispr has two local server entry points:

- `kwispr-local-stt-server.py` is a legacy API-wiring stub.
- `rust-local-stt/` is the real batch inference server. It exposes an OpenAI-compatible transcription endpoint, resolves Handy catalog v2 models by `slug`, and caches unified transcribe-cpp GGUF `Session`s.

Cloud and OpenRouter behavior is unchanged. Local mode remains opt-in:

```env
KWISPR_BACKEND=openai-transcriptions
KWISPR_API_URL=http://127.0.0.1:19650/v1/audio/transcriptions
KWISPR_LOCAL_STT_HOST=127.0.0.1
KWISPR_LOCAL_STT_PORT=19650
KWISPR_LOCAL_STT_CONFIGURED=1
KWISPR_MODEL=gigaam-v3-e2e-ctc
KWISPR_API_KEY=
KWISPR_LANGUAGE=ru
KWISPR_MODEL_DIR=~/.local/share/kwispr/models
```

## Handy catalog v2

`models/local-stt-catalog.json` contains all 67 models from Handy's generated catalog at commit `ea3c20a3a67c7401d8b19198723760da9d40ac45` (2026-07-28). The catalog itself records this provenance. Each entry includes:

- a user-facing `slug` (the Kwispr model id);
- Hugging Face repository and immutable commit `revision`;
- architecture, languages, and capabilities;
- available GGUF quantizations with size and SHA256;
- one `default_quant`.

Existing ids remain valid, including `gigaam-v3-e2e-ctc`, `parakeet-tdt-0.6b-v3`, and `whisper-large-v3-turbo`.

## Download and verification

```bash
./kwispr-models.py list
./kwispr-models.py download gigaam-v3-e2e-ctc
./kwispr-models.py verify gigaam-v3-e2e-ctc
```

The helper installs exactly the selected model's default-quant GGUF file under `$KWISPR_MODEL_DIR` (default `~/.local/share/kwispr/models`). It URL-quotes repository/file path segments and tries:

1. catalog mirrors at `{mirror}/{repo}/{revision}/{filename}`;
2. immutable Hugging Face `{repo}/resolve/{revision}/{filename}`.

Downloaded bytes must match both the catalog size and SHA256 before an atomic install. Each network read has a 60-second timeout, and a source that exceeds the declared size is aborted before its extra bytes are written. A valid file is not downloaded again; a missing or tampered file fails `verify` and is replaced by the next `download`. There is no archive extraction or directory-model layout.

## Build and run

Linux uses transcribe-cpp `0.1.3` with Handy's dynamic-backend posture: loadable CPU variants and Vulkan. `transcribe-rs` is retained only for optional Silero VAD.

For an end-user native Arch build, let the installer provision and later remove only the missing build dependencies:

```bash
./install.sh --build-backend host --with-local-stt
```

For a persistent development environment, add `--keep-build-deps`, or install the toolchain yourself and run:

```bash
cd rust-local-stt
cargo build --release --locked
```

Podman remains available as an optional isolated builder when already installed:

```bash
./rust-local-stt/build-in-podman.sh
```

The build stages `libtranscribe.so*` and `libggml*.so*` runtime/backend files into `rust-local-stt/target/release/` beside the binary. Keep these files together if copying the runtime.

Start the server from the repository root:

```bash
KWISPR_MODEL_DIR=~/.local/share/kwispr/models \
  ./rust-local-stt/target/release/kwispr-local-stt \
  --host 127.0.0.1 --port 19650 \
  --catalog models/local-stt-catalog.json
```

A successful start prints `kwispr local STT runtime listening on http://127.0.0.1:19650`; the server is then ready at `/v1/audio/transcriptions`.

## LAN server and remote client

Binding beyond loopback is always opt-in. A headless inference computer can be installed noninteractively with:

```bash
./install.sh --yes --with-local-stt \
  --local-stt-host 0.0.0.0 --local-stt-port 19650 \
  --local-stt-autostart
```

A recording-only computer can use it without installing the Rust runtime or downloading a local model:

```bash
./install.sh --yes --without-local-stt \
  --local-stt-url http://192.168.1.20:19650/v1/audio/transcriptions \
  --local-stt-model whisper-large-v3-turbo
```

The bind address and client destination are independent. `0.0.0.0` listens on interfaces but is never a valid `KWISPR_API_URL` host. KDE Settings exposes the endpoint on every client and the listen/LAN controls only where the local service is installed; applying a changed listen address or port restarts that user service. Download/Delete controls always manage model files on the current computer, not a remote server.

There is no authentication, TLS, reverse-proxy configuration, or firewall automation. Use LAN binding only on a trusted network and configure those layers separately if needed.

### Upgrade compatibility

Fresh installs default to port `19650`. If an older generated configuration still uses `http://127.0.0.1:9000/v1/audio/transcriptions` and has no dedicated server port, the installer records `KWISPR_LOCAL_STT_PORT=9000` so the existing client/server pairing continues to work. It preserves custom URLs and ports exactly on upgrade and rerun.

The server is batch-only; catalog streaming capability metadata does not enable streaming endpoints. Every model architecture uses `transcribe_cpp::Session`. Sessions are cached by slug for the process lifetime. An optional multipart `language` value is trimmed and checked against the model's catalog languages before it reaches `RunOptions`. Omit it or use `auto` for detection-capable models; models without detection fall back to English when supported, otherwise their first catalog language. Unsupported explicit languages return `400`.

Uploads may be WAV or OGG/Opus. OGG/Opus decoding requires `ffmpeg` in `PATH`; the decode timeout defaults to 30 seconds and can be changed with `KWISPR_FFMPEG_TIMEOUT_SECONDS`.

## API contract

```bash
curl -sS http://127.0.0.1:19650/v1/audio/transcriptions \
  -F model=gigaam-v3-e2e-ctc \
  -F response_format=json \
  -F language=ru \
  -F file=@sample.wav
```

Success is `{"text":"..."}`. Errors retain the existing `{"error":"..."}` contract:

- `400`: malformed multipart, missing fields, unsupported response format, or invalid/unsupported audio;
- `404`: unknown model slug;
- `422`: model path, load, session, or transcription failure;
- `500`: unexpected server failure.

The upload limit defaults to 256 MiB and can be overridden with `KWISPR_MAX_UPLOAD_BYTES`.

## Optional VAD

VAD is disabled by default. Energy VAD has no model dependency:

```bash
KWISPR_VAD_ENABLED=1
KWISPR_VAD_PROVIDER=energy
KWISPR_VAD_THRESHOLD=0.01
KWISPR_VAD_FRAME_MS=30
KWISPR_VAD_MIN_SPEECH_MS=150
KWISPR_VAD_PADDING_MS=120
```

The existing neural option remains Silero ONNX VAD (not STT inference):

```bash
KWISPR_VAD_ENABLED=1
KWISPR_VAD_PROVIDER=silero
KWISPR_VAD_MODEL=~/.local/share/kwispr/models/silero_vad_v4.onnx
KWISPR_VAD_THRESHOLD=0.3
```

Equivalent `--vad-*` flags are supported. `/health` reports active VAD settings. Both providers trim leading/trailing silence and return an empty transcript for no-speech/short-noise audio before model loading; `kwispr.sh` treats this local empty result as a clean `No speech` skip.

## Suggested slugs

| Need | Slug |
|---|---|
| Russian | `gigaam-v3-e2e-ctc` |
| European multilingual | `parakeet-tdt-0.6b-v3` |
| Broad multilingual fallback | `whisper-large-v3-turbo` |
| Low-latency English | `parakeet-unified-en-0.6b` or `moonshine-tiny` |

Use `./kwispr-models.py list` for the complete current catalog.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `curl: (7) Failed to connect` | Start the Rust runtime and check `curl http://127.0.0.1:19650/health`. |
| `[stub transcript]` | Stop the legacy Python stub and run the Rust runtime. |
| `unknown model` | Use an exact slug from `./kwispr-models.py list`. |
| `model ... is not installed` | Download the slug and ensure helper/server use the same `KWISPR_MODEL_DIR`. |
| OGG/Opus decode failure | Install `ffmpeg`, verify it is in `PATH`, and inspect the returned decode error. |
| checksum failure | Do not use the bytes; retry so another catalog source can be attempted. |
| backend initialization/load failure | Keep staged shared libraries beside the binary and verify the host Vulkan loader/driver; CPU remains available through dynamic CPU modules. |
| incorrect language | Supply an ISO language code supported by the selected model, or omit `KWISPR_LANGUAGE` for model-default behavior. |
