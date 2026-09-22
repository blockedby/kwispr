# Pinned transcription dependency patches

These sources are the crates.io distributions of `transcribe-cpp` and
`transcribe-cpp-sys` **0.1.3**, from upstream
<https://github.com/handy-computer/transcribe.cpp> commit
`a94e021ef658dc7c788837341a13f6acea3baf3c` (recorded in each
`.cargo_vcs_info.json`). Cargo's root `[patch.crates-io]` entries select these
copies for every build; the host Cargo registry is not modified.
The safe wrapper is also a workspace member so its FFI materialization tests
run against the same patched native library and root lockfile.

Original downloaded archive SHA-256:

| Archive | SHA-256 |
| --- | --- |
| `transcribe-cpp-0.1.3.crate` | `4c3c4d6136eeccf56cfe8a6669e2d63770d1ef051c7cafd2cb9226218c66cded` |
| `transcribe-cpp-sys-0.1.3.crate` | `278fd6a6da4d9d8d5f2716bd6761a76ea55c129fda6ba57856b80249a8570ed4` |

All distributed source and license files are retained, including the MIT
licenses at each crate root, `ggml/LICENSE`, `ggml/AUTHORS`, and the miniz
license under `src/third_party/miniz/LICENSE`. Cargo registry bookkeeping files
`.cargo-ok` and `.cargo-checksum.json` are omitted; these are patched path
dependencies, not unchanged registry packages.

## Why this patch exists

Conditioning each Whisper window on generated text can propagate a decoding
error into repeated phrases. Dropping that history while keeping the user's
original punctuation example and vocabulary on every window avoids that
feedback mechanism. Upstream 0.1.3 has the necessary prefix construction but
rejects that option combination, and its safe Rust API omits the prompt mode.

The original static-prompt changes are:

1. `transcribe-cpp/src/family.rs` exposes the native prompt mode as the typed
   `WhisperPromptCondition` enum and optional `WhisperRunOptions.prompt_condition`
   field. `src/lib.rs` re-exports it. Unit tests check native materialization,
   prompt lifetime, and unchanged fallback/default settings.
2. `transcribe-cpp-sys/src/arch/whisper/model.cpp` removes only the serial
   rejection of `ALL_SEGMENTS` with `condition_on_prev_tokens=false`. Its
   existing prefix branch then supplies the original prompt to every window
   without generated history. The existing batch-to-serial routing remains.
3. The native header's comments describe the newly permitted combination;
   no C ABI layout or numeric enum values change.

The runtime selects this combination only when a nonempty prompt is supplied.
Prompt-free requests, audio segmentation, the 223-token prompt capacity, and
temperature/compression/log-probability/no-speech fallback defaults remain
unchanged. This is a decoder-context fix, not transcript deduplication.
`kwispr-static-prompt.patch` records the complete diff from the original four
upstream source/header files for review.

## Optional language-detection candidates

`kwispr-allowed-languages.patch` applies after the static-prompt patch. It adds
an optional `WhisperRunOptions.allowed_languages` list and a distinct native
`WHR2` run extension. The original `WHRN` struct and initializer keep their
80-byte ABI. The v2 struct embeds that base and adds an owned-for-the-call CSV
pointer; its separate initializer and kind prevent old libraries from silently
ignoring the new option. Requests without the list still use the original kind.

Both serial and batch detection use `language-selection.h` to select the
highest language-token logit among the validated candidates. A supplied source
language wins over the candidates. Unknown languages, empty entries and lists
over 1024 bytes are rejected. This changes language identification only: text
token sampling, task selection, prompts, thresholds and audio windows retain
their existing behavior. A first-window language hint remains in use for the
whole native run, so restricting detection cannot guarantee perfect mixed
Russian/English transcription within one window.

The HTTP runtime exposes `allowed_languages=ru,en` as an optional multipart
field for Whisper models and advertises `whisper_allowed_languages` in health
capabilities. It canonicalizes case/region codes against the model catalog;
the native layer independently validates against the loaded model. Omission
keeps unrestricted detection; `language` remains an explicit override.

The new FFI declarations were regenerated with the actual upstream generators
from commit `a94e021ef658dc7c788837341a13f6acea3baf3c`, after copying the patched
Whisper header into that checkout:

```sh
python3 bindings/python/_generate/generate.py
cargo run --locked -p xtask -- bindgen
```

The upstream Python generator computes the normalized ABI digest
`2839898fc84a0f5a`; Rust bindgen 0.72.1 emits the committed declarations and
layout assertions. No generated layouts are edited by hand. The original
unpatched checkout passed `cargo run --locked -p xtask -- bindgen --check`
before regeneration.

## Validation

Run in the project's Podman builder from `rust-local-stt/`:

```sh
cargo test --release --locked --no-default-features -p kwispr-local-stt -p transcribe-cpp --lib --bins
cargo build --release --locked
c++ -std=c++17 -Wall -Wextra -Werror \
  -Ivendor/transcribe-cpp-sys-0.1.3/include \
  -Ivendor/transcribe-cpp-sys-0.1.3/src \
  tests/whisper-language-selection.cpp -o /tmp/kwispr-whisper-language-test
/tmp/kwispr-whisper-language-test
```

The native prefix assembly is internal and has no model-free public test hook.
The API tests cannot prove recognition quality. Before installation, replay a
recording longer than two 30-second windows against the rebuilt runtime and
check repetition, punctuation, vocabulary and final words. Also check genuine
spoken repetition and a later prompt-free request. Keep private recordings out
of the repository. Replace this patch with a verified upstream implementation
when equivalent independent prompt/history controls and language candidates
are available. Language-list tests cover the actual shared candidate selector,
legacy ABI guard bytes, v2 materialization and default behavior. Before enabling
the option, also compare real RU, EN and mixed speech with and without it;
unit tests establish mechanism correctness, not recognition quality.
