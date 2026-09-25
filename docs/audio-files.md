# Audio file transcription

`kwispr-files.py` imports one existing audio file and writes plain text. It uses the configured local Whisper HTTP service, with Russian and English language detection. It does not separate speakers, access the clipboard, delete the source, or use a cloud fallback.

```bash
python3 kwispr-files.py transcribe '/path/to/voice message.ogg'
python3 kwispr-files.py transcribe '/path/to/voice message.ogg' --output-dir '/path/to/results'
```

The default result root is `~/Documents/Kwispr/Transcriptions`. Each source and transcription setting set gets its own result directory, containing `transcript.txt`, `transcript.json`, a converted `audio.wav`, and `processing-checkpoint.json`. A failed job keeps completed chunks; run the same command again to resume. The source file is only read. Supported input formats depend on installed `ffmpeg`, including OGG/Opus, MP3, and WAV.

Set `KWISPR_CONFIG_FILE` to a config file with general `KWISPR_BACKEND=openai-transcriptions`, a loopback `KWISPR_API_URL`, and a `KWISPR_MODEL` beginning with `whisper`. Meeting-specific backend/model overrides are ignored for imports. The local server must advertise `whisper_allowed_languages` in `/health` so only `ru,en` are considered. If your general dictation settings use a cloud backend, supply a separate local config file for this command. No diarization runtime is needed.

The command prints one JSON object per line. Progress rows have `event`, `message`, `completed`, and `total`. The final row has `event: result`, `state: complete`, `source_path`, `text`, `transcript_path`, `transcript_json`, and `output_dir`. Failures print a message to stderr, an `event: error` row to stdout, and exit nonzero. A graphical caller can run one file per process and queue multiple files itself.

Files up to five minutes are sent in one request to preserve sentence context. Longer files use chunks of at most five minutes. The importer seeks a quiet 120 ms stretch near a boundary; if none exists, it keeps a two-second overlap so words at the cut can be heard on both sides. Transcript text preserves the model output from each chunk, so words may appear twice at a forced boundary. `transcript.json` preserves chunk timing and language metadata for review.
