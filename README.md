# Kwispr

**Kwispr is a Linux voice-dictation tool for Wayland/KDE.** Hit one hotkey, speak, hit it again, and your words are transcribed, copied to the clipboard, and optionally pasted into the focused app.

This fork is no longer “just a tiny Bash wrapper around OpenAI Whisper”. It now supports:

- cloud transcription through OpenAI `/v1/audio/transcriptions`;
- OpenRouter chat/audio models;
- local/offline OpenAI-compatible STT on `127.0.0.1:9000`;
- a Rust local STT runtime with model catalog support;
- optional VAD/no-speech handling;
- Wayland clipboard + `ydotool` auto-paste;
- persistent notifications and sound cues;
- retry-safe recording archive.

![demo](demo.gif)

## How it feels

1. Put the cursor where you want text.
2. Press your Kwispr hotkey.
3. Speak naturally, Russian/English/mixed depending on the selected backend/model.
4. Press the hotkey again.
5. Kwispr transcribes and pastes the result.

If auto-paste fails, the transcript is still in the clipboard.

## Architecture

```text
KDE hotkey
  └─ kwispr.sh toggle
       ├─ start recording with ffmpeg/PipeWire/Pulse into ~/.cache/kwispr/*.wav
       └─ stop recording gracefully, then POST the WAV to configured STT backend
            ├─ OpenAI Whisper / compatible cloud endpoint
            ├─ OpenRouter audio-capable chat model
            └─ local Rust STT runtime on http://127.0.0.1:9000
                 └─ Handy catalog v2 GGUF models via transcribe-cpp 0.1.3
                      ├─ cached batch Sessions
                      └─ dynamic CPU/Vulkan backends on Linux
```

`kwispr.sh` itself stays simple and stateless. The long-running part, if you use local mode, is the local STT server.

## Dependencies

Runtime packages:

| Package | Purpose |
|---|---|
| `ffmpeg` | microphone recording |
| `curl` | STT HTTP request |
| `jq` | JSON parsing |
| `wl-clipboard` | clipboard integration on Wayland |
| `libnotify` / `notify-send` | status notifications |
| `pipewire-pulse` | Pulse-compatible recording source |
| `ydotool` + `ydotoold` | optional auto-paste on Wayland |

On CachyOS/Arch:

```bash
sudo pacman -S --needed ffmpeg curl jq wl-clipboard libnotify pipewire-pulse ydotool
```

For local Rust STT builds on the host:

```bash
sudo pacman -S --needed rust cmake clang vulkan-headers vulkan-icd-loader shaderc spirv-headers pkgconf
```

Or build in Podman so build dependencies do not pollute the host; see [Build local STT in Podman](#build-local-stt-in-podman).

## Install

```bash
git clone git@github.com:blockedby/kwispr.git
cd kwispr
cp .env.example .env
chmod 600 .env
```

Optional setup helper:

```bash
./setup.sh
```

Then edit `.env` for one of the backend modes below.

## Bind a hotkey in KDE

1. Open **System Settings → Shortcuts → Add New → Command/URL Shortcut**.
2. Trigger: press your desired key/combo.
3. Action: absolute path to `kwispr.sh`, for example:

```text
/home/you/code/kwispr/kwispr.sh
```

Press once to start recording, press again to stop and transcribe.

## Configuration

### Local/offline STT — recommended for this fork

Download a model. Model names are Handy `slug` values; the helper selects that model's catalog `default_quant`, downloads revision-pinned GGUF bytes (mirror first, then Hugging Face), and verifies SHA256:

```bash
./kwispr-models.py list
./kwispr-models.py download whisper-large-v3-turbo
# or for Russian-only dictation:
./kwispr-models.py download gigaam-v3-e2e-ctc
./kwispr-models.py verify gigaam-v3-e2e-ctc
# remove only the catalog-managed default GGUF for a slug:
./kwispr-models.py delete gigaam-v3-e2e-ctc
```

The KDE settings dialog also provides nonblocking **Download** and **Delete** actions beside the Local model selector. Deletion is confirmed and delegates to the same catalog-authoritative helper.

The full 67-model catalog is synced from Handy commit `ea3c20a3a67c7401d8b19198723760da9d40ac45`; provenance and immutable per-model revisions are recorded in `models/local-stt-catalog.json`.

Build and run the Rust server:

```bash
cd rust-local-stt
cargo build --release
KWISPR_MODEL_DIR=~/.local/share/kwispr/models \
  ./target/release/kwispr-local-stt \
  --host 127.0.0.1 \
  --port 9000 \
  --catalog ../models/local-stt-catalog.json
```

`.env`:

```bash
KWISPR_BACKEND=openai-transcriptions
KWISPR_API_URL=http://127.0.0.1:9000/v1/audio/transcriptions
KWISPR_MODEL=whisper-large-v3-turbo
KWISPR_API_KEY=
KWISPR_LANGUAGE=
KWISPR_AUTOPASTE=1
KWISPR_PASTE_HOTKEY=shift-insert
KWISPR_AUTOPASTE_DELAY=0.30
KWISPR_SOUNDS=1
KWISPR_PULSE_SOURCE=default
```

Useful model choices:

| Need | Model id |
|---|---|
| Russian dictation | `gigaam-v3-e2e-ctc` |
| Mixed Russian/English | `whisper-large-v3-turbo` |
| European multilingual dictation | `parakeet-tdt-0.6b-v3` |

### Build local STT in Podman

A container build file is included at `rust-local-stt/Containerfile`.

Convenience script:

```bash
./rust-local-stt/build-in-podman.sh
```

One-shot build without installing Rust/CMake/Vulkan headers on the host:

```bash
podman run --rm \
  -v "$PWD":/work:Z \
  -w /work/rust-local-stt \
  docker.io/library/archlinux:latest \
  bash -lc 'pacman -Syu --noconfirm --needed base-devel rust cmake clang vulkan-headers vulkan-icd-loader shaderc spirv-headers git pkgconf ccache && cargo build --release'
```

Or build a reusable build image:

```bash
podman build -t kwispr-local-stt-builder -f rust-local-stt/Containerfile rust-local-stt
podman run --rm -v "$PWD":/work:Z kwispr-local-stt-builder
```

The binary and its required transcribe-cpp shared runtime/backend modules land together in `rust-local-stt/target/release/`. Keep the staged `libtranscribe.so*` and `libggml*.so*` files beside the binary when copying it elsewhere.

Run it on the host so it can see your GPU/Vulkan driver stack and model directory.

### OpenAI Whisper cloud mode

`.env`:

```bash
KWISPR_BACKEND=openai-transcriptions
KWISPR_API_URL=https://api.openai.com/v1/audio/transcriptions
KWISPR_MODEL=whisper-1
KWISPR_API_KEY=sk-...
KWISPR_LANGUAGE=
```

### OpenRouter audio mode

`.env`:

```bash
KWISPR_BACKEND=openrouter-chat
KWISPR_API_URL=https://openrouter.ai/api/v1/chat/completions
KWISPR_MODEL=google/gemini-2.5-flash
KWISPR_API_KEY=sk-or-...
KWISPR_HTTP_REFERER=https://github.com/blockedby/kwispr
KWISPR_APP_TITLE=kwispr
KWISPR_AUDIO_FORMAT=wav
KWISPR_TRANSCRIPTION_PROMPT='Transcribe this audio exactly as spoken. The speech may be Russian, English, or mixed. Do not translate. Return only the transcript.'
```

## Commands

```bash
./kwispr.sh                 # same as toggle
./kwispr.sh toggle          # start/stop recording
./kwispr.sh retry file.wav  # retry an archived failed recording
```

Recordings and transcripts are stored in:

```text
~/.cache/kwispr/
```

Old `*.wav` and `*.txt` files are rotated after 30 days.

## Auto-paste

Kwispr always writes the transcript to the clipboard with `wl-copy`.

If `KWISPR_AUTOPASTE=1`, it also asks `ydotool` to press a paste hotkey. Supported values:

```bash
KWISPR_PASTE_HOTKEY=ctrl-v
KWISPR_PASTE_HOTKEY=ctrl-shift-v
KWISPR_PASTE_HOTKEY=shift-insert
```

Disable auto-paste:

```bash
KWISPR_AUTOPASTE=0
```

## Sounds and notifications

Default cues live in `sounds/`:

- `start.wav` — recording started;
- `stop.wav` — recording stopped, processing;
- `ready.wav` — transcript ready.

Disable sounds:

```bash
KWISPR_SOUNDS=0
```

Override sounds:

```bash
KWISPR_SOUND_START=/path/to/start.wav
KWISPR_SOUND_STOP=/path/to/stop.wav
KWISPR_SOUND_READY=/path/to/ready.wav
```

## Long recordings and upload limits

The local Rust server accepts larger uploads than Axum's small default body limit. This matters because 16 kHz mono WAV is roughly 32 KB/sec; a 2-minute recording is about 3.8 MB.

If long recordings fail, check:

```bash
curl http://127.0.0.1:9000/health
```

and make sure you are running a freshly rebuilt `kwispr-local-stt` binary from this fork.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `No .env` | `cp .env.example .env; chmod 600 .env` |
| `API 413` / `failed to read stream` on long local recordings | Rebuild the Rust local STT runtime from this fork; it raises the body limit. |
| Local mode returns `[stub transcript]` | You are running `kwispr-local-stt-server.py`; run the Rust runtime for real inference. |
| `curl: (7) Failed to connect` | Local server is not running or wrong port. |
| `unknown model` | Run `./kwispr-models.py list` and download the selected model. |
| Empty transcript for silence | Expected in local VAD/no-speech cases. |
| Records but does not paste | Check `ydotoold` and `/dev/uinput`; or set `KWISPR_AUTOPASTE=0`. |
| Wrong paste shortcut in terminal | Try `KWISPR_PASTE_HOTKEY=shift-insert` or `ctrl-shift-v`. |
| No notifications | Install `libnotify` / make sure `notify-send` exists. |
| Mic not recording | Check `pactl list sources short` and `KWISPR_PULSE_SOURCE`. |
| Stale mic/ffmpeg after crash | `pkill -f 'ffmpeg.*pulse'` |

## Optional KDE Whisper tray app

This fork also contains an experimental optional KDE/Qt tray and settings app:

```bash
./kde-whisper/scripts/podman-test.sh
./kde-whisper/build/kde-whisper
```

It does **not** replace the proven CLI path. Your KDE global shortcut can keep running:

```bash
/path/to/kwispr.sh toggle
```

The tray action delegates to the same `kwispr.sh toggle` command, and model downloads still use the existing helper:

```bash
./kwispr-models.py download whisper-large-v3-turbo
```

See [`docs/kde-whisper.md`](docs/kde-whisper.md) for build, test, install, and current limitation notes.

## Development notes

- `kwispr.sh` is the user-facing toggle script.
- `kwispr-models.py` manages the local model catalog and downloads.
- `rust-local-stt/` contains the real local inference server.
- `kwispr-local-stt-server.py` is only a legacy Python stub for API wiring tests.
- More local STT details: [`docs/local-stt.md`](docs/local-stt.md).
- Optional KDE tray/settings app details: [`docs/kde-whisper.md`](docs/kde-whisper.md).

## License

MIT — see [LICENSE](LICENSE).
