# Dictation quality and personal vocabulary

Open **Kwispr Settings → Dictation** to edit your personal vocabulary and recording preferences. The application and `kwispr.sh` use the same `~/.config/kwispr/config.env` file. Click **Apply** to save; changes take effect on the next dictation.

## Personal vocabulary

Enter one spelling per line: product names, people, project names, or technical terms. The settings editor stores the list as comma-separated `KWISPR_VOCABULARY`. Add the exact spelling you want to see, for example `Kwispr` or `OpenRouter`.

These are recognition hints, not unconditional replacements. A word should appear only when the model hears it. Keep the list short and relevant: Whisper retains approximately 223 initial context tokens; a very long list can crowd out the punctuation sample. The API validation limit is 4096 Unicode characters for the combined sample and vocabulary, but that is not a promise that the model can attend to the entire list.

Vocabulary is supported for local Whisper, compatible transcription endpoints that accept `prompt`, and OpenRouter audio chat. Other local model families retain the saved settings but do not receive Whisper hints. The CLI recognizes configured local endpoints and legacy loopback endpoints; the local-only audio-tail field is omitted when switching to cloud backends. Update the local server as well as the application; older servers ignore prompt fields.

## Punctuation

Use the punctuation sample button, or write a short example in the language you dictate. A Russian example is:

> Привет! Да, всё хорошо. Давай обсудим эту задачу: сначала проверим код, потом запустим тесты. Что нужно исправить?

`KWISPR_WHISPER_PROMPT` provides transcript-style context for Whisper. It is independent of `KWISPR_TRANSCRIPTION_PROMPT`, which remains the instruction field for OpenRouter. The application normalizes the sample to a single configuration line.

The local runtime passes this context into the Whisper decoder. With a hint present, successive audio segments retain previous text context so punctuation does not lose its example after the first segment. Without hints, the original decoder behavior remains unchanged. This improves recognition guidance; it is not a separate proofreading or rewriting model, and punctuation is still model-dependent.

## End of recording

- **Stop delay** continues microphone capture briefly after you press Stop; `350` ms is a useful starting point. The allowed range is `0–2000` ms, and the default is `0`.
- **Preserve audio ending** tells the local server to keep everything from the detected speech start to the end of the recorded file. VAD still rejects silence. It cannot recover speech that was never recorded.
- `KWISPR_VAD_PADDING_MS` controls ordinary VAD padding when ending preservation is off. Increasing it has no effect if VAD already keeps the complete ending.

The stop sound plays after capture has stopped. Cloud transcription services do not use the local `preserve_audio_tail` extension.

## Configuration example

```bash
KWISPR_WHISPER_PROMPT='Привет! Да, всё хорошо. Что нужно исправить?'
KWISPR_VOCABULARY='Kwispr, Codex, OpenRouter'
KWISPR_STOP_DELAY_MS=350
KWISPR_PRESERVE_AUDIO_TAIL=1
```

All new features are opt-in. An empty sample and vocabulary, zero delay, and disabled ending preservation retain the original request behavior.

## Local API

`POST /v1/audio/transcriptions` accepts optional multipart fields:

- `prompt`: up to 4096 Unicode characters, without NUL; supported only for models with architecture `whisper`.
- `preserve_audio_tail`: `1`, `0`, `true`, or `false`; defaults to false.

`GET /health` advertises `capabilities.whisper_prompt` and `capabilities.preserve_audio_tail`. Neither feature requires uploading local audio to a cloud service.
