"""Fixture-only checks for the optional OpenRouter meeting benchmark."""
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

SCRIPT = Path(__file__).resolve().parents[1] / "tools/benchmark_meeting_audio.py"
spec = importlib.util.spec_from_file_location("benchmark_meeting_audio", SCRIPT)
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


class Response:
    headers = {"X-Generation-Id": "fixture-generation"}

    def __init__(self, data):
        self.data = json.dumps(data).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, limit):
        return self.data[:limit]


class Opener:
    def __init__(self, replies, on_open=None):
        self.replies = iter(replies)
        self.requests = []
        self.on_open = on_open

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if self.on_open:
            self.on_open(request)
        return Response(next(self.replies))


class MeetingAudioBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.clips = []

    def add_clip(self, name="clip", reference=None, reviewed=False):
        path = self.root / (name + ".wav")
        with wave.open(str(path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(b"\0\0" * 16000)
        clip = {"id": name, "path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "seconds": 1.0, "kind": "target", "local_text": "локальный текст", "source": "fixture",
                "target_start": 0.0, "target_end": 1.0, "reference": reference, "reference_reviewed": reviewed}
        self.clips.append(clip)
        return clip

    def manifest(self):
        path = self.root / "manifest.json"
        path.write_text(json.dumps({"clips": self.clips}), encoding="utf-8")
        return path

    def test_dry_run_reads_fixture_models_without_exposing_key_or_sending_audio(self):
        self.add_clip()
        manifest = self.manifest()
        key_file = self.root / "key.txt"
        key_file.write_text("# fixture\nOPENROUTER_API_KEY=fixture-secret\nMODELS:\nprovider/model-a\nprovider/model-b\n")
        output = io.StringIO()
        with patch.object(benchmark.urllib.request, "build_opener", side_effect=AssertionError("network")), redirect_stdout(output):
            benchmark.main(["--manifest", str(manifest), "--key-file", str(key_file), "--mode", "chat"])
        self.assertIn('"models": ["provider/model-a", "provider/model-b"]', output.getvalue())
        self.assertNotIn("fixture-secret", output.getvalue())
        self.assertFalse((self.root / "openrouter-results").exists())
        self.assertEqual(benchmark.read_key_file(self._legacy_key()), ("fixture-secret", []))

    def _legacy_key(self):
        path = self.root / "legacy-key.txt"
        path.write_text("fixture-secret\n")
        return path

    def test_bad_model_value_is_rejected_before_it_can_be_printed(self):
        self.add_clip()
        key_file = self.root / "key.txt"
        key_file.write_text("OPENROUTER_API_KEY=fixture-secret\nMODELS:\nfixture-secret\n")
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr), self.assertRaises(SystemExit):
            benchmark.main(["--manifest", str(self.manifest()), "--key-file", str(key_file), "--mode", "chat"])
        self.assertNotIn("fixture-secret", stdout.getvalue() + stderr.getvalue())

    def test_transcription_request_resume_and_actual_usage_soft_stop(self):
        self.add_clip("one")
        self.add_clip("two")
        manifest = self.manifest()
        args = ["--manifest", str(manifest), "--mode", "transcription", "--model", "provider/any-model",
                "--max-cost-usd", "0.05", "--execute"]
        opener = Opener([{"text": "Привет мир", "usage": {"cost": 0.06}}])
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "fixture-secret"}), \
             patch.object(benchmark.urllib.request, "build_opener", return_value=opener), \
             redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, "Soft budget reached"):
            benchmark.main(args)
        self.assertEqual(len(opener.requests), 1)
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, benchmark.ENDPOINTS["transcription"])
        self.assertEqual(timeout, 75)
        self.assertEqual(json.loads(request.data)["model"], "provider/any-model")
        self.assertEqual(json.loads(request.data)["input_audio"]["format"], "wav")
        saved = list((self.root / "openrouter-results").glob("*.json"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(list((self.root / "openrouter-results").glob("*.attempt")), [])
        self.assertEqual(json.loads(saved[0].read_text())["score"]["status"], "not_evaluated")
        opener = Opener([{"text": "two", "usage": {"cost": 0.01}}])
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "fixture-secret"}), \
             patch.object(benchmark.urllib.request, "build_opener", return_value=opener), redirect_stdout(io.StringIO()):
            benchmark.main(args[:-2] + ["0.20", "--execute"])
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(len(list((self.root / "openrouter-results").glob("*.json"))), 2)

    def test_unknown_outcome_marker_prevents_an_automatic_repeat(self):
        self.add_clip()
        manifest = self.manifest()
        args = ["--manifest", str(manifest), "--mode", "transcription", "--model", "provider/model", "--execute"]
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "fixture-secret"}), \
             patch.object(benchmark, "_request", side_effect=RuntimeError("fixture interrupted")), \
             redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, "fixture interrupted"):
            benchmark.main(args)
        markers = list((self.root / "openrouter-results").glob("*.attempt"))
        self.assertEqual(len(markers), 1)
        self.assertEqual(list((self.root / "openrouter-results").glob("*.json")), [])
        with patch.object(benchmark.urllib.request, "build_opener", side_effect=AssertionError("network")), \
             redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, "outcome is unknown"):
            benchmark.main(args)

    def test_resume_refreshes_reviewed_reference_without_http_or_new_charge(self):
        clip = self.add_clip(reference=None, reviewed=False)
        manifest = self.manifest()
        args = ["--manifest", str(manifest), "--mode", "transcription", "--model", "provider/model", "--execute"]
        opener = Opener([{"text": "привет мир", "usage": {"cost": 0.03}}])
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "fixture-secret"}), \
             patch.object(benchmark.urllib.request, "build_opener", return_value=opener), redirect_stdout(io.StringIO()):
            benchmark.main(args)
        result_path = next((self.root / "openrouter-results").glob("*.json"))
        original = json.loads(result_path.read_text())
        marker = result_path.with_suffix(".attempt")
        marker.write_text("interrupted after save")
        clip.update(local_text="новый локальный текст", reference="Привет, мир!", reference_reviewed=True)
        self.manifest()
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": ""}), \
             patch.object(benchmark.urllib.request, "build_opener", side_effect=AssertionError("network")), \
             redirect_stdout(io.StringIO()):
            benchmark.main(args)
        refreshed = json.loads(result_path.read_text())
        self.assertEqual(refreshed["text"], original["text"])
        self.assertEqual(refreshed["usage"], original["usage"])
        self.assertEqual(refreshed["identity"], original["identity"])
        self.assertEqual(refreshed["local_text"], "новый локальный текст")
        self.assertEqual(refreshed["reference"], "Привет, мир!")
        self.assertEqual(refreshed["score"]["wer"], 0)
        self.assertFalse(marker.exists())

    def test_chat_payload_and_incomplete_response_are_recorded_without_scoring(self):
        self.add_clip(reference="Привет, мир!", reviewed=True)
        opener = Opener([{"choices": [{"message": {"content": "привет мир"}, "finish_reason": "length"}],
                          "usage": {"cost": 0.01}}])
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "fixture-secret"}), \
             patch.object(benchmark.urllib.request, "build_opener", return_value=opener), redirect_stdout(io.StringIO()):
            benchmark.main(["--manifest", str(self.manifest()), "--mode", "chat", "--model", "provider/any-model", "--execute"])
        request = opener.requests[0][0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, benchmark.ENDPOINTS["chat"])
        self.assertEqual(payload["max_tokens"], 1024)
        self.assertNotIn("reasoning", payload)
        self.assertEqual(payload["messages"][0]["content"][0]["text"], benchmark.PROMPT)
        self.assertEqual(payload["messages"][0]["content"][1]["type"], "input_audio")
        saved = json.loads(next((self.root / "openrouter-results").glob("*.json")).read_text())
        self.assertEqual(saved["finish_reason"], "length")
        self.assertTrue(saved["incomplete"])
        self.assertEqual(saved["score"], {"status": "not_evaluated", "reason": "incomplete"})

    def test_chat_settings_change_identity_and_are_saved_before_http(self):
        clip = self.add_clip()
        manifest = self.manifest()
        legacy_fields = {"mode": "chat", "model": "provider/model", "clip_id": clip["id"],
                         "sha256": clip["sha256"], "prompt_variant": benchmark.PROMPT_VARIANT}
        legacy_identity = hashlib.sha256(json.dumps(legacy_fields, sort_keys=True).encode()).hexdigest()
        self.assertEqual(benchmark._identity("chat", "provider/model", clip), legacy_identity)
        self.assertEqual(benchmark._identity("chat", "provider/model", clip, 1024, None), legacy_identity)
        self.assertNotEqual(benchmark._identity("chat", "provider/model", clip, 4096, "minimal"), legacy_identity)
        args = ["--manifest", str(manifest), "--mode", "chat", "--model", "provider/model", "--execute"]
        default_opener = Opener([{"choices": [{"message": {"content": "one"}, "finish_reason": "stop"}],
                                  "usage": {"cost": 0.01}}])
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "fixture-secret"}), \
             patch.object(benchmark.urllib.request, "build_opener", return_value=default_opener), \
             redirect_stdout(io.StringIO()):
            benchmark.main(args)
        seen_markers = []
        output = self.root / "openrouter-results"

        def observe(_request):
            seen_markers.extend(json.loads(path.read_text()) for path in output.glob("*.attempt"))

        tuned_opener = Opener([{"choices": [{"message": {"content": "two"}, "finish_reason": "stop"}],
                                "usage": {"cost": 0.02}}], on_open=observe)
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "fixture-secret"}), \
             patch.object(benchmark.urllib.request, "build_opener", return_value=tuned_opener), \
             redirect_stdout(io.StringIO()):
            benchmark.main(args + ["--max-output-tokens", "4096", "--reasoning-effort", "minimal"])
        self.assertEqual(len(tuned_opener.requests), 1)
        payload = json.loads(tuned_opener.requests[0][0].data)
        self.assertEqual(payload["max_tokens"], 4096)
        self.assertEqual(payload["reasoning"], {"effort": "minimal"})
        self.assertEqual(seen_markers[0]["request_options"],
                         {"max_output_tokens": 4096, "reasoning_effort": "minimal"})
        results = [json.loads(path.read_text()) for path in output.glob("*.json")]
        self.assertEqual(len(results), 2)
        self.assertEqual({result["identity"] for result in results},
                         {legacy_identity, benchmark._identity("chat", "provider/model", clip, 4096, "minimal")})
        self.assertIn({"max_output_tokens": 4096, "reasoning_effort": "minimal"},
                      [result["request_options"] for result in results])
        self.assertEqual(list(output.glob("*.attempt")), [])

    def test_chat_only_settings_rejected_for_transcription(self):
        self.add_clip()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            benchmark.main(["--manifest", str(self.manifest()), "--mode", "transcription",
                            "--model", "provider/model", "--reasoning-effort", "minimal"])

    def test_scoring_needs_reviewed_reference_and_handles_empty_reference(self):
        clip = self.add_clip(reference="Привет, мир!", reviewed=True)
        self.assertEqual(benchmark.score("привет мир", clip)["wer"], 0)
        self.assertEqual(benchmark.score("привет мир", clip)["cer"], 0)
        clip["reference_reviewed"] = False
        self.assertEqual(benchmark.score("привет мир", clip), {"status": "not_evaluated"})
        clip.update(reference="", reference_reviewed=True)
        self.assertEqual(benchmark.score("лишняя речь", clip),
                         {"status": "reviewed_empty_reference", "false_positive": True, "wer": None, "cer": None})

    def test_manifest_rejects_duplicate_ids_changed_wav_and_unsafe_paths(self):
        clip = self.add_clip()
        self.clips.append(dict(clip))
        with self.assertRaisesRegex(RuntimeError, "Duplicate clip ID"):
            benchmark.load_manifest(self.manifest())
        self.clips.pop()
        clip["sha256"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "SHA-256 differs"):
            benchmark.load_manifest(self.manifest())
        clip["path"] = "../outside.wav"
        with self.assertRaisesRegex(RuntimeError, "inside the manifest"):
            benchmark.load_manifest(self.manifest())


if __name__ == "__main__":
    unittest.main()
