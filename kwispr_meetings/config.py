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
        _ = url.port  # Reject malformed ports before recording any audio.
    except ValueError as error:
        raise ConfigError("Meetings require a valid local STT HTTP endpoint.") from error
    address = None
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    loopback = hostname.lower() == "localhost" or bool(address and address.is_loopback)
    # A stale local-mode flag must never authorize uploads to a cloud endpoint.
    private_networks = (ipaddress.ip_network("10.0.0.0/8"),
                        ipaddress.ip_network("172.16.0.0/12"),
                        ipaddress.ip_network("192.168.0.0/16"),
                        ipaddress.ip_network("fc00::/7"))
    private_lan = bool(address and any(address in network for network in private_networks))
    if url.scheme not in {"http", "https"} or not hostname or url.username or url.password:
        raise ConfigError("Meetings require a valid local STT HTTP endpoint.")
    if not loopback and not (private_lan and config.get("KWISPR_LOCAL_STT_CONFIGURED") == "1"):
        raise ConfigError("Meetings require local STT on localhost or an explicitly configured private LAN IP; cloud fallback is disabled.")


def output_dir(config: dict[str, str]) -> Path:
    return Path(config.get("KWISPR_MEETING_OUTPUT_DIR") or "~/Documents/Kwispr/Meetings").expanduser().resolve()


def runtime_dir(config=None):
    config = config or {}
    override = config.get("KWISPR_MEETING_RUNTIME_DIR") or os.environ.get("KWISPR_MEETING_RUNTIME_DIR")
    return Path(override).expanduser() if override else Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "kwispr/meeting-runtime"


def meeting_language(config: dict[str, str], track: str) -> str:
    """Select a meeting track language independently of ordinary dictation."""
    if track not in {"microphone", "remote"}:
        raise ConfigError("Unknown meeting audio track.")
    key = "KWISPR_MEETING_MIC_LANGUAGE" if track == "microphone" else "KWISPR_MEETING_REMOTE_LANGUAGE"
    language = config.get(key, "").strip().lower()
    if language in {"", "auto"}:
        return ""
    if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", language):
        raise ConfigError(f"{key} must be Auto or a language code, such as ru or en.")
    return language


def speaker_count(value: str | int) -> int:
    try:
        count = int(value)
    except (ValueError, TypeError) as error:
        raise ConfigError("Speaker count must be 0 (automatic) or 1–16 remote speakers.") from error
    if not 0 <= count <= 16:
        raise ConfigError("Speaker count must be 0 (automatic) or 1–16 remote speakers.")
    return count
