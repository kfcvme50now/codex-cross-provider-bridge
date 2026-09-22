#!/usr/bin/env python3
"""Codex lifecycle hook entrypoint for provider compatibility repair."""

from __future__ import annotations


import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from codex_branch_handoff import run_branch_handoff
from codex_config_guard import inspect_config_route
from codex_cross_provider_bridge import lookup_conversation_metadata
from codex_internal import INTERNAL_ENV
from codex_lifecycle_policy import (
    _normalize_policy,
    decide_compact_action,
    load_lifecycle_policy,
)
from codex_provider_probe import run_configured_probe
from codex_thread_provider_migrate import apply_thread_provider_migration


BranchRunner = Callable[..., dict]
ProbeRunner = Callable[..., dict]
RouteRepairRunner = Callable[[Path, str], dict]
BridgeEnsureRunner = Callable[[str], dict]

DEFAULT_BRIDGE_URL = "http://127.0.0.1:15722/v1"
ROUTE_REPAIR_TIMEOUT_SECONDS = 180


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temporary.replace(path)


def _append_json_line(path: Path, payload: object) -> None:
    """Append-only trail for hook actions; the status file only keeps the last one."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _short_summary(decision: dict, result: str) -> str:
    title = decision.get("conversationTitle") or decision["sessionId"]
    return (
        f"{title} ({decision.get('conversationCwd') or 'cwd unknown'}): "
        f"provider {decision.get('currentProvider') or 'unknown'} -> "
        f"{decision.get('targetProvider') or 'unknown'}, "
        f"result={result}"
    )


def run_precompact_hook(
    event: dict,
    policy: dict,
    codex_home: Path,
    config_path: Path,
    status_path: Path,
    apply: bool,
    branch_runner: BranchRunner | None = None,
) -> dict:
    normalized = _normalize_policy(policy)
    session_id = str(event.get("session_id") or "").strip()
    decision = decide_compact_action(
        codex_home=codex_home,
        config_path=config_path,
        session_id=session_id,
        compact_mode=normalized["compactRepairMode"],
        target_provider="",
        target_model="",
        auto_branch_enabled=normalized["autoBranchEnabled"],
    )
    action = decision["action"]
    result_status = "allowed"
    migration_result = None
    branch_result = None
    error = ""

    repair_actions = {
        "repair-and-continue",
        "repair-and-stop",
        "repair-and-branch",
    }
    if action in repair_actions:
        if apply:
            try:
                migration_result = apply_thread_provider_migration(
                    codex_home=codex_home,
                    conversation_id=session_id,
                    target_provider=decision["targetProvider"],
                )
                result_status = "repaired"
            except Exception as exc:
                result_status = "repair-failed"
                error = str(exc)
        else:
            result_status = "repair-planned"

    if action in {"repair-and-branch", "branch-only"}:
        if action == "repair-and-branch" and result_status == "repair-failed":
            error = error or "repair failed before branch creation"
        elif apply:
            runner = branch_runner or run_branch_handoff
            try:
                branch_result = runner(
                    codex_home=codex_home,
                    config_path=config_path,
                    conversation_id=session_id,
                    target_provider=decision["targetProvider"],
                    target_model=decision["targetModel"],
                    backend=normalized["branchBackend"],
                    continue_prompt="",
                    history_path=status_path.parent / "branch-history.jsonl",
                    apply=True,
                )
                if branch_result.get("status") == "branched":
                    result_status = "branch-created"
                else:
                    result_status = "branch-failed"
                    error = str(branch_result.get("error") or "branch failed")
            except Exception as exc:
                result_status = "branch-failed"
                error = str(exc)
        else:
            result_status = "branch-planned"

    if action == "repair-and-continue":
        result_status = (
            "repaired-and-continued"
            if result_status == "repaired"
            else result_status
        )
        output = {
            "continue": True,
            "systemMessage": _short_summary(decision, result_status),
        }
    elif action in {"branch-only", "repair-and-branch"}:
        if result_status == "branch-created" and branch_result:
            stop_reason = (
                "Compaction was stopped; a compatibility branch was created at "
                f"{branch_result.get('newConversationId')}."
            )
            system_message = (
                f"{_short_summary(decision, result_status)}; "
                f"newConversationId={branch_result.get('newConversationId')}"
            )
        else:
            stop_reason = (
                "Provider metadata repair failed; no compatibility branch was created."
                if result_status == "repair-failed"
                else "Compatibility branch creation failed; compaction was stopped."
            )
            system_message = _short_summary(decision, result_status)
        output = {
            "continue": False,
            "stopReason": stop_reason,
            "systemMessage": system_message,
        }
    elif action in {"repair-and-stop", "block-only"}:
        if action == "repair-and-stop" and result_status == "repaired":
            result_status = "repaired-and-stopped"
            stop_reason = (
                f"Repaired provider metadata for {decision['currentModel']!r} "
                f"({decision.get('currentProvider')} -> "
                f"{decision.get('targetProvider')}). Re-run compaction."
            )
        else:
            result_status = "blocked"
            stop_reason = (
                "Stopped compaction because the persisted provider/model "
                f"combination is unsafe: {decision.get('reason')}."
            )
        output = {
            "continue": False,
            "stopReason": stop_reason,
            "systemMessage": _short_summary(decision, result_status),
        }
    else:
        output = {
            "continue": True,
            "systemMessage": _short_summary(decision, result_status),
        }

    if action in repair_actions and result_status == "repair-failed":
        output = {
            "continue": False,
            "stopReason": "Provider metadata repair failed; compaction was stopped.",
            "systemMessage": _short_summary(decision, result_status),
        }

    _write_json_atomic(
        status_path,
        {
            "event": "PreCompact",
            "trigger": str(event.get("trigger") or ""),
            "turnId": str(event.get("turn_id") or ""),
            "sessionId": session_id,
            "conversationTitle": decision.get("conversationTitle", ""),
            "conversationCwd": decision.get("conversationCwd", ""),
            "action": action,
            "reason": decision.get("reason", ""),
            "result": result_status,
            "error": error,
            "migrationBackupDirectory": (
                migration_result or {}
            ).get("backupDirectory", ""),
            "newConversationId": (
                branch_result or {}
            ).get("newConversationId", ""),
            "targetProvider": decision.get("targetProvider", ""),
            "targetModel": decision.get("targetModel", ""),
        },
    )
    return output


def run_session_start_hook(
    event: dict,
    policy: dict,
    codex_home: Path,
    config_path: Path,
    status_path: Path,
    apply: bool,
    probe_runner: ProbeRunner | None = None,
    bridge_url: str = "",
    route_repair_script: Path | None = None,
    route_repair_runner: RouteRepairRunner | None = None,
    bridge_ensure_runner: BridgeEnsureRunner | None = None,
) -> dict:
    normalized = _normalize_policy(policy)
    session_id = str(event.get("session_id") or "").strip()
    target_url = (bridge_url or DEFAULT_BRIDGE_URL).rstrip("/")
    route_before = ""
    route_after = ""
    route_repair_result = "not-required"
    route_repair_error = ""
    bridge_ensure_result: dict = {}

    try:
        route = inspect_config_route(config_path)
        route_before = str(route.get("baseUrl") or "").rstrip("/")
        route_after = route_before
        if normalized["routeRepairMode"] == "disabled":
            route_repair_result = "disabled"
        elif route_before == target_url:
            route_repair_result = "route-ok"
        elif not route.get("eligibleForAutomaticBridgeRepair"):
            route_repair_result = "route-not-applicable"
        elif normalized["routeRepairMode"] == "inspect" or not apply:
            route_repair_result = "repair-planned"
        else:
            runner = route_repair_runner or (
                lambda config, bridge: run_configured_route_repair(
                    config,
                    bridge,
                    route_repair_script,
                )
            )
            repair = runner(config_path, target_url)
            if repair.get("ok"):
                route_repair_result = "repaired"
                route_after = target_url
            else:
                route_repair_result = "repair-failed"
                route_repair_error = str(
                    repair.get("error")
                    or repair.get("status")
                    or "unknown failure"
                )
    except Exception as exc:  # noqa: BLE001 - startup must remain recoverable
        route_repair_result = "repair-failed"
        route_repair_error = str(exc)

    if (
        apply
        and route_after == target_url
        and normalized["routeRepairMode"] != "disabled"
        and (bridge_ensure_runner is not None or route_repair_script is not None)
    ):
        ensure_runner = bridge_ensure_runner or (
            lambda bridge: run_configured_bridge_ensure(
                bridge,
                route_repair_script,
            )
        )
        bridge_ensure_result = ensure_runner(target_url)
        if not bridge_ensure_result.get("ok"):
            route_repair_result = "bridge-start-failed"
            route_repair_error = str(
                bridge_ensure_result.get("error")
                or bridge_ensure_result.get("status")
                or "unknown failure"
            )

    decision = decide_compact_action(
        codex_home=codex_home,
        config_path=config_path,
        session_id=session_id,
        compact_mode="inspect",
        target_provider="",
        target_model="",
        auto_branch_enabled=normalized["autoBranchEnabled"],
    )
    result_status = "allowed"
    migration_result = None
    probe_result = None
    error = ""
    if normalized["sessionStartMode"] == "disabled":
        result_status = "disabled"
    elif not decision["remoteCompactRisk"]:
        result_status = "not-required"
    elif decision["reason"] in {
        "target-provider-is-reserved",
        "target-provider-not-configured",
    }:
        result_status = "blocked-unsafe-target"
    elif apply:
        try:
            migration_result = apply_thread_provider_migration(
                codex_home=codex_home,
                conversation_id=session_id,
                target_provider=decision["targetProvider"],
            )
            result_status = "repaired"
        except Exception as exc:
            result_status = "repair-failed"
            error = str(exc)
    else:
        result_status = "repair-planned"

    if normalized["sessionStartMode"] == "repair-and-probe":
        if normalized["postSwitchProbeMode"] == "disabled":
            result_status = "repair-probe-disabled"
        elif apply and result_status not in {"repair-failed", "blocked-unsafe-target"}:
            runner = probe_runner or run_configured_probe
            probe_result = runner(
                config_path=config_path,
                codex_home=codex_home,
                mode=normalized["postSwitchProbeMode"],
                timeout_seconds=normalized["probeTimeoutSeconds"],
            )
            if probe_result.get("ok"):
                result_status = (
                    "repaired-and-verified"
                    if result_status == "repaired"
                    else "verified"
                )
            else:
                result_status = (
                    "repaired-probe-failed"
                    if result_status == "repaired"
                    else "probe-failed"
                )

    additional_context = ""
    if result_status == "repaired":
        additional_context = (
            "Provider compatibility metadata was repaired for this session. "
            "Continue normally."
        )
    elif result_status in {"repaired-and-verified", "verified"}:
        additional_context = (
            "Provider compatibility metadata was repaired and the active route "
            "was verified with a real model request."
        )
    elif result_status == "repair-failed":
        additional_context = (
            "Provider compatibility metadata could not be repaired. "
            "Compaction may fail if this session uses a third-party model."
        )
    if route_repair_result == "repaired":
        additional_context = (
            "The cross-provider bridge route was restored for this session. "
            + additional_context
        ).strip()
    elif route_repair_result in {"repair-failed", "bridge-start-failed"}:
        additional_context = (
            "The cross-provider bridge route could not be restored: "
            f"{route_repair_error}. " + additional_context
        ).strip()
    output = {
        "continue": True,
        "systemMessage": _short_summary(decision, result_status),
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": additional_context,
        },
    }
    _write_json_atomic(
        status_path,
        {
            "event": "SessionStart",
            "source": str(event.get("source") or ""),
            "sessionId": session_id,
            "conversationTitle": decision.get("conversationTitle", ""),
            "conversationCwd": decision.get("conversationCwd", ""),
            "action": decision.get("action", ""),
            "reason": decision.get("reason", ""),
            "result": result_status,
            "error": error,
            "routeRepairResult": route_repair_result,
            "routeRepairError": route_repair_error,
            "routeBefore": route_before,
            "routeAfter": route_after,
            "bridgeUrl": target_url,
            "bridgeEnsure": bridge_ensure_result,
            "migrationBackupDirectory": (
                migration_result or {}
            ).get("backupDirectory", ""),
            "probe": probe_result or {},
            "targetProvider": decision.get("targetProvider", ""),
            "targetModel": decision.get("targetModel", ""),
        },
    )
    return output


def run_configured_route_repair(
    config_path: Path,
    bridge_url: str,
    repair_script: Path | None,
) -> dict:
    """Repair the live route through the same script the CLI repair action uses."""
    if repair_script is None or not repair_script.is_file():
        return {
            "ok": False,
            "status": "repair-script-missing",
            "error": f"route repair script not found: {repair_script}",
        }
    executable = (
        shutil.which("pwsh.exe")
        or shutil.which("pwsh")
        or shutil.which("powershell.exe")
    )
    if not executable:
        return {
            "ok": False,
            "status": "powershell-missing",
            "error": "pwsh or powershell was not found on PATH",
        }
    try:
        completed = subprocess.run(
            [
                executable,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(repair_script),
                "-ConfigPath",
                str(config_path),
                "-BridgeUrl",
                bridge_url,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=ROUTE_REPAIR_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "status": "repair-script-error", "error": str(exc)}
    ok = completed.returncode == 0
    return {
        "ok": ok,
        "status": "repaired" if ok else "repair-script-failed",
        "exitCode": completed.returncode,
        "error": (
            ""
            if ok
            else (completed.stderr or completed.stdout or "").strip()[:400]
        ),
    }


def _bridge_endpoint(bridge_url: str) -> tuple[str, int]:
    parsed = urlsplit(bridge_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("bridge URL must use loopback HTTP")
    if parsed.port is None:
        raise ValueError("bridge URL must include a port")
    return parsed.hostname, parsed.port


def _bridge_is_listening(bridge_url: str, timeout: float = 0.25) -> bool:
    host, port = _bridge_endpoint(bridge_url)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def run_configured_bridge_ensure(
    bridge_url: str,
    route_repair_script: Path | None,
) -> dict:
    """Start the managed bridge task when its configured loopback port is down."""
    try:
        if _bridge_is_listening(bridge_url):
            return {"ok": True, "status": "already-running"}
    except ValueError as exc:
        return {"ok": False, "status": "invalid-bridge-url", "error": str(exc)}

    manager = (
        route_repair_script.parent / "Manage-CodexCrossProviderBridge.ps1"
        if route_repair_script is not None
        else None
    )
    if manager is None or not manager.is_file():
        return {
            "ok": False,
            "status": "bridge-manager-missing",
            "error": f"bridge manager not found: {manager}",
        }
    executable = (
        shutil.which("pwsh.exe")
        or shutil.which("pwsh")
        or shutil.which("powershell.exe")
    )
    if not executable:
        return {
            "ok": False,
            "status": "powershell-missing",
            "error": "pwsh or powershell was not found on PATH",
        }
    try:
        completed = subprocess.run(
            [
                executable,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(manager),
                "-Action",
                "start",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "status": "start-error", "error": str(exc)}
    if completed.returncode != 0:
        return {
            "ok": False,
            "status": "start-failed",
            "exitCode": completed.returncode,
            "error": (completed.stderr or completed.stdout or "").strip()[:400],
        }

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if _bridge_is_listening(bridge_url):
            return {"ok": True, "status": "started"}
        time.sleep(0.1)
    return {
        "ok": False,
        "status": "start-timeout",
        "error": "scheduled task did not open the bridge port within 5 seconds",
    }


def run_user_prompt_submit_hook(
    event: dict,
    policy: dict,
    codex_home: Path,
    config_path: Path,
    status_path: Path,
    apply: bool,
    bridge_url: str = "",
    route_repair_script: Path | None = None,
    route_repair_runner: RouteRepairRunner | None = None,
    bridge_ensure_runner: BridgeEnsureRunner | None = None,
) -> dict:
    """Keep the bridge in the request path when CC Switch rewrote config.toml.

    Runs on the first prompt after a provider switch: the route is inspected and,
    when it points straight at CC Switch, repaired before the turn is sent. The
    blocking turn is intentional, so the next attempt uses the repaired route.
    """
    normalized = _normalize_policy(policy)
    mode = normalized["routeRepairMode"]
    session_id = str(event.get("session_id") or "").strip()
    target_url = (bridge_url or DEFAULT_BRIDGE_URL).rstrip("/")

    result_status = "route-ok"
    error = ""
    route_before = ""
    route_after = ""
    provider = ""
    repair_result: dict = {}
    bridge_ensure_result: dict = {}

    try:
        route = inspect_config_route(config_path)
    except (OSError, ValueError) as exc:
        route = {}
        result_status = "config-unreadable"
        error = str(exc)

    if route:
        provider = str(route.get("activeProvider") or "")
        route_before = str(route.get("baseUrl") or "").rstrip("/")
        route_after = route_before
        if mode == "disabled":
            result_status = "disabled"
        elif route_before == target_url:
            result_status = "route-ok"
        elif not route.get("eligibleForAutomaticBridgeRepair"):
            result_status = "route-not-applicable"
        elif mode == "inspect" or not apply:
            result_status = "repair-planned"
        else:
            runner = route_repair_runner or (
                lambda config, bridge: run_configured_route_repair(
                    config,
                    bridge,
                    route_repair_script,
                )
            )
            try:
                repair_result = runner(config_path, target_url)
            except Exception as exc:  # noqa: BLE001 - a hook must not crash the prompt
                repair_result = {
                    "ok": False,
                    "status": "runner-error",
                    "error": str(exc),
                }
            if repair_result.get("ok"):
                result_status = "repaired"
                route_after = target_url
            else:
                result_status = "repair-failed"
                error = str(
                    repair_result.get("error")
                    or repair_result.get("status")
                    or "unknown failure"
                )

    if (
        apply
        and route_after == target_url
        and mode != "disabled"
        and (bridge_ensure_runner is not None or route_repair_script is not None)
    ):
        ensure_runner = bridge_ensure_runner or (
            lambda bridge: run_configured_bridge_ensure(
                bridge,
                route_repair_script,
            )
        )
        try:
            bridge_ensure_result = ensure_runner(target_url)
        except Exception as exc:  # noqa: BLE001 - hook must block safely
            bridge_ensure_result = {
                "ok": False,
                "status": "runner-error",
                "error": str(exc),
            }
        if not bridge_ensure_result.get("ok"):
            result_status = "bridge-start-failed"
            error = str(
                bridge_ensure_result.get("error")
                or bridge_ensure_result.get("status")
                or "unknown failure"
            )

    if result_status == "repaired":
        output = {
            "continue": False,
            "stopReason": (
                "Cross-provider bridge was re-inserted into the Codex route after a "
                "provider switch. Send the message again."
            ),
            "systemMessage": (
                f"Route repaired: {route_before or 'unknown'} -> {target_url}. "
                "The message was not sent; resend it."
            ),
        }
    elif result_status in {"repair-failed", "bridge-start-failed"}:
        output = {
            "continue": False,
            "stopReason": (
                "The cross-provider bridge is unavailable, so this message was not "
                f"sent: {error}"
            ),
            "systemMessage": "Bridge recovery failed; see the lifecycle status log.",
        }
    elif result_status == "repair-planned":
        output = {
            "continue": True,
            "systemMessage": (
                "Route repair is planned (inspect mode): "
                f"{route_before or 'no base_url'} -> {target_url}"
            ),
        }
    else:
        output = {"continue": True}

    try:
        metadata = lookup_conversation_metadata(codex_home, session_id)
    except Exception:  # noqa: BLE001 - metadata is only for logging
        metadata = {}
    recorded = {
        "event": "UserPromptSubmit",
        "timestamp": time.time(),
        "sessionId": session_id,
        "conversationTitle": str(metadata.get("title") or ""),
        "conversationCwd": str(metadata.get("cwd") or ""),
        "turnId": str(event.get("turn_id") or ""),
        "cwd": str(event.get("cwd") or ""),
        "model": str(event.get("model") or ""),
        "mode": mode,
        "result": result_status,
        "provider": provider,
        "routeBefore": route_before,
        "routeAfter": route_after,
        "bridgeUrl": target_url,
        "error": error,
        "repair": repair_result,
        "bridgeEnsure": bridge_ensure_result,
    }
    _write_json_atomic(status_path, recorded)
    if result_status in {
        "repair-planned",
        "repaired",
        "repair-failed",
        "bridge-start-failed",
        "config-unreadable",
    }:
        _append_json_line(status_path.parent / "lifecycle-events.jsonl", recorded)
    return output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", required=True)
    parser.add_argument("--codex-home", default=str(Path.home() / ".codex"))
    parser.add_argument(
        "--config",
        default=str(Path.home() / ".codex" / "config.toml"),
    )
    parser.add_argument("--status-file", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--bridge-url", default=DEFAULT_BRIDGE_URL)
    parser.add_argument("--route-repair-script", default="")
    return parser.parse_args()



def main() -> int:
    if os.environ.get(INTERNAL_ENV) == "1":
        print(json.dumps({"continue": True}, ensure_ascii=False))
        return 0

    args = parse_args()
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        print(json.dumps({"continue": True}, ensure_ascii=False))
        return 0
    event_name = str(event.get("hook_event_name") or "")
    if event_name not in {"PreCompact", "SessionStart", "UserPromptSubmit"}:
        print(json.dumps({"continue": True}, ensure_ascii=False))
        return 0

    try:
        policy = load_lifecycle_policy(Path(args.policy))
        if event_name == "UserPromptSubmit":
            result = run_user_prompt_submit_hook(
                event=event,
                policy=policy,
                codex_home=Path(args.codex_home),
                config_path=Path(args.config),
                status_path=Path(args.status_file),
                apply=args.apply,
                bridge_url=args.bridge_url,
                route_repair_script=(
                    Path(args.route_repair_script)
                    if args.route_repair_script
                    else None
                ),
            )
        else:
            runner = (
                run_precompact_hook
                if event_name == "PreCompact"
                else run_session_start_hook
            )
            result = runner(
                event=event,
                policy=policy,
                codex_home=Path(args.codex_home),
                config_path=Path(args.config),
                status_path=Path(args.status_file),
                apply=args.apply,
                bridge_url=args.bridge_url,
                route_repair_script=(
                    Path(args.route_repair_script)
                    if args.route_repair_script
                    else None
                ),
            )
    except Exception as exc:  # noqa: BLE001 - a hook must not block the prompt
        sys.stderr.write(f"codex lifecycle hook failed: {exc}\n")
        print(json.dumps({"continue": True}, ensure_ascii=False))
        return 0
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
