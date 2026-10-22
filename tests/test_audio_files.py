"""Imported audio contract: selected backend/track, decoding, and durable retries."""
from __future__ import annotations

import json
import hashlib
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
    def _video(self, root: Path, extension: str, *, audio: bool = True) -> Path:
        source = root / f"видео с пробелами{extension}"
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                   "-f", "lavfi", "-i", "color=c=black:s=32x32:r=10:d=1.4"]
        if audio:
            command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=1.4"]
        command += ["-c:v", "mpeg4"]
        if audio:
            command += ["-c:a", "aac"]
        command.append(str(source))
        subprocess.run(command, check=True)
        return source

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

    def test_cloud_files_inherit_meetings_without_local_stt_and_resume_without_rebilling(self):
        from kwispr_meetings import openrouter as cloud
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            before = source.read_bytes()
            config = root / "config.env"
            config.write_text("KWISPR_BACKEND=openrouter-chat\nKWISPR_MODEL=google/dictation\n"
                              "KWISPR_MEETING_PROVIDER=openrouter\nKWISPR_MEETING_OPENROUTER_KEY=fixture-secret\n"
                              "KWISPR_MEETING_CLOUD_MODEL=google/gemini-test\n")
            def reply(raw, turns, values):
                self.assertEqual(values["KWISPR_MEETING_CLOUD_MODEL"], "google/gemini-test")
                self.assertEqual(turns[0]["speakers"], [])
                self.assertLessEqual(turns[0]["end"], 45)
                return {"text":json.dumps({"turns":[{"id":"file","text":"Текст записи"}]}),
                        "finish_reason":"stop","usage":{"cost":.01}}
            with patch.dict(os.environ, {"KWISPR_CONFIG_FILE":str(config)}, clear=True), \
                 patch.object(cloud, "_request", side_effect=reply) as request, \
                 patch.object(audio_files, "_prepare_language_policy") as health, \
                 patch.object(audio_files, "_request_transcription") as local:
                first = transcribe(source, root / "results")
                second = transcribe(source, root / "results")
                self.assertEqual(request.call_count, 2)
                self.assertEqual(first, second)
                self.assertEqual(first["processing_provider"], "openrouter")
                self.assertAlmostEqual(first["cloud_cost_usd"], .02)
                health.assert_not_called()
                local.assert_not_called()
            document = json.loads(Path(first["transcript_json"]).read_text())
            self.assertEqual(document["chunks"][0]["start"], 0)
            self.assertEqual(document["chunks"][0]["end"], document["chunks"][1]["start"])
            self.assertAlmostEqual(document["chunks"][-1]["end"], 65, places=2)
            self.assertNotIn("fixture-secret", Path(first["transcript_json"]).read_text())
            self.assertEqual(source.read_bytes(), before)

    def test_selected_audio_track_is_decoded_and_has_separate_results(self):
        from kwispr_meetings import openrouter as cloud
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "two tracks.mkv"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono:d=1",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=1",
                "-map", "0:a", "-map", "1:a", "-c:a", "pcm_s16le", str(source)], check=True)
            config = root / "config.env"
            config.write_text("KWISPR_MEETING_OPENROUTER_KEY=fixture-secret\n")
            def reply(raw, turns, values):
                import io
                with wave.open(io.BytesIO(raw), "rb") as reader:
                    pcm = reader.readframes(reader.getnframes())
                text = "sound" if any(pcm) else "silence"
                return {"text":json.dumps({"turns":[{"id":"file","text":text}]}),
                        "finish_reason":"stop","usage":{"cost":.001}}
            with patch.dict(os.environ, {"KWISPR_CONFIG_FILE":str(config)}, clear=True), \
                 patch.object(cloud, "_request", side_effect=reply):
                first = transcribe(source, root / "results", provider="openrouter", audio_stream=0)
                second = transcribe(source, root / "results", provider="openrouter", audio_stream=1)
                self.assertEqual((first["text"], second["text"]), ("silence", "sound"))
                self.assertNotEqual(first["output_dir"], second["output_dir"])
                with self.assertRaisesRegex(RuntimeError, "no audio track"):
                    transcribe(source, root / "results", provider="openrouter", audio_stream=2)
                with self.assertRaises(ConfigError):
                    transcribe(source, root / "results", provider="openrouter", audio_stream=-1)

    def test_cloud_file_budget_and_unknown_request_preserve_retry_state(self):
        from kwispr_meetings import openrouter as cloud
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            config = root / "config.env"
            config.write_text("KWISPR_MEETING_PROVIDER=openrouter\nKWISPR_MEETING_OPENROUTER_KEY=fixture-secret\n")
            with patch.dict(os.environ, {"KWISPR_CONFIG_FILE":str(config)}, clear=True), \
                 patch.object(cloud, "_request", side_effect=TimeoutError("network")) as request:
                with self.assertRaisesRegex(RuntimeError, "budget"):
                    transcribe(source, root / "results", cloud_budget=.001)
                request.assert_not_called()
                with self.assertRaisesRegex(RuntimeError, "unknown"):
                    transcribe(source, root / "results")
                count = request.call_count
                with self.assertRaisesRegex(RuntimeError, "unknown"):
                    transcribe(source, root / "results")
                self.assertEqual(request.call_count, count)

    def test_local_files_accept_non_whisper_stt_models(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            config = root / "config.env"
            config.write_text("KWISPR_MEETING_PROVIDER=local\nKWISPR_MEETING_BACKEND=openai-transcriptions\n"
                              "KWISPR_MEETING_API_URL=http://localhost:19550/v1/audio/transcriptions\n"
                              "KWISPR_MEETING_MODEL=Qwen3-ASR-1.7B\n")
            with patch.dict(os.environ, {"KWISPR_CONFIG_FILE":str(config)}, clear=True), \
                 patch.object(audio_files, "_request_transcription", return_value={"text":"Local Qwen"}) as request:
                result = transcribe(source, root / "results")
                self.assertEqual(result["text"], "Local Qwen")
                self.assertEqual(request.call_args.args[1]["KWISPR_MODEL"], "Qwen3-ASR-1.7B")

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

    def test_mp4_and_mkv_video_transcribe_first_audio_track(self):
        observed = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"capabilities":{"whisper_allowed_languages":true}}')

            def do_POST(self):
                observed.append(self.rfile.read(int(self.headers["Content-Length"])))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"text":"local transcript","language":"en"}')

            def log_message(self, *args):
                pass

        with tempfile.TemporaryDirectory() as temporary, ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                root = Path(temporary)
                config = root / "config.env"
                config.write_text("KWISPR_BACKEND=openai-transcriptions\n"
                                  f"KWISPR_API_URL=http://127.0.0.1:{server.server_port}/v1/audio/transcriptions\n"
                                  "KWISPR_MODEL=whisper-large-v3-turbo\n")
                with patch.dict(os.environ, {"KWISPR_CONFIG_FILE": str(config)}, clear=True):
                    for extension in (".mp4", ".mkv"):
                        source = self._video(root, extension)
                        before = hashlib.sha256(source.read_bytes()).digest()
                        result = transcribe(source, root / "results")
                        document = json.loads(Path(result["transcript_json"]).read_text())
                        self.assertEqual(result["source_path"], str(source))
                        self.assertEqual(document["source_path"], str(source))
                        self.assertEqual(result["text"], "local transcript")
                        self.assertEqual(Path(result["transcript_path"]).read_text(), "local transcript\n")
                        self.assertEqual(len(document["chunks"]), 1)
                        self.assertGreater(document["chunks"][0]["end"], 1.25)
                        self.assertLess(document["chunks"][0]["end"], 1.55)
                        self.assertEqual(hashlib.sha256(source.read_bytes()).digest(), before)
                    self.assertEqual(len(observed), 2)
                    self.assertTrue(all(b'name="allowed_languages"\r\n\r\nru,en' in post for post in observed))
                    silent = self._video(root, ".mov", audio=False)
                    before = hashlib.sha256(silent.read_bytes()).digest()
                    with self.assertRaisesRegex(RuntimeError, "no audio track to transcribe"):
                        transcribe(silent, root / "results")
                    command = [sys.executable, str(Path(__file__).resolve().parents[1] / "kwispr-files.py"),
                               "transcribe", str(silent), "--output-dir", str(root / "results")]
                    cli = subprocess.run(command, capture_output=True, text=True, env=os.environ.copy())
                    self.assertNotEqual(cli.returncode, 0)
                    self.assertIn("no audio track to transcribe", cli.stderr)
                    self.assertEqual(json.loads(cli.stdout.splitlines()[-1])["event"], "error")
                    self.assertEqual(hashlib.sha256(silent.read_bytes()).digest(), before)
                    self.assertEqual(len(observed), 2)
            finally:
                server.shutdown()
                worker.join()


if __name__ == "__main__":
    unittest.main()
