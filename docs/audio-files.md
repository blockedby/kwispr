# Audio and video file transcription

Open **Audio & video files…** from the tray. The default **Result** is **Speakers and timestamps**. Choose **Processing** and **Audio track**, then click **Add audio or video files…**. Select one or more saved Telegram voice messages, audio files, or videos; they enter the queue immediately. Results are saved automatically under `~/Documents/Kwispr/Transcriptions`. Select a completed row to read the text, **Copy text**, or **Open saved folder**. Closing the window leaves processing running. Meeting recording and ordinary dictation remain available.

**Same as Meetings** uses the saved meeting processing provider. **Local STT** and **OpenRouter / Gemini** explicitly override it for files. OpenRouter shares the model, API key and budget configured in Meetings; the budget applies separately to each file. The tab displays the cloud model before adding files and the actual cost after completion. Processing and track selections are remembered for the next visit, and queued files retain the provider, cloud model, budget and track selected when added. These choices do not change dictation or meeting settings.

For OBS recordings with a mixed track 1, microphone track 2 and call track 3, select **Audio track: 3**, **Separate microphone track: 2** and the number of **Other speakers**. Without a separate microphone track, the speaker count includes everyone. Zero means automatic detection. Speaker mode uses the existing durable Meetings queue and saves the same Markdown/JSON exports with speaker labels and timestamps. **Plain text** hides the speaker controls and keeps the lightweight file transcription path. Speaker mode requires the meeting runtime and models; setup is available on the Meetings tab.

The installed command is `kwispr files transcribe '/path/to/voice message.ogg'`.

```bash
python3 kwispr-files.py transcribe '/path/to/voice message.ogg'
python3 kwispr-files.py transcribe '/path/to/recording.mkv' --provider openrouter --speaker-labels --audio-stream 2 --microphone-stream 1 --speakers 2
python3 kwispr-files.py transcribe '/path/to/recording.mkv' --provider local --audio-stream 2 --output-dir '/path/to/results'
```

The CLI defaults to `--provider meetings`. `--audio-stream` is zero-based, while the graphical selector starts at 1. `--cloud-model` and `--cloud-budget` override the shared cloud settings for one invocation. A missing selected audio track reports an error. Video is ignored and the source file is only read. Supported input formats depend on installed `ffmpeg`, including OGG/Opus, MP3, WAV, MP4 and MKV. The CLI retains plain text by default; add `--speaker-labels` for diarization and timestamps. `--microphone-stream` is also zero-based and `--speakers 0` selects automatic detection. No diarization runtime is needed for plain file transcription.

Set `KWISPR_CONFIG_FILE` to use a different configuration file. An explicitly configured `KWISPR_MEETING_PROVIDER` and meeting backend/model overrides are shared by default. Without a meeting provider setting, the CLI retains the original general local STT settings. Local recognition requires a loopback endpoint, or an explicitly configured private LAN endpoint; it supports Whisper and other compatible local STT models. For Whisper, the server must advertise `whisper_allowed_languages` in `/health` to enforce Russian/English detection. OpenRouter requires explicit selection in Meetings or via `--provider openrouter`; general dictation cloud settings alone do not authorize uploads.

Plain-text jobs use a separate result directory for each source, backend, model and selected track, containing `transcript.txt`, `transcript.json`, a converted `audio.wav` and `processing-checkpoint.json`. Failed jobs retain completed work. OpenRouter also saves private request receipts and actual costs under `openrouter/`. Rerunning the same command reuses completed responses without billing them again. A soft budget stops new requests; in-flight requests may exceed it. Known incomplete responses can be retried explicitly; an unknown network outcome retains an attempt marker and blocks new requests until inspected.

Speaker jobs retain a `meeting-job.json` pointer to their durable session. Re-adding the same recording with the same speaker/track choices reuses that job; a failed job can be explicitly retried without redoing completed cloud windows. The full labelled Markdown is displayed and copied in the file tab.

The command prints one JSON object per line. Progress rows have `event`, `message`, `completed` and `total`. The final row has `event: result`, `state: complete`, `source_path`, `text`, `transcript_path`, `transcript_json`, `output_dir`, `processing_provider` and `cloud_cost_usd` (null for local STT). Failures print a message to stderr, an `event: error` row to stdout, and exit nonzero.

Local files up to five minutes are sent in one request. Longer files use chunks of at most five minutes, seeking a quiet 120 ms stretch near a boundary; otherwise a two-second overlap preserves words around the cut. Exact model text is retained, so words may repeat at forced boundaries. OpenRouter uses consecutive context windows of at most 45 seconds. `transcript.json` preserves timing and backend metadata for review.
