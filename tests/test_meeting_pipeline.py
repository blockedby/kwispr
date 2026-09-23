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
from contextlib import contextmanager, ExitStack

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

    def test_local_http_payload_keeps_vocabulary_without_dictation_language_or_prompt(self):
        requests = []
        authorization = []
        health_requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                health_requests.append(self.path)
                self.send_response(404); self.end_headers()
            def do_POST(self):
                requests.append(self.rfile.read(int(self.headers["Content-Length"])))
                authorization.append(self.headers.get("Authorization"))
                if self.path == "/redirect":
                    self.send_response(307); self.send_header("Location", "https://example.invalid/upload"); self.end_headers()
                else:
                    self.send_response(200); self.end_headers(); self.wfile.write(json.dumps({"text": " Готово. ", "language": "ru"}).encode())
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            config = {"KWISPR_API_URL": f"http://127.0.0.1:{server.server_port}/ok", "KWISPR_MODEL": "whisper-large-v3-turbo",
                      "KWISPR_LANGUAGE": "ru", "KWISPR_WHISPER_PROMPT": "Привет!", "KWISPR_VOCABULARY": "Kwispr, @secret;type=text/plain", "OPENAI_API_KEY": "test-legacy-key"}
            self.assertEqual(pipeline._request_transcript(b"wav-content", config), "Готово.")
            self.assertIn(b"Kwispr, @secret;type=text/plain", requests[0])
            self.assertNotIn("Привет!".encode(), requests[0])
            self.assertNotIn(b'name="language"', requests[0])
            self.assertIn(b"wav-content", requests[0])
            self.assertEqual(authorization, ["Bearer test-legacy-key"])
            self.assertEqual(health_requests, [])  # Default behavior has no probe.
            self.assertEqual(pipeline._request_transcription(b"probe", config, language="en", vocabulary=False),
                             {"text": "Готово.", "language": "ru"})
            self.assertIn(b'name="language"\r\n\r\nen', requests[1])
            self.assertNotIn(b'name="prompt"', requests[1])
            config["KWISPR_API_URL"] = f"http://127.0.0.1:{server.server_port}/redirect"
            with self.assertRaisesRegex(RuntimeError, "redirected"):
                pipeline._request_transcript(b"audio", config)
            config["KWISPR_API_URL"] = "https://api.openai.com/v1/audio/transcriptions"
            with self.assertRaises(ValueError):
                pipeline._request_transcript(b"audio", config)
        finally:
            server.shutdown(); server.server_close(); thread.join()

    @contextmanager
    def language_api(self, health):
        observed = {"health": [], "audio": [], "authorization": []}
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                observed["health"].append(self.path)
                observed["authorization"].append(self.headers.get("Authorization"))
                if health.get("redirect") and self.path == "/health":
                    self.send_response(307); self.send_header("Location", "/forwarded"); self.end_headers()
                    return
                self.send_response(health.get("status", 200)); self.end_headers()
                self.wfile.write(json.dumps(health.get("body", {})).encode())
            def do_POST(self):
                observed["audio"].append(self.rfile.read(int(self.headers["Content-Length"])))
                observed["authorization"].append(self.headers.get("Authorization"))
                self.send_response(200); self.end_headers()
                self.wfile.write(json.dumps({"text": "Обсудим React components.", "language": "ru"}).encode())
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1/audio/transcriptions", observed
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_allowed_languages_probe_once_preserves_config_and_explicit_language(self):
        with self.language_api({"body": {"capabilities": {"whisper_allowed_languages": True}}}) as (url, observed):
            config = {"KWISPR_API_URL": url, "KWISPR_WHISPER_ALLOWED_LANGUAGES": " RU, en, ru ",
                      "KWISPR_VOCABULARY": "React, Kwispr", "KWISPR_API_KEY": "private-test-key"}
            with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1", "no_proxy": ""}):
                prepared = pipeline._prepare_language_policy(config)
                answer = pipeline._request_transcription(b"audio", prepared, language="en")
                pipeline._request_transcription(b"audio", prepared, vocabulary=False)
            self.assertNotIn("_meeting_language_policy", config)
            self.assertEqual(observed["health"], ["/health"])
            self.assertEqual(answer["text"], "Обсудим React components.")  # No text/script filtering.
            for body in observed["audio"]:
                self.assertIn(b'name="allowed_languages"\r\n\r\nru,en', body)
            self.assertIn(b'name="language"\r\n\r\nen', observed["audio"][0])
            self.assertIn(b"React, Kwispr", observed["audio"][0])
            self.assertNotIn(b'name="prompt"', observed["audio"][1])
            self.assertEqual(observed["authorization"], ["Bearer private-test-key"] * 3)

    def test_allowed_language_old_server_and_health_redirect_fall_back_without_field(self):
        for health in ({"body": {"capabilities": {}}}, {"status": 404},
                       {"body": {"capabilities": {"whisper_allowed_languages": "true"}}},
                       {"redirect": True, "body": {"capabilities": {"whisper_allowed_languages": True}}}):
            with self.subTest(health=health), self.language_api(health) as (url, observed):
                config = {"KWISPR_API_URL": url, "KWISPR_WHISPER_ALLOWED_LANGUAGES": "ru,en"}
                answer = pipeline._request_transcription(b"audio", config)
                self.assertEqual(answer["language"], "ru")
                self.assertEqual(observed["health"], ["/health"])
                self.assertNotIn(b'name="allowed_languages"', observed["audio"][0])

    def test_non_whisper_model_ignores_language_candidates_and_cloud_is_rejected_before_probe(self):
        with self.language_api({}) as (url, observed):
            pipeline._request_transcription(b"audio", {"KWISPR_API_URL": url, "KWISPR_MODEL": "parakeet-tdt-0.6b-v3",
                                                     "KWISPR_WHISPER_ALLOWED_LANGUAGES": "ru,en"})
            self.assertEqual(observed["health"], [])
            self.assertNotIn(b'name="allowed_languages"', observed["audio"][0])
        with patch.object(pipeline.urllib.request, "build_opener") as build:
            with self.assertRaises(ValueError):
                pipeline._prepare_language_policy({"KWISPR_API_URL": "https://api.openai.com/v1/audio/transcriptions",
                                                   "KWISPR_WHISPER_ALLOWED_LANGUAGES": "ru,en"})
            build.assert_not_called()

    def test_server_upgrade_invalidates_unrestricted_checkpoint(self):
        segments = [{"start": 0, "end": 5, "speaker": "speaker_01"}]
        health = {"body": {"capabilities": {}}}
        with self.language_api(health) as (url, observed), self.processing_fixture(segments) as (root, config):
            config.update(KWISPR_API_URL=url, KWISPR_WHISPER_ALLOWED_LANGUAGES="ru,en")
            with patch.object(pipeline, "_diarize", side_effect=[segments, [], segments, []]):
                pipeline.process_session(root, config)
                old_signature = pipeline.read_json(root / "processing-checkpoint.json", {})["signature"]
                self.assertNotIn(b'name="allowed_languages"', observed["audio"][0])
                health["body"]["capabilities"]["whisper_allowed_languages"] = True
                pipeline.process_session(root, config)
                new_signature = pipeline.read_json(root / "processing-checkpoint.json", {})["signature"]
                self.assertNotEqual(old_signature, new_signature)
                self.assertIn(b'name="allowed_languages"\r\n\r\nru,en', observed["audio"][1])
                pipeline.process_session(root, config)
                self.assertEqual(len(observed["audio"]), 2)  # Same effective policy resumes.
                self.assertEqual(len(observed["health"]), 3)  # Recheck once each run.
                document = pipeline.read_json(root / "transcript.json", {})
                self.assertEqual(document["language_policy"], {"configured_allowed_languages": "ru,en",
                    "effective_allowed_languages": "ru,en", "supported": True})
                self.assertEqual(document["utterances"][0]["text"], "Обсудим React components.")

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
                      patch.object(pipeline, "_refine_remote_speakers", side_effect=lambda audio, rows, *args: (rows, {})),
                      patch.object(pipeline, "_wav_slice", return_value=b"audio"), patch.object(pipeline.time, "sleep")]
            with ExitStack() as stack:
                for item in common: stack.enter_context(item)
                stack.enter_context(patch.object(pipeline, "_diarize", return_value=segments))
                with patch.object(pipeline, "_request_transcription", side_effect=[{"text": "first"}, RuntimeError("offline")]):
                    with self.assertRaisesRegex(RuntimeError, "offline"):
                        pipeline.process_session(root, config)
                self.assertEqual((root / "remote.wav").read_bytes(), b"fixture")
                with patch.object(pipeline, "_request_transcription", return_value={"text": "retry"}) as request:
                    result = pipeline.process_session(root, config)
                self.assertEqual(request.call_count, 3)  # remaining remote + two mic spans
                document = json.loads(Path(result["transcript_json"]).read_text())
                self.assertIn("first", [row["text"] for row in document["utterances"]])

    @contextmanager
    def processing_fixture(self, remote, microphone=()):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary)
            for name in ("remote.wav", "microphone.wav"):
                (root / name).write_bytes(b"fixture")
            pipeline.atomic_json(root / "session.json", {"title": "Call", "speakers": 2})
            config = {"KWISPR_API_URL": "http://127.0.0.1:19650/v1/audio/transcriptions"}
            for item in [patch.object(pipeline, "require_ready"), patch.object(pipeline, "_load_track", return_value=[0] * 800000),
                         patch.object(pipeline, "split_intervals", side_effect=lambda intervals, audio: intervals),
                         patch.object(pipeline, "_wav_slice", return_value=b"audio"), patch.object(pipeline.time, "sleep"),
                         patch.object(pipeline, "_refine_remote_speakers", side_effect=lambda audio, rows, *args: (rows, {})),
                         patch.object(pipeline, "_diarize", side_effect=[remote, microphone])]:
                stack.enter_context(item)
            yield root, config

    def test_known_remote_count_refinement_resumes_and_warns_without_inventing_speaker(self):
        segments = [{"start": 0, "end": 5, "speaker": "speaker_01"}]
        refined = [{"start": 0, "end": 5, "speakers": ["speaker_unknown"], "speaker_assignment": "uncertain"}]
        report = {"status": "uncertain", "requested_speakers": 2, "supported_profiles": 0,
                  "unknown_seconds": 5, "warnings": ["Insufficient voice separation"]}
        with self.processing_fixture(segments, segments) as (root, config):
            with patch.object(pipeline, "_refine_remote_speakers", return_value=(refined, report)) as refine, \
                    patch.object(pipeline, "_request_transcription", return_value={"text": "Сохранённая речь", "language": "ru"}) as request:
                first = pipeline.process_session(root, config)
                pipeline.process_session(root, config)
            self.assertEqual(refine.call_count, 1)
            self.assertEqual(refine.call_args.args[2], 2)
            self.assertEqual(request.call_count, 2)
            self.assertEqual(first["speaker_count"], 0)
            self.assertTrue(first["has_unknown_speaker"])
            document = pipeline.read_json(root / "transcript.json", {})
            self.assertEqual(document["diarization_report"], report)
            remote = next(row for row in document["utterances"] if row["track"] == "remote")
            self.assertEqual(remote["source_intervals"][0]["speaker_assignment"], "uncertain")
            self.assertEqual(remote["text"], "Сохранённая речь")
            self.assertIn("не удалось надёжно подтвердить", (root / "transcript.md").read_text())

    def test_auto_count_leaves_native_diarization_unchanged(self):
        segments = [{"start": 0, "end": 5, "speaker": "speaker_03"}]
        with self.processing_fixture(segments) as (root, config):
            pipeline.atomic_json(root / "session.json", {"speakers": 0})
            with patch.object(pipeline, "_refine_remote_speakers") as refine, \
                    patch.object(pipeline, "_request_transcription", return_value={"text": "Речь"}):
                pipeline.process_session(root, config)
            refine.assert_not_called()
            self.assertEqual(pipeline.read_json(root / "transcript.json", {})["utterances"][0]["speakers"], ["speaker_03"])

    def test_auto_tracks_follow_long_turn_language_switches_and_anchor_short_turns(self):
        segments = [{"start": start, "end": end, "speaker": "speaker_01"}
                    for start, end in [(0, 5), (6, 7), (8, 13), (14, 15)]]
        responses = [{"text": "English sentence", "language": "en"}, {"text": "Yes", "language": "en"},
                     {"text": "Русская реплика with React", "language": "ru"}, {"text": "Да", "language": "ru"}]
        with self.processing_fixture(segments) as (root, config):
            config.update(KWISPR_LANGUAGE="zh", KWISPR_WHISPER_PROMPT="ignored dictation context")
            with patch.object(pipeline, "_request_transcription", side_effect=responses) as request:
                pipeline.process_session(root, config)
            self.assertEqual([call.kwargs["language"] for call in request.call_args_list], ["", "en", "", "ru"])
            document = json.loads((root / "transcript.json").read_text())
            self.assertEqual(document["track_languages"]["remote"]["detected"], ["en", "ru"])
            self.assertEqual(document["utterances"][2]["text"], "Русская реплика with React")

    def test_explicit_languages_are_independent_for_the_two_tracks(self):
        segments = [{"start": 0, "end": 5, "speaker": "speaker_01"}]
        with self.processing_fixture(segments, segments) as (root, config):
            config.update(KWISPR_MEETING_REMOTE_LANGUAGE="en", KWISPR_MEETING_MIC_LANGUAGE="ru", KWISPR_LANGUAGE="zh")
            with patch.object(pipeline, "_request_transcription", side_effect=[{"text": "Hello"}, {"text": "Привет"}]) as request:
                pipeline.process_session(root, config)
            self.assertEqual([call.kwargs["language"] for call in request.call_args_list], ["en", "ru"])
            document = json.loads((root / "transcript.json").read_text())
            self.assertEqual(document["track_languages"]["remote"]["requested"], "en")
            self.assertEqual(document["track_languages"]["microphone"]["requested"], "ru")

    def test_short_first_turn_uses_unprompted_representative_probe(self):
        segments = [{"start": 0, "end": 1, "speaker": "speaker_01"},
                    {"start": 2, "end": 14, "speaker": "speaker_01"}]
        responses = [{"text": "Probe text is not an output", "language": "en"},
                     {"text": "Yes", "language": "en"}, {"text": "Longer actual turn", "language": "en"}]
        with self.processing_fixture(segments) as (root, config):
            with patch.object(pipeline, "_request_transcription", side_effect=responses) as request:
                pipeline.process_session(root, config)
            self.assertFalse(request.call_args_list[0].kwargs["vocabulary"])
            self.assertEqual(request.call_args_list[1].kwargs["language"], "en")
            self.assertEqual(request.call_args_list[2].kwargs["language"], "")
            document = json.loads((root / "transcript.json").read_text())
            self.assertEqual([row["text"] for row in document["utterances"]], ["Yes", "Longer actual turn"])

    def test_servers_without_language_metadata_keep_auto_and_retry_restores_context(self):
        segments = [{"start": 0, "end": 5, "speaker": "speaker_01"},
                    {"start": 6, "end": 7, "speaker": "speaker_01"}]
        with self.processing_fixture(segments) as (root, config):
            with patch.object(pipeline, "_request_transcription", side_effect=[{"text": "Long"}, {"text": "Short"}]) as request:
                pipeline.process_session(root, config)
            self.assertEqual([call.kwargs["language"] for call in request.call_args_list], ["", ""])
        with self.processing_fixture(segments) as (root, config):
            with patch.object(pipeline, "_request_transcription", side_effect=[{"text": "Long", "language": "en"}, RuntimeError("offline")]):
                with self.assertRaisesRegex(RuntimeError, "offline"):
                    pipeline.process_session(root, config)
            with patch.object(pipeline, "_request_transcription", return_value={"text": "Retry", "language": "en"}) as request:
                pipeline.process_session(root, config)
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.kwargs["language"], "en")

    def test_fingerprint_tracks_meeting_languages_not_dictation_hints(self):
        with self.processing_fixture([]) as (root, config):
            original = pipeline._fingerprint(root, config, {})
            self.assertEqual(original, pipeline._fingerprint(root, {**config, "KWISPR_LANGUAGE": "ru", "KWISPR_WHISPER_PROMPT": "Example"}, {}))
            for key, value in [("KWISPR_MEETING_MIC_LANGUAGE", "ru"), ("KWISPR_MEETING_REMOTE_LANGUAGE", "en"),
                               ("KWISPR_VOCABULARY", "React"), ("KWISPR_WHISPER_ALLOWED_LANGUAGES", "ru,en")]:
                self.assertNotEqual(original, pipeline._fingerprint(root, {**config, key: value}, {}))

    def test_unconfigured_runtime_is_actionable(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "Set up meeting models"):
                pipeline.require_ready({"KWISPR_MEETING_RUNTIME_DIR": temporary})


if __name__ == "__main__":
    unittest.main()
