#!/usr/bin/env python3
"""Kwispr meeting CLI; optionally use the installed offline processing runtime."""
from pathlib import Path
import os
import sys


def main() -> int:
    from kwispr_meetings.config import ConfigError, load_config, runtime_dir
    try:
        config = load_config()
    except (ConfigError, OSError):
        # Control commands must still stop a running recorder if config was damaged.
        # Start/process report the configuration error in the session CLI.
        config = {}
    runtime = runtime_dir(config)
    python = runtime / "venv/bin/python"
    # Compare prefixes, not resolved executable paths: a venv Python is a symlink.
    if python.is_file() and Path(sys.prefix).resolve() != (runtime / "venv").resolve():
        os.execv(str(python), [str(python), str(Path(__file__).resolve()), *sys.argv[1:]])
    from kwispr_meetings.sessions import main as session_main
    return session_main()


if __name__ == "__main__":
    raise SystemExit(main())
