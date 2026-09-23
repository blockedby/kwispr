#!/usr/bin/env python3
"""Compare reviewed short meeting clips with OpenRouter. Dry run is the default.

Example: python3 tools/benchmark_meeting_audio.py --manifest clips/manifest.json \
    --key-file ~/.config/kwispr/openrouter-benchmark.key --mode chat
Add --execute only after checking the models, clips, and account spending limit.
The soft budget sums actual usage across the selected clips/models and is checked
between requests; one request can exceed it. Set a separate account hard limit.
"""
import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import urllib.error
import urllib.request
import unicodedata
import uuid
import wave

ENDPOINTS = {"transcription": "https://openrouter.ai/api/v1/audio/transcriptions",
             "chat": "https://openrouter.ai/api/v1/chat/completions"}
PROMPT_VARIANT = "strict-multilingual-v1"
PROMPT = ("Transcribe this audio verbatim in the language spoken. Speech may be Russian, English, "
          "or mixed. Preserve the spoken words and language switches. Do not translate, summarize, "
          "complete unfinished speech, or add commentary. Return only the transcript.")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError("Redirect refused; no request was retried")


def read_key_file(path):
    """Return (secret, model IDs); never include file contents in errors."""
    key, models, in_models = "", [], False
    for raw in Path(path).expanduser().read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line == "MODELS:":
            in_models = True
        elif line.startswith("OPENROUTER_API_KEY="):
            key = line.split("=", 1)[1].strip()
        elif in_models:
            models.append(line)
        elif not key:
            key = line  # Legacy one-line raw key.
        else:
            raise RuntimeError("Invalid benchmark key-file format")
    return key, models


def load_manifest(path):
    root = Path(path).expanduser().resolve().parent
    manifest = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    clips = manifest.get("clips") if isinstance(manifest, dict) else None
    if not isinstance(clips, list) or not 1 <= len(clips) <= 40:
        raise RuntimeError("Manifest must contain 1 to 40 clips")
    seen, audio, seconds = set(), {}, 0.0
    for clip in clips:
        if not isinstance(clip, dict) or not isinstance(clip.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", clip["id"]):
            raise RuntimeError("Invalid clip ID")
        if clip["id"] in seen:
            raise RuntimeError("Duplicate clip ID")
        seen.add(clip["id"])
        rel = clip.get("path")
        if not isinstance(rel, str) or not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
            raise RuntimeError("Clip path must stay inside the manifest directory")
        candidate = root / rel
        if any(part.is_symlink() for part in (candidate, *candidate.parents) if part != root and root in part.parents):
            raise RuntimeError("Clip path may not contain a symlink")
        wav = candidate.resolve()
        if not wav.is_relative_to(root) or wav.suffix.lower() != ".wav" or not wav.is_file() or wav.stat().st_size > 16 * 1024 * 1024:
            raise RuntimeError("Clip must be a local WAV of at most 16 MiB")
        sha = clip.get("sha256")
        declared = clip.get("seconds")
        if not isinstance(sha, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", sha):
            raise RuntimeError("Invalid clip SHA-256")
        if isinstance(declared, bool) or not isinstance(declared, (int, float)) or not math.isfinite(declared) or not 0 < declared <= 30:
            raise RuntimeError("Clip duration must be at most 30 seconds")
        for field in ("kind", "local_text", "source"):
            if not isinstance(clip.get(field), str):
                raise RuntimeError("Manifest clip metadata is incomplete")
        if not isinstance(clip.get("reference_reviewed"), bool) or not (clip.get("reference") is None or isinstance(clip["reference"], str)):
            raise RuntimeError("Invalid reference metadata")
        for field in ("target_start", "target_end"):
            value = clip.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise RuntimeError("Invalid target timing")
        if clip["target_end"] <= clip["target_start"]:
            raise RuntimeError("Invalid target timing")
        raw = wav.read_bytes()
        if hashlib.sha256(raw).hexdigest().lower() != sha.lower():
            raise RuntimeError("Clip SHA-256 differs from manifest")
        try:
            with wave.open(str(wav), "rb") as stream:
                actual = stream.getnframes() / stream.getframerate()
        except (wave.Error, ZeroDivisionError, EOFError):
            raise RuntimeError("Invalid WAV clip") from None
        if not 0 < actual <= 30 or abs(actual - declared) > 0.01:
            raise RuntimeError("WAV duration differs from manifest or exceeds 30 seconds")
        seconds += actual
        audio[clip["id"]] = raw
    if seconds > 900:
        raise RuntimeError("Manifest exceeds 900 seconds of audio")
    return clips, audio, seconds


def _distance(left, right):
    previous = list(range(len(right) + 1))
    for i, value in enumerate(left, 1):
        current = [i]
        for j, other in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[j] + 1,
                               previous[j - 1] + (value != other)))
        previous = current
    return previous[-1]


def _normalize(text):
    return " ".join("".join(" " if unicodedata.category(char).startswith("P") else char
                            for char in text.casefold()).split())


def score(text, clip, incomplete=False):
    if incomplete:
        return {"status": "not_evaluated", "reason": "incomplete"}
    if clip["reference_reviewed"] is not True or not isinstance(clip["reference"], str):
        return {"status": "not_evaluated"}
    reference = _normalize(clip["reference"])
    candidate = _normalize(text)
    if not reference:
        return {"status": "reviewed_empty_reference", "false_positive": bool(candidate), "wer": None, "cer": None}
    return {"status": "evaluated", "wer": _distance(reference.split(), candidate.split()) / len(reference.split()),
            "cer": _distance(reference, candidate) / len(reference)}


def _identity(mode, model, clip, max_output_tokens=1024, reasoning_effort=None):
    fields = {"mode": mode, "model": model, "clip_id": clip["id"], "sha256": clip["sha256"].lower(),
              "prompt_variant": PROMPT_VARIANT if mode == "chat" else "none"}
    # Keep the original cache key for default settings and for transcription.
    if mode == "chat" and max_output_tokens != 1024:
        fields["max_output_tokens"] = max_output_tokens
    if mode == "chat" and reasoning_effort is not None:
        fields["reasoning_effort"] = reasoning_effort
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def _request(opener, key, mode, model, raw, max_output_tokens=1024, reasoning_effort=None):
    audio = {"data": base64.b64encode(raw).decode("ascii"), "format": "wav"}
    if mode == "chat":
        payload = {"model": model, "max_tokens": max_output_tokens, "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT}, {"type": "input_audio", "input_audio": audio}]}]}
        if reasoning_effort is not None:
            payload["reasoning"] = {"effort": reasoning_effort}
    else:
        payload = {"model": model, "input_audio": audio}
    request = urllib.request.Request(ENDPOINTS[mode], data=json.dumps(payload).encode(),
                                     headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with opener.open(request, timeout=75) as response:
            body = response.read(2 * 1024 * 1024 + 1)
            generation_id = response.headers.get("X-Generation-Id")
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        raise RuntimeError(f"OpenRouter returned HTTP {code}; no automatic retry") from None
    except (urllib.error.URLError, TimeoutError, RuntimeError, OSError):
        raise RuntimeError("OpenRouter request failed or redirected; no automatic retry") from None
    if len(body) > 2 * 1024 * 1024:
        raise RuntimeError("OpenRouter response exceeded 2 MiB")
    try:
        result = json.loads(body)
        if mode == "chat":
            choice = result["choices"][0]
            text, finish = choice["message"]["content"], choice.get("finish_reason")
        else:
            text, finish = result["text"], result.get("finish_reason")
        if not isinstance(text, str):
            raise ValueError()
    except (ValueError, KeyError, IndexError, TypeError):
        raise RuntimeError("OpenRouter returned an invalid transcript response") from None
    return text, finish, result.get("usage") or {}, generation_id


def _private_json(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    _sync_directory(path.parent)


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_json(path, data):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        _private_json(temporary, data)
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _evaluation(text, clip, incomplete):
    return {"local_text": clip["local_text"], "reference": clip["reference"],
            "reference_reviewed": clip["reference_reviewed"], "score": score(text, clip, incomplete)}


def _cost(usage):
    value = usage.get("cost") if isinstance(usage, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise RuntimeError("Actual usage cost unavailable; inspect saved results before further spending")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--mode", choices=ENDPOINTS, required=True)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--model", action="append", help="OpenRouter model ID; repeat to compare models")
    parser.add_argument("--max-output-tokens", type=int, default=1024,
                        help="Chat max_tokens (default: 1024); changes the cache identity")
    parser.add_argument("--reasoning-effort", choices=("minimal", "low", "medium", "high"),
                        help="Optional chat reasoning effort; omitted by default and changes the cache identity")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--max-cost-usd", type=float, default=0.25,
                        help="Soft total for selected clips/models; check actual cost between requests, not a hard cap")
    parser.add_argument("--execute", action="store_true", help="Send verified clips to OpenRouter")
    args = parser.parse_args(argv)
    if not math.isfinite(args.max_cost_usd) or args.max_cost_usd <= 0:
        parser.error("Soft budget must be finite and positive")
    if not 1 <= args.max_output_tokens <= 32768:
        parser.error("Chat max output tokens must be between 1 and 32768")
    if args.mode != "chat" and (args.max_output_tokens != 1024 or args.reasoning_effort is not None):
        parser.error("Output tokens and reasoning effort apply only to chat mode")
    request_options = ({"max_output_tokens": args.max_output_tokens, "reasoning_effort": args.reasoning_effort}
                       if args.mode == "chat" else {})
    clips, audio, seconds = load_manifest(args.manifest)
    file_key, file_models = read_key_file(args.key_file) if args.key_file else ("", [])
    models = list(dict.fromkeys(args.model or file_models))
    if not models or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:+/-]*", model)
                         for model in models):
        parser.error("Specify valid provider/model IDs with --model or MODELS: entries")
    print(json.dumps({"execute": args.execute, "mode": args.mode, "clips": len(clips),
                      "audio_seconds_per_model": round(seconds, 3), "models": models,
                      "maximum_requests": len(clips) * len(models), "soft_budget_usd": args.max_cost_usd,
                      "request_options": request_options}))
    if not args.execute:
        print("Dry run only: no audio sent and no network request.")
        return
    output = (args.output_dir or args.manifest.expanduser().resolve().parent / "openrouter-results").expanduser()
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    spent, pending = 0.0, []
    for model in models:
        for clip in clips:
            identity = _identity(args.mode, model, clip, args.max_output_tokens, args.reasoning_effort)
            path = output / (identity + ".json")
            marker = path.with_suffix(".attempt")
            if path.exists():
                previous = json.loads(path.read_text(encoding="utf-8"))
                if (previous.get("identity") != identity or previous.get("clip_id") != clip["id"]
                        or previous.get("model") != model or previous.get("mode") != args.mode
                        or previous.get("sha256") != clip["sha256"].lower()
                        or not isinstance(previous.get("text"), str)):
                    raise RuntimeError("Invalid saved response identity")
                spent += _cost(previous.get("usage"))
                refreshed = {**previous, "request_options": request_options,
                             **_evaluation(previous["text"], clip, previous.get("incomplete") is True)}
                if refreshed != previous:
                    _atomic_json(path, refreshed)
                if marker.exists():
                    marker.unlink()
                    _sync_directory(output)
            else:
                if marker.exists():
                    raise RuntimeError("Previous request outcome is unknown; inspect billing/results before manual resolution")
                pending.append((model, clip, identity, path, marker))
    if pending:
        key = file_key or os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key:
            raise RuntimeError("Set OPENROUTER_API_KEY or provide --key-file with a key")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    for model, clip, identity, path, marker in pending:
        if spent >= args.max_cost_usd:
            raise RuntimeError("Soft budget reached; completed results retained")
        try:
            _private_json(marker, {"identity": identity, "clip_id": clip["id"], "model": model,
                                   "mode": args.mode, "request_options": request_options})
        except FileExistsError:
            raise RuntimeError("Previous request outcome is unknown; inspect billing/results before manual resolution") from None
        text, finish, usage, generation_id = _request(opener, key, args.mode, model, audio[clip["id"]],
                                                      args.max_output_tokens, args.reasoning_effort)
        incomplete = finish == "length" or (args.mode == "chat" and finish != "stop")
        _atomic_json(path, {"identity": identity, "mode": args.mode, "prompt_variant": PROMPT_VARIANT if args.mode == "chat" else "none",
                             "clip_id": clip["id"], "sha256": clip["sha256"].lower(), "model": model,
                             "text": text, "usage": usage,
                             "generation_id": generation_id, "finish_reason": finish,
                             "incomplete": incomplete, "request_options": request_options,
                             **_evaluation(text, clip, incomplete)})
        marker.unlink()
        _sync_directory(output)
        spent += _cost(usage)
        print(json.dumps({"clip": clip["id"], "model": model, "actual_total_cost_usd": spent}))
    print("Finished. Cloud agreement is not a reviewed reference transcript.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError, KeyError) as error:
        raise SystemExit(str(error)) from None
