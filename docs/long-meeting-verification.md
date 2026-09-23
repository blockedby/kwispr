# Long meeting recovery verification — 2026-09-23

Three real local recordings (approximately 62, 72 and 109 minutes) exposed two
separate problems: short native overlap boundaries fragmented acoustic phrases,
and native Auto clustering produced many tiny speaker identities. The original
audio, transcripts and metadata were backed up and left unchanged; recovery runs
used separate copied sessions. Private audio and transcripts are not committed.

All six WAV tracks decoded completely with ffmpeg. RIFF lengths, PCM sizes and
sample counts were consistent. This establishes file integrity, not that every
spoken word was captured. In the first recording, the wall-clock start/stop span
exceeds the audio length by about 10.8 seconds; start metadata precedes capture
startup, but existing logs cannot conclusively localize this difference.

The new known-count refinement trained identities on clean 2–5 second windows.
With two requested remote speakers, the first recording supported two separated
profiles; the other two supported only one common profile and emitted warnings.
Uncertain intervals remain in the transcript as unknown. Tests and real replays
preserved the exact native speech coverage. Profile geometry is not ground-truth
person identification, and Auto count selection remains unchanged.

Acoustic groups must be planned before refined identity annotations are applied.
An initial integration applied refinement first and lost the grouping benefit;
pipeline version 5 fixes that order. Actual requests for two problematic phrases
were byte-identical to the successful grouped control clips and returned the
same coherent text. A full recovered recording also preserved both tracks'
speech-time coverage exactly, with unknown excluded from the speaker count.

Quiet speech remains a separate limitation. Controlled amplification recovered
two plausible microphone phrases but also produced invented closing phrases on
other quiet inputs. Disabling energy VAD produced similar false positives. No
global VAD weakening, automatic gain or silent insertion of uncertain words was
enabled; those two candidates remain separate for listening review.

Verification: `python3 -m unittest discover` passed 132 tests with two optional
audio-environment tests skipped. `./kde-whisper/scripts/podman-test.sh` passed all
14 suites, including retry speaker-count UI behavior. Independent review checked
source/time metadata, cache invalidation, retry validation and installer coverage.
