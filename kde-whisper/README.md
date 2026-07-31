# KDE Whisper

`kde-whisper` is the optional KDE tray/settings app for Kwispr.

It is intentionally thin: recording, retry, clipboard, and backend calls remain in the existing `kwispr.sh toggle` workflow. The tray app gives a native KDE entry point and settings UI without changing the proven CLI path.

## Development stack

- C++20
- Qt 6 Widgets
- KDE Frameworks 6
- `KStatusNotifierItem` tray integration
- KF6 `KGlobalAccel` native global shortcuts
- CMake/Ninja
- Podman development container

## Test/build in Podman

From the repository root:

```bash
./kde-whisper/scripts/podman-test.sh
```

Run a single test:

```bash
./kde-whisper/scripts/podman-test.sh -R SettingsDialogTest
```

The wrapper passes `QT_QPA_PLATFORM=offscreen` into the container by default for headless Qt tests.

## Run locally

After building:

```bash
./kde-whisper/build/kde-whisper
```

The running tray registers **Ctrl+.** through `KGlobalAccel` on a fresh KDE registration when it is available. In **Settings → Global dictation shortcut**, record one key combination or clear it to disable the native shortcut. Apply updates KGlobalAccel immediately; existing saved/custom/cleared choices are preserved, and conflicts are rejected without stealing another action. A one-time upgrade migration moves an exact legacy desktop-launcher binding (`org.kwispr.KdeWhisper.desktop` / `_launch`) only when all owners match that action. Mixed or foreign ownership is refused without changing it; a journal under `$XDG_STATE_HOME/kwispr` makes the one-way migration recoverable and non-repeating.

The native action and tray **Toggle Recording** action both call the same `kwispr.sh toggle` workflow. Keep the installed command as a fallback when the tray is not running:

```bash
~/.local/bin/kwispr toggle
```

## Model management

The Local model row provides asynchronous **Download** and confirmed **Delete** controls. Downloads show live percentage, compact ETA, and checksum-verification state. KDE Whisper delegates both to the catalog-authoritative helper without invoking a shell:

```bash
./kwispr-models.py download whisper-large-v3-turbo
./kwispr-models.py delete whisper-large-v3-turbo
./kwispr-models.py verify whisper-large-v3-turbo
```

Delete removes only the catalog-selected default GGUF for that slug. Language is one optional hint: choose **Auto detect** (empty `KWISPR_LANGUAGE`) for mixed-language speech when the selected catalog model supports detection.

## Installation and metadata

For normal rootless installation, run `./install.sh` from the repository root. It provides optional Gum presentation, plain/noninteractive fallback, XDG configuration, autostart, and local STT service setup.

The CMake install target includes relocatable `kwispr`/`kde-whisper` launchers, the tray binary, shell/model helpers, catalog, sounds, desktop metadata, AppStream metadata, and the app icon.

Staged install example:

```bash
DESTDIR=/tmp/kde-whisper-install cmake --install kde-whisper/build --prefix /usr
```
