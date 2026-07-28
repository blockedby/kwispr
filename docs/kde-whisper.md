# KDE Whisper tray/settings app

`kde-whisper` is an optional KDE-native tray and settings shell for Kwispr. It does not replace the stable command-line workflow: your existing KDE shortcut can keep running `kwispr.sh toggle`, and `kwispr.sh` remains the recording/transcription path.

## What it does today

- shows a KDE tray entry through `KStatusNotifierItem`;
- opens a Qt settings dialog for backend, model, prompt, paste, and VAD settings;
- reads and writes the same `.env` file used by `kwispr.sh`;
- delegates recording/retry to the existing `kwispr.sh toggle` and `kwispr.sh retry` commands;
- reads the local STT model catalog and delegates downloads to `kwispr-models.py`.

## Build and test in Podman

All KDE/Qt development dependencies are installed inside the Arch-based Podman image:

```bash
./kde-whisper/scripts/podman-test.sh
```

Run one test:

```bash
./kde-whisper/scripts/podman-test.sh -R TrayControllerTest
```

The test wrapper sets `QT_QPA_PLATFORM=offscreen` by default so Qt tests can run headless in the container.

## Build artifact

The test command configures and builds under:

```text
kde-whisper/build/
```

After a successful build, the binary is:

```text
kde-whisper/build/kde-whisper
```

Run it on your KDE desktop from the repository root:

```bash
./kde-whisper/build/kde-whisper
```

## Optional install into a staging directory

For packaging or local inspection:

```bash
cmake --install kde-whisper/build --prefix /usr --component Runtime
```

For a safe staged install:

```bash
DESTDIR=/tmp/kde-whisper-install cmake --install kde-whisper/build --prefix /usr
```

This installs the binary, desktop file, AppStream metainfo, and icon into the staged tree.

## Hotkey behavior

The app deliberately does not hijack the existing KDE global shortcut. Keep your current KDE shortcut pointed at:

```bash
/path/to/kwispr.sh toggle
```

The tray action also delegates to `kwispr.sh toggle`, so both paths exercise the same tested CLI behavior.

## Local STT models

The **Local model** row in KDE Whisper settings shows catalog install state and provides asynchronous **Download** and confirmed **Delete** actions. Downloads report live percentage and compact ETA, then clearly show checksum verification. The same operations are available through the authoritative helper:

```bash
./kwispr-models.py download whisper-large-v3-turbo
./kwispr-models.py delete whisper-large-v3-turbo
```

Deletion removes only the slug's catalog-selected default GGUF from `KWISPR_MODEL_DIR`. Select the model in the KDE Whisper settings dialog or set:

```env
KWISPR_MODEL=whisper-large-v3-turbo
```

**Language** is a single optional hint. Catalog models offer only their supported language codes and offer **Auto detect** only when detection is supported. Choose Auto (an empty `KWISPR_LANGUAGE`) for mixed-language dictation such as Russian plus English. OpenAI accepts one editable hint; OpenRouter does not use this setting.

## Current limitations

- This is an optional experimental KDE control surface.
- It is not a replacement for the documented shell workflow yet.
- Model install/delete/verify behavior stays authoritative in `kwispr-models.py`.
- Local STT still requires a built `rust-local-stt/target/release/kwispr-local-stt` binary.
