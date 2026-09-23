import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import wave


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "reprocess_meeting_openrouter.py"
spec = importlib.util.spec_from_file_location("meeting_cloud_reprocess", SCRIPT)
cloud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud)


def wav(path, frames):
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\x00\x00" * frames)


class CloudReprocessTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.session = self.root / "saved-meeting"
        self.session.mkdir()
        self.output = self.root / "cloud"
        (self.session / "session.json").write_text(json.dumps({"title": "Звонок", "started_at": "2026-09-23T12:00:00Z"}))
        self.plan = self.root / "plan.json"
        self.plan.write_text(json.dumps({"model": "google/gemini-test", "prompt": "Transcribe verbatim.",
                                         "max_output_tokens": 8192, "reasoning_effort": "minimal"}))
        self.key = self.root / "key"
        self.key.write_text("OPENROUTER_API_KEY=fixture-secret\n")

    def args(self, *extra):
        return ["--session", str(self.session), "--output-root", str(self.output),
                "--plan", str(self.plan), "--key-file", str(self.key), *extra]

    def test_exact_coverage_including_silent_frames_and_speaker_labels(self):
        wav(self.session / "microphone.wav", cloud.WINDOW + 13)
        wav(self.session / "remote.wav", 123)
        original = [(self.session / name).read_bytes() for name in ("microphone.wav", "remote.wav")]
        output, metadata, sources, items = cloud._session_items(self.session, self.output, cloud._plan(self.plan))
        mic = [item for item in items if item["track"] == "microphone"]
        self.assertEqual([(item["first_frame"], item["last_frame"]) for item in mic],
                         [(0, cloud.WINDOW), (cloud.WINDOW, cloud.WINDOW + 13)])
        self.assertEqual(len(items), 3)
        self.assertEqual([item["chunk_sha256"] for item in mic],
                         [cloud.hashlib.sha256(cloud._chunk_bytes(self.session / "microphone.wav", a, b)).hexdigest()
                          for a, b in [(0, cloud.WINDOW), (cloud.WINDOW, cloud.WINDOW + 13)]])
        self.assertEqual(original, [(self.session / name).read_bytes() for name in ("microphone.wav", "remote.wav")])
        self.assertEqual(sources["remote"]["frames"], 123)
        self.assertEqual(metadata["title"], "Звонок")
        self.assertFalse(output.exists())

    def test_execute_cache_and_complete_transcript_without_recharge(self):
        wav(self.session / "microphone.wav", 20)
        wav(self.session / "remote.wav", 24)
        def response(*args, **kwargs):
            return ("Привет" if len(args[4]) == len(cloud._chunk_bytes(self.session / "microphone.wav", 0, 20)) else "Здравствуйте",
                    "stop", {"cost": 0.01}, "gen-fixture")
        with mock.patch.object(cloud.benchmark, "_request", side_effect=response) as request:
            self.assertEqual(cloud.main(self.args("--execute")), 0)
            self.assertEqual(request.call_count, 2)
        output = next(self.output.iterdir())
        doc = json.loads((output / "transcript.json").read_text())
        self.assertTrue(doc["complete"])
        self.assertEqual({row["track"]: row["speakers"] for row in doc["utterances"]},
                         {"microphone": ["self"], "remote": ["speaker_01"]})
        self.assertEqual(doc["speakers"], {"self": "Я", "speaker_01": "Собеседник 1"})
        self.assertIn("Собеседник 1", (output / "transcript.md").read_text())
        with mock.patch.object(cloud.benchmark, "_request", side_effect=AssertionError("charged again")):
            self.assertEqual(cloud.main(self.args("--execute")), 0)
        self.assertEqual(len(list((output / "results").glob("*.json"))), 2)
        self.assertFalse(list((output / "results").glob("*.attempt")))

    def test_unknown_attempt_halts_before_http(self):
        wav(self.session / "microphone.wav", 20)
        wav(self.session / "remote.wav", 0)
        output, _, _, items = cloud._session_items(self.session, self.output, cloud._plan(self.plan))
        (output / "results").mkdir(parents=True)
        cloud.benchmark._private_json(cloud._marker_path(output, items[0]), {"identity": items[0]["identity"]})
        with mock.patch.object(cloud.benchmark, "_request", side_effect=AssertionError("must not send")):
            with self.assertRaises(SystemExit):
                cloud.main(self.args("--execute"))
        self.assertTrue(cloud._marker_path(output, items[0]).exists())

    def test_dry_run_never_reads_key_or_writes_output(self):
        wav(self.session / "microphone.wav", 2)
        wav(self.session / "remote.wav", 3)
        with mock.patch.object(cloud.benchmark, "read_key_file", side_effect=AssertionError("key read")), \
             mock.patch.object(cloud.benchmark, "_request", side_effect=AssertionError("HTTP")):
            self.assertEqual(cloud.main(self.args()), 0)
        self.assertFalse(self.output.exists())

    def test_cached_costs_across_sessions_count_before_new_requests(self):
        wav(self.session / "microphone.wav", 2)
        wav(self.session / "remote.wav", 0)
        second = self.root / "second-meeting"
        second.mkdir()
        (second / "session.json").write_text('{"title":"Second"}')
        wav(second / "microphone.wav", 2)
        wav(second / "remote.wav", 0)
        with mock.patch.object(cloud.benchmark, "_request", return_value=("hi", "stop", {"cost": 0.04}, None)):
            self.assertEqual(cloud.main(self.args("--execute")), 0)
            self.assertEqual(cloud.main(["--session", str(second), "--output-root", str(self.output),
                                         "--plan", str(self.plan), "--key-file", str(self.key), "--execute"]), 0)
        with mock.patch.object(cloud.benchmark, "_request", side_effect=AssertionError("charged")):
            with self.assertRaises(SystemExit):
                cloud.main(["--session", str(self.session), "--session", str(second),
                            "--output-root", str(self.output), "--plan", str(self.plan),
                            "--key-file", str(self.key), "--execute", "--budget-usd", "0.07"])

    def test_soft_budget_and_incomplete_result(self):
        wav(self.session / "microphone.wav", cloud.WINDOW + 1)
        wav(self.session / "remote.wav", 0)
        with mock.patch.object(cloud.benchmark, "_request", side_effect=AssertionError("insufficient reserve")):
            with self.assertRaises(SystemExit):
                cloud.main(self.args("--execute", "--budget-usd", "0.05"))
        output = next(self.output.iterdir())
        self.assertFalse(list((output / "results").glob("*.attempt")))
        self.assertFalse(json.loads((output / "transcript.json").read_text())["complete"])
        with mock.patch.object(cloud.benchmark, "_request", return_value=("часть", "length", {"cost": 0.02}, None)) as request:
            with self.assertRaises(SystemExit):
                cloud.main(self.args("--execute"))
            self.assertEqual(request.call_count, 2)
        doc = json.loads((output / "transcript.json").read_text())
        self.assertFalse(doc["complete"])
        self.assertTrue(all(row["incomplete"] for row in doc["utterances"]))
        self.assertIn("НЕПОЛНАЯ", (output / "transcript.md").read_text())
        with mock.patch.object(cloud.benchmark, "_request", side_effect=AssertionError("retry")):
            with self.assertRaises(SystemExit):
                cloud.main(self.args("--execute"))

    def test_continue_skips_unknown_and_processes_later_batches_and_session(self):
        wav(self.session / "microphone.wav", 17)
        wav(self.session / "remote.wav", 4)
        second = self.root / "second-meeting"
        second.mkdir()
        (second / "session.json").write_text('{"title":"Second"}')
        wav(second / "microphone.wav", 4)
        wav(second / "remote.wav", 4)
        argv = ["--session", str(self.session), "--session", str(second),
                "--output-root", str(self.output), "--plan", str(self.plan),
                "--key-file", str(self.key), "--execute", "--continue-on-error"]
        count = 0
        def response(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                raise RuntimeError("OpenRouter returned HTTP 429; no automatic retry")
            return "fixture transcript", "stop", {"cost": 0.001}, None
        with mock.patch.object(cloud, "WINDOW", 4), \
             mock.patch.object(cloud.benchmark, "_request", side_effect=response) as request:
            with self.assertRaises(SystemExit):
                cloud.main(argv)
            self.assertEqual(request.call_count, 8)
            with mock.patch.object(cloud.benchmark, "_request", side_effect=AssertionError("charged again")):
                with self.assertRaises(SystemExit):
                    cloud.main(argv)
        outputs = [json.loads(path.read_text()) for path in self.output.glob("*/transcript.json")]
        self.assertEqual(sorted(doc["complete"] for doc in outputs), [False, True])
        self.assertEqual(sum(row["incomplete"] for doc in outputs for row in doc["utterances"]), 1)
        markers = list(self.output.glob("*/results/*.attempt"))
        self.assertEqual(len(markers), 1)
        diagnostic = markers[0].with_suffix(".diagnostic")
        self.assertEqual(json.loads(diagnostic.read_text()),
                         {"exception_class": "RuntimeError", "http_status": 429, "timeout": False})
        self.assertNotIn("fixture-secret", diagnostic.read_text())
        self.assertNotIn("OpenRouter returned", diagnostic.read_text())
        self.assertEqual(len(list(self.output.glob("*/results/*.json"))), 7)

    def test_continue_after_incomplete_response_and_respect_unknown_reserve(self):
        wav(self.session / "microphone.wav", 12)
        wav(self.session / "remote.wav", 0)
        answers = iter([RuntimeError("OpenRouter request failed or redirected; no automatic retry"),
                        ("partial", "length", {"cost": 0.01}, None)])
        def response(*args, **kwargs):
            value = next(answers)
            if isinstance(value, Exception):
                raise value
            return value
        with mock.patch.object(cloud, "WINDOW", 4), \
             mock.patch.object(cloud.benchmark, "_request", side_effect=response) as request:
            with self.assertRaises(SystemExit):
                cloud.main(self.args("--execute", "--continue-on-error", "--budget-usd", "0.11"))
            self.assertEqual(request.call_count, 2)
        output = next(self.output.iterdir())
        document = json.loads((output / "transcript.json").read_text())
        self.assertFalse(document["complete"])
        self.assertTrue(all(row["incomplete"] for row in document["utterances"]))
        self.assertEqual(len(list((output / "results").glob("*.attempt"))), 1)
        self.assertEqual(len(list((output / "results").glob("*.json"))), 1)

    def test_existing_unknown_reserve_limits_other_session_before_http(self):
        wav(self.session / "microphone.wav", 2)
        wav(self.session / "remote.wav", 0)
        second = self.root / "second-meeting"
        second.mkdir()
        (second / "session.json").write_text('{"title":"Second"}')
        wav(second / "microphone.wav", 2)
        wav(second / "remote.wav", 0)
        output, _, _, items = cloud._session_items(self.session, self.output, cloud._plan(self.plan))
        (output / "results").mkdir(parents=True)
        cloud.benchmark._private_json(cloud._marker_path(output, items[0]), {"identity": items[0]["identity"]})
        with mock.patch.object(cloud.benchmark, "_request", side_effect=AssertionError("budget failed")):
            with self.assertRaises(SystemExit):
                cloud.main(["--session", str(self.session), "--session", str(second),
                            "--output-root", str(self.output), "--plan", str(self.plan),
                            "--key-file", str(self.key), "--execute", "--continue-on-error",
                            "--budget-usd", "0.09"])
        docs = [json.loads(path.read_text()) for path in self.output.glob("*/transcript.json")]
        self.assertEqual(len(docs), 2)
        self.assertTrue(all(not doc["complete"] for doc in docs))
        self.assertFalse(list(self.output.glob("*/results/*.json")))


if __name__ == "__main__":
    unittest.main()
