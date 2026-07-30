#!/usr/bin/env python3
"""Regression contract tests for the existing kwispr.sh CLI."""

from __future__ import annotations

import json
import unittest

from tests.kwispr_contract_utils import KwisprScriptHarness


class KwisprShellContractTest(unittest.TestCase):
    def test_xdg_config_takes_precedence_over_legacy_repo_env(self) -> None:
        with KwisprScriptHarness() as h:
            wav = h.make_wav(size=40000)
            h.write_env(
                KWISPR_BACKEND="openai-transcriptions",
                KWISPR_API_URL="http://legacy.invalid/v1/audio/transcriptions",
                KWISPR_MODEL="legacy-model",
                KWISPR_AUTOPASTE="0",
            )
            h.write_config(
                KWISPR_BACKEND="openai-transcriptions",
                KWISPR_API_URL="http://127.0.0.1:19650/v1/audio/transcriptions",
                KWISPR_MODEL="xdg-model",
                KWISPR_AUTOPASTE="0",
            )
            h.fake_curl_response(200, {"text": "configured"})

            result = h.run("retry", str(wav))

            self.assertEqual(result.returncode, 0, result.stderr)
            args = h.curl_invocations()[0]
            self.assertIn("http://127.0.0.1:19650/v1/audio/transcriptions", args)
            self.assertIn("model=xdg-model", args)
            self.assertNotIn("model=legacy-model", args)

    def test_missing_config_does_not_require_repository_dotenv(self) -> None:
        with KwisprScriptHarness() as h:
            wav = h.make_wav(size=40000)

            result = h.run("retry", str(wav))

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Open Kwispr Settings", result.stderr)
            self.assertNotIn("Copy .env.example", result.stderr)

    def test_local_empty_transcript_is_clean_skip(self) -> None:
        with KwisprScriptHarness() as h:
            wav = h.make_wav(size=40000)
            h.write_env(
                KWISPR_BACKEND="openai-transcriptions",
                KWISPR_API_URL="http://127.0.0.1:19650/v1/audio/transcriptions",
                KWISPR_API_KEY="",
                KWISPR_MODEL="whisper-large-v3-turbo",
                KWISPR_AUTOPASTE="0",
            )
            h.fake_curl_response(200, {"text": ""})

            result = h.run("retry", str(wav))

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((h.cache_dir / "last-failed.txt").exists())
            self.assertEqual(h.clipboard_text(), "")

    def test_remote_local_stt_empty_transcript_is_clean_skip(self) -> None:
        with KwisprScriptHarness() as h:
            wav = h.make_wav(size=40000)
            h.write_config(
                KWISPR_BACKEND="openai-transcriptions",
                KWISPR_API_URL="http://inference-box.lan:19650/v1/audio/transcriptions",
                KWISPR_LOCAL_STT_CONFIGURED="1",
                KWISPR_MODEL="remote-catalog-slug",
                KWISPR_AUTOPASTE="0",
            )
            h.fake_curl_response(200, {"text": ""})

            result = h.run("retry", str(wav))

            self.assertEqual(result.returncode, 0, result.stderr)
            args = h.curl_invocations()[0]
            self.assertIn("http://inference-box.lan:19650/v1/audio/transcriptions", args)
            self.assertIn("model=remote-catalog-slug", args)
            self.assertFalse((h.cache_dir / "last-failed.txt").exists())

    def test_non_local_empty_transcript_records_retry(self) -> None:
        with KwisprScriptHarness() as h:
            wav = h.make_wav(size=40000)
            h.write_env(
                KWISPR_BACKEND="openai-transcriptions",
                KWISPR_API_URL="https://api.openai.com/v1/audio/transcriptions",
                KWISPR_API_KEY="sk-test",
                KWISPR_MODEL="whisper-1",
                KWISPR_AUTOPASTE="0",
            )
            h.fake_curl_response(200, {"text": ""})

            result = h.run("retry", str(wav))

            self.assertNotEqual(result.returncode, 0)
            retry_path = (h.cache_dir / "last-failed.txt").read_text(encoding="utf-8").rstrip("\n")
            self.assertEqual(retry_path, str(wav))
            self.assertIn("kwispr.sh retry", h.clipboard_text())
            self.assertIn(str(wav), h.clipboard_text())

    def test_openai_compatible_request_fields_and_local_auth_omission(self) -> None:
        with KwisprScriptHarness() as h:
            wav = h.make_wav(size=40000)
            h.write_env(
                KWISPR_BACKEND="openai-transcriptions",
                KWISPR_API_URL="http://127.0.0.1:19650/v1/audio/transcriptions",
                KWISPR_API_KEY="",
                KWISPR_MODEL="whisper-large-v3-turbo",
                KWISPR_LANGUAGE="ru",
                KWISPR_AUTOPASTE="0",
            )
            h.fake_curl_response(200, {"text": "привет"})
            h.cache_dir.mkdir(parents=True)
            (h.cache_dir / "last-failed.txt").write_text("stale.wav\n", encoding="utf-8")

            result = h.run("retry", str(wav))

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((h.cache_dir / "last-failed.txt").exists())
            args = h.curl_invocations()[0]
            self.assertIn("-F", args)
            self.assertIn("model=whisper-large-v3-turbo", args)
            self.assertIn("response_format=json", args)
            self.assertIn("temperature=0", args)
            self.assertIn("language=ru", args)
            self.assertTrue(any(arg == f"file=@{wav}" for arg in args))
            self.assertNotIn("Authorization: Bearer", "\n".join(args))

    def test_openrouter_sends_input_audio_prompt_and_model(self) -> None:
        with KwisprScriptHarness() as h:
            wav = h.make_wav(size=40000)
            h.write_env(
                KWISPR_BACKEND="openrouter-chat",
                KWISPR_API_URL="https://openrouter.ai/api/v1/chat/completions",
                KWISPR_API_KEY="sk-or-test",
                KWISPR_MODEL="openai/gpt-4o-mini-transcribe",
                KWISPR_AUDIO_FORMAT="wav",
                KWISPR_TRANSCRIPTION_PROMPT="transcribe exactly",
                KWISPR_AUTOPASTE="0",
            )
            h.fake_curl_response(200, {"choices": [{"message": {"content": "hello"}}]})

            result = h.run("retry", str(wav))

            self.assertEqual(result.returncode, 0, result.stderr)
            args = h.curl_invocations()[0]
            self.assertIn("Content-Type: application/json", args)
            data_path = h.data_binary_path(args)
            payload = json.loads(data_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["model"], "openai/gpt-4o-mini-transcribe")
            content = payload["messages"][0]["content"]
            self.assertEqual(content[0], {"type": "text", "text": "transcribe exactly"})
            self.assertEqual(content[1]["type"], "input_audio")
            self.assertEqual(content[1]["input_audio"]["format"], "wav")
            self.assertTrue(content[1]["input_audio"]["data"])


if __name__ == "__main__":
    unittest.main()
