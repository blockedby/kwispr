from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from kwispr_meetings.config import ConfigError, allowed_whisper_languages, load_config, meeting_language, output_dir, speaker_count, validate_local_backend


class MeetingConfigTests(unittest.TestCase):
    def test_meeting_languages_are_independent_and_normalized(self):
        config = {"KWISPR_LANGUAGE": "zh", "KWISPR_MEETING_MIC_LANGUAGE": " RU ", "KWISPR_MEETING_REMOTE_LANGUAGE": "en-US"}
        self.assertEqual(meeting_language(config, "microphone"), "ru")
        self.assertEqual(meeting_language(config, "remote"), "en-us")
        self.assertEqual(meeting_language({"KWISPR_LANGUAGE": "ru"}, "remote"), "")
        self.assertEqual(meeting_language({"KWISPR_MEETING_REMOTE_LANGUAGE": "AUTO"}, "remote"), "")
        self.assertEqual(meeting_language({"KWISPR_MEETING_REMOTE_LANGUAGE": "en-x-test"}, "remote"), "en-x-test")
        for value in ("English", "en ru", "x", "../../ru"):
            with self.assertRaises(ConfigError):
                meeting_language({"KWISPR_MEETING_REMOTE_LANGUAGE": value}, "remote")

    def test_allowed_language_candidates_validate_and_normalize(self):
        self.assertEqual(allowed_whisper_languages({}), "")
        self.assertEqual(allowed_whisper_languages({"KWISPR_WHISPER_ALLOWED_LANGUAGES": "  "}), "")
        self.assertEqual(allowed_whisper_languages({"KWISPR_WHISPER_ALLOWED_LANGUAGES": " RU, en,ru, EN-x-test "}), "ru,en,en-x-test")
        for value in ("ru,,en", "ru,", ",en", "English", "en ru", "x", "ru\0", "en-123456789", "ru," * 400):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                allowed_whisper_languages({"KWISPR_WHISPER_ALLOWED_LANGUAGES": value})

    def test_literal_quotes_preserve_dictionary_without_shell_execution(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True):
            root = Path(temporary)
            config = root / "config.env"
            sentinel = root / "should-not-exist"
            config.write_text("KWISPR_VOCABULARY='Kwispr, O'\\''Reilly, Кодекс'\n"
                              "KWISPR_WHISPER_PROMPT='Привет! Да, всё хорошо.'\n"
                              f"KWISPR_MODEL='$(touch {sentinel})'\n"
                              'export KWISPR_LANGUAGE="ru" # selected language\n')
            values = load_config(config)
            self.assertEqual(values["KWISPR_VOCABULARY"], "Kwispr, O'Reilly, Кодекс")
            self.assertEqual(values["KWISPR_WHISPER_PROMPT"], "Привет! Да, всё хорошо.")
            self.assertIn("$(touch", values["KWISPR_MODEL"])
            self.assertFalse(sentinel.exists())
            self.assertEqual(values["KWISPR_LANGUAGE"], "ru")

    def test_environment_overrides_and_unquoted_whitespace_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"KWISPR_MODEL": "override"}, clear=True):
            config = Path(temporary) / "config.env"
            config.write_text("KWISPR_MODEL=original\n")
            self.assertEqual(load_config(config)["KWISPR_MODEL"], "override")
            config.write_text("KWISPR_MODEL=foo bar\n")
            with self.assertRaises(ConfigError):
                load_config(config)

    def test_explicit_meeting_overrides_isolate_local_meetings_from_cloud_dictation(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True):
            config = Path(temporary) / "config.env"
            config.write_text(
                "KWISPR_BACKEND='openrouter-chat'\n"
                "KWISPR_API_URL='https://openrouter.ai/api/v1/chat/completions'\n"
                "KWISPR_MODEL='google/gemini-3.8-flash'\n"
                "KWISPR_API_KEY='global-dictation-key'\n"
                "OPENAI_API_KEY='global-openai-key'\n"
                "KWISPR_MEETING_BACKEND='openai-transcriptions'\n"
                "KWISPR_MEETING_API_URL='http://127.0.0.1:19650/v1/audio/transcriptions'\n"
                "KWISPR_MEETING_MODEL='whisper-large-v3-turbo'\n"
                "KWISPR_MEETING_API_KEY=''\n"
                "KWISPR_MEETING_LOCAL_STT_CONFIGURED='1'\n"
            )
            values = load_config(config)
            self.assertEqual(values["KWISPR_BACKEND"], "openai-transcriptions")
            self.assertEqual(values["KWISPR_API_URL"], "http://127.0.0.1:19650/v1/audio/transcriptions")
            self.assertEqual(values["KWISPR_MODEL"], "whisper-large-v3-turbo")
            self.assertEqual(values["KWISPR_API_KEY"], "")
            self.assertEqual(values["OPENAI_API_KEY"], "")
            self.assertEqual(values["KWISPR_LOCAL_STT_CONFIGURED"], "1")
            validate_local_backend(values)

    def test_meeting_override_environment_wins_and_absence_preserves_cloud_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.env"
            config.write_text(
                "KWISPR_BACKEND='openrouter-chat'\n"
                "KWISPR_API_URL='https://openrouter.ai/api/v1/chat/completions'\n"
                "KWISPR_MODEL='google/gemini-3.8-flash'\n"
                "KWISPR_MEETING_BACKEND='openai-transcriptions'\n"
                "KWISPR_MEETING_API_URL='http://127.0.0.1:19650/file-config'\n"
            )
            with patch.dict(os.environ, {"KWISPR_MEETING_API_URL": "http://localhost:19650/env-config"}, clear=True):
                values = load_config(config)
                self.assertEqual(values["KWISPR_API_URL"], "http://localhost:19650/env-config")
                validate_local_backend(values)
            with patch.dict(os.environ, {}, clear=True):
                values = load_config(config)
                self.assertEqual(values["KWISPR_API_URL"], "http://127.0.0.1:19650/file-config")
                validate_local_backend(values)
                cloud_config = Path(temporary) / "cloud.env"
                cloud_config.write_text(
                    "KWISPR_BACKEND='openrouter-chat'\n"
                    "KWISPR_API_URL='https://openrouter.ai/api/v1/chat/completions'\n"
                    "KWISPR_MODEL='google/gemini-3.8-flash'\n"
                )
                values = load_config(cloud_config)
                self.assertEqual(values["KWISPR_BACKEND"], "openrouter-chat")
                with self.assertRaises(ConfigError):
                    validate_local_backend(values)

    def test_meeting_cloud_endpoint_remains_rejected_even_with_local_backend_override(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True):
            config = Path(temporary) / "config.env"
            config.write_text(
                "KWISPR_BACKEND='openrouter-chat'\n"
                "KWISPR_MEETING_BACKEND='openai-transcriptions'\n"
                "KWISPR_MEETING_API_URL='https://api.openai.com/v1/audio/transcriptions'\n"
            )
            with self.assertRaises(ConfigError):
                validate_local_backend(load_config(config))

    def test_local_endpoint_is_required_no_cloud_fallback(self):
        for url in ("http://127.0.0.1:19650/v1/audio/transcriptions", "http://localhost:1/x", "http://[::1]:1/x"):
            validate_local_backend({"KWISPR_API_URL": url})
        for url in ("https://api.openai.com/v1/audio/transcriptions", "file:///tmp/x", "http://user:pass@localhost/x", "http://[invalid", ""):
            with self.subTest(url=url), self.assertRaises(ConfigError):
                validate_local_backend({"KWISPR_API_URL": url})
        with self.assertRaises(ConfigError):
            validate_local_backend({"KWISPR_BACKEND": "openrouter-chat", "KWISPR_API_URL": "http://localhost/x"})
        for host in ("192.168.1.10", "10.1.2.3", "172.20.1.1", "[fd12::1]"):
            validate_local_backend({"KWISPR_API_URL": f"http://{host}:19650/x", "KWISPR_LOCAL_STT_CONFIGURED": "1"})
        for host in ("api.openai.com", "example.local", "8.8.8.8", "169.254.1.1", "192.0.2.1", "[2001:db8::1]"):
            with self.subTest(host=host), self.assertRaises(ConfigError):
                validate_local_backend({"KWISPR_API_URL": f"https://{host}/x", "KWISPR_LOCAL_STT_CONFIGURED": "1"})
        with self.assertRaises(ConfigError):
            validate_local_backend({"KWISPR_API_URL": "http://localhost:invalid/x"})

    def test_speaker_range_and_output_location(self):
        self.assertEqual(speaker_count("0"), 0)
        self.assertEqual(speaker_count("16"), 16)
        for value in (-1, 17, "auto", None):
            with self.assertRaises(ConfigError):
                speaker_count(value)
        self.assertEqual(output_dir({}).name, "Meetings")
        self.assertEqual(output_dir({"KWISPR_MEETING_OUTPUT_DIR": "/tmp/custom"}), Path("/tmp/custom"))
