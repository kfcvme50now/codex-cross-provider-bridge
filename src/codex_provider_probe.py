#!/usr/bin/env python3
"""Perform one minimal real model request for provider compatibility."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
import tomllib
from pathlib import Path
from typing import Callable

from codex_config_guard import inspect_config_route
from codex_executable import resolve_codex_command
from codex_internal import INTERNAL_ENV


PROBE_PROMPT = "Reply with exactly: bridge-probe-ok"
ProbeRunner = Callable[..., dict]


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temporary.replace(path)


def route_fingerprint(config_path: Path) -> str:
    route = inspect_config_route(config_path)
    with config_path.open("rb") as handle:
        config = tomllib.load(handle)
    providers = config.get("model_providers") or {}
    selected_provider = route["activeProvider"]
    selected_block = providers.get(selected_provider) or {}
    relevant = {
        "provider": selected_provider,
        "model": route["model"],
        "baseUrl": route["baseUrl"],
        "wireApi": str(selected_block.get("wire_api") or "responses"),
    }
    encoded = json.dumps(
        relevant,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _classify_probe_failure(returncode: int, output: str) -> str:
    lowered = output.lower()
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    if (
        "not supported" in lowered
        or "unsupported model" in lowered
        or "model is not supported" in lowered
    ):
        return "unsupported-model"
    if (
        "unauthorized" in lowered
        or "authentication" in lowered
        or "invalid api key" in lowered
        or " 401" in lowered
    ):
        return "authentication"
    if "rate limit" in lowered or " 429" in lowered:
        return "rate-limit"
    if (
        "connection" in lowered
        or "connect" in lowered
        or "dns" in lowered
        or "tls" in lowered
    ):
        return "transport"
    if "configuration" in lowered or "config.toml" in lowered:
        return "configuration"
    return "unknown" if returncode != 0 else "reported-failure"


def _parse_cli_probe_output(
    returncode: int,
    stdout: str,
    stderr: str,
) -> dict:
    probe_id = ""
    completed = False
    failed = False
    for line in stdout.splitlines():
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        event_type = str(payload.get("type") or "")
        if event_type == "thread.started":
            probe_id = str(payload.get("thread_id") or "")
        elif event_type == "turn.completed":
            completed = True
        elif event_type in {"turn.failed", "error"}:
            failed = True

    if returncode == 0 and completed and not failed:
        return {
            "ok": True,
            "probeId": probe_id,
            "errorCategory": "",
        }
    category = _classify_probe_failure(returncode, stdout + "\n" + stderr)
    return {
        "ok": False,
        "probeId": probe_id,
        "errorCategory": category,
    }


def _run_cli_probe(
    command: list[str],
    provider: str,
    model: str,
    codex_home: Path,
    timeout_seconds: int,
) -> dict:
    full_command = [
        *command,
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--json",
        "-c",
        f"model_provider={json.dumps(provider)}",
        "-c",
        f"model={json.dumps(model)}",
        PROBE_PROMPT,
    ]
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(codex_home)
    environment[INTERNAL_ENV] = "1"
    try:
        completed = subprocess.run(
            full_command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            env=environment,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "probeId": "",
            "errorCategory": "timeout",
        }
    return _parse_cli_probe_output(
        completed.returncode,
        completed.stdout,
        completed.stderr,
    )


def run_configured_probe(
    config_path: Path,
    codex_home: Path,
    mode: str,
    timeout_seconds: int,
    command: list[str] | None = None,
) -> dict:
    if mode == "disabled":
        return {
            "ok": None,
            "status": "disabled",
            "provider": "",
            "model": "",
            "probeId": "",
            "errorCategory": "",
        }
    if mode not in {"cli", "app-server"}:
        raise ValueError(f"Unsupported probe mode: {mode}")

    route = inspect_config_route(config_path)
    provider = route["activeProvider"]
    model = route["model"]
    if not provider or not model:
        return {
            "ok": False,
            "status": "configuration-error",
            "provider": provider,
            "model": model,
            "probeId": "",
            "errorCategory": "configuration",
        }

    if mode == "cli":
        execution = _run_cli_probe(
            command=command or resolve_codex_command(),
            provider=provider,
            model=model,
            codex_home=codex_home,
            timeout_seconds=timeout_seconds,
        )
    else:
        from codex_app_server_client import probe_provider_with_app_server

        execution = probe_provider_with_app_server(
            codex_home=codex_home,
            provider=provider,
            model=model,
            timeout_seconds=timeout_seconds,
            command=command,
        )
    return {
        **execution,
        "status": "verified" if execution["ok"] else "failed",
        "provider": provider,
        "model": model,
        "mode": mode,
        "timeoutSeconds": timeout_seconds,
        "checkedAt": time.time(),
    }


def maybe_probe_after_switch(
    config_path: Path,
    codex_home: Path,
    mode: str,
    scope: str,
    timeout_seconds: int,
    state_path: Path,
    status_path: Path,
    probe_runner: ProbeRunner | None = None,
    apply: bool = False,
) -> dict:
    fingerprint = route_fingerprint(config_path)
    previous = {}
    if state_path.exists():
        try:
            with state_path.open("r", encoding="utf-8") as handle:
                previous = json.load(handle)
        except (json.JSONDecodeError, OSError):
            previous = {}
    if previous.get("fingerprint") == fingerprint:
        return {
            "status": "unchanged",
            "fingerprint": fingerprint,
            "probe": previous.get("lastProbe") or {},
            "dryRun": not apply,
        }
    if mode == "disabled":
        if apply:
            _write_json_atomic(
                state_path,
                {
                    "schemaVersion": 1,
                    "fingerprint": fingerprint,
                    "lastScope": scope,
                    "lastProbe": {"status": "disabled", "ok": None},
                    "updatedAt": time.time(),
                },
            )
        return {
            "status": "disabled",
            "fingerprint": fingerprint,
            "probe": {"status": "disabled", "ok": None},
            "dryRun": not apply,
        }
    if not apply:
        return {
            "status": "planned",
            "fingerprint": fingerprint,
            "probe": {},
            "dryRun": True,
        }

    runner = probe_runner or run_configured_probe
    probe = runner(
        config_path=config_path,
        codex_home=codex_home,
        mode=mode,
        timeout_seconds=timeout_seconds,
    )
    _write_json_atomic(
        state_path,
        {
            "schemaVersion": 1,
            "fingerprint": fingerprint,
            "lastScope": scope,
            "lastProbe": probe,
            "updatedAt": time.time(),
        },
    )
    _write_json_atomic(
        status_path,
        {
            "updatedAt": time.time(),
            "event": "provider-switch",
            "fingerprint": fingerprint,
            "scope": scope,
            "probe": probe,
        },
    )
    return {
        "status": "probed",
        "fingerprint": fingerprint,
        "probe": probe,
        "dryRun": False,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(Path.home() / ".codex" / "config.toml"))
    parser.add_argument("--codex-home", default=str(Path.home() / ".codex"))
    parser.add_argument("--mode", choices=("disabled", "cli", "app-server"), required=True)
    parser.add_argument("--scope", choices=("preserve", "next", "all"), default="next")
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--status-file", required=True)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = maybe_probe_after_switch(
        config_path=Path(args.config),
        codex_home=Path(args.codex_home),
        mode=args.mode,
        scope=args.scope,
        timeout_seconds=args.timeout_seconds,
        state_path=Path(args.state_file),
        status_path=Path(args.status_file),
        apply=args.apply,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
