#!/usr/bin/env python3
"""Transcribe one imported audio file through the local Whisper service."""
from __future__ import annotations

import argparse
import json
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    transcribe = commands.add_parser("transcribe", help="Transcribe one local audio file")
    transcribe.add_argument("source_file")
    transcribe.add_argument("--output-dir", default="~/Documents/Kwispr/Transcriptions")
    args = parser.parse_args(argv)

    def progress(message, completed, total):
        print(json.dumps({"event": "progress", "message": message,
                          "completed": completed, "total": total}, ensure_ascii=False), flush=True)

    try:
        from kwispr_meetings.audio_files import transcribe as run
        result = run(args.source_file, args.output_dir, progress)
    except (OSError, ValueError, RuntimeError) as error:
        message = str(error) or type(error).__name__
        print(message, file=sys.stderr)
        print(json.dumps({"event": "error", "message": message}, ensure_ascii=False), flush=True)
        return 1
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
