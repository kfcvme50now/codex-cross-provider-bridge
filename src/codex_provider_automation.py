#!/usr/bin/env python3
"""Safe, opt-in automation for provider-sensitive Codex conversations.

The automation deliberately has a narrow trigger:

* the persisted thread provider is ``openai``;
* the persisted thread model is not an official Codex/GPT model; and
* the configured target provider exists and is not OpenAI.

That catches the remote-compaction mismatch without touching ordinary
ChatGPT/GPT conversations.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from codex_history_audit import is_official_model, read_thread_rows
from codex_thread_provider_migrate import (
    apply_thread_provider_migration,
    plan_thread_provider_migration,
)

UNSAFE_TARGET_PROVIDERS = {"openai", "cc-switch-official"}


def _target_provider_ids(config_path: Path) -> set[str]:
    if not config_path.exists():
        return set()

    import tomllib

    with config_path.open("rb") as handle:
        config = tomllib.load(handle)
    providers = config.get("model_providers") or {}
    return set(providers) if isinstance(providers, dict) else set()


def _idle_seconds_for_thread(rollout_path: str, now: float) -> float | None:
    path = Path(rollout_path)
    if not path.exists():
        return None
    return max(0.0, now - path.stat().st_mtime)


def plan_provider_automation(
    codex_home: Path,
    config_path: Path,
    scope: str,
    conversation_id: str,
    target_provider: str,
    idle_seconds: int,
    max_items: int,
    exclude_conversation_id: str = "",
) -> dict:
    """Return a read-only plan for narrowly scoped provider migrations."""
    if scope not in {"all", "next", "conversation"}:
        raise ValueError(f"Unsupported automation scope: {scope}")
    if scope == "conversation" and not conversation_id:
        raise ValueError("conversation scope requires a conversation ID")
    if max_items < 1:
        raise ValueError("max_items must be at least 1")

    configured_providers = _target_provider_ids(config_path)
    target_safe = target_provider not in UNSAFE_TARGET_PROVIDERS
    target_available = target_provider in configured_providers and target_safe
    target_rejection_reason = ""
    if not target_safe:
        target_rejection_reason = "target provider is reserved for official routes"
    elif target_provider not in configured_providers:
        target_rejection_reason = "target provider is not configured"
    now = time.time()
    candidates: list[dict] = []
    eligible_candidates: list[dict] = []
    skipped: list[dict] = []

    if target_available:
        rows = read_thread_rows(codex_home)
        for row in rows:
            if scope == "conversation" and row["conversationId"] != conversation_id:
                continue
            if (
                exclude_conversation_id
                and row["conversationId"] == exclude_conversation_id
            ):
                continue

            model = row.get("model", "")
            if row.get("modelProvider") != "openai":
                continue
            if not model or is_official_model(model):
                continue

            plan = plan_thread_provider_migration(
                codex_home=codex_home,
                conversation_id=row["conversationId"],
                target_provider=target_provider,
            )
            if not plan.get("threadFound") or not plan.get("remoteCompactRisk"):
                continue

            idle = _idle_seconds_for_thread(row.get("rolloutPath", ""), now)
            eligible = {
                **plan,
                "idleSeconds": None if idle is None else round(idle, 3),
            }
            eligible_candidates.append(eligible)
            if idle is None:
                skipped.append(
                    {
                        "conversationId": row["conversationId"],
                        "reason": "rollout-not-found",
                    }
                )
                continue
            if idle < idle_seconds:
                skipped.append(
                    {
                        "conversationId": row["conversationId"],
                        "reason": "recently-active",
                        "idleSeconds": round(idle, 3),
                    }
                )
                continue

            candidates.append(eligible)

    candidates.sort(
        key=lambda item: item.get("idleSeconds", 0.0),
        reverse=True,
    )
    selected = candidates[: max_items if scope == "all" else 1]

    return {
        "scope": scope,
        "conversationId": conversation_id or None,
        "targetProvider": target_provider,
        "targetProviderAvailable": target_available,
        "targetProviderRejectionReason": target_rejection_reason,
        "configuredProviderIds": sorted(configured_providers),
        "idleSeconds": idle_seconds,
        "maxItems": max_items,
        "excludeConversationId": exclude_conversation_id or None,
        "candidateCount": len(eligible_candidates),
        "eligibleCount": len(eligible_candidates),
        "readyCount": len(candidates),
        "selectedCount": len(selected),
        "selected": selected,
        "skippedCount": len(skipped),
        "skipped": skipped[:100],
    }


def run_provider_automation(
    codex_home: Path,
    config_path: Path,
    scope: str,
    conversation_id: str,
    target_provider: str,
    idle_seconds: int,
    max_items: int,
    apply: bool,
    exclude_conversation_id: str = "",
) -> dict:
    plan = plan_provider_automation(
        codex_home=codex_home,
        config_path=config_path,
        scope=scope,
        conversation_id=conversation_id,
        target_provider=target_provider,
        idle_seconds=idle_seconds,
        max_items=max_items,
        exclude_conversation_id=exclude_conversation_id,
    )
    if not apply:
        return {**plan, "dryRun": True, "appliedCount": 0, "migrated": []}
    if not plan["targetProviderAvailable"]:
        return {
            **plan,
            "dryRun": False,
            "appliedCount": 0,
            "migrated": [],
            "error": (
                plan["targetProviderRejectionReason"]
                or f"Target provider is not configured: {target_provider}"
            ),
        }

    migrated: list[dict] = []
    failures: list[dict] = []
    for candidate in plan["selected"]:
        try:
            result = apply_thread_provider_migration(
                codex_home=codex_home,
                conversation_id=candidate["conversationId"],
                target_provider=target_provider,
                min_idle_seconds=idle_seconds,
            )
            migrated.append(result)
        except Exception as exc:  # pragma: no cover - exercised by integration
            failures.append(
                {
                    "conversationId": candidate["conversationId"],
                    "error": str(exc),
                }
            )

    return {
        **plan,
        "dryRun": False,
        "appliedCount": len(migrated),
        "migrated": migrated,
        "failureCount": len(failures),
        "failures": failures,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safely migrate provider-sensitive Codex conversations."
    )
    parser.add_argument("--codex-home", default=str(Path.home() / ".codex"))
    parser.add_argument(
        "--config",
        default=str(Path.home() / ".codex" / "config.toml"),
    )
    parser.add_argument(
        "--scope",
        choices=("all", "next", "conversation"),
        default="conversation",
    )
    parser.add_argument("--conversation-id", default="")
    parser.add_argument("--target-provider", default="custom")
    parser.add_argument("--idle-seconds", type=int, default=300)
    parser.add_argument("--max-items", type=int, default=10)
    parser.add_argument("--exclude-conversation-id", default="")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_provider_automation(
        codex_home=Path(args.codex_home),
        config_path=Path(args.config),
        scope=args.scope,
        conversation_id=args.conversation_id,
        target_provider=args.target_provider,
        idle_seconds=args.idle_seconds,
        max_items=args.max_items,
        apply=args.apply,
        exclude_conversation_id=args.exclude_conversation_id,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
