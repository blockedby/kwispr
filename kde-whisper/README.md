# KDE Whisper

`kde-whisper` is the optional KDE tray/settings app for Kwispr.

It is intentionally thin: recording, retry, clipboard, and backend calls remain in the existing `kwispr.sh toggle` workflow. The tray app gives a native KDE entry point and settings UI without changing the proven CLI path.

## Development stack

- C++20
- Qt 6 Widgets
- KDE Frameworks 6
- `KStatusNotifierItem` tray integration
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

Use your existing KDE global shortcut for dictation:

```bash
/path/to/kwispr.sh toggle
```

The tray Toggle Recording action calls the same `kwispr.sh toggle` command.

## Model management

The Local model row provides asynchronous **Download** and confirmed **Delete** controls. Downloads show live percentage, compact ETA, and checksum-verification state. KDE Whisper delegates both to the catalog-authoritative helper without invoking a shell:

```bash
./kwispr-models.py download whisper-large-v3-turbo
./kwispr-models.py delete whisper-large-v3-turbo
./kwispr-models.py verify whisper-large-v3-turbo
```

Delete removes only the catalog-selected default GGUF for that slug. Language is one optional hint: choose **Auto detect** (empty `KWISPR_LANGUAGE`) for mixed-language speech when the selected catalog model supports detection.

## Install metadata

The CMake install target includes:

- `kde-whisper` binary;
- `org.kwispr.KdeWhisper.desktop`;
- `org.kwispr.KdeWhisper.metainfo.xml`;
- scalable app icon.

Staged install example:

```bash
DESTDIR=/tmp/kde-whisper-install cmake --install kde-whisper/build --prefix /usr
```
