#!/usr/bin/env python3
"""Create additive conversation branches for cross-provider handoff."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from codex_config_guard import inspect_config_route
from codex_executable import resolve_codex_command
from codex_internal import INTERNAL_ENV
from codex_thread_provider_migrate import plan_thread_provider_migration


ForkExecutor = Callable[[dict, str], dict]


def plan_branch_handoff(
    codex_home: Path,
    config_path: Path,
    conversation_id: str,
    target_provider: str,
    target_model: str,
    backend: str,
) -> dict:
    if backend not in {"app-server", "cli"}:
        raise ValueError(f"Unsupported branch backend: {backend}")
    if not conversation_id:
        raise ValueError("conversation_id is required")

    route = inspect_config_route(config_path)
    selected_provider = target_provider or route["activeProvider"]
    selected_model = target_model or route["model"]
    if not selected_provider:
        raise ValueError("No target provider is available")
    if selected_provider not in route["providerIds"]:
        raise ValueError(f"Target provider is not configured: {selected_provider}")

    migration = plan_thread_provider_migration(
        codex_home=codex_home,
        conversation_id=conversation_id,
        target_provider=selected_provider,
    )
    if not migration.get("threadFound"):
        raise ValueError(f"Conversation not found: {conversation_id}")

    return {
        **migration,
        "backend": backend,
        "codexHome": str(codex_home),
        "targetProvider": selected_provider,
        "targetModel": selected_model,
        "conversationTitle": migration.get("title", ""),
        "conversationCwd": migration.get("cwd", ""),
        "sourceConversationId": conversation_id,
        "dryRun": False,
    }


def _append_history(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        handle.write("\n")


def list_branch_history(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                records.append(payload)
    return records


def _execute_app_server_branch(plan: dict, continue_prompt: str) -> dict:
    from codex_app_server_client import fork_thread_with_app_server

    return fork_thread_with_app_server(
        codex_home=Path(plan["codexHome"]),
        source_thread_id=plan["sourceConversationId"],
        provider=plan["targetProvider"],
        model=plan["targetModel"],
        continue_prompt=continue_prompt,
        timeout_seconds=120,
    )


def _execute_cli_branch(plan: dict, continue_prompt: str) -> dict:
    prompt = (
        continue_prompt
        or "Continue from the forked conversation. Do not repeat completed work."
    )
    command = [
        *resolve_codex_command(),
        "exec",
        "fork",
        "--json",
        "--skip-git-repo-check",
        "-c",
        f"model_provider={json.dumps(plan['targetProvider'])}",
        "-c",
        f"model={json.dumps(plan['targetModel'])}",
        plan["sourceConversationId"],
        prompt,
    ]
    environment = os.environ.copy()
    environment["CODEX_HOME"] = plan["codexHome"]
    environment[INTERNAL_ENV] = "1"
    completed = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        env=environment,
        check=False,
        cwd=plan["conversationCwd"] or None,
    )
    new_thread_id = ""
    completed_turn = False
    for line in completed.stdout.splitlines():
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if payload.get("type") == "thread.started":
            new_thread_id = str(payload.get("thread_id") or "")
        elif payload.get("type") == "turn.completed":
            completed_turn = True
    if completed.returncode != 0 or not new_thread_id or not completed_turn:
        raise RuntimeError("CLI branch handoff failed")
    return {
        "newConversationId": new_thread_id,
        "lastTurnId": "",
        "backend": "cli",
        "continued": True,
    }


def _execute_branch_backend(plan: dict, continue_prompt: str) -> dict:
    if plan["backend"] == "app-server":
        return _execute_app_server_branch(plan, continue_prompt)
    if plan["backend"] == "cli":
        return _execute_cli_branch(plan, continue_prompt)
    raise ValueError(f"Unsupported branch backend: {plan['backend']}")


def run_branch_handoff(
    codex_home: Path,
    config_path: Path,
    conversation_id: str,
    target_provider: str,
    target_model: str,
    backend: str,
    continue_prompt: str,
    history_path: Path,
    apply: bool,
    fork_executor: ForkExecutor | None = None,
) -> dict:
    plan = plan_branch_handoff(
        codex_home=codex_home,
        config_path=config_path,
        conversation_id=conversation_id,
        target_provider=target_provider,
        target_model=target_model,
        backend=backend,
    )
    if not apply:
        return {
            **plan,
            "status": "planned",
            "dryRun": True,
            "newConversationId": "",
        }

    executor = fork_executor or _execute_branch_backend
    try:
        execution = executor(plan, continue_prompt)
    except Exception as exc:
        failure = {
            **plan,
            "status": "failed",
            "dryRun": False,
            "newConversationId": "",
            "error": str(exc),
        }
        _append_history(
            history_path,
            {
                "createdAt": time.time(),
                "status": "failed",
                "sourceConversationId": conversation_id,
                "conversationTitle": plan["conversationTitle"],
                "conversationCwd": plan["conversationCwd"],
                "targetProvider": plan["targetProvider"],
                "targetModel": plan["targetModel"],
                "backend": backend,
                "errorType": type(exc).__name__,
            },
        )
        return failure

    new_conversation_id = str(execution.get("newConversationId") or "")
    if not new_conversation_id:
        raise ValueError("Branch backend did not return a new conversation ID")
    continued = bool(execution.get("continued", bool(continue_prompt)))
    record = {
        "createdAt": time.time(),
        "status": "branched",
        "sourceConversationId": conversation_id,
        "conversationTitle": plan["conversationTitle"],
        "conversationCwd": plan["conversationCwd"],
        "currentProvider": plan.get("currentProvider", ""),
        "currentModel": plan.get("model", ""),
        "targetProvider": plan["targetProvider"],
        "targetModel": plan["targetModel"],
        "backend": backend,
        "newConversationId": new_conversation_id,
        "continued": continued,
    }
    _append_history(history_path, record)
    return {
        **plan,
        "status": "branched",
        "dryRun": False,
        "newConversationId": new_conversation_id,
        "continued": continued,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-home", default=str(Path.home() / ".codex"))
    parser.add_argument(
        "--config",
        default=str(Path.home() / ".codex" / "config.toml"),
    )
    parser.add_argument("--conversation-id", default="")
    parser.add_argument("--target-provider", default="")
    parser.add_argument("--target-model", default="")
    parser.add_argument("--backend", choices=("app-server", "cli"), default="app-server")
    parser.add_argument("--continue-prompt", default="")
    parser.add_argument("--history-file", default="")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    history_path = Path(args.history_file) if args.history_file else (
        Path(__file__).resolve().parents[1] / "state" / "branch-history.jsonl"
    )
    if args.list:
        result: object = list_branch_history(history_path)
    else:
        if not args.conversation_id:
            raise SystemExit("--conversation-id is required")
        result = run_branch_handoff(
            codex_home=Path(args.codex_home),
            config_path=Path(args.config),
            conversation_id=args.conversation_id,
            target_provider=args.target_provider,
            target_model=args.target_model,
            backend=args.backend,
            continue_prompt=args.continue_prompt,
            history_path=history_path,
            apply=args.apply,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
