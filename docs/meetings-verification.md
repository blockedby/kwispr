# Meeting mode verification — 2026-09-21

Implementation baseline: `0c41f6ccb0485c2d52f33e6868f1598771d92335`, branch
`codex/meeting-transcripts`, built on top of the installed dictation-quality changes.

## Automated checks

- `python3 -m unittest discover`: 92 tests, OK, 1 optional real-audio test skipped.
- `KWISPR_MEETING_AUDIO_SMOKE=1 python3 -m unittest tests.test_meeting_sessions.PulseCaptureSmokeTests -v`:
  1 passed; real PulseAudio/ffmpeg recording from a disposable null sink. Both
  tracks contained the tone; onset difference 26.438 ms. Audio defaults unchanged.
- `./kde-whisper/scripts/podman-test.sh`: 14/14 CTest suites passed, including
  meeting window, normal dictation settings/tray, installer and metadata checks.
- `git diff --check`: clean before implementation commit.

Behavioral regressions cover concurrent Start, dead worker/recorder cleanup,
failed processing and retry, existing folder permissions, API credentials and
literal vocabulary, local-only endpoint/redirect handling, config-only runtime
override, overlapping voices, and hidden active-window status polling.

The Qt window was rendered offscreen with actual fonts at 600×660, 400×460 and
460×420. Narrow layouts scroll; recording actions remain accessible.

## Real models and local server

Ran the pinned setup script from a fresh user runtime directory. It created an
isolated environment, downloaded and checked both model SHA-256 digests, and
successfully imported sherpa-onnx/numpy. An existing-runtime rerun also passed.

Public fixture:
[sherpa-onnx four-speaker sample](https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/0-four-speakers-zh.wav),
56.861 seconds. SHA-256:
`bedf036caed208386c67b4ef4b11f83d74dd0d420b102163a1c33cd09cde7010`.

- Automatic clustering at threshold 0.9 returned 4 remote voices, with returning
  speakers retaining their labels. CPU diarization alone took about 1.6 seconds.
- Actual local Whisper processed the turn crops and wrote Markdown and JSON with
  times, all four voices and a separate microphone track.
- The fixture is Chinese: verification used `KWISPR_LANGUAGE=zh` and cleared the
  Russian punctuation prompt for that process only. Reusing the installed Russian
  prompt with language Auto produced incorrect language guesses on short Chinese
  turns. User configuration was preserved; language/prompt suitability is documented.

## Installed application acceptance

Reinstalled the verified Qt build and meeting scripts; local STT binary and
dictation configuration were preserved. The running `/proc/<pid>/exe` hashes
matched installed artifacts, and STT `/health` returned OK.

- Qt SHA-256: `c8a8b4b6ff25d608929578b41a4abc7e9e20ecbecf1395a409e69cd53ffd14af`.
- Local STT SHA-256: `3b753b018897176083dfdc30065980380939fd1b1f1852c0e72f0b603187488d`.
- Backup: `~/.local/state/kwispr/backups/20260921-230114-meeting-transcripts`.

Tested the installed `~/.local/bin/kwispr meetings` wrapper through
Start → real capture → Stop → diarization → local STT → complete. Two disposable
null sinks and one remapped test source supplied the first 18 seconds of the
public fixture plus 7 seconds of separate microphone audio. This produced
three remote voices and `self`, five timed utterances, and both output formats.
Track lengths were 19.100/19.102 seconds including startup/tail. Host audio defaults
were unchanged; test devices were removed. No real microphone was recorded.

These checks prove the installed file workflow and fixture behavior. They do not
establish diarization accuracy for an actual noisy call, overlapping speech or
voices replayed through speakers into the microphone.
