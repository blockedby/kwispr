#!/usr/bin/env python3
"""Install the optional, pinned local meeting runtime without system packages."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

from kwispr_meetings.pipeline import MODEL_FILES, atomic_json, runtime_dir
from kwispr_meetings.config import load_config

RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
ASSETS = {
    "segmentation.tar.bz2": (RELEASES + "speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2",
                             6958444, "24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488"),
    "embedding.onnx": (RELEASES + "speaker-recongition-models/nemo_en_titanet_small.onnx",
                       40257283, MODEL_FILES["embedding.onnx"][1]),
}
PACKAGES = ["sherpa-onnx==1.13.8", "sherpa-onnx-core==1.13.8",
            "numpy==2.5.3" if sys.version_info >= (3, 12) else "numpy==2.2.6"]


def verified(path, size, digest):
    if not path.is_file() or path.stat().st_size != size:
        return False
    with path.open("rb") as stream:
        checksum = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
        return checksum.hexdigest() == digest


def download(path, url, size, digest):
    if verified(path, size, digest):
        return
    part = path.with_name(path.name + ".part")
    print(f"Downloading {path.name} ({size // 1024 // 1024} MB)…", flush=True)
    try:
        with urllib.request.urlopen(url, timeout=60) as source, part.open("wb") as output:
            received = 0
            while data := source.read(1024 * 1024):
                received += len(data)
                if received > size:
                    raise RuntimeError(f"{path.name}: unexpected download size")
                output.write(data)
        if not verified(part, size, digest):
            raise RuntimeError(f"{path.name}: checksum verification failed")
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)


def install(destination):
    destination.mkdir(parents=True, exist_ok=True)
    with (destination / "setup.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Meeting setup is already running.")
        venv = destination / "venv"
        python = venv / "bin/python"
        uv = shutil.which("uv")
        if not python.exists():
            print("Creating an isolated Python environment…", flush=True)
            subprocess.run([uv, "venv", str(venv), "--python", sys.executable] if uv else
                           [sys.executable, "-m", "venv", str(venv)], check=True)
        print("Installing the meeting runtime…", flush=True)
        command = [uv, "pip", "install", "--python", str(python), *PACKAGES] if uv else [str(python), "-m", "pip", "install", *PACKAGES]
        subprocess.run(command, check=True)
        models = destination / "models"
        models.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="download-", dir=destination) as temporary:
            temporary = Path(temporary)
            if not verified(models / "segmentation.onnx", *MODEL_FILES["segmentation.onnx"]):
                archive = temporary / "segmentation.tar.bz2"
                download(archive, *ASSETS[archive.name])
                # Read only named regular members, never extract archive paths.
                with tarfile.open(archive) as source:
                    prefix = "sherpa-onnx-pyannote-segmentation-3-0/"
                    for member_name, target_name in [("model.onnx", "segmentation.onnx"), ("LICENSE", "segmentation.LICENSE")]:
                        member = source.getmember(prefix + member_name)
                        if not member.isfile() or member.size > 8 * 1024 * 1024:
                            raise RuntimeError("Unexpected segmentation model archive member")
                        output = temporary / target_name
                        with source.extractfile(member) as data, output.open("wb") as target:
                            shutil.copyfileobj(data, target)
                        if target_name.endswith(".onnx") and not verified(output, *MODEL_FILES[target_name]):
                            raise RuntimeError("Extracted model checksum verification failed")
                        output.replace(models / target_name)
            download(models / "embedding.onnx", *ASSETS["embedding.onnx"])
        atomic_json(models / "manifest.json", {
            "segmentation": {"source": ASSETS["segmentation.tar.bz2"][0], "sha256": MODEL_FILES["segmentation.onnx"][1]},
            "embedding": {"source": ASSETS["embedding.onnx"][0], "sha256": MODEL_FILES["embedding.onnx"][1]},
            "packages": PACKAGES,
        })
        subprocess.run([str(python), "-c", "import sherpa_onnx, numpy; print('Meeting runtime imports OK')"], check=True)
        print(f"Meeting models are ready: {models}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    try:
        install((args.directory or runtime_dir(load_config())).expanduser().absolute())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, tarfile.TarError) as error:
        print(f"Meeting setup failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
