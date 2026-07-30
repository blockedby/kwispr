#!/usr/bin/env bash
# Voice dictation: toggle recording, transcribe via OpenAI Whisper, put in clipboard
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
XDG_CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}"
CACHE_DIR="$XDG_CACHE_HOME/kwispr"
PID_FILE="$CACHE_DIR/current.pid"
WAV_POINTER="$CACHE_DIR/current.path"
FIFO_PATH="$CACHE_DIR/ffmpeg.fifo"
NOTIFY_ID_FILE="$CACHE_DIR/notify.id"
LAST_FAILED="$CACHE_DIR/last-failed.txt"
TOGGLE_LOCK="$CACHE_DIR/toggle.lock"
CONFIG_FILE="${KWISPR_CONFIG_FILE:-$XDG_CONFIG_HOME/kwispr/config.env}"
LEGACY_ENV_FILE="$SCRIPT_DIR/.env"
ACTIVE_CONFIG_FILE="$CONFIG_FILE"

# Minimum wav size to attempt transcription (bytes)
# 16kHz * 16bit * 1ch = 32000 B/s; 32 KB ≈ 1s of audio
# Whisper produces garbage / subtitle hallucinations on <1s audio.
MIN_WAV_BYTES=32768

# Default sound cues — generated tones in sounds/ (start=high pips,
# stop=low pups, ready=ascending ding-dong). Override in the UI or config.
: "${KWISPR_SOUND_START:=$SCRIPT_DIR/sounds/start.wav}"
: "${KWISPR_SOUND_STOP:=$SCRIPT_DIR/sounds/stop.wav}"
: "${KWISPR_SOUND_READY:=$SCRIPT_DIR/sounds/ready.wav}"

mkdir -p "$CACHE_DIR"

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

notify() {
  local title="$1" body="${2:-}"
  notify-send -a "kwispr" -t 5000 "$title" "$body" 2>/dev/null || true
}

# Persistent status bubble that survives across script runs.
# Holds one notification ID in $NOTIFY_ID_FILE and keeps replacing it so the
# user sees a single plate transforming through stages instead of stacked toasts.
status() {
  local title="$1" timeout="${2:-0}"  # 0 = no expiry
  local prev_id id
  prev_id="$(cat "$NOTIFY_ID_FILE" 2>/dev/null || echo 0)"
  local args=(-a "kwispr" -t "$timeout" -p "$title")
  if [[ "$prev_id" -gt 0 ]]; then
    args+=(-r "$prev_id")
  fi
  id="$(notify-send "${args[@]}" 2>/dev/null || echo 0)"
  if [[ "$id" -gt 0 ]]; then
    echo "$id" > "$NOTIFY_ID_FILE"
  fi
}

status_clear() {
  rm -f "$NOTIFY_ID_FILE"
}

# Play a short sound cue asynchronously (doesn't block script).
# Silent fallback if paplay/file missing or sounds disabled.
play_cue() {
  [[ "${KWISPR_SOUNDS:-1}" == "1" ]] || return 0
  local file="$1"
  [[ -f "$file" ]] || return 0
  command -v paplay >/dev/null 2>&1 || return 0
  paplay "$file" >/dev/null 2>&1 &
  disown
}

copy_text() {
  if command -v wl-copy >/dev/null 2>&1; then
    wl-copy
  elif command -v qdbus6 >/dev/null 2>&1 && qdbus6 org.kde.klipper /klipper >/dev/null 2>&1; then
    local text
    text="$(cat)"
    qdbus6 org.kde.klipper /klipper org.kde.klipper.klipper.setClipboardContents "$text" >/dev/null
  else
    return 1
  fi
}

die() {
  notify "❌ kwispr error" "$1"
  echo "ERROR: $1" >&2
  exit 1
}

load_env() {
  # XDG config is authoritative. A repository-local .env remains a read-only
  # compatibility fallback so existing checkouts continue to work while the
  # installer/UI migrate settings out of the source tree.
  ACTIVE_CONFIG_FILE="$CONFIG_FILE"
  if [[ ! -f "$ACTIVE_CONFIG_FILE" && -f "$LEGACY_ENV_FILE" ]]; then
    ACTIVE_CONFIG_FILE="$LEGACY_ENV_FILE"
  fi

  if [[ -f "$ACTIVE_CONFIG_FILE" ]]; then
    set -a
    # This file is created mode 0600 by Kwispr's settings UI/installer.
    # shellcheck disable=SC1090
    source "$ACTIVE_CONFIG_FILE"
    set +a
  fi

  : "${KWISPR_BACKEND:=openai-transcriptions}"
  : "${KWISPR_API_URL:=https://api.openai.com/v1/audio/transcriptions}"
  : "${KWISPR_MODEL:=whisper-1}"
  : "${KWISPR_AUDIO_FORMAT:=wav}"
  : "${KWISPR_PULSE_SOURCE:=default}"
  : "${KWISPR_TRANSCRIPTION_PROMPT:=Transcribe this audio exactly as spoken. The speech may be Russian, English, or mixed. Do not translate. Return only the transcript.}"

  # Backward compatibility: keep OPENAI_API_KEY working, but allow custom
  # OpenAI-compatible backends (OpenRouter, LocalAI, local Whisper servers)
  # to use KWISPR_API_KEY instead.
  if [[ -z "${KWISPR_API_KEY:-}" && -n "${OPENAI_API_KEY:-}" ]]; then
    KWISPR_API_KEY="$OPENAI_API_KEY"
  fi

  if [[ "$KWISPR_API_URL" == "https://api.openai.com/"* ]]; then
    [[ -n "${KWISPR_API_KEY:-}" && "$KWISPR_API_KEY" != "sk-REPLACE_ME" ]] \
      || die "Open Kwispr Settings and configure an API key (config: $CONFIG_FILE)"
  fi
}

rotate_cache() {
  find "$CACHE_DIR" -maxdepth 1 \( -name '*.wav' -o -name '*.txt' \) -mtime +30 -delete 2>/dev/null || true
}

# -----------------------------------------------------------------------------
# Recording control
# -----------------------------------------------------------------------------

is_recording() {
  [[ -f "$PID_FILE" ]] && kill -0 "$(sed -n 1p "$PID_FILE")" 2>/dev/null
}

start_recording() {
  local ts wav
  ts="$(date +%Y-%m-%d_%H-%M-%S)"
  wav="$CACHE_DIR/$ts.wav"
  echo "$wav" > "$WAV_POINTER"

  # ffmpeg needs writable stdin to accept 'q' for graceful shutdown — that's
  # the only reliable way to make it flush buffers and close the WAV trailer.
  # SIGTERM/SIGINT can leave the WAV file empty (no trailer). A FIFO keeps
  # ffmpeg's stdin open across our script's exit.
  rm -f "$FIFO_PATH"
  mkfifo "$FIFO_PATH"

  # Open FIFO for writing in the background and keep it open by holding
  # a sleep on the write side. When we want to stop, we'll write 'q' to it
  # and then kill the holder so the FIFO closes.
  ( exec 3>"$FIFO_PATH"; sleep 86400 ) &
  local holder=$!
  # Small delay to ensure the writer is open before ffmpeg reads
  sleep 0.05

  ffmpeg -hide_banner -loglevel error \
    -f pulse -i "$KWISPR_PULSE_SOURCE" \
    -ar 16000 -ac 1 \
    "$wav" < "$FIFO_PATH" &
  local ffpid=$!

  printf '%s\n%s\n' "$ffpid" "$holder" > "$PID_FILE"

  play_cue "$KWISPR_SOUND_START"
  status "🎙 Listening"
}

stop_recording() {
  local ffpid holder wav
  ffpid="$(sed -n 1p "$PID_FILE")"
  holder="$(sed -n 2p "$PID_FILE")"
  wav="$(cat "$WAV_POINTER")"

  play_cue "$KWISPR_SOUND_STOP"
  status "⏳ Processing"

  # Send 'q' to ffmpeg via the FIFO — this triggers its graceful shutdown
  # path which flushes the WAV header + data. Then kill the FIFO holder
  # so ffmpeg sees stdin EOF and exits.
  printf 'q' > "$FIFO_PATH" 2>/dev/null || true
  kill "$holder" 2>/dev/null || true

  # Wait up to 5s for ffmpeg to finish writing
  local waited=0
  while kill -0 "$ffpid" 2>/dev/null && [[ $waited -lt 25 ]]; do
    sleep 0.2
    waited=$((waited + 1))
  done

  # Still alive after 5s — force kill (rare)
  if kill -0 "$ffpid" 2>/dev/null; then
    kill -KILL "$ffpid" 2>/dev/null || true
    sleep 0.3
  fi

  rm -f "$PID_FILE" "$WAV_POINTER" "$FIFO_PATH"
  echo "$wav"
}

# -----------------------------------------------------------------------------
# Transcription
# -----------------------------------------------------------------------------

transcribe() {
  local wav="$1"
  local txt="${wav%.wav}.txt"

  # Short recordings produce empty/garbage transcripts and waste an API call.
  # Also the wav may be zero bytes if ffmpeg was killed before writing header.
  local size
  size="$(stat -c%s "$wav" 2>/dev/null || echo 0)"
  if [[ "$size" -lt "$MIN_WAV_BYTES" ]]; then
    rm -f "$wav"
    status "⚠ Too short" 3000
    status_clear
    return 1
  fi

  local http_code response curl_args
  response="$(mktemp)"

  curl_args=(
    -sS -w '%{http_code}' -o "$response"
    --max-time 120
    "$KWISPR_API_URL"
  )
  # Add auth only when configured. Local OpenAI-compatible servers often do
  # not require a bearer token.
  if [[ -n "${KWISPR_API_KEY:-}" && "$KWISPR_API_KEY" != "sk-REPLACE_ME" ]]; then
    curl_args+=(-H "Authorization: Bearer $KWISPR_API_KEY")
  fi
  # Optional OpenRouter leaderboard/app headers.
  if [[ -n "${KWISPR_HTTP_REFERER:-}" ]]; then
    curl_args+=(-H "HTTP-Referer: $KWISPR_HTTP_REFERER")
  fi
  if [[ -n "${KWISPR_APP_TITLE:-}" ]]; then
    curl_args+=(-H "X-Title: $KWISPR_APP_TITLE")
  fi

  case "$KWISPR_BACKEND" in
    openai-transcriptions)
      # No prompt: a bilingual prompt was causing Whisper to *translate* speech
      # into the language of the prompt instead of transcribing as-is. Subtitle
      # hallucinations are scrubbed by the post-processing regex below.
      # temperature=0 for deterministic output.
      curl_args+=(
        -F "model=$KWISPR_MODEL"
        -F response_format=json
        -F temperature=0
        -F file=@"$wav"
      )
      # Optional: force language if KWISPR_LANGUAGE is configured.
      if [[ -n "${KWISPR_LANGUAGE:-}" ]]; then
        curl_args+=(-F "language=$KWISPR_LANGUAGE")
      fi
      ;;
    openrouter-chat)
      local request_json audio_b64
      request_json="$(mktemp)"
      audio_b64="$(mktemp)"
      base64 -w0 "$wav" > "$audio_b64"
      jq -n \
        --arg model "$KWISPR_MODEL" \
        --arg prompt "$KWISPR_TRANSCRIPTION_PROMPT" \
        --arg format "$KWISPR_AUDIO_FORMAT" \
        --rawfile audio "$audio_b64" \
        '{model:$model,messages:[{role:"user",content:[{type:"text",text:$prompt},{type:"input_audio",input_audio:{data:$audio,format:$format}}]}]}' \
        > "$request_json"
      rm -f "$audio_b64"
      curl_args+=(
        -H "Content-Type: application/json"
        --data-binary "@$request_json"
      )
      ;;
    *)
      rm -f "$response"
      die "Unknown KWISPR_BACKEND: $KWISPR_BACKEND"
      ;;
  esac

  http_code="$(curl "${curl_args[@]}" || echo "000")"
  [[ -n "${request_json:-}" ]] && rm -f "$request_json"

  if [[ "$http_code" != "200" ]]; then
    local retry_cmd="$SCRIPT_DIR/kwispr.sh retry \"$wav\""
    printf '%s\n' "$wav" > "$LAST_FAILED"
    echo -n "$retry_cmd" | copy_text || true
    local msg
    msg="$(cat "$response" 2>/dev/null || echo '')"
    rm -f "$response"
    status "❌ API $http_code" 5000
    status_clear
    echo "HTTP $http_code: $msg" >&2
    return 1
  fi

  local text
  case "$KWISPR_BACKEND" in
    openai-transcriptions) text="$(jq -r '.text // empty' "$response")" ;;
    openrouter-chat) text="$(jq -r '.choices[0].message.content // empty' "$response")" ;;
  esac
  rm -f "$response"

  # Strip known whisper hallucinations that leak from subtitle training data.
  # These phrases never occur in real dictation; they're artifacts of training
  # on subtitles/credits. Match greedily to end of line since they're always
  # tacked on at the very end of the transcript.
  text="$(printf '%s' "$text" | sed -E \
    -e 's/[[:space:]]*Редактор субтитров.*$//I' \
    -e 's/[[:space:]]*Корректор[[:space:]]+[А-ЯЁA-Z]\.?[^[:space:]]*.*$//I' \
    -e 's/[[:space:]]*Субтитры:?.*$//I' \
    -e 's/[[:space:]]*Продолжение следует\.?[[:space:]]*$//I' \
    -e 's/[[:space:]]*Thanks for watching[!.]?[[:space:]]*$//I' \
    -e 's/[[:space:]]*Thank you for watching[!.]?[[:space:]]*$//I' \
    -e 's/[[:space:]]*Subtitles by.*$//I' \
    -e 's/[[:space:]]*\[Музыка\]//gI' \
    -e 's/[[:space:]]*\[Music\]//gI' \
    -e 's/^[[:space:]]+//; s/[[:space:]]+$//')"

  if [[ -z "$text" ]]; then
    # Local VAD servers may intentionally return an empty transcript for
    # silence/no-speech audio. Treat that as a clean skip instead of an API
    # failure that pollutes last-failed.txt and the clipboard.
    if [[ "$KWISPR_BACKEND" == "openai-transcriptions" && "$KWISPR_API_URL" == http://127.0.0.1:* ]]; then
      rm -f "$txt"
      status "⚠ No speech" 2000
      status_clear
      return 0
    fi
    local retry_cmd="$SCRIPT_DIR/kwispr.sh retry \"$wav\""
    printf '%s\n' "$wav" > "$LAST_FAILED"
    echo -n "$retry_cmd" | copy_text || true
    status "❌ Empty transcript" 5000
    status_clear
    return 1
  fi

  printf '%s' "$text" > "$txt"
  rm -f "$LAST_FAILED"
  if ! printf '%s' "$text" | copy_text; then
    status "❌ Clipboard unavailable" 5000
    status_clear
    return 1
  fi

  # Auto-paste via ydotool (Ctrl+V): works on Wayland via /dev/uinput,
  # independent of keyboard layout because we send raw keycodes for a
  # stable shortcut — the app pulls ru/en text straight from clipboard.
  # Falls back silently if ydotool unavailable or daemon not running.
  local pasted=0
  if [[ "${KWISPR_AUTOPASTE:-1}" == "1" ]] && command -v ydotool >/dev/null 2>&1; then
    # Give wl-copy a moment to register the clipboard offer with the compositor.
    sleep "${KWISPR_AUTOPASTE_DELAY:-0.30}"
    local paste_keys
    case "${KWISPR_PASTE_HOTKEY:-ctrl-v}" in
      ctrl-v)
        # 29 = LEFTCTRL, 47 = V
        paste_keys=(29:1 47:1 47:0 29:0)
        ;;
      ctrl-shift-v)
        # 29 = LEFTCTRL, 42 = LEFTSHIFT, 47 = V
        paste_keys=(29:1 42:1 47:1 47:0 42:0 29:0)
        ;;
      shift-insert)
        # 42 = LEFTSHIFT, 110 = INSERT. Often more layout-agnostic than Ctrl+V.
        paste_keys=(42:1 110:1 110:0 42:0)
        ;;
      *)
        paste_keys=(29:1 47:1 47:0 29:0)
        ;;
    esac
    if YDOTOOL_SOCKET=/run/user/$(id -u)/.ydotool_socket \
         ydotool key "${paste_keys[@]}" 2>/dev/null; then
      pasted=1
    fi
  fi

  play_cue "$KWISPR_SOUND_READY"
  if [[ "$pasted" == "1" ]]; then
    status "✅ Pasted" 2000
  else
    status "✅ Ready (in clipboard)" 3000
  fi
  status_clear
}

# -----------------------------------------------------------------------------
# Commands
# -----------------------------------------------------------------------------

cmd_toggle() {
  rotate_cache
  load_env

  # If transcription of the previous recording is still running, tell user
  # and bail out — no parallel sessions.
  if [[ -f "$TOGGLE_LOCK" ]] && kill -0 "$(cat "$TOGGLE_LOCK")" 2>/dev/null; then
    local lock_pid=$$
    if [[ "$(cat "$TOGGLE_LOCK")" != "$lock_pid" ]]; then
      notify "⏳ Busy"
      exit 0
    fi
  fi
  # Also bail if a previous recording's toast hasn't finished (prevents
  # stacking two persistent bubbles).
  echo $$ > "$TOGGLE_LOCK"
  trap 'rm -f "$TOGGLE_LOCK"' EXIT

  if is_recording; then
    local wav
    wav="$(stop_recording)"
    [[ -f "$wav" ]] || die "Recording disappeared: $wav"
    transcribe "$wav" || true
  else
    # Clean stale lock
    [[ -f "$PID_FILE" ]] && rm -f "$PID_FILE" "$WAV_POINTER"
    start_recording
    # Recording runs in background (ffmpeg), release toggle lock so next F5
    # can reach stop_recording. Transcription phase re-acquires its own lock.
    rm -f "$TOGGLE_LOCK"
    trap - EXIT
  fi
}

cmd_retry() {
  local wav="${1:-}"
  [[ -n "$wav" && -f "$wav" ]] || die "Usage: kwispr.sh retry <path/to.wav>"
  load_env
  transcribe "$wav"
}

case "${1:-toggle}" in
  toggle) cmd_toggle ;;
  retry)  shift; cmd_retry "$@" ;;
  *)      die "Unknown command: $1. Use: toggle | retry <wav>" ;;
esac
