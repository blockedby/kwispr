#!/usr/bin/env python3
"""Test harness for exercising kwispr.sh without real desktop/API side effects."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]


class KwisprScriptHarness:
    def __enter__(self) -> "KwisprScriptHarness":
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        self.xdg_config_home = self.home / ".config"
        self.xdg_cache_home = self.home / ".cache"
        self.cache_dir = self.xdg_cache_home / "kwispr"
        self.repo.mkdir()
        self.home.mkdir()
        self.bin.mkdir()
        shutil.copy2(REPO_ROOT / "kwispr.sh", self.repo / "kwispr.sh")
        (self.repo / "sounds").mkdir()
        self._write_fakes()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._tmp.cleanup()

    def write_env(self, **values: str) -> Path:
        lines = [f"{key}={self._quote(value)}" for key, value in values.items()]
        path = self.repo / ".env"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def write_config(self, **values: str) -> Path:
        lines = [f"{key}={self._quote(value)}" for key, value in values.items()]
        path = self.xdg_config_home / "kwispr" / "config.env"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        path.chmod(0o600)
        return path

    def make_wav(self, name: str = "sample.wav", size: int = 40000) -> Path:
        wav = self.root / name
        wav.write_bytes(b"0" * size)
        return wav

    def fake_curl_response(self, http_code: int, body: dict[str, Any] | str) -> None:
        if not isinstance(body, str):
            body = json.dumps(body)
        (self.root / "curl_response.json").write_text(body, encoding="utf-8")
        (self.root / "curl_code.txt").write_text(str(http_code), encoding="utf-8")

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.bin}:{env.get('PATH', '')}",
                "XDG_CONFIG_HOME": str(self.xdg_config_home),
                "XDG_CACHE_HOME": str(self.xdg_cache_home),
                "KWISPR_CONTRACT_ROOT": str(self.root),
            }
        )
        return subprocess.run(
            [str(self.repo / "kwispr.sh"), *args],
            cwd=self.repo,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )

    def clipboard_text(self) -> str:
        path = self.root / "clipboard.txt"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def curl_invocations(self) -> list[list[str]]:
        path = self.root / "curl_invocations.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def data_binary_path(self, args: list[str]) -> Path:
        # Fake curl copies --data-binary @file payloads before kwispr.sh deletes
        # the mktemp request JSON.
        copied = self.root / "curl_data_binary_0.json"
        if copied.exists():
            return copied
        for index, arg in enumerate(args):
            candidate = ""
            if arg == "--data-binary" and index + 1 < len(args):
                candidate = args[index + 1]
            elif arg.startswith("--data-binary="):
                candidate = arg.split("=", 1)[1]
            if candidate.startswith("@"):
                return Path(candidate[1:])
        raise AssertionError(f"No --data-binary @file in curl args: {args}")

    def _write_fakes(self) -> None:
        self._write_executable(
            "curl",
            r'''#!/usr/bin/env python3
import json, os, shutil, sys
root = os.environ["KWISPR_CONTRACT_ROOT"]
args = sys.argv[1:]
with open(os.path.join(root, "curl_invocations.jsonl"), "a", encoding="utf-8") as fh:
    fh.write(json.dumps(args) + "\n")
out = None
for i, arg in enumerate(args):
    if arg == "-o" and i + 1 < len(args):
        out = args[i + 1]
for i, arg in enumerate(args):
    value = None
    if arg == "--data-binary" and i + 1 < len(args):
        value = args[i + 1]
    elif arg.startswith("--data-binary="):
        value = arg.split("=", 1)[1]
    if value and value.startswith("@"):
        src = value[1:]
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(root, "curl_data_binary_0.json"))
body_path = os.path.join(root, "curl_response.json")
code_path = os.path.join(root, "curl_code.txt")
body = open(body_path, encoding="utf-8").read() if os.path.exists(body_path) else "{}"
code = open(code_path, encoding="utf-8").read().strip() if os.path.exists(code_path) else "200"
if out:
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(body)
sys.stdout.write(code)
''',
        )
        self._write_executable(
            "wl-copy",
            r'''#!/usr/bin/env python3
import os, sys
root = os.environ["KWISPR_CONTRACT_ROOT"]
text = sys.stdin.read()
with open(os.path.join(root, "clipboard.txt"), "w", encoding="utf-8") as fh:
    fh.write(text)
''',
        )
        self._write_executable(
            "notify-send",
            r'''#!/usr/bin/env python3
import sys
if "-p" in sys.argv[1:]:
    print("1")
''',
        )
        self._write_executable("ydotool", "#!/usr/bin/env bash\nexit 0\n")
        self._write_executable("paplay", "#!/usr/bin/env bash\nexit 0\n")

    def _write_executable(self, name: str, content: str) -> None:
        path = self.bin / name
        path.write_text(content, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    @staticmethod
    def _quote(value: str) -> str:
        if value == "" or any(ch.isspace() for ch in value) or any(ch in value for ch in "'\"$`\\"):
            return "'" + value.replace("'", "'\\''") + "'"
        return value
