"""Read Kwispr configuration as literal data, never as executable shell."""
from __future__ import annotations

import ipaddress
import os
import re
import shlex
from pathlib import Path
from urllib.parse import urlsplit


class ConfigError(ValueError):
    pass


def config_path() -> Path:
    if os.environ.get("KWISPR_CONFIG_FILE"):
        return Path(os.environ["KWISPR_CONFIG_FILE"]).expanduser()
    return Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "kwispr/config.env"


def load_config(path: Path | None = None) -> dict[str, str]:
    """Load shell-quoted assignments without expansion; environment wins."""
    config: dict[str, str] = {}
    source = path or config_path()
    if source.exists():
        for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.fullmatch(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)", line)
            if not match:
                raise ConfigError(f"Invalid assignment in {source.name}, line {number}")
            key, raw = match.groups()
            try:
                parts = shlex.split(raw, comments=True, posix=True)
            except ValueError as error:
                raise ConfigError(f"Invalid quoted value in {source.name}, line {number}") from error
            if len(parts) > 1:
                raise ConfigError(f"Expected a quoted value in {source.name}, line {number}")
            config[key] = parts[0] if parts else ""
    config.update({key: value for key, value in os.environ.items()
                   if key.startswith("KWISPR_") or key == "OPENAI_API_KEY"})
    return config


def validate_local_backend(config: dict[str, str]) -> None:
    """Meetings never silently send audio to the dictation cloud backend."""
    if config.get("KWISPR_BACKEND", "openai-transcriptions") != "openai-transcriptions":
        raise ConfigError("Meetings require the local STT backend. Configure local recognition first.")
    try:
        url = urlsplit(config.get("KWISPR_API_URL", ""))
        hostname = url.hostname or ""
    except ValueError as error:
        raise ConfigError("Meetings require a valid local STT HTTP endpoint.") from error
    try:
        loopback = hostname.lower() == "localhost" or ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        loopback = False
    if url.scheme not in {"http", "https"} or not hostname or url.username or url.password:
        raise ConfigError("Meetings require a valid local STT HTTP endpoint.")
    if not loopback and config.get("KWISPR_LOCAL_STT_CONFIGURED") != "1":
        raise ConfigError("Meetings require local STT; cloud fallback is disabled. Configure a local endpoint first.")


def output_dir(config: dict[str, str]) -> Path:
    return Path(config.get("KWISPR_MEETING_OUTPUT_DIR") or "~/Documents/Kwispr/Meetings").expanduser().resolve()


def speaker_count(value: str | int) -> int:
    try:
        count = int(value)
    except (ValueError, TypeError) as error:
        raise ConfigError("Speaker count must be 0 (automatic) or 1–16 remote speakers.") from error
    if not 0 <= count <= 16:
        raise ConfigError("Speaker count must be 0 (automatic) or 1–16 remote speakers.")
    return count
