# KDE Whisper tray/settings app

`kde-whisper` is an optional KDE-native tray and settings shell for Kwispr. It does not replace the stable command-line workflow: your existing KDE shortcut can keep running `kwispr.sh toggle`, and `kwispr.sh` remains the recording/transcription path.

## What it does today

- shows a KDE tray entry through `KStatusNotifierItem`;
- opens a Qt settings dialog for backend, model, prompt, paste, and VAD settings;
- creates and manages the same XDG configuration used by `kwispr.sh`;
- registers a configurable native KDE global dictation shortcut through KF6 `KGlobalAccel`;
- uses KF6 `KDBusService::Unique` so repeated launches cannot duplicate the tray or shortcut;
- routes repeated settings launches to the existing singleton settings dialog;
- delegates recording/retry to the existing `kwispr.sh toggle` and `kwispr.sh retry` commands;
- reads the local STT model catalog and delegates downloads to `kwispr-models.py`.

## Build and test

The top-level installer supports a native Arch build with temporary dependencies:

```bash
./install.sh --build-backend host
```

It snapshots pacman package state, installs only missing build packages as dependencies, and removes only packages introduced by that build. Required KDE runtime packages, including Arch's `kdbusaddons`, remain installed. Use `--keep-build-deps` to retain a development environment. Podman remains an optional isolated backend:

```bash
./kde-whisper/scripts/podman-test.sh
```

Run one containerized test:

```bash
./kde-whisper/scripts/podman-test.sh -R TrayControllerTest
```

The test wrapper sets `QT_QPA_PLATFORM=offscreen` by default so Qt tests can run headless in the container.

## Build artifact

The Podman command configures and builds under `kde-whisper/build/`. The installer keeps native caches separately under `kde-whisper/build-host/`, preventing container source paths or generator choices from poisoning a later host build.

After a successful build, the binary is respectively:

```text
kde-whisper/build/kde-whisper
kde-whisper/build-host/kde-whisper
```

Run it on your KDE desktop from the repository root:

```bash
./kde-whisper/build/kde-whisper
```

## Install

For a polished rootless installation, including scripts, catalog, sounds, desktop metadata, autostart, and optional local STT:

```bash
./install.sh
```

For packaging or staged inspection, CMake installs the same core layout with relocatable launchers:

```bash
DESTDIR=/tmp/kde-whisper-install cmake --install kde-whisper/build --prefix /usr
```

Configuration is saved with mode `0600` at `$XDG_CONFIG_HOME/kwispr/config.env`; users do not need to prepare `.env`.

## Local STT connection and LAN service

The Local STT backend accepts an editable OpenAI-compatible transcription URL, including a remote LAN hostname/IP and port. The resolved request URL remains visible. When this computer has the local user service installed, Settings also shows the independent listen address/port and **Allow LAN clients** control. Loopback (`127.0.0.1:19650`) is the fresh default; LAN binding is opt-in and applies by restarting `kwispr-local-stt.service` with visible success/failure feedback.

A wildcard listen address such as `0.0.0.0` is never reused as the client destination. Remote-only clients may choose any catalog model slug even when no runtime or model file is installed locally. **Download here** and **Delete local** affect only the current computer, never the remote server.

## Global dictation shortcut

While the tray is running it owns a native `KGlobalAccel` action named **Toggle Dictation Recording**. A fresh registration requests **Ctrl+.** only when KDE reports it available. A saved custom shortcut or an explicitly cleared shortcut is autoloaded unchanged, so upgrades do not reset the user's KGlobalAccel choice.

Before first registering the native action, an upgrade checks for the former desktop-launcher action (`org.kwispr.KdeWhisper.desktop` / `_launch`). Its exact Ctrl+. or custom keys are moved only if every owner reported by KGlobalAccel is that legacy component/action. Mixed, foreign, or stale ownership is refused without changing those bindings. A pending journal at `$XDG_STATE_HOME/kwispr/global-shortcut-migration.ini` (normally `~/.local/state/kwispr/...`) is synced before the one-way steal, supports startup recovery, and is cleared into a completion marker only after the native keys match exactly and the legacy keys are empty. Existing native custom and explicitly-cleared choices take precedence, and completed/refused migrations are not repeated. Kwispr never edits `kglobalshortcutsrc` directly.

Open **Kwispr Settings → Global dictation shortcut** to record one key combination or clear it. **Apply** changes the running registration immediately and KGlobalAccel persists it; the XDG `config.env` deliberately contains no competing shortcut value. Conflicting sequences are rejected with a visible error and no other global action is reassigned.

The tray owns a unique session-bus service. A second ordinary `kde-whisper` activation exits without creating another tray or global shortcut. `kde-whisper --settings` and `kwispr settings` route to `TrayApp::openSettings`, restoring and focusing the already-open singleton dialog when necessary.

The action calls the same `kwispr.sh toggle` path as the tray menu. Keep this command-based shortcut as a fallback when the tray is not running:

```bash
~/.local/bin/kwispr toggle
```

Avoid assigning the fallback command to the same sequence while the native tray shortcut is enabled.

## Local STT models

The **Local model** row in KDE Whisper settings shows catalog install state and provides asynchronous **Download** and confirmed **Delete** actions. Downloads report live percentage and compact ETA, then clearly show checksum verification. The same operations are available through the authoritative helper:

```bash
~/.local/bin/kwispr models download whisper-large-v3-turbo
# Repository-development equivalent:
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
- Hosting Local STT still requires the installed Rust runtime; connecting to a remote Local STT server does not.
- LAN mode does not provide authentication, TLS, reverse-proxy setup, or firewall automation.
