# Repetition, device defaults and concurrent recording — 2026-09-21

A later 66-second dictation exposed a regression in the earlier punctuation
change: the prompted decoder appended the same unspoken sentence ten times.
Replaying the original WAV to the installed local server reproduced it.
The same complete audio without a prompt, and its last 31 seconds with a prompt,
both ended normally. Original user files were kept unchanged and were never
uploaded externally or committed.

## Decoder correction

The old prompted mode conditioned later windows on generated text. The new mode
repeats only the original user prompt on every native window, with generated
history disabled. Audio segmentation, ending preservation and the normal native
fallback thresholds remain unchanged. There is no transcript deduplication.
The two pinned dependency patches and provenance are documented in
`rust-local-stt/vendor/README.md`; an independent source review found no actionable
issue in the four upstream files changed.

Podman release build (`cargo build --release --locked --offline`) passed.
`cargo test --release --locked --offline --no-default-features -p kwispr-local-stt -p transcribe-cpp --lib --bins`
passed 27 runtime tests and 2 FFI/default tests. The rebuilt binary SHA-256 is
`a881c0e1384faea4ee6077b425ae0d12312872c323bc06a4fd8d8fa88632e9df`.

Real local-server replay against the rebuilt candidate passed:

- 66-second reproducer: no invented repeated sentence; punctuation and the actual
  final words retained.
- Earlier 43.55-second recording: punctuation remains throughout, including the end.
- Earlier 16.05-second ending-loss recording: final phrase retained.
- Earlier 7.55-second recording: punctuated text retained.
- Synthetic 36.2-second test made from four copies of a real 8.05-second utterance
  plus silence: all four genuinely present repetitions retained.
- Prompt-free replay: same unpunctuated baseline output; original mode preserved.

The corpus checks this regression and previous examples, not perfect recognition
for arbitrary future speech.

## Audio-device display

Meeting microphone/output pickers mark the system default, preserve an explicitly
selected different device, and reveal truncated selected descriptions. Refreshing
devices updates the default marker without changing the selection.

`./kde-whisper/scripts/podman-test.sh -R 'MeetingDialogTest|TrayAppTest|TrayControllerTest'`:
3/3 suites passed. Actual offscreen renders with fonts at 400×460 and 600×660 were
inspected, including long device descriptions.

## Concurrent recording

`KWISPR_MEETING_AUDIO_SMOKE=1 python3 -m unittest tests.test_meeting_dictation_coexistence -v`:
passed twice (7.886 and 7.969 seconds) using real Pulse/ffmpeg and separate state
directories. Only synthetic tones entered disposable audio devices; transcription
and clipboard were stubbed. Meeting tracks each contained 5.10 seconds; concurrent
dictation contained 2.55 seconds. Stopping dictation left meeting capture running,
and another dictation completed while the meeting was marked processing.
Host audio defaults were unchanged and temporary devices were removed.

The capture paths coexist, but both receive speech from the shared microphone.
The common STT mutex serializes inference: this test does not claim simultaneous
model computation, bounded waiting time or scheduling priority.
