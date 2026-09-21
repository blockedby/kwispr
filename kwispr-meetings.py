#!/usr/bin/env python3
"""Kwispr meeting CLI; optionally use the installed offline processing runtime."""
from pathlib import Path
import os
import sys


def main() -> int:
    runtime = Path(os.environ.get("KWISPR_MEETING_RUNTIME_DIR") or
                   str(Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser() /
                       "kwispr/meeting-runtime")).expanduser()
    python = runtime / "venv/bin/python"
    # Compare prefixes, not resolved executable paths: a venv Python is a symlink.
    if python.is_file() and Path(sys.prefix).resolve() != (runtime / "venv").resolve():
        os.execv(str(python), [str(python), str(Path(__file__).resolve()), *sys.argv[1:]])
    from kwispr_meetings.sessions import main as session_main
    return session_main()


if __name__ == "__main__":
    raise SystemExit(main())
