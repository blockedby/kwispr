# Meeting language recovery — 2026-09-22

## Confirmed cause and change

A local 196-second recording had English remote audio and Russian microphone
speech with technical terms. Dictation had a Russian punctuation example and
automatic language selection. Applying that example independently to every
remote turn produced Cyrillic transliteration, repetitions, and fragments in
unrelated scripts.

Replayed the same 19.019-second remote crop against the existing local server:

- Auto plus the dictation punctuation example reproduced the bad output.
- Auto without the example returned coherent English.
- Explicit English without the example returned the same English text.
- A separate 30-second crop without the example also returned English.

Four short ranges were compared with Auto versus explicit English, with the
example removed: three returned the same short English utterances, and a
67-millisecond overlap returned empty text in both modes. No short-speech filter
was added and no original audio or utterance was deleted.

Meetings now share the model and vocabulary, but not dictation's language or
example prose. Microphone and remote languages have independent optional
settings. On a server returning optional `language` metadata, Auto turns lasting
at least four seconds continue to detect their own language. Shorter turns use
the last such language on the same track. If the first turn is short, an available
10–20-second speech turn supplies an unprompted language probe; its text is never
inserted into the transcript. Without suitable audio or metadata, Auto remains
Auto. This heuristic can still miss a short language switch; it does not claim
perfect bilingual recognition or spelling.

Pipeline version 2 invalidates old checkpoints. New checkpoints retain language
metadata, allowing a retry to restore the context of already completed turns.
`transcript.json` records requested/reported languages and each turn's mode.

## Actual audio integrity

Inspected copies; the original session and its outputs were not modified.

| Check | Microphone | Remote |
|---|---:|---:|
| Format | Mono, 16 kHz, PCM16 | Mono, 16 kHz, PCM16 |
| Duration | 196.100 s | 196.153875 s |
| Declared/read PCM bytes | 6,275,200 / 6,275,200 | 6,276,924 / 6,276,924 |
| Peak | -9.58 dBFS | 0 dBFS |
| Full-scale samples | 0 | 10 of 3,138,462 |
| Digital-zero runs at least 50 ms | None | See below |

The remote zero ranges were 0–0.48, 64.37–155.78, 183.02–183.27, and
189.04–196.15 seconds. The user confirmed that video playback was paused during
the long middle interval. Without the source video, the 250-millisecond interval
at 183 seconds cannot be classified conclusively as source silence or a dropout.
There was no widespread clipping, malformed/truncated WAV body, or capture error
in `worker.log`. The 53.9-millisecond difference between track lengths and roughly
0.24–0.30-second difference from manifest wall time do not prove sample-perfect
synchronization or word-for-word capture completeness.

## Full local recovery

Used a separate local server on port 19551, built from optional language metadata
commit `350b885`, without restarting the installed server. Binary SHA-256:
`71bbd78d24aad65590ad0c9ea951df8b98492c8c7eabe5ae26e30ffa3c546acc`.

The complete copied session processed in 23.08 seconds with Auto languages and
the original Auto speaker setting. It produced 34 timed rows: 16 remote and 18
microphone. Reported remote languages were English only; microphone languages
were Russian only. English remote content was no longer rendered as mixed
Cyrillic/Korean fragments. Technical names in the microphone transcript remain
imperfect; no word-level accuracy score is claimed without a reference transcript.

Auto diarization still returned three labels. The third accounted for only
0.3206 seconds; a separate forced-two-speaker diarization comparison removed it.
The language repair does not claim to fix this speaker-count error or establish
ground-truth speaker attribution. Known participant counts remain useful.

## Regression checks

`python3 -m unittest tests.test_meeting_config tests.test_meeting_pipeline -v`:
17 tests passed. `python3 -m unittest discover`: 99 tests passed, with two optional
real-audio capture tests skipped; `git diff --check` was clean. Coverage includes independent languages, no inherited dictation
prompt/language, literal vocabulary, optional metadata, long-turn language
switches, short-turn context, unprompted probes, old-server fallback, checkpoint
retry context, and fingerprint invalidation. Mixed Russian/English text is kept
as returned; there is no translation or output-script filter.

The audio, transcripts and probe outputs used for this verification are private
local artifacts and are not included in the repository.
