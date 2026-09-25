"""Resumable, speaker-free transcription of imported audio using local Whisper."""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
import re
import struct
import subprocess
import time
import uuid
import wave
from pathlib import Path

from .config import ConfigError, validate_local_backend
from .pipeline import _prepare_language_policy, _request_transcription, atomic_json, read_json

SAMPLE_RATE = 16000
MAX_SECONDS = 300
SEARCH_SECONDS = 15
FRAME_SECONDS = .02
FRAME_SAMPLES = int(SAMPLE_RATE * FRAME_SECONDS)
CHUNK_VERSION = 2


def _atomic_text(path: Path, value: str) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _source_digest(source: Path) -> str:
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_name(source: Path) -> str:
    stem = re.sub(r"[^\w.-]+", "_", source.stem, flags=re.UNICODE).strip("._")[:80] or "audio"
    identity = hashlib.sha256(os.fsencode(str(source))).hexdigest()[:12]
    return stem + "-" + identity


def _convert(source: Path, destination: Path) -> None:
    temporary = destination.with_name("audio-" + uuid.uuid4().hex + ".wav")
    try:
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                   "-i", str(source), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
                   "-c:a", "pcm_s16le", "-f", "wav", str(temporary)]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=1800, check=False)
        except FileNotFoundError as error:
            raise RuntimeError("ffmpeg is required to import audio files.") from error
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("Audio conversion timed out; source and completed work were kept.") from error
        if result.returncode:
            raise RuntimeError("ffmpeg could not decode this audio file: " + result.stderr.strip()[-500:])
        with wave.open(str(temporary), "rb") as reader:
            if (reader.getnchannels(), reader.getsampwidth(), reader.getframerate()) != (1, 2, SAMPLE_RATE):
                raise RuntimeError("ffmpeg returned an unsupported WAV format.")
        temporary.chmod(0o600)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _quiet_boundary(reader: wave.Wave_read, start: int, limit: int) -> tuple[int, bool]:
    """Choose a sustained low-energy frame near the 30-second limit."""
    search_start = max(start + 30 * SAMPLE_RATE, limit - SEARCH_SECONDS * SAMPLE_RATE)
    reader.setpos(search_start)
    frames = reader.readframes(limit - search_start)
    energies = []
    for offset in range(0, len(frames) - FRAME_SAMPLES * 2 + 1, FRAME_SAMPLES * 2):
        samples = struct.unpack_from("<" + "h" * FRAME_SAMPLES, frames, offset)
        energies.append(sum(value * value for value in samples) / FRAME_SAMPLES)
    if not energies:
        return limit, False
    # Six adjacent 20 ms frames give a quiet 120 ms gap. The absolute floor
    # also avoids calling a continuously loud stretch a silence.
    window = 6
    candidates = [(sum(energies[index:index + window]) / window, index)
                  for index in range(max(0, len(energies) - window + 1))]
    energy, index = min(candidates, key=lambda item: (item[0], -item[1]))
    if energy < 500 ** 2:
        return search_start + int((index + window / 2) * FRAME_SAMPLES), True
    return limit, False


def _plan_chunks(path: Path) -> list[dict]:
    chunks = []
    with wave.open(str(path), "rb") as reader:
        if (reader.getnchannels(), reader.getsampwidth(), reader.getframerate()) != (1, 2, SAMPLE_RATE):
            raise RuntimeError("Converted audio is not mono 16 kHz 16-bit PCM.")
        total = reader.getnframes()
        start = 0
        while start < total:
            limit = min(total, start + MAX_SECONDS * SAMPLE_RATE)
            if limit == total:
                end, quiet = limit, True
            else:
                end, quiet = _quiet_boundary(reader, start, limit)
            chunks.append({"start_frame": start, "end_frame": end})
            if end >= total:
                break
            next_start = end if quiet else max(start + SAMPLE_RATE, end - 2 * SAMPLE_RATE)
            start = next_start
    return chunks


def _wav_chunk(reader: wave.Wave_read, start: int, end: int) -> bytes:
    import io
    reader.setpos(start)
    pcm = reader.readframes(end - start)
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(SAMPLE_RATE)
        writer.writeframes(pcm)
    return output.getvalue()


def _join_text(parts: list[str]) -> str:
    # Keep exact ASR text for auditability, including real repeated phrases.
    # A forced boundary can repeat words from its two-second overlap.
    return " ".join(part.strip() for part in parts if part.strip())


def transcribe(source_file: str | Path, output_root: str | Path, progress=None) -> dict:
    from .config import load_config
    source = Path(source_file).expanduser().resolve(strict=True)
    if not source.is_file():
        raise RuntimeError("Source must be a regular audio file.")
    config = load_config(meeting_overrides=False)
    validate_local_backend(config)
    model = config.get("KWISPR_MODEL", "whisper-large-v3-turbo")
    if not model.startswith("whisper"):
        raise ConfigError("Audio files require a local Whisper model in KWISPR_MODEL.")
    # Every imported file uses multilingual RU/EN detection. The local server
    # reports whether it can enforce the candidate list; refuse a silent loss
    # of that constraint rather than sending unrestricted audio.
    config["KWISPR_WHISPER_ALLOWED_LANGUAGES"] = "ru,en"
    config = _prepare_language_policy(config)
    if config["_meeting_language_policy"]["effective_allowed_languages"] != "ru,en":
        raise RuntimeError("Local STT does not advertise RU/EN language restriction. Update or start the local Whisper server.")
    source_hash = _source_digest(source)
    signature = hashlib.sha256(json.dumps({"version": CHUNK_VERSION, "source": source_hash,
        "model": model, "url": config.get("KWISPR_API_URL"), "vocabulary": config.get("KWISPR_VOCABULARY", ""),
        "languages": "ru,en", "chunk_seconds": MAX_SECONDS, "search_seconds": SEARCH_SECONDS}, sort_keys=True).encode()).hexdigest()
    # Changed source or settings start a separate result directory, so an old
    # completed transcript cannot appear to belong to a failed new attempt.
    output_dir = Path(output_root).expanduser().resolve() / (_safe_name(source) + "-" + signature[:12])
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_fd = os.open(output_dir / ".processing.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        return _transcribe_locked(source, source_hash, output_dir, signature, config, progress)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _transcribe_locked(source, source_hash, output_dir, signature, config, progress):
    wav_path = output_dir / "audio.wav"
    checkpoint_path = output_dir / "processing-checkpoint.json"
    checkpoint = read_json(checkpoint_path, {})
    if checkpoint.get("signature") != signature:
        checkpoint = {"signature": signature, "source_sha256": source_hash, "chunks": {}}
        if progress:
            progress("Decoding audio", 0, 0)
        _convert(source, wav_path)
        atomic_json(checkpoint_path, checkpoint)
    elif not wav_path.is_file():
        _convert(source, wav_path)
    chunks = _plan_chunks(wav_path)
    with wave.open(str(wav_path), "rb") as reader:
        for index, chunk in enumerate(chunks):
            key = f"{chunk['start_frame']}:{chunk['end_frame']}"
            if key not in checkpoint["chunks"]:
                answer = _request_transcription(_wav_chunk(reader, chunk["start_frame"], chunk["end_frame"]), config)
                checkpoint["chunks"][key] = answer
                atomic_json(checkpoint_path, checkpoint)
                time.sleep(.05)
            if progress:
                progress(f"Transcribing audio: {index + 1}/{len(chunks)}", index + 1, len(chunks))
    rows = [{"start": round(chunk["start_frame"] / SAMPLE_RATE, 3),
             "end": round(chunk["end_frame"] / SAMPLE_RATE, 3),
             **checkpoint["chunks"][f"{chunk['start_frame']}:{chunk['end_frame']}"]} for chunk in chunks]
    text = _join_text([row["text"] for row in rows])
    transcript_path = output_dir / "transcript.txt"
    transcript_json = output_dir / "transcript.json"
    document = {"schema_version": 1, "source_path": str(source), "source_sha256": source_hash,
                "text": text, "chunks": rows, "language_policy": config["_meeting_language_policy"]}
    atomic_json(transcript_json, document)
    _atomic_text(transcript_path, text + "\n")
    return {"event": "result", "state": "complete", "source_path": str(source), "text": text,
            "transcript_path": str(transcript_path), "transcript_json": str(transcript_json),
            "output_dir": str(output_dir)}
