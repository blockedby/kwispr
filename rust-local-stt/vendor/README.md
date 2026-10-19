# Pinned transcription dependency patches

The vendored `transcribe-cpp` and `transcribe-cpp-sys` crates are the published
**0.4.0** distributions, selected together by the root Cargo patches. The native
provider includes its matching ggml sources. All upstream license files remain.

Upstream release: https://github.com/handy-computer/transcribe.cpp/releases/tag/v0.4.0

Original archive SHA256 and source commit:

- `transcribe-cpp`: `f6cad85eeb846a7064891c8b49b92a57a0f2d4dd089104cb199608b23043555e`; commit `f1dcd15858c4bbba87ca97862b4e448b2cf15b8f`.
- `transcribe-cpp-sys`: `7f0976932ee2a0d9bcfb7a90947d0a73771cee383b163bd7189a8e0d67da1157`; commit `f1dcd15858c4bbba87ca97862b4e448b2cf15b8f`.

## Upstream changes used directly

Qwen vocabulary uses upstream `RunOptions.vocabulary` and system-context token
construction. No local Qwen decoder patch or Python inference is needed.
Exact CPU/GPU device handles replace Kwispr's former `gpu_device=-1` workaround.
The old 0.1.3 device-index patch is removed, not carried forward.
The bindings, shared library, and backend modules must be built and installed
together because the 0.2/0.4 API and ABI are incompatible with 0.1.

## Retained Whisper behavior

`kwispr-whisper-0.4.patch` records every local difference from the published
archives, including the matching native/Rust local ABI digest:

- Expose the native prompt-window policy in the safe Rust wrapper and permit
  ALL_SEGMENTS with generated-history conditioning disabled. This retains the
  user's original prompt on every Whisper window without feeding generated
  text into later windows. Prompt-free defaults and fallback thresholds stay.
- Retain the separate WHR2 family extension for allowed auto-detection
  languages. The existing WHRN layout remains unchanged. Serial and batch
  paths share the candidate selector; explicit language still takes precedence.
- Preserve the 0.4 generic-context/prefix checks when validating the extended
  Whisper options. The local ABI digest distinguishes this provider and wrapper
  from the unpatched release, so mixed installations are rejected.
- Let model-gated upstream tests find the sibling vendored sys crate when the
  original upstream repository layout is absent. Missing fixtures skip cleanly.

## Verification

Use `./rust-local-stt/build-in-podman.sh`, then in the same builder image run
`cargo test --release --workspace`. Real-model checks must also cover Qwen
vocabulary on/off/on, Russian and English, no dictionary dumping, and exact
CPU/integrated/discrete GPU selection. Whisper checks must cover a clip longer
than two 30-second windows with and without its static prompt and RU/EN candidate
list. Existing GGUF model files can be retained; verify them against the catalog.
