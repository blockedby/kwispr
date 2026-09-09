# Kwispr

> Fast voice dictation for Linux, Wayland, and KDE.

Press a hotkey, speak, and press it again. Kwispr transcribes your speech, copies the result to the clipboard, and can paste it directly into the focused app.

- private local transcription;
- OpenAI and OpenRouter support;
- Russian, English, and mixed-language speech;
- KDE tray, notifications, and sound cues;
- recording archive with retry support.

![Kwispr demo](demo.gif)

## Quick start

```bash
git clone https://github.com/blockedby/kwispr.git
cd kwispr
./setup.sh    # system dependencies
./install.sh  # install the app under ~/.local
```

Open **Kwispr Settings**, choose a backend, and assign a global hotkey. The first press starts recording; the second stops and transcribes it.

Settings are stored in `~/.config/kwispr/config.env`. To run directly from the repository, copy `.env.example` to `.env`.

## Local transcription

For private offline dictation, install the local runtime and a model from the built-in verified catalog:

```bash
./install.sh --with-local-stt
./kwispr-models.py list
./kwispr-models.py download whisper-large-v3-turbo
systemctl --user enable --now kwispr-local-stt.service
```

Equivalent settings in `~/.config/kwispr/config.env`:

```bash
KWISPR_BACKEND=openai-transcriptions
KWISPR_API_URL=http://127.0.0.1:19650/v1/audio/transcriptions
KWISPR_MODEL=whisper-large-v3-turbo
KWISPR_API_KEY=
KWISPR_AUTOPASTE=1
KWISPR_PASTE_HOTKEY=shift-insert

# Keep quiet phrase endings from being trimmed too early
KWISPR_VAD_ENABLED=1
KWISPR_VAD_PROVIDER=energy
KWISPR_VAD_THRESHOLD=0.01
KWISPR_VAD_PADDING_MS=1500
```

Suggested models:

| Use case | Model |
|---|---|
| Russian speech | `gigaam-v3-e2e-ctc` |
| Mixed Russian and English | `whisper-large-v3-turbo` |
| Multilingual speech | `parakeet-tdt-0.6b-v3` |

## Cloud backends

<details>
<summary><strong>OpenAI Whisper</strong></summary>

```bash
KWISPR_BACKEND=openai-transcriptions
KWISPR_API_URL=https://api.openai.com/v1/audio/transcriptions
KWISPR_MODEL=whisper-1
KWISPR_API_KEY=sk-...
```

</details>

<details>
<summary><strong>OpenRouter</strong></summary>

```bash
KWISPR_BACKEND=openrouter-chat
KWISPR_API_URL=https://openrouter.ai/api/v1/chat/completions
KWISPR_MODEL=google/gemini-2.5-flash
KWISPR_API_KEY=sk-or-...
KWISPR_AUDIO_FORMAT=wav
```

</details>

## Commands

```bash
./kwispr.sh toggle          # start or stop dictation
./kwispr.sh retry file.wav  # retry a failed transcription
./kwispr-models.py list     # list local models
```

Recordings and transcripts are stored in `~/.cache/kwispr/` and removed after 30 days.

## How it works

```text
Global hotkey / KDE tray
          │
          ▼
      kwispr.sh ──► WAV ──► OpenAI / OpenRouter
                         └─► local Rust STT ──► GGUF model
          │
          └──────────────► clipboard + auto-paste
```

The KDE app uses the same proven `kwispr.sh` path, so the CLI keeps working independently of the tray.

## Troubleshooting

| Symptom | What to check |
|---|---|
| Microphone is not recording | `pactl list sources short` and `KWISPR_PULSE_SOURCE` |
| The end of a phrase is clipped | Increase `KWISPR_VAD_PADDING_MS`, for example to `1500` |
| Text is not pasted | Ensure `ydotoold` is running; try `shift-insert` |
| Local server is unavailable | `curl http://127.0.0.1:19650/health` |
| `unknown model` error | Download it with `kwispr-models.py download` |

## Development

```bash
python3 -m unittest discover
./kde-whisper/scripts/podman-test.sh
./rust-local-stt/build-in-podman.sh
```

Learn more: [local STT](docs/local-stt.md) · [KDE app](docs/kde-whisper.md)

## License

[MIT](LICENSE)
