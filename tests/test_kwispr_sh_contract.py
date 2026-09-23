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
            self.assertFalse(any(arg.startswith("prompt=") for arg in args))
            self.assertNotIn("preserve_audio_tail=1", args)

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

    def test_whisper_context_and_vocabulary_are_literal_form_strings(self) -> None:
        with KwisprScriptHarness() as h:
            wav = h.make_wav()
            prompt = '@/etc/passwd;type=text/plain\nЯ работаю над проектом.'
            vocabulary = 'Kwispr, KDE, $(touch injected), "quote", `id`, <file'
            h.write_config(
                KWISPR_API_URL="http://127.0.0.1:19650/v1/audio/transcriptions",
                KWISPR_LOCAL_STT_CONFIGURED="1",
                KWISPR_MODEL="whisper-large-v3-turbo",
                KWISPR_WHISPER_PROMPT=prompt,
                KWISPR_VOCABULARY=vocabulary,
                KWISPR_TRANSCRIPTION_PROMPT="CHAT INSTRUCTIONS MUST STAY OUT",
                KWISPR_AUTOPASTE="0",
            )
            h.fake_curl_response(200, {"text": "Привет, Kwispr!"})

            result = h.run("retry", str(wav))

            self.assertEqual(result.returncode, 0, result.stderr)
            args = h.curl_invocations()[0]
            index = args.index(f"prompt={prompt}\n{vocabulary}")
            self.assertEqual(args[index - 1], "--form-string")
            self.assertNotIn("CHAT INSTRUCTIONS MUST STAY OUT", "\n".join(args))
            self.assertFalse((h.repo / "injected").exists())
            self.assertEqual(h.clipboard_text(), "Привет, Kwispr!")

    def test_vocabulary_without_whisper_context_is_sent_as_prompt(self) -> None:
        with KwisprScriptHarness() as h:
            wav = h.make_wav()
            h.write_config(
                KWISPR_API_URL="https://api.openai.com/v1/audio/transcriptions",
                KWISPR_API_KEY="sk-test", KWISPR_VOCABULARY="Kwispr, PipeWire",
                KWISPR_PRESERVE_AUDIO_TAIL="1",
                KWISPR_AUTOPASTE="0",
            )
            h.fake_curl_response(200, {"text": "Kwispr"})
            result = h.run("retry", str(wav))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("prompt=Kwispr, PipeWire", h.curl_invocations()[0])
            self.assertNotIn("preserve_audio_tail=1", h.curl_invocations()[0])

    def test_local_non_whisper_omits_retained_context_but_can_preserve_tail(self) -> None:
        with KwisprScriptHarness() as h:
            wav = h.make_wav()
            h.write_config(
                KWISPR_API_URL="http://inference-box.lan:19650/v1/audio/transcriptions",
                KWISPR_LOCAL_STT_CONFIGURED="1", KWISPR_MODEL="parakeet-tdt-0.6b-v3",
                KWISPR_WHISPER_PROMPT="Сохранённый контекст.", KWISPR_VOCABULARY="Kwispr",
                KWISPR_PRESERVE_AUDIO_TAIL="1", KWISPR_AUTOPASTE="0",
            )
            h.fake_curl_response(200, {"text": "Kwispr"})
            result = h.run("retry", str(wav))
            self.assertEqual(result.returncode, 0, result.stderr)
            args = h.curl_invocations()[0]
            self.assertFalse(any(arg.startswith("prompt=") for arg in args))
            index = args.index("preserve_audio_tail=1")
            self.assertEqual(args[index - 1], "--form-string")

    def test_legacy_loopback_non_whisper_skips_context_and_preserve_tail_can_be_disabled(self) -> None:
        with KwisprScriptHarness() as h:
            wav = h.make_wav()
            h.write_config(
                KWISPR_API_URL="http://127.0.0.1:19650/v1/audio/transcriptions",
                KWISPR_LOCAL_STT_CONFIGURED="0", KWISPR_MODEL="parakeet-tdt-0.6b-v3",
                KWISPR_WHISPER_PROMPT="Сохранённый контекст.", KWISPR_VOCABULARY="Kwispr",
                KWISPR_PRESERVE_AUDIO_TAIL="0", KWISPR_AUTOPASTE="0",
            )
            h.fake_curl_response(200, {"text": "Kwispr"})
            result = h.run("retry", str(wav))
            self.assertEqual(result.returncode, 0, result.stderr)
            args = h.curl_invocations()[0]
            self.assertFalse(any(arg.startswith("prompt=") for arg in args))
            self.assertNotIn("preserve_audio_tail=1", args)

    def test_openrouter_adds_literal_vocabulary_to_chat_instructions_only(self) -> None:
        with KwisprScriptHarness() as h:
            wav = h.make_wav()
            vocabulary = 'Kwispr, "quoted", $(touch injected); <file'
            h.write_config(
                KWISPR_BACKEND="openrouter-chat",
                KWISPR_API_URL="https://openrouter.ai/api/v1/chat/completions",
                KWISPR_TRANSCRIPTION_PROMPT="Transcribe exactly.",
                KWISPR_WHISPER_PROMPT="WHISPER CONTEXT MUST STAY OUT",
                KWISPR_VOCABULARY=vocabulary, KWISPR_PRESERVE_AUDIO_TAIL="1",
                KWISPR_AUTOPASTE="0",
            )
            h.fake_curl_response(200, {"choices": [{"message": {"content": "Kwispr"}}]})
            result = h.run("retry", str(wav))
            self.assertEqual(result.returncode, 0, result.stderr)
            args = h.curl_invocations()[0]
            payload = json.loads(h.data_binary_path(args).read_text())
            prompt = payload["messages"][0]["content"][0]["text"]
            self.assertEqual(prompt, "Transcribe exactly.\nVocabulary (use these spellings only when heard): " + vocabulary)
            self.assertNotIn("preserve_audio_tail=1", args)
            self.assertFalse((h.repo / "injected").exists())

    def test_local_whisper_language_candidates_keep_mixed_text_and_explicit_hint(self) -> None:
        with KwisprScriptHarness() as h:
            wav = h.make_wav()
            h.write_config(
                KWISPR_API_URL="http://localhost:19650/v1/audio/transcriptions",
                KWISPR_MODEL="whisper-large-v3-turbo", KWISPR_LANGUAGE="ru",
                KWISPR_WHISPER_ALLOWED_LANGUAGES=" RU, en ", KWISPR_AUTOPASTE="0",
            )
            transcript = "Проверим pull request в GitHub. Looks good, запускай тесты."
            h.fake_curl_response(200, {"text": transcript, "language": "ru"})
            result = h.run("retry", str(wav))
            self.assertEqual(result.returncode, 0, result.stderr)
            args = h.curl_invocations()[0]
            index = args.index("allowed_languages=ru,en")
            self.assertEqual(args[index - 1], "--form-string")
            self.assertIn("language=ru", args)
            self.assertEqual(h.clipboard_text(), transcript)

    def test_language_candidates_are_not_sent_to_cloud_or_other_local_models(self) -> None:
        for endpoint, model in (("https://api.openai.com/v1/audio/transcriptions", "whisper-1"),
                                ("http://localhost:19650/v1/audio/transcriptions", "parakeet-tdt-0.6b-v3")):
            with self.subTest(endpoint=endpoint), KwisprScriptHarness() as h:
                wav = h.make_wav()
                h.write_config(KWISPR_API_URL=endpoint, KWISPR_MODEL=model, KWISPR_API_KEY="sk-test",
                               KWISPR_WHISPER_ALLOWED_LANGUAGES="ru,en", KWISPR_AUTOPASTE="0")
                h.fake_curl_response(200, {"text": "Hello, привет."})
                result = h.run("retry", str(wav))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(any(arg.startswith("allowed_languages=") for arg in h.curl_invocations()[0]))

    def test_invalid_quality_settings_fail_before_request(self) -> None:
        invalid_settings = [
            ("KWISPR_STOP_DELAY_MS", value) for value in ("-1", "2001", "1.5", "00000", "1;id")
        ] + [
            ("KWISPR_PRESERVE_AUDIO_TAIL", "true"),
            ("KWISPR_VOCABULARY", "one\ntwo"),
            ("KWISPR_VOCABULARY", "one\rtwo"),
            ("KWISPR_WHISPER_PROMPT", "я" * 4097),
            ("KWISPR_WHISPER_ALLOWED_LANGUAGES", "ru,,en"),
            ("KWISPR_WHISPER_ALLOWED_LANGUAGES", "ru\nen"),
            ("KWISPR_WHISPER_ALLOWED_LANGUAGES", "@/etc/passwd"),
            ("KWISPR_WHISPER_ALLOWED_LANGUAGES", "ru;en"),
        ]
        for key, value in invalid_settings:
            with self.subTest(key=key, value=value[:30]), KwisprScriptHarness() as h:
                wav = h.make_wav()
                h.write_config(KWISPR_API_URL="http://localhost:19650/v1/audio/transcriptions", **{key: value})
                result = h.run("retry", str(wav))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(key, result.stderr)
                self.assertEqual(h.curl_invocations(), [])

    def test_context_limit_counts_unicode_characters_and_joining_newline(self) -> None:
        for prompt, vocabulary, accepted in (("я" * 4096, "", True), ("я" * 4094, "Я", True), ("я" * 4095, "Я", False)):
            with self.subTest(length=len(prompt), vocabulary=vocabulary), KwisprScriptHarness() as h:
                wav = h.make_wav()
                h.write_config(
                    KWISPR_API_URL="http://localhost:19650/v1/audio/transcriptions",
                    KWISPR_WHISPER_PROMPT=prompt, KWISPR_VOCABULARY=vocabulary,
                    KWISPR_AUTOPASTE="0",
                )
                h.fake_curl_response(200, {"text": "Привет."})
                result = h.run("retry", str(wav))
                self.assertEqual(result.returncode == 0, accepted, result.stderr)
                self.assertEqual(bool(h.curl_invocations()), accepted)

    def test_stop_delay_precedes_recorder_stop_and_cue_follows_finalization(self) -> None:
        for delay in (None, "0350", "2000"):
            with self.subTest(delay=delay), KwisprScriptHarness() as h:
                h.prepare_recording()
                options = {} if delay is None else {"KWISPR_STOP_DELAY_MS": delay}
                h.write_config(
                    KWISPR_API_URL="http://localhost:19650/v1/audio/transcriptions",
                    KWISPR_AUTOPASTE="0", **options,
                )
                h.fake_curl_response(200, {"text": "Последние слова."})
                result = h.run("toggle")
                self.assertEqual(result.returncode, 0, result.stderr)
                events = h.recording_events()
                stop = next(event for event in events if event["event"] == "stop-request")
                finalized = next(event for event in events if event["event"] == "finalized")
                cue = next(event for event in events if event["event"] == "cue")
                self.assertGreater(cue["at"], finalized["at"])
                sleeps_before_stop = [event for event in events if event["event"] == "sleep" and event["at"] < stop["at"]]
                if delay is None:
                    self.assertEqual(sleeps_before_stop, [])
                else:
                    self.assertEqual(len(sleeps_before_stop), 1)
                    self.assertAlmostEqual(float(sleeps_before_stop[0]["seconds"]), int(delay) / 1000)
                    self.assertGreaterEqual(stop["at"] - sleeps_before_stop[0]["at"], int(delay) / 1000)
                self.assertFalse((h.cache_dir / "current.pid").exists())
                self.assertEqual(h.clipboard_text(), "Последние слова.")

    def test_real_speech_is_not_deleted_as_subtitle_hallucinations(self) -> None:
        transcript = (
            "Можешь сам субтитры посмотреть. А теперь важное продолжение.\n"
            "Редактор субтитров нужен для видео. Корректор Иван проверит текст.\n"
            "Subtitles by Alice are available. Keep this sentence too.\n"
            "[Музыка] и [Music] — названия меток. Thanks for watching!"
        )
        for locale in ("C", "C.UTF-8"):
            with self.subTest(locale=locale), KwisprScriptHarness() as h:
                wav = h.make_wav()
                h.write_config(
                    KWISPR_API_URL="http://localhost:19650/v1/audio/transcriptions",
                    KWISPR_AUTOPASTE="0",
                )
                h.fake_curl_response(200, {"text": transcript})
                result = h.run("retry", str(wav), env_overrides={"LC_ALL": locale})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(h.clipboard_text(), transcript)
                self.assertEqual(wav.with_suffix(".txt").read_text(), transcript)

    def test_known_subtitle_credit_is_trimmed_only_as_a_standalone_trailing_line(self) -> None:
        cases = (
            ("Spoken sentence. Субтитры сделал DimaTorzok.", "Spoken sentence.", "C"),
            ("Говорил prefix.\nСУБТИТРЫ СДЕЛАЛ ДИМА ТОРЖОК!!!  ", "Говорил prefix.", "C.UTF-8"),
            ("Субтитры сделал Дима Торжок!", "", "C.UTF-8"),
        )
        for transcript, expected, locale in cases:
            with self.subTest(transcript=transcript, locale=locale), KwisprScriptHarness() as h:
                wav = h.make_wav()
                h.write_config(
                    KWISPR_API_URL="http://localhost:19650/v1/audio/transcriptions",
                    KWISPR_AUTOPASTE="0",
                )
                h.fake_curl_response(200, {"text": transcript})
                result = h.run("retry", str(wav), env_overrides={"LC_ALL": locale})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(h.clipboard_text(), expected)
                if expected:
                    self.assertEqual(wav.with_suffix(".txt").read_text(), expected)
                else:
                    self.assertFalse(wav.with_suffix(".txt").exists())

    def test_known_credit_mention_generic_name_and_following_speech_are_preserved(self) -> None:
        transcripts = (
            "Он произнёс: «Субтитры сделал Дима Торжок».",
            "Важно: Субтитры сделал Дима Торжок, а потом продолжил.",
            "Субтитры сделал Иван.",
            "Subtitles by DimaTorzok.",
            "Субтитры сделал Дима Торжок — вот так звучит фраза.",
        )
        for transcript in transcripts:
            with self.subTest(transcript=transcript), KwisprScriptHarness() as h:
                wav = h.make_wav()
                h.write_config(
                    KWISPR_API_URL="http://localhost:19650/v1/audio/transcriptions",
                    KWISPR_AUTOPASTE="0",
                )
                h.fake_curl_response(200, {"text": transcript})
                result = h.run("retry", str(wav))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(h.clipboard_text(), transcript)
                self.assertEqual(wav.with_suffix(".txt").read_text(), transcript)

    def test_whitespace_only_local_response_is_no_speech(self) -> None:
        with KwisprScriptHarness() as h:
            wav = h.make_wav()
            h.write_config(
                KWISPR_API_URL="http://localhost:19650/v1/audio/transcriptions",
                KWISPR_AUTOPASTE="0",
            )
            h.fake_curl_response(200, {"text": " \t\n "})
            result = h.run("retry", str(wav))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(wav.with_suffix(".txt").exists())
            self.assertEqual(h.clipboard_text(), "")


if __name__ == "__main__":
    unittest.main()
