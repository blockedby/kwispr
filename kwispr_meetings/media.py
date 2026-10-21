"""Import saved recordings into the durable meeting transcription queue."""
from pathlib import Path
import wave
import subprocess
import uuid


def convert_audio(source: Path, destination: Path, audio_stream: int = 0) -> None:
    """Decode a selected audio track; ignore video without modifying the source."""
    temporary = destination.with_name("audio-" + uuid.uuid4().hex + ".wav")
    try:
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                   "-i", str(source), "-map", f"0:a:{audio_stream}", "-vn", "-ac", "1", "-ar", "16000",
                   "-c:a", "pcm_s16le", "-f", "wav", str(temporary)]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=1800, check=False)
        except FileNotFoundError as error:
            raise RuntimeError("ffmpeg is required to import audio files.") from error
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("Audio conversion timed out; source and completed work were kept.") from error
        if result.returncode:
            if "matches no streams" in result.stderr.lower():
                raise RuntimeError(f"Media file has no audio track to transcribe: {source.name}")
            raise RuntimeError("ffmpeg could not decode this audio file: " + result.stderr.strip()[-500:])
        with wave.open(str(temporary), "rb") as reader:
            if (reader.getnchannels(), reader.getsampwidth(), reader.getframerate()) != (1, 2, 16000):
                raise RuntimeError("ffmpeg returned an unsupported WAV format.")
        temporary.chmod(0o600)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def session_tracks(session):
    if session.get("source_kind") == "media" and session.get("microphone_stream") is None:
        return ("remote",)
    return ("remote", "microphone")


def prepare_recording(directory, session, progress=None):
    directory = Path(directory)
    for track in session_tracks(session):
        destination = directory / (track + ".wav")
        if destination.is_file():
            continue
        if progress:
            progress(f"Extracting {track} audio from recording…")
        stream = session["audio_stream"] if track == "remote" else session["microphone_stream"]
        convert_audio(Path(session["source_path"]), destination, audio_stream=stream)
        with wave.open(str(destination), "rb") as reader:
            if reader.getnframes() == 0:
                destination.unlink()
                raise RuntimeError("The selected audio track is empty. Choose another track.")
