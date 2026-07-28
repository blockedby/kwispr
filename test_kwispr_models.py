#!/usr/bin/env python3
"""Tests for the Handy catalog v2 GGUF model helper."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).with_name("kwispr-models.py")
spec = importlib.util.spec_from_file_location("kwispr_models", MODULE_PATH)
assert spec and spec.loader
kwispr_models = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kwispr_models)


class KwisprModelsValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.last_stdout = ""
        self.last_stderr = ""

    def write_catalog(self, root: Path, payload: bytes = b"test gguf bytes", *, filename: str = "tiny model-Q8_0.gguf") -> tuple[Path, dict]:
        revision = "a" * 40
        repo_id = "example org/tiny model-gguf"
        source = root / "source"
        artifact = source / "example org" / "tiny model-gguf" / revision / filename
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(payload)
        model = {
            "id": repo_id,
            "revision": revision,
            "slug": "tiny-model",
            "name": "Tiny test model",
            "architecture": "whisper",
            "languages": ["en"],
            "capabilities": {"streaming": False, "translate": False, "lang_detect": True, "timestamps": "segment"},
            "files": [
                {"filename": "tiny-Q4.gguf", "quant": "Q4", "size_bytes": 10, "sha256": "0" * 64},
                {"filename": filename, "quant": "Q8_0", "size_bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()},
            ],
            "default_quant": "Q8_0",
        }
        catalog = {"catalog_version": 2, "mirrors": [source.as_uri()], "models": [model]}
        path = root / "catalog.json"
        path.write_text(json.dumps(catalog), encoding="utf-8")
        return path, model

    def run_cli(self, *args: str) -> int:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = kwispr_models.main(list(args))
        self.last_stdout = stdout.getvalue()
        self.last_stderr = stderr.getvalue()
        return rc

    def test_repository_catalog_v2_is_valid_complete_and_unique(self) -> None:
        catalog = kwispr_models.load_catalog(Path("models/local-stt-catalog.json"))
        self.assertEqual(catalog["catalog_version"], 2)
        self.assertEqual(catalog["source"]["commit"], "ea3c20a3a67c7401d8b19198723760da9d40ac45")
        self.assertEqual(len(catalog["models"]), 67)
        slugs = [model["slug"] for model in catalog["models"]]
        repo_ids = [model["id"] for model in catalog["models"]]
        self.assertEqual(len(slugs), len(set(slugs)))
        self.assertEqual(len(repo_ids), len(set(repo_ids)))
        for expected in ("gigaam-v3-e2e-ctc", "parakeet-tdt-0.6b-v3", "whisper-large-v3-turbo"):
            self.assertIn(expected, slugs)
        for model in catalog["models"]:
            selected = kwispr_models.default_file(model)
            self.assertEqual(selected["quant"], model["default_quant"])
            self.assertTrue(selected["filename"].endswith(".gguf"))

    def test_default_quant_controls_model_path_and_list_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            catalog_path, model = self.write_catalog(tmp)
            self.assertEqual(kwispr_models.default_file(model)["quant"], "Q8_0")
            self.assertEqual(kwispr_models.model_path(tmp / "models", model).name, "tiny model-Q8_0.gguf")
            self.assertEqual(self.run_cli("--catalog", str(catalog_path), "--model-dir", str(tmp / "models"), "list"), 0)
            self.assertIn("tiny-model\tnot-installed\t0 MB\tTiny test model (Q8_0)", self.last_stdout)

    def test_urls_quote_segments_and_pin_revision_with_mirror_then_hf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            catalog_path, model = self.write_catalog(tmp)
            catalog = kwispr_models.load_catalog(catalog_path)
            urls = kwispr_models.download_urls(catalog, model, kwispr_models.default_file(model))
            suffix = "example%20org/tiny%20model-gguf/" + "a" * 40 + "/tiny%20model-Q8_0.gguf"
            self.assertEqual(urls[0], f"{(tmp / 'source').as_uri()}/{suffix}")
            self.assertEqual(urls[1], f"https://huggingface.co/{suffix.replace('/' + 'a' * 40 + '/', '/resolve/' + 'a' * 40 + '/')}")
            self.assertNotIn("/resolve/main/", urls[1])

    def test_download_url_progress_is_throttled_with_initial_and_final_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            destination = Path(tmp_s) / "download.gguf"
            reports: list[tuple[int, int]] = []
            clock_values = iter((0.0, 0.05, 0.16, 0.17))
            old_chunk_size = kwispr_models.DOWNLOAD_CHUNK_BYTES
            kwispr_models.DOWNLOAD_CHUNK_BYTES = 2
            try:
                with mock.patch.object(kwispr_models.urllib.request, "urlopen", return_value=io.BytesIO(b"abcdef")):
                    kwispr_models.download_url(
                        "https://example.invalid/model.gguf",
                        destination,
                        6,
                        lambda done, total: reports.append((done, total)),
                        clock=lambda: next(clock_values),
                        progress_interval=0.1,
                    )
            finally:
                kwispr_models.DOWNLOAD_CHUNK_BYTES = old_chunk_size
            self.assertEqual(destination.read_bytes(), b"abcdef")
            self.assertEqual(reports, [(0, 6), (4, 6), (6, 6)])

    def test_jsonl_progress_protocol_is_valid_monotonic_and_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            catalog_path, _ = self.write_catalog(tmp)
            rc = self.run_cli(
                "--catalog", str(catalog_path), "--model-dir", str(tmp / "models"),
                "download", "tiny-model", "--progress", "jsonl",
            )

            self.assertEqual(rc, 0)
            events = [json.loads(line) for line in self.last_stdout.splitlines()]
            self.assertTrue(events)
            for event in events:
                self.assertEqual(event["protocol"], kwispr_models.PROGRESS_PROTOCOL)
                self.assertEqual(event["model_slug"], "tiny-model")
                self.assertEqual(event["bytes_total"], len(b"test gguf bytes"))
                self.assertLessEqual(event["bytes_done"], event["bytes_total"])
            self.assertEqual(events[0]["event"], "attempt")
            progress = [event["bytes_done"] for event in events if event["event"] == "progress"]
            self.assertEqual(progress[0], 0)
            self.assertEqual(progress[-1], len(b"test gguf bytes"))
            self.assertEqual(progress, sorted(progress))
            phases = [event.get("phase") for event in events if event["event"] == "status"]
            self.assertIn("checksum-verification", phases)
            self.assertIn("installing", phases)
            self.assertEqual(phases[-1], "done")
            self.assertNotIn("downloading ", self.last_stdout)

    def test_jsonl_fallback_emits_reset_and_restarts_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            catalog_path, model = self.write_catalog(tmp)
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            catalog["mirrors"] = [(tmp / "missing-mirror").as_uri()]
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            selected_file = kwispr_models.default_file(model)
            hf_file = tmp / "hf" / "example org" / "tiny model-gguf" / "resolve" / model["revision"] / selected_file["filename"]
            hf_file.parent.mkdir(parents=True)
            hf_file.write_bytes(b"test gguf bytes")
            old_hf = kwispr_models.HF_BASE_URL
            kwispr_models.HF_BASE_URL = (tmp / "hf").as_uri()
            try:
                rc = self.run_cli(
                    "--catalog", str(catalog_path), "--model-dir", str(tmp / "models"),
                    "download", "tiny-model", "--progress", "jsonl",
                )
            finally:
                kwispr_models.HF_BASE_URL = old_hf

            self.assertEqual(rc, 0)
            events = [json.loads(line) for line in self.last_stdout.splitlines()]
            attempts = [event for event in events if event["event"] == "attempt"]
            self.assertEqual([event["attempt"] for event in attempts], [1, 2])
            reset_index = next(i for i, event in enumerate(events) if event["event"] == "reset")
            self.assertEqual(events[reset_index]["bytes_done"], 0)
            self.assertEqual(events[reset_index + 1]["event"], "attempt")
            second_progress = [
                event["bytes_done"] for event in events[reset_index:]
                if event["event"] == "progress"
            ]
            self.assertEqual(second_progress[0], 0)
            self.assertEqual(second_progress, sorted(second_progress))
            self.assertEqual(second_progress[-1], selected_file["size_bytes"])

    def test_normal_download_output_contract_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            catalog_path, model = self.write_catalog(tmp)
            target = kwispr_models.model_path(tmp / "models", model)
            self.assertEqual(self.run_cli(
                "--catalog", str(catalog_path), "--model-dir", str(tmp / "models"),
                "download", "tiny-model",
            ), 0)
            self.assertEqual(
                self.last_stdout,
                f"downloading {json.loads(catalog_path.read_text(encoding='utf-8'))['mirrors'][0]}/example%20org/tiny%20model-gguf/{'a' * 40}/tiny%20model-Q8_0.gguf\n"
                f"tiny-model: installed at {target}\n",
            )

    def test_local_download_verify_idempotence_tamper_and_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            catalog_path, model = self.write_catalog(tmp)
            model_dir = tmp / "models"
            args = ("--catalog", str(catalog_path), "--model-dir", str(model_dir))

            self.assertEqual(self.run_cli(*args, "download", "tiny-model"), 0)
            target = kwispr_models.model_path(model_dir, model)
            self.assertEqual(target.read_bytes(), b"test gguf bytes")
            self.assertEqual([path.name for path in model_dir.iterdir()], [target.name])
            self.assertEqual(self.run_cli(*args, "verify", "tiny-model"), 0)
            self.assertIn("tiny-model: ok", self.last_stdout)

            self.assertEqual(self.run_cli(*args, "download", "tiny-model"), 0)
            self.assertIn("already installed", self.last_stdout)

            target.write_bytes(b"tampered")
            self.assertEqual(self.run_cli(*args, "verify", "tiny-model"), 1)
            self.assertIn("missing-or-invalid", self.last_stdout)
            self.assertEqual(self.run_cli(*args, "download", "tiny-model"), 0)
            self.assertEqual(target.read_bytes(), b"test gguf bytes")
            self.assertEqual(self.run_cli(*args, "verify", "tiny-model"), 0)

    def test_delete_removes_only_catalog_default_gguf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            catalog_path, model = self.write_catalog(tmp)
            model_dir = tmp / "models"
            model_dir.mkdir()
            target = kwispr_models.model_path(model_dir, model)
            target.write_bytes(b"installed artifact")
            unrelated = model_dir / "keep-me.gguf"
            unrelated.write_bytes(b"unrelated")
            non_default = model_dir / "tiny-Q4.gguf"
            non_default.write_bytes(b"other quant")

            rc = self.run_cli(
                "--catalog", str(catalog_path), "--model-dir", str(model_dir),
                "delete", "tiny-model",
            )

            self.assertEqual(rc, 0)
            self.assertFalse(target.exists())
            self.assertEqual(unrelated.read_bytes(), b"unrelated")
            self.assertEqual(non_default.read_bytes(), b"other quant")
            self.assertIn("tiny-model: deleted", self.last_stdout)

    def test_delete_missing_model_file_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            catalog_path, _ = self.write_catalog(tmp)
            model_dir = tmp / "models"
            common = ("--catalog", str(catalog_path), "--model-dir", str(model_dir), "delete", "tiny-model")

            self.assertEqual(self.run_cli(*common), 0)
            self.assertIn("tiny-model: not installed", self.last_stdout)
            self.assertEqual(self.run_cli(*common), 0)
            self.assertIn("tiny-model: not installed", self.last_stdout)
            self.assertFalse(model_dir.exists())

    def test_delete_rejects_unknown_slug_without_touching_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            catalog_path, _ = self.write_catalog(tmp)
            model_dir = tmp / "models"
            model_dir.mkdir()
            unrelated = model_dir / "do-not-delete.gguf"
            unrelated.write_bytes(b"safe")

            rc = self.run_cli(
                "--catalog", str(catalog_path), "--model-dir", str(model_dir),
                "delete", "../do-not-delete.gguf",
            )

            self.assertEqual(rc, 2)
            self.assertIn("unknown model", self.last_stderr)
            self.assertEqual(unrelated.read_bytes(), b"safe")

    def test_missing_mirror_falls_back_to_revision_pinned_hf_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            catalog_path, model = self.write_catalog(tmp)
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            catalog["mirrors"] = [(tmp / "missing-mirror").as_uri()]
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            file = kwispr_models.default_file(model)
            hf_file = tmp / "hf" / "example org" / "tiny model-gguf" / "resolve" / model["revision"] / file["filename"]
            hf_file.parent.mkdir(parents=True)
            hf_file.write_bytes(b"test gguf bytes")
            old_hf = kwispr_models.HF_BASE_URL
            kwispr_models.HF_BASE_URL = (tmp / "hf").as_uri()
            try:
                rc = self.run_cli("--catalog", str(catalog_path), "--model-dir", str(tmp / "models"), "download", "tiny-model")
            finally:
                kwispr_models.HF_BASE_URL = old_hf
            self.assertEqual(rc, 0)
            self.assertEqual(kwispr_models.model_path(tmp / "models", model).read_bytes(), b"test gguf bytes")
            self.assertEqual(self.last_stdout.count("downloading "), 2)

    def test_size_mismatch_does_not_install_or_stream_past_catalog_limit(self) -> None:
        for declared_size, expected_error in ((3, "exceeds catalog size"), (100, "size mismatch")):
            with self.subTest(declared_size=declared_size), tempfile.TemporaryDirectory() as tmp_s:
                tmp = Path(tmp_s)
                catalog_path, model = self.write_catalog(tmp)
                catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
                catalog["models"][0]["files"][1]["size_bytes"] = declared_size
                catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
                old_hf = kwispr_models.HF_BASE_URL
                kwispr_models.HF_BASE_URL = (tmp / "missing-hf").as_uri()
                try:
                    rc = self.run_cli(
                        "--catalog",
                        str(catalog_path),
                        "--model-dir",
                        str(tmp / "models"),
                        "download",
                        "tiny-model",
                    )
                finally:
                    kwispr_models.HF_BASE_URL = old_hf
                self.assertEqual(rc, 1)
                self.assertFalse(kwispr_models.model_path(tmp / "models", model).exists())
                self.assertIn(expected_error, self.last_stderr)

    def test_checksum_failure_does_not_install_untrusted_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            catalog_path, model = self.write_catalog(tmp)
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            catalog["models"][0]["files"][1]["sha256"] = "f" * 64
            catalog["mirrors"] = [catalog["mirrors"][0]]
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            old_hf = kwispr_models.HF_BASE_URL
            kwispr_models.HF_BASE_URL = (tmp / "missing-hf").as_uri()
            try:
                rc = self.run_cli("--catalog", str(catalog_path), "--model-dir", str(tmp / "models"), "download", "tiny-model")
            finally:
                kwispr_models.HF_BASE_URL = old_hf
            self.assertEqual(rc, 1)
            self.assertFalse(kwispr_models.model_path(tmp / "models", model).exists())
            self.assertIn("checksum mismatch", self.last_stderr)

    def test_unknown_model_is_a_clean_cli_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            catalog_path, _ = self.write_catalog(tmp)
            common = ("--catalog", str(catalog_path), "--model-dir", str(tmp / "models"))
            self.assertEqual(self.run_cli(*common, "download", "missing"), 2)
            self.assertIn("unknown model: missing", self.last_stderr)
            self.assertEqual(self.run_cli(*common, "verify", "missing"), 2)
            self.assertIn("unknown model: missing", self.last_stderr)
            self.assertEqual(self.run_cli(*common, "delete", "missing"), 2)
            self.assertIn("unknown model: missing", self.last_stderr)

    def test_catalog_rejects_duplicate_slugs_and_missing_default_quant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            catalog_path, _ = self.write_catalog(tmp)
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            catalog["models"].append(dict(catalog["models"][0]))
            with self.assertRaisesRegex(kwispr_models.CatalogError, "duplicate model slug"):
                kwispr_models.validate_catalog(catalog)
            catalog["models"] = catalog["models"][:1]
            catalog["models"][0]["default_quant"] = "missing"
            with self.assertRaisesRegex(kwispr_models.CatalogError, "default_quant"):
                kwispr_models.validate_catalog(catalog)


if __name__ == "__main__":
    unittest.main()
