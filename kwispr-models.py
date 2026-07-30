#!/usr/bin/env python3
"""Manage Kwispr's revision-pinned GGUF model catalog."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

DEFAULT_CATALOG = Path(__file__).resolve().parent / "models" / "local-stt-catalog.json"
HF_BASE_URL = "https://huggingface.co"
DOWNLOAD_TIMEOUT_SECONDS = 60
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
PROGRESS_INTERVAL_SECONDS = 0.15
PROGRESS_PROTOCOL = "kwispr-model-download-v1"


class CatalogError(ValueError):
    """The catalog is not usable by the Kwispr downloader."""


class DownloadError(OSError):
    """Downloaded bytes do not match the catalog's declared size."""


def default_model_dir() -> Path:
    if os.environ.get("KWISPR_MODEL_DIR"):
        return Path(os.environ["KWISPR_MODEL_DIR"]).expanduser()
    data_home = Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser()
    return data_home / "kwispr" / "models"


def load_catalog(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        catalog = json.load(f)
    validate_catalog(catalog)
    return catalog


def validate_catalog(catalog: dict[str, Any]) -> None:
    if catalog.get("catalog_version") != 2:
        raise CatalogError("catalog_version must be 2")
    models = catalog.get("models")
    if not isinstance(models, list) or not models:
        raise CatalogError("catalog models must be a non-empty list")

    slugs: set[str] = set()
    repo_ids: set[str] = set()
    filenames: set[str] = set()
    for model in models:
        slug = model.get("slug")
        repo_id = model.get("id")
        revision = model.get("revision")
        files = model.get("files")
        default_quant = model.get("default_quant")
        if not isinstance(slug, str) or not slug or slug in slugs:
            raise CatalogError(f"invalid or duplicate model slug: {slug!r}")
        repo_parts = repo_id.split("/") if isinstance(repo_id, str) else []
        if len(repo_parts) != 2 or any(part in {"", ".", ".."} for part in repo_parts) or repo_id in repo_ids:
            raise CatalogError(f"invalid or duplicate Hugging Face repo id: {repo_id!r}")
        if not isinstance(revision, str) or len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
            raise CatalogError(f"model {slug}: revision must be a 40-character lowercase commit SHA")
        if not isinstance(files, list) or not files:
            raise CatalogError(f"model {slug}: files must be non-empty")
        quant_files = [f for f in files if f.get("quant") == default_quant]
        if len(quant_files) != 1:
            raise CatalogError(f"model {slug}: default_quant must select exactly one file")
        for file in files:
            filename = file.get("filename")
            sha256 = file.get("sha256")
            if not isinstance(filename, str) or Path(filename).name != filename or not filename.endswith(".gguf"):
                raise CatalogError(f"model {slug}: invalid GGUF filename {filename!r}")
            if not isinstance(sha256, str) or len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
                raise CatalogError(f"model {slug}: invalid SHA256 for {filename}")
            if not isinstance(file.get("size_bytes"), int) or file["size_bytes"] <= 0:
                raise CatalogError(f"model {slug}: invalid size for {filename}")
        selected_name = quant_files[0]["filename"]
        if selected_name in filenames:
            raise CatalogError(f"duplicate default filename: {selected_name}")
        slugs.add(slug)
        repo_ids.add(repo_id)
        filenames.add(selected_name)


def model_map(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map user-facing Handy slugs to catalog models."""
    return {model["slug"]: model for model in catalog["models"]}


def default_file(model: dict[str, Any]) -> dict[str, Any]:
    quant = model["default_quant"]
    return next(file for file in model["files"] if file["quant"] == quant)


def model_path(root: Path, model: dict[str, Any]) -> Path:
    return root / default_file(model)["filename"]


def quote_path(value: str) -> str:
    return "/".join(quote(segment, safe="") for segment in value.split("/"))


def download_urls(catalog: dict[str, Any], model: dict[str, Any], file: dict[str, Any]) -> list[str]:
    repo = quote_path(model["id"])
    revision = quote(model["revision"], safe="")
    filename = quote(file["filename"], safe="")
    suffix = f"{repo}/{revision}/{filename}"
    mirrors = [f"{base.rstrip('/')}/{suffix}" for base in catalog.get("mirrors", [])]
    return mirrors + [f"{HF_BASE_URL}/{repo}/resolve/{revision}/{filename}"]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_installed(root: Path, model: dict[str, Any]) -> bool:
    target = model_path(root, model)
    return target.is_file() and sha256_file(target) == default_file(model)["sha256"]


def download_url(
    url: str,
    dest: Path,
    expected_size: int,
    progress: Callable[[int, int], None] | None = None,
    *,
    clock: Callable[[], float] = time.monotonic,
    progress_interval: float = PROGRESS_INTERVAL_SECONDS,
) -> None:
    """Download exactly expected_size bytes, with throttled initial/final progress."""
    request = urllib.request.Request(url, headers={"User-Agent": "kwispr-models/2.0"})
    downloaded = 0
    last_reported_at = clock()
    if progress:
        progress(0, expected_size)
    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response, dest.open("wb") as out:
        while chunk := response.read(DOWNLOAD_CHUNK_BYTES):
            downloaded += len(chunk)
            if downloaded > expected_size:
                raise DownloadError(
                    f"download exceeds catalog size (expected {expected_size} bytes)"
                )
            out.write(chunk)
            now = clock()
            if progress and (downloaded == expected_size or now - last_reported_at >= progress_interval):
                progress(downloaded, expected_size)
                last_reported_at = now
    if downloaded != expected_size:
        raise DownloadError(
            f"download size mismatch (expected {expected_size} bytes, got {downloaded})"
        )


class JsonlProgress:
    """Emit the stable, line-delimited model download protocol."""

    def __init__(self, slug: str, bytes_total: int) -> None:
        self.slug = slug
        self.bytes_total = bytes_total

    def emit(self, event: str, bytes_done: int, **fields: Any) -> None:
        payload = {
            "protocol": PROGRESS_PROTOCOL,
            "event": event,
            "model_slug": self.slug,
            "bytes_done": bytes_done,
            "bytes_total": self.bytes_total,
        }
        payload.update(fields)
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)

    def status(self, phase: str, bytes_done: int) -> None:
        self.emit("status", bytes_done, phase=phase)


def cmd_list(args: argparse.Namespace) -> int:
    catalog = load_catalog(args.catalog)
    for model in catalog["models"]:
        file = default_file(model)
        status = "installed" if is_installed(args.model_dir, model) else "not-installed"
        size_mb = file["size_bytes"] / (1024 * 1024)
        print(f"{model['slug']}\t{status}\t{size_mb:.0f} MB\t{model['name']} ({file['quant']})")
    return 0


def selected_models(catalog: dict[str, Any], slug: str | None) -> list[dict[str, Any]] | None:
    models = model_map(catalog)
    if slug is None:
        return list(models.values())
    model = models.get(slug)
    return [model] if model is not None else None


def cmd_verify(args: argparse.Namespace) -> int:
    catalog = load_catalog(args.catalog)
    selected = selected_models(catalog, args.model)
    if selected is None:
        print(f"unknown model: {args.model}", file=sys.stderr)
        return 2
    ok = True
    for model in selected:
        installed = is_installed(args.model_dir, model)
        print(f"{model['slug']}: {'ok' if installed else 'missing-or-invalid'}")
        ok = ok and installed
    return 0 if ok else 1


def cmd_delete(args: argparse.Namespace) -> int:
    """Delete only the catalog-managed default GGUF for a model slug."""
    catalog = load_catalog(args.catalog)
    selected = selected_models(catalog, args.model)
    if selected is None:
        print(f"unknown model: {args.model}", file=sys.stderr)
        return 2
    model = selected[0]
    target = model_path(args.model_dir, model)
    try:
        target.unlink()
        print(f"{model['slug']}: deleted {target}")
    except FileNotFoundError:
        print(f"{model['slug']}: not installed")
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    catalog = load_catalog(args.catalog)
    selected = selected_models(catalog, args.model)
    if selected is None:
        print(f"unknown model: {args.model}", file=sys.stderr)
        return 2
    model = selected[0]
    file = default_file(model)
    total = file["size_bytes"]
    jsonl = getattr(args, "progress", None) == "jsonl"
    reporter = JsonlProgress(model["slug"], total) if jsonl else None
    root = args.model_dir
    root.mkdir(parents=True, exist_ok=True)
    target = model_path(root, model)
    if is_installed(root, model):
        if reporter:
            reporter.status("already-installed", total)
            reporter.emit("progress", total)
            reporter.status("done", total)
        else:
            print(f"{model['slug']}: already installed at {target}")
        return 0

    urls = download_urls(catalog, model, file)
    with tempfile.TemporaryDirectory(prefix="kwispr-model-", dir=str(root)) as tmp_dir:
        temporary = Path(tmp_dir) / file["filename"]
        errors: list[str] = []
        for attempt, url in enumerate(urls, start=1):
            if reporter:
                if attempt > 1:
                    reporter.emit("reset", 0, attempt=attempt)
                reporter.emit("attempt", 0, attempt=attempt)
                reporter.status("downloading", 0)
            else:
                print(f"downloading {url}")
            try:
                download_url(
                    url,
                    temporary,
                    total,
                    (lambda done, _total: reporter.emit("progress", done)) if reporter else None,
                )
            except (OSError, urllib.error.URLError) as error:
                errors.append(f"{url}: {error}")
                temporary.unlink(missing_ok=True)
                continue
            if reporter:
                reporter.status("checksum-verification", total)
            actual = sha256_file(temporary)
            if actual != file["sha256"]:
                errors.append(f"{url}: checksum mismatch (expected {file['sha256']}, got {actual})")
                temporary.unlink(missing_ok=True)
                continue
            if reporter:
                reporter.status("installing", total)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary, target)
            if reporter:
                reporter.status("done", total)
            else:
                print(f"{model['slug']}: installed at {target}")
            return 0

    if reporter:
        reporter.status("failed", 0)
    print(f"failed to download a verified copy of {model['slug']}", file=sys.stderr)
    if reporter:
        for error in errors[:5]:
            bounded = error if len(error) <= 500 else error[:497] + "..."
            print(f"  {bounded}", file=sys.stderr)
        if len(errors) > 5:
            print(f"  ... and {len(errors) - 5} more errors", file=sys.stderr)
    else:
        for error in errors:
            print(f"  {error}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG, help=f"catalog JSON (default: {DEFAULT_CATALOG})")
    parser.add_argument("--model-dir", type=Path, default=default_model_dir(), help="model install directory (default: $KWISPR_MODEL_DIR or $XDG_DATA_HOME/kwispr/models)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list catalog models and install status").set_defaults(func=cmd_list)
    download = sub.add_parser("download", help="download and install a model's default GGUF quant")
    download.add_argument("model", help="catalog model slug")
    download.add_argument("--progress", choices=("jsonl",), help="emit machine-readable JSONL progress on stdout")
    download.set_defaults(func=cmd_download)
    delete = sub.add_parser("delete", help="delete a model's catalog-managed default GGUF")
    delete.add_argument("model", help="catalog model slug")
    delete.set_defaults(func=cmd_delete)
    verify = sub.add_parser("verify", help="verify installed model checksums")
    verify.add_argument("model", nargs="?", help="catalog model slug (default: all)")
    verify.set_defaults(func=cmd_verify)
    args = parser.parse_args(argv)
    args.model_dir = args.model_dir.expanduser()
    try:
        return args.func(args)
    except (CatalogError, json.JSONDecodeError, OSError) as error:
        print(f"catalog error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
