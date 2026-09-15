#!/usr/bin/env python3
"""Decide how lifecycle hooks should handle provider compatibility."""

from __future__ import annotations

import json
import os
from pathlib import Path

from codex_config_guard import inspect_config_route
from codex_thread_provider_migrate import plan_thread_provider_migration


COMPACT_MODES = {
    "disabled",
    "inspect",
    "repair-and-continue",
    "repair-and-stop",
    "repair-and-branch",
    "branch-only",
    "block-only",
}
BRANCH_MODES = {"repair-and-branch", "branch-only"}
BRANCH_BACKENDS = {"app-server", "cli"}
PROBE_MODES = {"disabled", "cli", "app-server"}
POST_SWITCH_SCOPES = {"preserve", "next", "all"}
SESSION_START_MODES = {"disabled", "repair", "repair-and-probe"}
ROUTE_REPAIR_MODES = {"disabled", "inspect", "repair"}
UNSAFE_TARGET_PROVIDERS = {"openai", "cc-switch-official"}
DEFAULT_LIFECYCLE_POLICY = {
    "schemaVersion": 1,
    "compactRepairMode": "repair-and-continue",
    "autoBranchEnabled": False,
    "branchBackend": "app-server",
    "postSwitchProbeMode": "disabled",
    "postSwitchScope": "preserve",
    "sessionStartMode": "repair",
    "routeRepairMode": "repair",
    "probeTimeoutSeconds": 30,
}


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temporary.replace(path)


def _normalize_policy(payload: object) -> dict:
    source = payload if isinstance(payload, dict) else {}
    policy = dict(DEFAULT_LIFECYCLE_POLICY)
    for key in policy:
        if key in source:
            policy[key] = source[key]

    if policy["compactRepairMode"] not in COMPACT_MODES:
        raise ValueError("Invalid compactRepairMode")
    if not isinstance(policy["autoBranchEnabled"], bool):
        raise ValueError("autoBranchEnabled must be a boolean")
    if policy["branchBackend"] not in BRANCH_BACKENDS:
        raise ValueError("Invalid branchBackend")
    if policy["postSwitchProbeMode"] not in PROBE_MODES:
        raise ValueError("Invalid postSwitchProbeMode")
    if policy["postSwitchScope"] not in POST_SWITCH_SCOPES:
        raise ValueError("Invalid postSwitchScope")
    if policy["sessionStartMode"] not in SESSION_START_MODES:
        raise ValueError("Invalid sessionStartMode")
    if policy["routeRepairMode"] not in ROUTE_REPAIR_MODES:
        raise ValueError("Invalid routeRepairMode")
    timeout = policy["probeTimeoutSeconds"]
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        raise ValueError("probeTimeoutSeconds must be an integer")
    if timeout < 1 or timeout > 300:
        raise ValueError("probeTimeoutSeconds must be between 1 and 300")
    if policy["schemaVersion"] != 1:
        raise ValueError("Unsupported lifecycle policy schema version")
    return policy


def load_lifecycle_policy(path: Path) -> dict:
    if not path.exists():
        return dict(DEFAULT_LIFECYCLE_POLICY)
    with path.open("r", encoding="utf-8") as handle:
        return _normalize_policy(json.load(handle))


def save_lifecycle_policy(path: Path, payload: object) -> dict:
    policy = _normalize_policy(payload)
    _write_json_atomic(path, policy)
    return policy


def decide_compact_action(
    codex_home: Path,
    config_path: Path,
    session_id: str,
    compact_mode: str,
    target_provider: str,
    target_model: str,
    auto_branch_enabled: bool,
) -> dict:
    if compact_mode not in COMPACT_MODES:
        raise ValueError(f"Unsupported compact mode: {compact_mode}")
    if not session_id:
        raise ValueError("session_id is required")

    route = inspect_config_route(config_path)
    selected_provider = target_provider or route["activeProvider"]
    selected_model = target_model or route["model"]
    plan = plan_thread_provider_migration(
        codex_home=codex_home,
        conversation_id=session_id,
        target_provider=selected_provider,
    )
    risk = bool(plan.get("threadFound") and plan.get("remoteCompactRisk"))

    action = "allow"
    reason = "no-remote-compact-risk"
    if compact_mode == "disabled":
        reason = "hook-disabled"
    elif not plan.get("threadFound"):
        reason = "session-not-found"
    elif not risk:
        reason = "no-remote-compact-risk"
    elif selected_provider in UNSAFE_TARGET_PROVIDERS:
        action = "block-only"
        reason = "target-provider-is-reserved"
    elif selected_provider not in route["providerIds"]:
        action = "block-only"
        reason = "target-provider-not-configured"
    elif compact_mode == "inspect":
        reason = "inspect-only"
    elif compact_mode in BRANCH_MODES and not auto_branch_enabled:
        action = "block-only"
        reason = "automatic-branch-disabled"
    else:
        action = compact_mode
        reason = "remote-compact-risk"

    return {
        "action": action,
        "reason": reason,
        "mode": compact_mode,
        "sessionId": session_id,
        "conversationTitle": plan.get("title", ""),
        "conversationCwd": plan.get("cwd", ""),
        "currentProvider": plan.get("currentProvider", ""),
        "currentModel": plan.get("model", ""),
        "targetProvider": selected_provider,
        "targetModel": selected_model,
        "remoteCompactRisk": risk,
        "activeProvider": route["activeProvider"],
        "activeModel": route["model"],
        "activeBaseUrl": route["baseUrl"],
        "eligibleForAutomaticBridgeRepair": route[
            "eligibleForAutomaticBridgeRepair"
        ],
        "configuredProviderIds": route["providerIds"],
        "autoBranchEnabled": auto_branch_enabled,
    }
