#!/usr/bin/env python3
"""Reprocess saved two-track meetings with OpenRouter chat audio.

Dry run by default. --execute sends every 45-second source window, including silence.
The USD budget is soft: actual usage is known only after a request returns. Each
in-flight request reserves $0.05, but a single request (especially at 8192
output tokens) can cost more. Configure a separate provider-side hard limit.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sys
import urllib.request
import urllib.error
import uuid
import wave

sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmark_meeting_audio as benchmark

RATE = 16000
WINDOW = 45 * RATE
RESERVE = 0.05
TRACKS = (("microphone", "self", "Я"), ("remote", "speaker_01", "Собеседник 1"))
UNKNOWN = object()


def _sha_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _wav_info(path):
    try:
        with wave.open(str(path), "rb") as wav:
            if (wav.getnchannels(), wav.getframerate(), wav.getsampwidth(), wav.getcomptype()) != (1, RATE, 2, "NONE"):
                raise ValueError()
            return wav.getnframes()
    except (wave.Error, EOFError, ValueError, OSError):
        raise RuntimeError("Source must be PCM 16 kHz mono 16-bit WAV") from None


def _chunk_bytes(path, first, last):
    with wave.open(str(path), "rb") as source:
        source.setpos(first)
        frames = source.readframes(last - first)
    if len(frames) != (last - first) * 2:
        raise RuntimeError("Source WAV ended before planned frame boundary")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(RATE)
        target.writeframes(frames)
    return buffer.getvalue()


def _plan(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("Invalid plan")
    model, prompt = data.get("model"), data.get("prompt")
    tokens, effort = data.get("max_output_tokens"), data.get("reasoning_effort")
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:+/-]*", model):
        raise RuntimeError("Invalid plan model ID")
    if not isinstance(prompt, str) or not 1 <= len(prompt) <= 8000:
        raise RuntimeError("Invalid plan prompt")
    if type(tokens) is not int or not 1 <= tokens <= 32768:
        raise RuntimeError("Invalid plan max_output_tokens")
    if effort not in (None, "minimal", "low", "medium", "high"):
        raise RuntimeError("Invalid plan reasoning_effort")
    return {"model": model, "prompt": prompt, "max_output_tokens": tokens, "reasoning_effort": effort}


def _identity(item, plan):
    fields = {"source_sha256": item["source_sha256"], "chunk_sha256": item["chunk_sha256"],
              "track": item["track"], "first_frame": item["first_frame"], "last_frame": item["last_frame"],
              "model": plan["model"], "prompt": plan["prompt"],
              "max_output_tokens": plan["max_output_tokens"], "reasoning_effort": plan["reasoning_effort"]}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _session_items(session, output_root, plan):
    source = session.resolve()
    metadata = json.loads((source / "session.json").read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise RuntimeError("Invalid session metadata")
    output = output_root / (re.sub(r"[^A-Za-z0-9_-]", "_", source.name) + "-" + hashlib.sha256(str(source).encode()).hexdigest()[:10])
    if output.resolve().is_relative_to(source):
        raise RuntimeError("Output directory must be outside source session")
    items = []
    sources = {}
    for track, speaker, _ in TRACKS:
        path = source / (track + ".wav")
        if not path.is_file() or path.is_symlink():
            raise RuntimeError("Missing or linked source WAV")
        count = _wav_info(path)
        source_sha = _sha_file(path)
        sources[track] = {"path": str(path), "sha256": source_sha, "frames": count}
        previous = 0
        for first in range(0, count, WINDOW):
            last = min(first + WINDOW, count)
            if first != previous:
                raise RuntimeError("Chunk plan has a gap or overlap")
            previous = last
            item = {"chunk_id": f"{track}-{first:010d}-{last:010d}", "track": track, "speaker": speaker,
                    "source_path": str(path), "source_sha256": source_sha,
                    "first_frame": first, "last_frame": last,
                    "chunk_sha256": hashlib.sha256(_chunk_bytes(path, first, last)).hexdigest()}
            item["identity"] = _identity(item, plan)
            items.append(item)
        if previous != count:
            raise RuntimeError("Chunk plan does not cover source")
    return output, metadata, sources, items


def _result_path(output, item):
    return output / "results" / (item["identity"] + ".json")


def _marker_path(output, item):
    return output / "results" / (item["identity"] + ".attempt")


def _diagnostic_path(output, item):
    return output / "results" / (item["identity"] + ".diagnostic")


def _safe_diagnostic(error):
    status = error.code if isinstance(error, urllib.error.HTTPError) else None
    # The shared request helper converts HTTPError to this fixed, sanitized text.
    if status is None and type(error) is RuntimeError:
        match = re.fullmatch(r"OpenRouter returned HTTP ([0-9]{3}); no automatic retry", str(error))
        if match:
            status = int(match.group(1))
    reason = error.reason if isinstance(error, urllib.error.URLError) else None
    return {"exception_class": type(error).__name__, "http_status": status,
            "timeout": isinstance(error, TimeoutError) or isinstance(reason, TimeoutError)}


def _cached(output, item, plan, continue_on_error=False):
    path, marker = _result_path(output, item), _marker_path(output, item)
    if not path.exists():
        if marker.exists():
            if continue_on_error:
                return UNKNOWN
            raise RuntimeError("Unresolved attempt marker; request outcome unknown")
        return None
    result = json.loads(path.read_text(encoding="utf-8"))
    if (result.get("identity") != item["identity"] or result.get("item") != item or
            result.get("plan") != plan or not isinstance(result.get("text"), str) or
            result.get("incomplete") is not (result.get("finish_reason") != "stop")):
        raise RuntimeError("Cached result identity mismatch")
    benchmark._cost(result.get("usage"))
    return result


def _send(output, item, plan, key):
    marker = _marker_path(output, item)
    benchmark._private_json(marker, {"identity": item["identity"], "chunk_id": item["chunk_id"]})
    try:
        raw = _chunk_bytes(Path(item["source_path"]), item["first_frame"], item["last_frame"])
        if hashlib.sha256(raw).hexdigest() != item["chunk_sha256"]:
            raise RuntimeError("Source changed after planning")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), benchmark.NoRedirect())
        text, finish, usage, generation = benchmark._request(
            opener, key, "chat", plan["model"], raw, plan["max_output_tokens"], plan["reasoning_effort"])
        result = {"identity": item["identity"], "item": item, "plan": plan, "text": text,
                  "finish_reason": finish, "incomplete": finish != "stop", "usage": usage,
                  "generation_id": generation}
        benchmark._atomic_json(_result_path(output, item), result)
        marker.unlink()
        benchmark._sync_directory(marker.parent)
        benchmark._cost(usage)
        return result
    except Exception as error:
        benchmark._atomic_json(_diagnostic_path(output, item), _safe_diagnostic(error))
        raise


def _private_text(path, content):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        benchmark._sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _publish(output, metadata, sources, items, results):
    rows = []
    for item in items:
        result = results.get(item["identity"])
        incomplete = result is None or result["incomplete"]
        rows.append({"start": item["first_frame"] / RATE, "end": item["last_frame"] / RATE,
                     "track": item["track"], "speakers": [item["speaker"]],
                     "text": result["text"] if result else "", "chunk_id": item["chunk_id"],
                     "incomplete": incomplete})
    rows.sort(key=lambda row: (row["start"], row["track"]))
    complete = all(not row["incomplete"] for row in rows)
    document = {"schema_version": 1, "title": metadata.get("title") or "Встреча",
                "started_at": metadata.get("started_at"),
                "speakers": {"self": "Я", "speaker_01": "Собеседник 1"},
                "source_tracks": sources, "complete": complete,
                "timing_note": "chunk bounds, not word alignment", "utterances": rows}
    benchmark._atomic_json(output / "transcript.json", document)
    lines = ["# " + str(document["title"]).replace("\n", " "), ""]
    if not complete:
        lines += ["**НЕПОЛНАЯ РАСШИФРОВКА: есть отсутствующие или обрезанные окна.**", ""]
    for row in rows:
        minutes, seconds = divmod(int(row["start"]), 60)
        label = document["speakers"][row["speakers"][0]]
        status = " [НЕПОЛНО/НЕТ РЕЗУЛЬТАТА]" if row["incomplete"] else ""
        lines.append(f"[{minutes:02d}:{seconds:02d}] **{label}{status}:** {row['text']}")
    _private_text(output / "transcript.md", "\n".join(lines) + "\n")
    return complete


def run(args):
    plan = _plan(args.plan)
    benchmark.PROMPT = plan["prompt"]  # Constant for this invocation, including all worker threads.
    if not math.isfinite(args.budget_usd) or args.budget_usd <= 0:
        raise RuntimeError("Budget must be finite and positive")
    root = args.output_root.expanduser().resolve()
    sessions = [Path(value).expanduser().resolve() for value in args.session]
    if len(set(sessions)) != len(sessions):
        raise RuntimeError("Duplicate session")
    prepared = [_session_items(session, root, plan) for session in sessions]
    if not args.execute:
        print(json.dumps({"execute": False, "sessions": len(prepared),
                          "chunks": sum(len(entry[3]) for entry in prepared)}, sort_keys=True))
        return 0
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    spent = 0.0
    unknown_reserved = 0.0
    inventory = []
    for output, metadata, sources, items in prepared:
        output.mkdir(mode=0o700, exist_ok=True)
        (output / "results").mkdir(mode=0o700, exist_ok=True)
        results = {}
        pending = []
        unknown = 0
        for item in items:
            cached = _cached(output, item, plan, args.continue_on_error)
            if cached is None:
                pending.append(item)
            elif cached is UNKNOWN:
                unknown += 1
                unknown_reserved += RESERVE
            else:
                results[item["identity"]] = cached
                spent += benchmark._cost(cached["usage"])
        inventory.append((output, metadata, sources, items, results, pending, unknown))
    if spent >= args.budget_usd and not args.continue_on_error:
        raise RuntimeError("Cached usage reaches soft budget")
    key = benchmark.read_key_file(args.key_file)[0] if args.key_file else os.environ.get("OPENROUTER_API_KEY", "")
    if any(pending for _, _, _, _, _, pending, _ in inventory) and not key:
        raise RuntimeError("OpenRouter key is required for --execute")
    any_incomplete = False
    for output, metadata, sources, items, results, pending, unknown in inventory:
        for track in sources.values():
            if _sha_file(Path(track["path"])) != track["sha256"]:
                raise RuntimeError("Source changed after planning")
        with ThreadPoolExecutor(max_workers=4) as pool:
            while pending:
                count = min(4, len(pending))
                while count and spent + unknown_reserved + RESERVE * count >= args.budget_usd:
                    count -= 1
                if not count:
                    break
                batch, pending = pending[:count], pending[count:]
                futures = [pool.submit(_send, output, item, plan, key) for item in batch]
                failure = None
                for future in futures:
                    try:
                        result = future.result()
                        results[result["identity"]] = result
                        spent += benchmark._cost(result["usage"])
                        if result["incomplete"]:
                            failure = "Incomplete model response saved; no automatic retry"
                    except Exception:
                        unknown += 1
                        unknown_reserved += RESERVE
                        failure = "Request outcome requires inspection; no automatic retry"
                if failure and not args.continue_on_error:
                    _publish(output, metadata, sources, items, results)
                    raise RuntimeError(failure)
        complete = _publish(output, metadata, sources, items, results)
        any_incomplete |= not complete
        print(json.dumps({"session_id": output.name, "chunks": len(items),
                          "completed": sum(not row["incomplete"] for row in results.values()),
                          "complete": complete, "unknown_attempts": unknown,
                          "spent_usd": round(spent, 6)}, sort_keys=True))
        if not complete and not args.continue_on_error:
            if any(result["incomplete"] for result in results.values()):
                raise RuntimeError("Cached incomplete response retained; no automatic retry")
            raise RuntimeError("Soft budget stopped before all windows were requested")
    if any_incomplete:
        raise RuntimeError("One or more session transcripts are incomplete; inspect attempt markers and diagnostics")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, action="append", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--continue-on-error", action="store_true",
                        help="Skip unknown attempts and continue other windows; exit nonzero if incomplete")
    parser.add_argument("--budget-usd", type=float, default=1.5)
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (OSError, ValueError, KeyError, json.JSONDecodeError, RuntimeError) as error:
        parser.exit(2, "Error: " + str(error) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
