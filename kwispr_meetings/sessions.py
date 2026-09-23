"""Durable, explicitly started two-track PulseAudio meeting capture."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import importlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any
import uuid

from .config import ConfigError, load_config, output_dir, speaker_count, validate_local_backend

ACTIVE = {"starting", "recording", "stopping", "processing"}
STOP_TIMEOUT_SECONDS = 8
START_TIMEOUT_SECONDS = 20


class SessionError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise SessionError(f"Expected a private directory: {path}")
    path.chmod(0o700)
    return path


def state_dir() -> Path:
    root = Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser()
    return private_dir(root / "kwispr/meetings")


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: Path) -> dict[str, Any]:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as error:
        raise SessionError(f"Cannot read session state: {path.name}") from error
    if not isinstance(result, dict):
        raise SessionError(f"Invalid session state: {path.name}")
    return result


@contextmanager
def control_lock():
    root = state_dir()
    fd = os.open(root / "control.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield root
    finally:
        os.close(fd)


def process_identity(pid: int) -> str | None:
    """Linux start ticks and boot ID prevent a stale PID matching another task."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if stat[0] == "Z":
            return None
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return f"{boot}:{stat[19]}"
    except (OSError, ValueError, IndexError):
        return None


def owner_alive(current: dict[str, Any]) -> bool:
    pid = current.get("worker_pid")
    identity = current.get("worker_identity")
    return isinstance(pid, int) and bool(identity) and process_identity(pid) == identity


def current_locked(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    pointer = read_json(root / "current.json")
    if not pointer:
        return {}, {"state": "idle", "message": "No meeting has been recorded."}
    directory = Path(pointer["session_dir"])
    session = read_json(directory / "session.json")
    if session.get("token") != pointer.get("token"):
        raise SessionError("Session identity does not match the current recording.")
    if session.get("state") in ACTIVE and not owner_alive(pointer):
        session.update(state="failed", message="Meeting worker stopped unexpectedly. Audio was kept; retry processing from this folder.", updated_at=now())
        atomic_json(directory / "session.json", session)
    return pointer, session


def public_status(session: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in session.items()
            if key not in {"token", "worker_pid", "worker_identity", "stop_requested"}}


def status() -> dict[str, Any]:
    with control_lock() as root:
        _, session = current_locked(root)
        return public_status(session)


def update_session(directory: Path, token: str, **values: Any) -> dict[str, Any]:
    with control_lock() as root:
        pointer = read_json(root / "current.json")
        if pointer.get("token") != token or pointer.get("session_dir") != str(directory):
            raise SessionError("This meeting no longer owns the recorder.")
        session = read_json(directory / "session.json")
        if session.get("token") != token:
            raise SessionError("Session token changed.")
        session.update(values, updated_at=now())
        atomic_json(directory / "session.json", session)
        return session


def command_json(args: list[str]) -> Any:
    try:
        response = subprocess.run(args, check=True, capture_output=True, text=True, timeout=10)
        return json.loads(response.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise SessionError("Cannot list audio devices. Check that PipeWire/PulseAudio and pactl are available.") from error


def sources() -> dict[str, Any]:
    devices = command_json(["pactl", "--format=json", "list", "sources"])
    sinks = command_json(["pactl", "--format=json", "list", "sinks"])
    try:
        default_mic = subprocess.run(["pactl", "get-default-source"], check=True, capture_output=True, text=True, timeout=10).stdout.strip()
        default_sink = subprocess.run(["pactl", "get-default-sink"], check=True, capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise SessionError("Cannot read the default audio devices.") from error
    monitor_names = {sink.get("monitor_source") for sink in sinks}
    microphones, monitors = [], []
    for device in devices:
        name = device.get("name", "")
        if not name:
            continue
        item = {"name": name, "description": device.get("description") or name}
        monitor_of_sink = device.get("monitor_of_sink")
        is_monitor = name in monitor_names or name.endswith(".monitor") or (
            monitor_of_sink not in (None, "", -1, 4294967295, "4294967295"))
        (monitors if is_monitor else microphones).append(item)
    default_monitor = next((sink.get("monitor_source") for sink in sinks if sink.get("name") == default_sink), "")
    return {"microphones": microphones, "monitors": monitors,
            "default_microphone": default_mic, "default_monitor": default_monitor}


def ready(config: dict[str, str]) -> Any:
    validate_local_backend(config)
    try:
        pipeline = importlib.import_module("kwispr_meetings.pipeline")
    except ImportError as error:
        raise SessionError("Meeting processing is not installed. Run setup-meetings.sh first.") from error
    pipeline.require_ready(config)
    return pipeline


def ffmpeg_command(session: dict[str, Any], directory: Path) -> list[str]:
    # Pulse timestamps share a wall clock. -isync preserves the input start offset;
    # aresample pads each WAV from the common epoch and corrects device clock drift.
    return ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", "-copyts", "-start_at_zero",
            "-thread_queue_size", "4096", "-f", "pulse", "-wallclock", "1",
            "-sample_rate", "48000", "-channels", "1", "-name", "Kwispr meeting microphone",
            "-i", session["mic_source"],
            "-thread_queue_size", "4096", "-isync", "0", "-f", "pulse", "-wallclock", "1",
            "-sample_rate", "48000", "-channels", "1", "-name", "Kwispr meeting call audio",
            "-i", session["monitor_source"],
            "-map", "0:a:0", "-af", "aresample=16000:async=1:first_pts=0", "-ac", "1",
            "-c:a", "pcm_s16le", str(directory / "microphone.wav"),
            "-map", "1:a:0", "-af", "aresample=16000:async=1:first_pts=0", "-ac", "1",
            "-c:a", "pcm_s16le", str(directory / "remote.wav")]


def launch_locked(root: Path, directory: Path, session: dict[str, Any], mode: str) -> subprocess.Popen:
    token = session["token"]
    entry = Path(__file__).resolve().parent.parent / "kwispr-meetings.py"
    logfd = os.open(directory / "worker.log", os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        process = subprocess.Popen([sys.executable, str(entry), "_worker", str(directory), token, mode],
                                   stdin=subprocess.DEVNULL, stdout=logfd, stderr=logfd,
                                   close_fds=True, start_new_session=True)
    except OSError as error:
        session.update(state="failed", message=f"Could not start meeting worker: {error}")
        atomic_json(directory / "session.json", session)
        raise SessionError(session["message"]) from error
    finally:
        os.close(logfd)
    atomic_json(root / "current.json", {"session_dir": str(directory), "token": token,
                                       "worker_pid": process.pid, "worker_identity": process_identity(process.pid)})
    return process


def start(config: dict[str, str], mic: str | None = None, monitor: str | None = None,
          destination: str | None = None, speakers: int | None = None, title: str = "") -> dict[str, Any]:
    ready(config)
    if not shutil.which("ffmpeg"):
        raise SessionError("ffmpeg is required to record meeting audio.")
    devices = sources()
    mic = mic or config.get("KWISPR_MEETING_MIC_SOURCE") or devices["default_microphone"]
    monitor = monitor or config.get("KWISPR_MEETING_MONITOR_SOURCE") or devices["default_monitor"]
    if mic not in {item["name"] for item in devices["microphones"]}:
        raise SessionError("Select an available microphone before starting the meeting.")
    if monitor not in {item["name"] for item in devices["monitors"]}:
        raise SessionError("Select an available call audio output before starting the meeting.")
    count = speaker_count(config.get("KWISPR_MEETING_SPEAKERS", "0") if speakers is None else speakers)
    base = Path(destination).expanduser().resolve() if destination else output_dir(config)
    try:
        stop_delay_ms = min(2000, max(0, int(config.get("KWISPR_STOP_DELAY_MS", "350"))))
    except ValueError:
        stop_delay_ms = 350
    with control_lock() as root:
        _, previous = current_locked(root)
        if previous.get("state") in ACTIVE:
            raise SessionError("A meeting is already recording or processing.")
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not base.is_dir():
            raise SessionError("Choose an existing directory or a new output folder.")
        identifier = datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + "_" + uuid.uuid4().hex[:8]
        directory = base / identifier
        directory.mkdir(mode=0o700)
        session = {"version": 1, "session_id": identifier, "session_dir": str(directory),
                   "token": uuid.uuid4().hex, "state": "starting", "message": "Starting audio capture…",
                   "title": title.strip() or "Meeting", "started_at": now(), "updated_at": now(),
                   "mic_source": mic, "monitor_source": monitor, "speakers": count, "stop_delay_ms": stop_delay_ms,
                   "microphone_offset_seconds": 0.0, "remote_offset_seconds": 0.0}
        atomic_json(directory / "session.json", session)
        process = launch_locked(root, directory, session, "record")
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        current = status()
        if current["state"] != "starting":
            if current["state"] == "failed":
                raise SessionError(current["message"])
            return current
        if process.poll() is not None:
            break
        time.sleep(0.1)
    stop()
    raise SessionError("Audio capture did not become ready in time. See worker.log in the meeting folder.")


def stop() -> dict[str, Any]:
    with control_lock() as root:
        _, session = current_locked(root)
        if session.get("state") not in {"starting", "recording", "stopping"}:
            return public_status(session)
        session.update(stop_requested=True, state="stopping", message="Finishing the audio files…", updated_at=now())
        atomic_json(Path(session["session_dir"]) / "session.json", session)
        return public_status(session)


def retry(directory: Path, config: dict[str, str], speakers: int | None = None) -> dict[str, Any]:
    count = speaker_count(speakers) if speakers is not None else None
    ready(config)
    directory = directory.expanduser().resolve()
    with control_lock() as root:
        _, previous = current_locked(root)
        if previous.get("state") in ACTIVE:
            raise SessionError("Wait for the active meeting to finish first.")
        session = read_json(directory / "session.json")
        if not session or session.get("version") != 1:
            raise SessionError("Select a meeting folder containing session.json.")
        for track in ("microphone.wav", "remote.wav"):
            if not (directory / track).is_file() or (directory / track).stat().st_size <= 44:
                raise SessionError(f"The meeting does not have a usable {track}; recorded files were kept.")
        if count is not None:
            session["speakers"] = count
        session.update(token=uuid.uuid4().hex, session_dir=str(directory), state="processing",
                       message="Preparing transcription…", updated_at=now(), stop_requested=False)
        atomic_json(directory / "session.json", session)
        launch_locked(root, directory, session, "process")
        return public_status(session)


def finish_capture(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        if process.returncode:
            raise SessionError("Audio capture failed. See worker.log; any recorded audio was kept.")
        return
    try:
        process.stdin.write(b"q\n")
        process.stdin.flush()
    except (BrokenPipeError, OSError):
        pass
    try:
        code = process.wait(timeout=STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise SessionError("Audio capture did not stop cleanly. Audio was kept for recovery.")
    if code:
        raise SessionError("Audio capture stopped with an error. See worker.log; audio was kept.")


def capture_exec(command: list[str], parent_pid: int) -> None:
    """Exit if the supervisor disappears, including SIGKILL or a crash."""
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise SessionError("Cannot attach audio capture lifetime to the meeting worker.")
    if os.getppid() != parent_pid:
        raise SessionError("Meeting worker exited before capture started.")
    os.execvp(command[0], command)


def record(directory: Path, token: str, interrupted=lambda: False) -> None:
    session = read_json(directory / "session.json")
    entry = Path(__file__).resolve().parent.parent / "kwispr-meetings.py"
    command = [sys.executable, str(entry), "_capture", str(os.getpid()),
               *ffmpeg_command(session, directory)]
    process = subprocess.Popen(command, stdin=subprocess.PIPE,
                               stdout=subprocess.DEVNULL, stderr=sys.stderr, close_fds=True)
    try:
        deadline = time.monotonic() + START_TIMEOUT_SECONDS - 2
        stop_at = None
        while True:
            current = read_json(directory / "session.json")
            if current.get("stop_requested") or interrupted():
                if stop_at is None:
                    stop_at = time.monotonic() + session.get("stop_delay_ms", 350) / 1000
                if time.monotonic() >= stop_at:
                    break
            if process.poll() is not None:
                raise SessionError("Audio capture exited unexpectedly. Check the selected devices and worker.log.")
            files_ready = all((directory / name).exists() for name in ("microphone.wav", "remote.wav"))
            if session["state"] == "starting" and files_ready:
                session = update_session(directory, token, state="recording", message="Recording microphone and call audio.")
            if session["state"] == "starting" and time.monotonic() > deadline:
                raise SessionError("Audio devices did not start in time. Check the selected devices and worker.log.")
            time.sleep(0.1)
        finish_capture(process)
        update_session(directory, token, stopped_at=now())
    finally:
        if process.poll() is None:
            try:
                finish_capture(process)
            except SessionError:
                pass
        if process.stdin:
            process.stdin.close()
        for track in ("microphone.wav", "remote.wav"):
            if (directory / track).exists():
                (directory / track).chmod(0o600)


def worker(directory: Path, token: str, mode: str) -> int:
    os.umask(0o077)
    directory = directory.resolve()
    interrupted = False

    def on_signal(signum, frame):
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    try:
        # The parent holds the lock until our PID identity is durable.
        with control_lock() as root:
            pointer = read_json(root / "current.json")
            if pointer.get("token") != token or pointer.get("worker_pid") != os.getpid():
                raise SessionError("Worker does not own this meeting session.")
        config = load_config()
        pipeline = ready(config)
        if mode == "record":
            record(directory, token, lambda: interrupted)
        if interrupted:
            raise SessionError("Meeting worker was stopped. Audio was kept; retry processing from the meeting folder.")
        update_session(directory, token, state="processing", message="Separating speakers and transcribing locally…")

        def progress(value):
            fields = value if isinstance(value, dict) else {"message": str(value)}
            # Only presentation progress belongs to the capture manifest.
            fields = {key: item for key, item in fields.items() if key in {"message", "progress"}}
            update_session(directory, token, **fields)

        result = pipeline.process_session(directory, config, progress_callback=progress)
        if interrupted:
            raise SessionError("Processing was interrupted. Audio was kept; retry from the meeting folder.")
        if not isinstance(result, dict) or not result.get("transcript_path"):
            raise SessionError("Processing returned no transcript. Audio was kept for retry.")
        update_session(directory, token, state="complete", message="Transcript saved.", completed_at=now(), **result)
        return 0
    except Exception as error:
        try:
            update_session(directory, token, state="failed", message=str(error), failed_at=now())
        except Exception:
            pass
        print(f"Meeting failed: {error}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] == "_capture":
        capture_exec(arguments[2:], int(arguments[1]))
        return 1
    parser = argparse.ArgumentParser(description="Record calls and save speaker-labelled transcripts locally.")
    parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("sources", "status", "stop"):
        sub.add_parser(name).add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    capture = sub.add_parser("start")
    capture.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    capture.add_argument("--mic")
    capture.add_argument("--monitor")
    capture.add_argument("--output-dir")
    capture.add_argument("--speakers", type=int)
    capture.add_argument("--title", default="")
    process = sub.add_parser("process")
    process.add_argument("session_dir", type=Path)
    process.add_argument("--speakers", type=int)
    process.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    internal = sub.add_parser("_worker", help=argparse.SUPPRESS)
    internal.add_argument("session_dir", type=Path)
    internal.add_argument("token")
    internal.add_argument("mode", choices=("record", "process"))
    args = parser.parse_args(argv)
    try:
        if args.command == "_worker":
            return worker(args.session_dir, args.token, args.mode)
        if args.command == "sources":
            result = sources()
        elif args.command == "status":
            result = status()
        elif args.command == "stop":
            result = stop()
        elif args.command == "start":
            result = start(load_config(), args.mic, args.monitor, args.output_dir, args.speakers, args.title)
        else:
            result = retry(args.session_dir, load_config(), args.speakers)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (SessionError, ConfigError, OSError, RuntimeError) as error:
        print(json.dumps({"state": "error", "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
