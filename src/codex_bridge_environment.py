"""Portable defaults; explicit CLI arguments take precedence over environment."""
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def default_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def default_cc_switch_db() -> Path:
    home = Path(os.environ.get("CC_SWITCH_HOME") or Path.home() / ".cc-switch")
    return Path(os.environ.get("CC_SWITCH_DB") or home / "cc-switch.db").expanduser()


def default_config_path() -> Path:
    return default_codex_home() / "config.toml"


def default_bridge_url() -> str:
    return os.environ.get("CODEX_BRIDGE_URL", "http://127.0.0.1:15722/v1").rstrip("/")


def default_cc_switch_url() -> str:
    return os.environ.get("CODEX_BRIDGE_UPSTREAM_URL", "http://127.0.0.1:15721").rstrip("/")


def responses_url(url: str) -> str:
    parsed = urlsplit(url)
    return url.rstrip("/") + ("/v1" if parsed.path.rstrip("/") == "" else "")


def official_bridge_url(bridge_url: str) -> str:
    parsed = urlsplit(bridge_url)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.port is None or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("bridge URL must be loopback HTTP with an explicit port and no credentials")
    return urlunsplit((parsed.scheme, parsed.netloc, "/__codex_official__", "", ""))


def endpoint(url: str) -> tuple[str, int]:
    parsed = urlsplit(url)
    if not parsed.hostname or parsed.scheme not in {"http", "https"}:
        raise ValueError("upstream URL must be absolute HTTP(S)")
    return parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
