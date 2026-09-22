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

For Auto with Russian and English candidates, the sample button inserts a bilingual example:

> Привет! Давай проверим pull request: сначала code review, потом тесты. Looks good! What should we fix?

The button applies the sample only when clicked. Changing the candidate list does not replace a custom sample. A monolingual example can bias speech in another language; use a sample that matches the languages you actually speak.

The local runtime passes the same user-provided context into every Whisper audio segment, without carrying generated text from earlier segments. This keeps the punctuation example and vocabulary available on long recordings while avoiding repetition loops caused by generated-history conditioning. Without hints, the original decoder behavior remains unchanged. This improves recognition guidance; it is not a separate proofreading or rewriting model, and punctuation is still model-dependent.

## Russian and English in the same recording

Keep **Language** on **Auto** and choose **Russian + English (ru,en)** in the auto-detection candidates setting. This saves `KWISPR_WHISPER_ALLOWED_LANGUAGES=ru,en`. An empty list keeps all languages available. The setting applies to local Whisper dictation and meeting recognition; update the local server together with the application.

The native detector chooses its language token from the permitted candidates. It still transcribes with the complete multilingual vocabulary, without translating or deleting words from the result. An explicit language choice overrides automatic selection. Whisper currently selects its language token from the first audio window in a request; restricting the candidates prevents selection of an unrelated language but does not guarantee perfect code switching or the spelling of English names inside Russian speech. Put preferred spellings such as `React`, `Vue`, or `Svelte` in the personal vocabulary when those are words you use.

Meeting recognition has separate language choices for the microphone and other speakers. It uses personal vocabulary but does not inherit the dictation punctuation sample or its explicit language choice.

## End of recording

- **Stop delay** continues microphone capture briefly after you press Stop; `350` ms is a useful starting point. The allowed range is `0–2000` ms, and the default is `0`.
- **Preserve audio ending** tells the local server to keep everything from the detected speech start to the end of the recorded file. VAD still rejects silence. It cannot recover speech that was never recorded.
- `KWISPR_VAD_PADDING_MS` controls ordinary VAD padding when ending preservation is off. Increasing it has no effect if VAD already keeps the complete ending.

The stop sound plays after capture has stopped. Cloud transcription services do not use the local `preserve_audio_tail` extension.

## Configuration example

```bash
KWISPR_WHISPER_PROMPT='Привет! Да, всё хорошо. Что нужно исправить?'
KWISPR_VOCABULARY='Kwispr, Codex, OpenRouter'
KWISPR_WHISPER_ALLOWED_LANGUAGES=ru,en
KWISPR_STOP_DELAY_MS=350
KWISPR_PRESERVE_AUDIO_TAIL=1
```

All new features are opt-in. An empty sample and vocabulary, zero delay, and disabled ending preservation retain the original request behavior.

Recognized text is preserved without keyword-based cleanup. Mentioning subtitles, credits, music, or closing phrases must not remove that speech or the sentences after it.

## Local API

`POST /v1/audio/transcriptions` accepts optional multipart fields:

- `prompt`: up to 4096 Unicode characters, without NUL; supported only for models with architecture `whisper`.
- `preserve_audio_tail`: `1`, `0`, `true`, or `false`; defaults to false.
- `allowed_languages`: optional comma-separated language codes for Whisper auto-detection, for example `ru,en`. Omit for unrestricted detection. An explicit `language` takes precedence; invalid codes are rejected.

`GET /health` advertises `capabilities.whisper_prompt`, `capabilities.preserve_audio_tail`, and `capabilities.whisper_allowed_languages`. These features do not require uploading local audio to a cloud service.
