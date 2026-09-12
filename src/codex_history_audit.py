#!/usr/bin/env python3
"""Read-only audit of Codex thread provider metadata."""

from __future__ import annotations

import argparse
import json
import sqlite3
import tomllib
from pathlib import Path


def read_config_provider_ids(config_path: Path) -> set[str]:
    if not config_path.exists():
        return set()
    with config_path.open("rb") as handle:
        config = tomllib.load(handle)
    providers = config.get("model_providers") or {}
    return set(providers) if isinstance(providers, dict) else set()


def read_thread_rows(codex_home: Path) -> list[dict]:
    database = codex_home / "state_5.sqlite"
    if not database.exists():
        return []

    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        columns = {
            row[1] for row in connection.execute("pragma table_info(threads)")
        }
        if "model_provider" not in columns:
            return []
        title_expression = "title" if "title" in columns else "''"
        cwd_expression = "cwd" if "cwd" in columns else "''"
        rollout_expression = "rollout_path" if "rollout_path" in columns else "''"
        rows = []
        for thread_id, provider, title, cwd, path in connection.execute(
            f"select id, model_provider, {title_expression}, "
            f"{cwd_expression}, {rollout_expression} from threads"
        ):
            rows.append(
                {
                    "conversationId": str(thread_id),
                    "modelProvider": str(provider or ""),
                    "title": str(title or ""),
                    "cwd": str(cwd or ""),
                    "rolloutPath": str(path or ""),
                }
            )
        return rows
    finally:
        connection.close()


def audit_history(
    codex_home: Path,
    config_path: Path,
    scope: str,
    conversation_id: str,
) -> dict:
    rows = read_thread_rows(codex_home)
    if scope == "conversation":
        rows = [
            row for row in rows if row["conversationId"] == conversation_id
        ]

    provider_ids = sorted(
        {row["modelProvider"] for row in rows if row["modelProvider"]}
    )
    configured_ids = read_config_provider_ids(config_path)
    required_ids = [
        provider
        for provider in provider_ids
        if provider not in {"openai", "ollama", "lmstudio"}
    ]
    missing_ids = [provider for provider in required_ids if provider not in configured_ids]

    return {
        "scope": scope,
        "conversationId": conversation_id or None,
        "threadFound": bool(rows) if scope == "conversation" else None,
        "matchedThreads": len(rows),
        "threads": rows[:100],
        "providerIds": provider_ids,
        "requiredProviderIds": required_ids,
        "configuredProviderIds": sorted(configured_ids),
        "missingProviderIds": missing_ids,
        "historyRepairRequired": bool(missing_ids),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-home", default=str(Path.home() / ".codex"))
    parser.add_argument(
        "--config",
        default=str(Path.home() / ".codex" / "config.toml"),
    )
    parser.add_argument(
        "--scope",
        choices=("all", "conversation"),
        default="all",
    )
    parser.add_argument("--conversation-id", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.scope == "conversation" and not args.conversation_id:
        raise SystemExit("--conversation-id is required for conversation scope")
    result = audit_history(
        codex_home=Path(args.codex_home),
        config_path=Path(args.config),
        scope=args.scope,
        conversation_id=args.conversation_id,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
