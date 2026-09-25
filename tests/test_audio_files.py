"""Imported audio contract: local-only requests, decoding, and durable retries."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from kwispr_meetings import audio_files
from kwispr_meetings.audio_files import _convert, _join_text, _plan_chunks, transcribe
from kwispr_meetings.config import ConfigError, load_config


class AudioFilesTest(unittest.TestCase):
    def _source(self, root: Path) -> Path:
        wav = root / "голос с пробелами.wav"
        # 65 seconds, with short quiet stretches at safe chunk boundaries.
        with wave.open(str(wav), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16000)
            loud = (1000).to_bytes(2, "little", signed=True) * 16000
            quiet = b"\0" * 32000
            for second in range(65):
                writer.writeframes(quiet if second in {27, 54} else loud)
        output = root / "голос с пробелами.ogg"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                        "-i", str(wav), "-c:a", "libopus", str(output)], check=True)
        return output

    def test_ogg_conversion_local_requests_and_resume_after_failure(self):
        observed = {"posts": [], "fail_once": True}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"capabilities": {"whisper_allowed_languages": True}}).encode())

            def do_POST(self):
                payload = self.rfile.read(int(self.headers["Content-Length"]))
                observed["posts"].append(payload)
                if len(observed["posts"]) == 2 and observed["fail_once"]:
                    observed["fail_once"] = False
                    self.send_response(503)
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"text": f"слово {len(observed['posts'])}", "language": "ru"}).encode())

            def log_message(self, *args):
                pass

        with tempfile.TemporaryDirectory() as temporary, ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            root = Path(temporary)
            source = self._source(root)
            original_hash = source.read_bytes()
            config = root / "config.env"
            config.write_text("KWISPR_BACKEND=openai-transcriptions\n"
                              f"KWISPR_API_URL=http://127.0.0.1:{server.server_port}/v1/audio/transcriptions\n"
                              "KWISPR_MODEL=whisper-large-v3-turbo\n"
                              "KWISPR_MEETING_MODEL=wrong-meeting-model\n")
            with patch.dict(os.environ, {"KWISPR_CONFIG_FILE": str(config)}, clear=True), patch.object(audio_files, "MAX_SECONDS", 30), patch.object(audio_files, "SEARCH_SECONDS", 6):
                with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
                    transcribe(source, root / "results")
                self.assertEqual(len(observed["posts"]), 2)
                progress = []
                result = transcribe(source, root / "results", lambda *args: progress.append(args))
                self.assertEqual(result["state"], "complete")
                self.assertEqual(result["source_path"], str(source))
                self.assertEqual(len(observed["posts"]), 4)  # first completed chunk reused
                self.assertEqual(progress[-1][1:], (3, 3))
                self.assertEqual(Path(result["transcript_path"]).read_text().strip(), result["text"])
                document = json.loads(Path(result["transcript_json"]).read_text())
                self.assertEqual(len(document["chunks"]), 3)
                self.assertEqual(document["language_policy"]["effective_allowed_languages"], "ru,en")
                self.assertTrue(all(b'name="allowed_languages"\r\n\r\nru,en' in post for post in observed["posts"]))
                self.assertTrue(all(b'name="model"\r\n\r\nwhisper-large-v3-turbo' in post for post in observed["posts"]))
                self.assertEqual(source.read_bytes(), original_hash)
                self.assertEqual(Path(result["transcript_path"]).stat().st_mode & 0o777, 0o600)
                command = [sys.executable, str(Path(__file__).resolve().parents[1] / "kwispr-files.py"),
                           "transcribe", str(source), "--output-dir", str(root / "results")]
                cli = subprocess.run(command, capture_output=True, text=True, env=os.environ.copy())
                self.assertEqual(cli.returncode, 0, cli.stderr)
                events = [json.loads(line) for line in cli.stdout.splitlines()]
                self.assertTrue(all(event["event"] == "progress" for event in events[:-1]))
                self.assertEqual(events[-1]["state"], "complete")
                self.assertEqual(events[-1]["source_path"], str(source))
                self.assertEqual(events[-1]["text"], "слово 5")
                self.assertEqual(len(observed["posts"]), 5)
            server.shutdown()
            worker.join()

    def test_cli_rejects_cloud_config_before_audio_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.env"
            config.write_text("KWISPR_BACKEND=openrouter-chat\n"
                              "KWISPR_API_URL=https://api.openai.com/v1/audio/transcriptions\n"
                              "KWISPR_MODEL=whisper-large-v3-turbo\n"
                              "KWISPR_MEETING_BACKEND=openai-transcriptions\n"
                              "KWISPR_MEETING_API_URL=http://localhost:19550/v1/audio/transcriptions\n")
            with patch.dict(os.environ, {"KWISPR_CONFIG_FILE": str(config)}, clear=True):
                general = load_config(meeting_overrides=False)
                self.assertEqual(general["KWISPR_BACKEND"], "openrouter-chat")
                command = [sys.executable, str(Path(__file__).resolve().parents[1] / "kwispr-files.py"),
                           "transcribe", str(config), "--output-dir", str(root / "out")]
                result = subprocess.run(command, capture_output=True, text=True, env=os.environ.copy())
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("local STT", result.stderr)
                self.assertFalse((root / "out").exists())

    def test_chunks_preserve_all_samples_and_words(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "audio.wav"
            with wave.open(str(path), "wb") as writer:
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(16000)
                writer.writeframes((1000).to_bytes(2, "little", signed=True) * (70 * 16000))
            with patch.object(audio_files, "MAX_SECONDS", 30), patch.object(audio_files, "SEARCH_SECONDS", 6):
                chunks = _plan_chunks(path)
            self.assertEqual(chunks[0]["start_frame"], 0)
            self.assertEqual(chunks[-1]["end_frame"], 70 * 16000)
            self.assertTrue(all(right["start_frame"] <= left["end_frame"]
                                for left, right in zip(chunks, chunks[1:])))
            self.assertEqual(_join_text(["да да", "да ещё"]), "да да да ещё")

    def test_four_minute_file_is_one_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "audio.wav"
            with wave.open(str(path), "wb") as writer:
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(16000)
                writer.writeframes(b"\0" * (244 * 32000))
            self.assertEqual(_plan_chunks(path), [{"start_frame": 0, "end_frame": 244 * 16000}])

    def test_mp3_and_wav_decode_to_local_pcm(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "voice.wav"
            with wave.open(str(source), "wb") as writer:
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(16000)
                writer.writeframes(b"\0" * 32000)
            mp3 = root / "voice.mp3"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                            "-i", str(source), str(mp3)], check=True)
            for index, item in enumerate((source, mp3)):
                converted = root / f"converted-{index}.wav"
                _convert(item, converted)
                with wave.open(str(converted), "rb") as reader:
                    self.assertEqual((reader.getnchannels(), reader.getsampwidth(), reader.getframerate()), (1, 2, 16000))
                    self.assertGreater(reader.getnframes(), 0)
                self.assertEqual(converted.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
