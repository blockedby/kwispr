"""Behavioral contracts for file-only meeting processing and resumable jobs."""
from __future__ import annotations

import io
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
import wave

from kwispr_meetings import pipeline


class MeetingPipelineTest(unittest.TestCase):
    def test_config_only_runtime_override_is_shared_by_setup_and_launcher(self):
        def module(filename):
            spec = importlib.util.spec_from_file_location(filename.replace('-', '_'), Path(__file__).resolve().parents[1] / filename)
            value = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(value)
            return value
        setup = module('kwispr-meetings-setup.py')
        launcher = module('kwispr-meetings.py')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            custom = root / 'custom runtime'
            python = custom / 'venv/bin/python'
            python.parent.mkdir(parents=True)
            python.touch()
            config = root / 'config.env'
            config.write_text(f"KWISPR_MEETING_RUNTIME_DIR='{custom}'\n")
            with patch.dict(os.environ, {'KWISPR_CONFIG_FILE': str(config)}, clear=True):
                with patch.object(setup.sys, 'argv', ['setup']), patch.object(setup, 'install') as install:
                    self.assertEqual(setup.main(), 0)
                    install.assert_called_once_with(custom)
                with patch.object(launcher.os, 'execv', side_effect=RuntimeError('reexec')) as execute:
                    with self.assertRaisesRegex(RuntimeError, 'reexec'):
                        launcher.main()
                    self.assertEqual(execute.call_args.args[0], str(python))

    def test_overlap_is_explicit_and_speaker_return_keeps_identity(self):
        result = pipeline.speech_intervals([
            {"start": 0, "end": 2, "speaker": "speaker_01"},
            {"start": 1, "end": 3, "speaker": "speaker_02"},
            {"start": 4, "end": 5, "speaker": "speaker_01"},
        ], 6)
        self.assertEqual(result, [
            {"start": 0, "end": 1, "speakers": ["speaker_01"]},
            {"start": 1, "end": 2, "speakers": ["speaker_01", "speaker_02"]},
            {"start": 2, "end": 3, "speakers": ["speaker_02"]},
            {"start": 4, "end": 5, "speakers": ["speaker_01"]},
        ])

    def test_own_microphone_does_not_invent_additional_people(self):
        result = pipeline.speech_intervals([
            {"start": 0, "end": 2, "speaker": "speaker_01"},
            {"start": 2.1, "end": 3, "speaker": "speaker_02"},
        ], 3, own_microphone=True)
        self.assertEqual(result, [{"start": 0, "end": 3, "speakers": ["self"]}])

    def test_markdown_and_json_keep_timing_overlap_names_and_empty_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = [
                {"start": 3661.8, "end": 3663.2, "speakers": ["speaker_01", "speaker_02"], "track": "remote", "text": "Привет [ссылка](https://example.com)."},
                {"start": 0, "end": 2, "speakers": ["self"], "track": "microphone", "text": ""},
            ]
            result = pipeline.write_outputs(root, {"title": "Встреча\n# заголовок", "started_at": "2026-09-21T12:00:00Z"}, rows, {"speaker_01": "Аня"})
            markdown = Path(result["transcript_path"]).read_text()
            document = json.loads(Path(result["transcript_json"]).read_text())
            self.assertIn("[01:01:01–01:01:03] Аня + Собеседник 2", markdown)
            self.assertIn("говорят одновременно", markdown)
            self.assertIn("Речь обнаружена, текст не распознан", markdown)
            self.assertIn(r"\[ссылка\]", markdown)
            self.assertEqual(document["utterances"][0]["speakers"], ["self"])
            self.assertEqual(document["utterances"][1]["text"], rows[0]["text"])
            self.assertEqual(Path(result["transcript_path"]).stat().st_mode & 0o777, 0o600)
            self.assertEqual(Path(result["transcript_json"]).stat().st_mode & 0o777, 0o600)

    def test_local_http_payload_uses_shared_hints_and_avoids_redirects(self):
        requests = []
        authorization = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(self.rfile.read(int(self.headers["Content-Length"])))
                authorization.append(self.headers.get("Authorization"))
                if self.path == "/redirect":
                    self.send_response(307); self.send_header("Location", "https://example.invalid/upload"); self.end_headers()
                else:
                    self.send_response(200); self.end_headers(); self.wfile.write(json.dumps({"text": " Готово. "}).encode())
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            config = {"KWISPR_API_URL": f"http://127.0.0.1:{server.server_port}/ok", "KWISPR_MODEL": "whisper-large-v3-turbo",
                      "KWISPR_WHISPER_PROMPT": "Привет!", "KWISPR_VOCABULARY": "Kwispr, @secret;type=text/plain", "OPENAI_API_KEY": "test-legacy-key"}
            self.assertEqual(pipeline._request_transcript(b"wav-content", config), "Готово.")
            self.assertIn("Привет!\nKwispr, @secret;type=text/plain".encode(), requests[0])
            self.assertIn(b"wav-content", requests[0])
            self.assertEqual(authorization, ["Bearer test-legacy-key"])
            config["KWISPR_API_URL"] = f"http://127.0.0.1:{server.server_port}/redirect"
            with self.assertRaisesRegex(RuntimeError, "redirected"):
                pipeline._request_transcript(b"audio", config)
            config["KWISPR_API_URL"] = "https://api.openai.com/v1/audio/transcriptions"
            with self.assertRaises(ValueError):
                pipeline._request_transcript(b"audio", config)
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_failed_processing_keeps_progress_and_retry_skips_completed_turn(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ["remote.wav", "microphone.wav"]:
                (root / name).write_bytes(b"fixture")
            pipeline.atomic_json(root / "session.json", {"title": "Call", "speakers": 2})
            config = {"KWISPR_API_URL": "http://127.0.0.1:19650/v1/audio/transcriptions"}
            segments = [{"start": 0, "end": 2, "speaker": "speaker_01"}, {"start": 3, "end": 5, "speaker": "speaker_02"}]
            common = [patch.object(pipeline, "require_ready"), patch.object(pipeline, "_load_track", return_value=[0] * 160000),
                      patch.object(pipeline, "split_intervals", side_effect=lambda intervals, audio: intervals),
                      patch.object(pipeline, "_wav_slice", return_value=b"audio"), patch.object(pipeline.time, "sleep")]
            from contextlib import ExitStack
            with ExitStack() as stack:
                for item in common: stack.enter_context(item)
                stack.enter_context(patch.object(pipeline, "_diarize", return_value=segments))
                with patch.object(pipeline, "_request_transcript", side_effect=["first", RuntimeError("offline")]):
                    with self.assertRaisesRegex(RuntimeError, "offline"):
                        pipeline.process_session(root, config)
                self.assertEqual((root / "remote.wav").read_bytes(), b"fixture")
                with patch.object(pipeline, "_request_transcript", return_value="retry") as request:
                    result = pipeline.process_session(root, config)
                self.assertEqual(request.call_count, 3)  # remaining remote + two mic spans
                document = json.loads(Path(result["transcript_json"]).read_text())
                self.assertIn("first", [row["text"] for row in document["utterances"]])

    def test_unconfigured_runtime_is_actionable(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "Set up meeting models"):
                pipeline.require_ready({"KWISPR_MEETING_RUNTIME_DIR": temporary})


if __name__ == "__main__":
    unittest.main()
