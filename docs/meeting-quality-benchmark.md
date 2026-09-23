# Evaluating one-to-one meeting transcription

The current evaluation scope is two real one-to-one calls, approximately 72 and
109 minutes long. The local microphone is `self`; the call-output track contains
one remote participant. Group-call diarization is a separate follow-up.

Empty transcript counts do not measure recognition accuracy. A nonempty response
can omit quiet words, replace technical terms, repeat a phrase or invent speech
over noise. Likewise, a wider audio crop can return neighboring words without
recovering the missing target. The private benchmark therefore includes ordinary
nonempty speech, Russian/English technical vocabulary, quiet/empty candidates and
short-turn or phrase-end cases from both tracks of both calls.

Each prepared WAV has exact source frame bounds and SHA-256, plus a fresh local
result obtained on those same bytes. Audio is not amplified and the local VAD
configuration is unchanged. The enriched diagnostic sample is not an unbiased
estimate of the complete calls' error rate.

## Optional OpenRouter comparison

`tools/benchmark_meeting_audio.py` is a development tool separate from the meeting
recorder. It sends nothing unless `--execute` is supplied. Use a private manifest
outside the repository, containing at most 40 WAV clips of at most 30 seconds each
(900 seconds in total). Private audio, keys and transcript results must remain
outside source control.

The key file accepts a raw key for compatibility, or:

```text
OPENROUTER_API_KEY=your-key
MODELS:
provider/model-id
another-provider/model-id
```

The CLI requires exact model IDs. Friendly model names can be resolved against
the current public model catalog before running. Check that each selected model
accepts audio and use the corresponding mode: `transcription` for STT models,
`chat` for audio-input chat models. Models from different modes require separate
invocations. Language is not forced for mixed Russian/English speech.

```bash
python3 tools/benchmark_meeting_audio.py \
  --manifest /path/to/private/manifest.json \
  --key-file ~/.config/kwispr/openrouter-benchmark.key \
  --mode transcription
```

Review the dry-run summary before adding `--execute`. The cost limit is a soft
stop between requests, based on reported actual usage for the selected clips and
models. A request can cross that limit; an account/key limit is required for a
hard spending cap. Add costs across separate runs and request variants when
tracking an overall experiment budget. Redirects and automatic retries are
disabled. Successful requests are cached by mode, model, clip identity, audio
hash, prompt variant and nondefault chat settings. A durable attempt marker
blocks resending a request whose outcome is unknown. Inspect billing and resolve
that attempt explicitly before retrying. Adding a reviewed reference later
refreshes evaluation of a cached response without another paid request.

For reasoning chat models, the output limit can include internal reasoning.
Check `finish_reason` and usage in the pilot before running the whole sample.
For example, Gemini 3.8 Flash completed the real pilot with
`--max-output-tokens 4096 --reasoning-effort minimal`, while its default reasoning
exhausted the initial 1024-token limit. These options affect chat requests only;
they do not alter the recorder or local recognizer.

## Interpreting results

Cloud-model agreement is diagnostic evidence, not a verified reference. WER/CER
are produced only for a string reference explicitly marked
`reference_reviewed: true`. The normalization ignores case, whitespace and
punctuation; these metrics measure lexical transcription, not punctuation style.
Reviewed silence is assessed for false-positive text without division by zero.
Incomplete chat responses are retained and marked, not scored as normal answers.

Until references have been checked against the audio, report candidate issues
and disagreements, not an overall percentage of correctly recognized words.
