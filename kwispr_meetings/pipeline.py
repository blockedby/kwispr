"""Local meeting diarization and resumable transcription. No clipboard access."""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from pathlib import Path
from .config import allowed_whisper_languages, meeting_language, runtime_dir

PIPELINE_VERSION = 3
SHORT_TURN_SECONDS = 4.0
MODEL_FILES = {
    "segmentation.onnx": (5992913, "220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079"),
    "embedding.onnx": (40257283, "ad4a1802485d8b34c722d2a9d04249662f2ece5d28a7a039063ca22f515a789e"),
}


def require_ready(config=None):
    missing = [name for name, (size, _) in MODEL_FILES.items()
               if not (runtime_dir(config) / "models" / name).is_file()
               or (runtime_dir(config) / "models" / name).stat().st_size != size]
    if missing:
        raise RuntimeError("Meeting models are missing or incomplete. Open Meetings and choose Set up meeting models.")
    try:
        import numpy  # noqa: F401
        import sherpa_onnx  # noqa: F401
    except ImportError as error:
        raise RuntimeError("Meeting runtime is not installed. Open Meetings and choose Set up meeting models.") from error


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default


def _notify(callback, message):
    if callback:
        callback(message)


def _load_track(path):
    import numpy as np
    with wave.open(str(path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2 or source.getframerate() != 16000:
            raise RuntimeError(f"{path.name} must be mono, 16 kHz, 16-bit PCM WAV.")
        pcm = source.readframes(source.getnframes())
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def _diarize(audio, config, speakers, callback):
    import sherpa_onnx
    models = runtime_dir(config) / "models"
    model_config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(models / "segmentation.onnx")),
            num_threads=min(4, os.cpu_count() or 1),
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(models / "embedding.onnx"), num_threads=min(4, os.cpu_count() or 1),
        ),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=speakers if speakers > 0 else -1, threshold=0.9),
        min_duration_on=0.25, min_duration_off=0.4,
    )
    if not model_config.validate():
        raise RuntimeError("Invalid meeting diarization model configuration. Run Set up meeting models again.")
    model = sherpa_onnx.OfflineSpeakerDiarization(model_config)
    last_progress = -1

    def progress(done, total):
        nonlocal last_progress
        percentage = int(100 * done / max(1, total))
        if percentage // 5 != last_progress:
            last_progress = percentage // 5
            _notify(callback, f"Separating speakers: {percentage}%")
        return 0

    if len(audio) < 4000 or float(abs(audio).max(initial=0)) < 0.0001:
        return []
    result = model.process(audio, callback=progress).sort_by_start_time()
    # Names stay stable within the call and are assigned by first appearance.
    names = {}
    rows = []
    for segment in result:
        names.setdefault(segment.speaker, len(names) + 1)
        rows.append({"start": float(segment.start), "end": float(segment.end),
                     "speaker": f"speaker_{names[segment.speaker]:02d}"})
    return rows


def speech_intervals(segments, duration, own_microphone=False):
    """Turn possibly overlapping model ranges into unambiguous timeline spans."""
    events = {}
    for segment in segments:
        start, end = float(segment["start"]), float(segment["end"])
        if not math.isfinite(start) or not math.isfinite(end):
            continue
        start, end = max(0.0, start), min(duration, end)
        if end <= start:
            continue
        speaker = "self" if own_microphone else str(segment["speaker"])
        events.setdefault(start, []).append((speaker, 1))
        events.setdefault(end, []).append((speaker, -1))
    active, rows, previous = {}, [], None
    for point in sorted(events):
        speakers = sorted(key for key, count in active.items() if count > 0)
        if previous is not None and point > previous and speakers:
            if rows and rows[-1]["speakers"] == speakers and previous - rows[-1]["end"] <= 0.4:
                rows[-1]["end"] = point
            else:
                rows.append({"start": previous, "end": point, "speakers": speakers})
        for speaker, delta in events[point]:
            active[speaker] = active.get(speaker, 0) + delta
        previous = point
    return rows


def plan_transcription_intervals(intervals):
    """Keep a continuing voice around a subsecond overlap in one ASR request.

    Diarization still describes the exact speaker spans. A -> A+B -> A is a
    single acoustic phrase when the overlap is shorter than a second; decoding
    each boundary independently can feed Whisper only a fraction of a syllable.
    A real A -> B change, a longer overlap, or a pause remains separate.
    """
    result = []
    index = 0
    while index < len(intervals):
        current = dict(intervals[index])
        anchor = set(current["speakers"])
        while len(anchor) == 1 and index + 2 < len(intervals):
            bridge, following = intervals[index + 1:index + 3]
            if not (anchor < set(bridge["speakers"]) and set(following["speakers"]) == anchor
                    and 0 < bridge["end"] - bridge["start"] < 1.0
                    and 0 <= bridge["start"] - current["end"] <= 0.4
                    and 0 <= following["start"] - bridge["end"] <= 0.4):
                break
            sources = current.get("source_intervals", [dict(current)])
            sources = [*sources, dict(bridge), dict(following)]
            current = {"start": current["start"], "end": following["end"],
                       "speakers": sorted({speaker for source in sources for speaker in source["speakers"]}),
                       "speaker_grouped": True, "source_intervals": sources}
            index += 2
        result.append(current)
        index += 1
    return result


def _slice_transcription_interval(interval, start, end):
    result = {**interval, "start": start, "end": end}
    if "source_intervals" in interval:
        sources = [{**source, "start": max(start, source["start"]), "end": min(end, source["end"])}
                   for source in interval["source_intervals"] if source["end"] > start and source["start"] < end]
        result["source_intervals"] = sources
        result["speakers"] = sorted({speaker for source in sources for speaker in source["speakers"]})
        result["speaker_grouped"] = len(sources) > 1 and len(result["speakers"]) > 1
    return result


def split_intervals(intervals, audio, maximum=45.0):
    """Bound inference jobs and split long turns near a quiet 100 ms frame."""
    import numpy as np
    result = []
    for interval in intervals:
        start, end = interval["start"], interval["end"]
        while end - start > maximum:
            # Each sample remains assigned exactly once; don't drop the gap.
            candidates = np.arange(start + maximum - 5, start + maximum, 0.1)
            split = min(candidates, key=lambda t: float(np.mean(audio[int(t * 16000):int((t + .1) * 16000)] ** 2)))
            result.append(_slice_transcription_interval(interval, start, float(split)))
            start = float(split)
        result.append(_slice_transcription_interval(interval, start, end))
    return result


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError("Meeting transcription endpoint redirected. Configure the local endpoint directly.")


def _prepare_language_policy(config):
    """Check an opt-in capability once per run, preserving the full configuration."""
    from .config import validate_local_backend
    validate_local_backend(config)
    prepared = dict(config)
    policy = {"configured_allowed_languages": config.get("KWISPR_WHISPER_ALLOWED_LANGUAGES", "").strip(),
              "effective_allowed_languages": "", "supported": None}
    prepared["_meeting_language_policy"] = policy
    if not config.get("KWISPR_MODEL", "whisper-large-v3-turbo").startswith("whisper"):
        return prepared
    configured = allowed_whisper_languages(config)
    policy["configured_allowed_languages"] = configured
    if not configured:
        return prepared  # Default requests need no extra network round trip.
    endpoint = urllib.parse.urlsplit(config["KWISPR_API_URL"])
    health_url = urllib.parse.urlunsplit((endpoint.scheme, endpoint.netloc, "/health", "", ""))
    headers = {}
    key = config.get("KWISPR_API_KEY") or config.get("OPENAI_API_KEY", "")
    if key:
        headers["Authorization"] = "Bearer " + key
    request = urllib.request.Request(health_url, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=3) as response:
            health = json.loads(response.read(64 * 1024))
        capabilities = health.get("capabilities", {}) if isinstance(health, dict) else {}
        supported = isinstance(capabilities, dict) and capabilities.get("whisper_allowed_languages") is True
    except urllib.error.HTTPError as error:
        error.close()
        supported = False
    except (urllib.error.URLError, TimeoutError, ValueError, RuntimeError, OSError):
        supported = False
    policy["supported"] = supported
    if supported:
        policy["effective_allowed_languages"] = configured
    return prepared


def _request_transcription(pcm, config, *, language="", vocabulary=True):
    from .config import validate_local_backend
    validate_local_backend(config)
    if "_meeting_language_policy" not in config:
        config = _prepare_language_policy(config)
    fields = {"model": config.get("KWISPR_MODEL", "whisper-large-v3-turbo"),
              "response_format": "json", "temperature": "0", "preserve_audio_tail": "1"}
    allowed = config["_meeting_language_policy"]["effective_allowed_languages"]
    if fields["model"].startswith("whisper") and allowed:
        fields["allowed_languages"] = allowed
    if language:
        fields["language"] = language
    # Dictation's example prose and language describe the user's own voice, not
    # everyone in a call. In particular, a Russian example can turn English
    # speech into Cyrillic gibberish. Only the personal spellings are shared.
    context = config.get("KWISPR_VOCABULARY", "").strip() if vocabulary else ""
    if fields["model"].startswith("whisper") and context:
        if len(context) > 4096 or "\0" in context:
            raise RuntimeError("Whisper context must be at most 4096 characters and contain no NUL.")
        fields["prompt"] = context
    boundary = "kwispr-" + uuid.uuid4().hex
    chunks = []
    for name, value in fields.items():
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode() + value.encode() + b"\r\n")
    chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="turn.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode() + pcm + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    headers = {"Content-Type": "multipart/form-data; boundary=" + boundary}
    key = config.get("KWISPR_API_KEY") or config.get("OPENAI_API_KEY", "")
    if key:
        headers["Authorization"] = "Bearer " + key
    request = urllib.request.Request(config["KWISPR_API_URL"], data=b"".join(chunks), headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=180) as response:
            result = json.loads(response.read(2 * 1024 * 1024))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Local transcription returned HTTP {error.code}. Audio was kept; fix the endpoint/model and retry.") from error
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise RuntimeError(f"Local transcription failed ({type(error).__name__}). Audio and completed turns were kept; retry when the server is ready.") from error
    if not isinstance(result, dict) or not isinstance(result.get("text"), str):
        raise RuntimeError("Local transcription returned an invalid response; expected a text field.")
    answer = {"text": result["text"].strip()}
    detected = result.get("language")
    if isinstance(detected, str) and re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8})*", detected.strip()):
        answer["language"] = detected.strip().lower()
    return answer


def _request_transcript(pcm, config):
    """Compatibility text-only helper; meeting language is selected per track."""
    return _request_transcription(pcm, config)["text"]


def _wav_slice(audio, start, end):
    import numpy as np
    data = np.clip(audio[int(start * 16000):int(end * 16000)], -1.0, 1.0)
    pcm = np.rint(data * 32767).astype("<i2").tobytes()
    # Enough trailing silence for very short utterances, without adding speech.
    pcm += b"\0" * max(0, 32000 - len(pcm))
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(16000)
        writer.writeframes(pcm)
    return output.getvalue()


def _fingerprint(session_dir, config, session):
    tracks = []
    for name in ["microphone.wav", "remote.wav"]:
        stat = (session_dir / name).stat()
        tracks.append([name, stat.st_size, stat.st_mtime_ns])
    inputs = {"version": PIPELINE_VERSION, "tracks": tracks, "speakers": session.get("speakers", 0),
              "context": [config.get(key, "") for key in ["KWISPR_API_URL", "KWISPR_MODEL", "KWISPR_MEETING_MIC_LANGUAGE", "KWISPR_MEETING_REMOTE_LANGUAGE", "KWISPR_VOCABULARY"]]}
    if config.get("KWISPR_WHISPER_ALLOWED_LANGUAGES", "").strip():
        inputs["allowed_languages"] = {"configured": config["KWISPR_WHISPER_ALLOWED_LANGUAGES"],
                                       "policy": config.get("_meeting_language_policy", {})}
    return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()


def timestamp(seconds):
    value = max(0, int(seconds))
    return f"{value // 3600:02d}:{value // 60 % 60:02d}:{value % 60:02d}"


def _markdown_literal(text):
    return re.sub(r"([\\`*_{}\[\]<>#!|])", r"\\\1", " ".join(str(text).split()))


def write_outputs(session_dir, session, rows, names=None, languages=None, language_policy=None):
    session_dir = Path(session_dir)
    names = names or {}
    labels = {"self": "Я"}
    for row in rows:
        for speaker in row["speakers"]:
            if speaker != "self":
                label = "Собеседник (голос не определён)" if speaker == "speaker_unknown" else "Собеседник " + str(int(speaker.rsplit("_", 1)[1]))
                labels.setdefault(speaker, label)
    labels.update({key: value.strip() for key, value in names.items() if key in labels and isinstance(value, str) and value.strip()})
    ordered = sorted(rows, key=lambda row: (row["start"], row["end"], row["track"]))
    document = {"schema_version": 1, "title": session.get("title") or "Звонок", "started_at": session.get("started_at"),
                "speakers": labels, "utterances": ordered, "track_languages": languages or {},
                "language_policy": language_policy or {}}
    atomic_json(session_dir / "transcript.json", document)
    lines = ["# " + _markdown_literal(document["title"]), ""]
    if document["started_at"]:
        lines += ["Начало: " + _markdown_literal(document["started_at"]), ""]
    for row in ordered:
        label = " + ".join(labels[speaker] for speaker in row["speakers"])
        if len(row["speakers"]) > 1:
            label += " (несколько голосов внутри фрагмента)" if row.get("speaker_grouped") else " (говорят одновременно)"
        lines += [f"**[{timestamp(row['start'])}–{timestamp(row['end'])}] {_markdown_literal(label)}**", "",
                  _markdown_literal(row["text"]) or "[Речь обнаружена, текст не распознан]", ""]
    if not ordered:
        lines += ["Речь не обнаружена.", ""]
    path = session_dir / "transcript.md"
    temporary = path.with_name("transcript.md.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        stream.write("\n".join(lines)); stream.flush(); os.fsync(stream.fileno())
    temporary.replace(path)
    return {"transcript_path": str(path), "transcript_json": str(session_dir / "transcript.json"),
            "speaker_count": sum(speaker not in {"self", "speaker_unknown"} for speaker in labels),
            "has_unknown_speaker": "speaker_unknown" in labels}


def process_session(session_dir, config, progress_callback=None):
    from .config import validate_local_backend
    validate_local_backend(config)
    require_ready(config)
    # Recheck on every processing/retry run: upgrading an old server must
    # invalidate turns that were decoded without the requested restriction.
    config = _prepare_language_policy(config)
    session_dir = Path(session_dir)
    session = read_json(session_dir / "session.json", {})
    speakers = int(session.get("speakers", config.get("KWISPR_MEETING_SPEAKERS", 0)))
    if not 0 <= speakers <= 16:
        raise RuntimeError("Remote speaker count must be between 0 (Auto) and 16.")
    signature = _fingerprint(session_dir, config, session)
    checkpoint_path = session_dir / "processing-checkpoint.json"
    checkpoint = read_json(checkpoint_path, {})
    if checkpoint.get("signature") != signature:
        checkpoint = {"signature": signature, "turns": {}}
    diarization_path = session_dir / "diarization.json"
    diarization = read_json(diarization_path, {})
    if diarization.get("signature") != signature:
        diarization = {"signature": signature, "tracks": {}}
    all_rows, track_languages = [], {}
    for track in ["remote", "microphone"]:
        _notify(progress_callback, f"Reading {track} recording…")
        audio = _load_track(session_dir / (track + ".wav"))
        duration = len(audio) / 16000
        if track not in diarization["tracks"]:
            _notify(progress_callback, f"Separating speakers in {track} recording…")
            segments = _diarize(audio, config, 1 if track == "microphone" else speakers, progress_callback)
            diarization["tracks"][track] = speech_intervals(segments, duration, track == "microphone")
            atomic_json(diarization_path, diarization)
        intervals = split_intervals(plan_transcription_intervals(diarization["tracks"][track]), audio)
        explicit_language = meeting_language(config, track)
        fallback_language = ""
        detected_languages = []
        language_info = {"requested": explicit_language or "auto", "detected": detected_languages,
                         "short_turn_fallback": ""}
        track_languages[track] = language_info
        # Probe only when the first utterance is too short to identify a language.
        # Longer turns continue to autodetect, so a later language switch can
        # update the fallback instead of fixing a multilingual call to one language.
        if intervals and not explicit_language and intervals[0]["end"] - intervals[0]["start"] < SHORT_TURN_SECONDS:
            probe = checkpoint.setdefault("language_probes", {}).get(track)
            if probe is None:
                probe_interval = next((item for item in intervals if item["end"] - item["start"] >= 10), None)
                if probe_interval:
                    _notify(progress_callback, f"Detecting language in {track} recording…")
                    probe = _request_transcription(_wav_slice(audio, probe_interval["start"],
                                                              min(probe_interval["end"], probe_interval["start"] + 20)),
                                                   config, vocabulary=False)
                    # Probe text is not used as a transcript; every timed turn is
                    # still decoded. Persist only language metadata for retries.
                    probe = {"language": probe.get("language", "")}
                else:
                    probe = {}
                checkpoint["language_probes"][track] = probe
                atomic_json(checkpoint_path, checkpoint)
            fallback_language = probe.get("language", "")
            language_info["probe"] = fallback_language
        offset = float(session.get(track + "_offset_seconds", 0))
        if not math.isfinite(offset):
            raise RuntimeError("Invalid audio track timing offset.")
        for index, interval in enumerate(intervals):
            _notify(progress_callback, f"Transcribing {track}: {index + 1}/{len(intervals)}")
            key = f"{track}:{interval['start']:.6f}:{interval['end']:.6f}:{','.join(interval['speakers'])}"
            if key not in checkpoint["turns"]:
                # Extend only into surrounding silence, never across another speaker.
                previous_end = intervals[index - 1]["end"] if index else 0
                next_start = intervals[index + 1]["start"] if index + 1 < len(intervals) else duration
                start = max(previous_end, interval["start"] - 0.15)
                end = min(next_start, interval["end"] + 0.2)
                short = interval["end"] - interval["start"] < SHORT_TURN_SECONDS
                requested = explicit_language or (fallback_language if short else "")
                result = _request_transcription(_wav_slice(audio, start, end), config, language=requested)
                result["requested_language"] = requested or "auto"
                result["language_mode"] = "explicit" if explicit_language else "short-turn-context" if requested else "auto"
                checkpoint["turns"][key] = result
                atomic_json(checkpoint_path, checkpoint)
                # Give interactive dictation a chance between bounded requests.
                time.sleep(0.05)
            result = checkpoint["turns"][key]
            detected = result.get("language", "")
            if detected and detected not in detected_languages:
                detected_languages.append(detected)
            if detected and interval["end"] - interval["start"] >= SHORT_TURN_SECONDS:
                fallback_language = detected
            language_info["short_turn_fallback"] = fallback_language
            source_metadata = {}
            if "source_intervals" in interval:
                source_metadata = {"speaker_grouped": interval["speaker_grouped"],
                                   "source_intervals": [{**source, "start": source["start"] + offset,
                                                         "end": source["end"] + offset}
                                                        for source in interval["source_intervals"]]}
            all_rows.append({"start": round(interval["start"] + offset, 3), "end": round(interval["end"] + offset, 3),
                             "speakers": interval["speakers"], "track": track, **result, **source_metadata})
        del audio
    _notify(progress_callback, "Saving transcript files…")
    return write_outputs(session_dir, session, all_rows, read_json(session_dir / "speaker-names.json", {}),
                         track_languages, config["_meeting_language_policy"])
