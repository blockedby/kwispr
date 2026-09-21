# Dictation quality verification — 2026-09-21

The later [repetition regression and correction](dictation-repetition-verification.md)
supersedes the generated-history approach described in this original verification.

## Installed baseline

The active installation used local Whisper large-v3-turbo Q8_0, transcribe-cpp 0.1.3, automatic language selection, and energy VAD (threshold 0.01, padding 1500 ms). Its installed CLI and model catalog matched the current repository and `origin/main` fetched on this date. The application reports package version 0.1.0; that version alone does not identify an exact installed source revision.

The working baseline was 271e016, whose code matched `origin/main` (abbb2f2); only README language differed. Feature commits remain on the dedicated `codex/dictation-quality` branch. Integration branches were not rewritten.

## Automated checks

- `bash -n kwispr.sh`: passed.
- `python3 -m unittest tests/test_kwispr_sh_contract.py`: 16 passed, including default/opt-in request shape, backend switching, Unicode limits, locale behavior, and FIFO recording-stop ordering.
- `python3 -m unittest discover`: 70 passed after integration.
- `./kde-whisper/scripts/podman-test.sh`: 13 CTests passed, including settings persistence, clearing values, supported models, keyboard navigation, and constrained window sizes.
- Rust, in the existing `kwispr-local-stt-builder` Podman image: `cargo fmt --check && cargo test --release --locked && cargo build --release --locked`: 27 tests passed and the release binary built.
- Isolated local HTTP checks: health capabilities, default silence, prompted silence, unsupported architecture, oversized Unicode context, NUL context, and malformed tail flag behaved as expected.

The Rust check used a separate Cargo cache and a dedicated worktree, mounted as `/work`, rather than relying on host Vulkan dependencies:

```bash
podman run --rm \
  -v /tmp/kwispr-dictation-cargo:/root/.cargo:Z \
  -v "$PWD":/work:Z -w /work/rust-local-stt \
  kwispr-local-stt-builder \
  bash -lc 'cargo fmt --check && cargo test --release --locked && cargo build --release --locked'
```

## Actual audio and rendering

Three recent user dictations (7.55 s, 16.05 s, and 43.55 s) were replayed only to local servers. Original recording/transcript files were not changed, and no audio or transcript artifacts were added to the repository.

1. Current VAD settings versus VAD disabled produced identical transcripts on all three clips. These clips do not support blaming VAD for the observed failure.
2. A short Russian punctuation example improved both short clips. The 16.05-second clip also recovered a previously omitted ending.
3. For the 43.55-second clip, the initial prompt alone improved the first segment but left the final segment without punctuation. Enabling previous-text conditioning when a prompt is present restored punctuation in that final segment as well.
4. Native initial context is limited to approximately 223 tokens. The UI and documentation state that the dictionary and punctuation example share that capacity; the 4096-character API validation bound does not imply all text will be used.
5. A disposable PipeWire null sink and its monitor exercised the real CLI/ffmpeg capture path without using the microphone or changing the default audio device. Immediate stop recorded 2.00 s; a configured 350 ms grace period recorded 2.55 s, including capture buffering. The temporary sink was unloaded.
6. Real Qt offscreen renders at 520×560, 760×720, and 960×760 were visually inspected. The settings scroll, the action row remains reachable, and the changed form has no horizontal scrollbar. This was an offscreen Qt check, not desktop mouse automation.

These checks establish the observed improvement for the supplied clips, not perfect recognition for every speaker or future recording. Whisper hints can also change wording. No cloud postprocessing or model upgrade was enabled.
