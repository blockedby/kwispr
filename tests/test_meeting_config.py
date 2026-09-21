from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from kwispr_meetings.config import ConfigError, load_config, output_dir, speaker_count, validate_local_backend


class MeetingConfigTests(unittest.TestCase):
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

    def test_local_endpoint_is_required_no_cloud_fallback(self):
        for url in ("http://127.0.0.1:19650/v1/audio/transcriptions", "http://localhost:1/x", "http://[::1]:1/x"):
            validate_local_backend({"KWISPR_API_URL": url})
        for url in ("https://api.openai.com/v1/audio/transcriptions", "file:///tmp/x", "http://user:pass@localhost/x", "http://[invalid", ""):
            with self.subTest(url=url), self.assertRaises(ConfigError):
                validate_local_backend({"KWISPR_API_URL": url})
        with self.assertRaises(ConfigError):
            validate_local_backend({"KWISPR_BACKEND": "openrouter-chat", "KWISPR_API_URL": "http://localhost/x"})
        validate_local_backend({"KWISPR_API_URL": "http://192.168.1.10:19650/x", "KWISPR_LOCAL_STT_CONFIGURED": "1"})

    def test_speaker_range_and_output_location(self):
        self.assertEqual(speaker_count("0"), 0)
        self.assertEqual(speaker_count("16"), 16)
        for value in (-1, 17, "auto", None):
            with self.assertRaises(ConfigError):
                speaker_count(value)
        self.assertEqual(output_dir({}).name, "Meetings")
        self.assertEqual(output_dir({"KWISPR_MEETING_OUTPUT_DIR": "/tmp/custom"}), Path("/tmp/custom"))
