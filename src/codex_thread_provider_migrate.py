#!/usr/bin/env python3
"""Migrate persisted provider state for one Codex thread."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

from codex_history_audit import is_official_model, read_runtime_provider_ids


def _load_thread(codex_home: Path, conversation_id: str) -> dict | None:
    database = codex_home / "state_5.sqlite"
    if not database.exists():
        return None

    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        columns = {
            row[1] for row in connection.execute("pragma table_info(threads)")
        }
        required = {"id", "model_provider", "model", "rollout_path"}
        if not required.issubset(columns):
            return None
        row = connection.execute(
            "select id, model_provider, model, rollout_path, "
            "title, cwd from threads where id = ?",
            (conversation_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "conversationId": str(row[0]),
            "modelProvider": str(row[1] or ""),
            "model": str(row[2] or ""),
            "rolloutPath": str(row[3] or ""),
            "title": str(row[4] or ""),
            "cwd": str(row[5] or ""),
        }
    finally:
        connection.close()


def _scan_rollout(rollout_path: str) -> dict:
    path = Path(rollout_path)
    if not path.exists():
        return {
            "exists": False,
            "sessionMetaUpdates": 0,
            "runtimeSettingsUpdates": 0,
            "lines": 0,
        }

    session_meta_updates = 0
    runtime_updates = 0
    line_count = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line_count += 1
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = item.get("payload") or {}
            if item.get("type") == "session_meta":
                if payload.get("model_provider"):
                    session_meta_updates += 1
            elif (
                item.get("type") == "event_msg"
                and payload.get("type") == "thread_settings_applied"
            ):
                settings = payload.get("thread_settings") or {}
                if settings.get("model_provider_id"):
                    runtime_updates += 1
    return {
        "exists": True,
        "sessionMetaUpdates": session_meta_updates,
        "runtimeSettingsUpdates": runtime_updates,
        "lines": line_count,
    }


def _rollout_stamp(rollout_path: Path) -> tuple[int, int] | None:
    if not rollout_path.exists():
        return None
    stat = rollout_path.stat()
    return stat.st_size, stat.st_mtime_ns


def plan_thread_provider_migration(
    codex_home: Path,
    conversation_id: str,
    target_provider: str,
) -> dict:
    thread = _load_thread(codex_home, conversation_id)
    if not thread:
        return {
            "threadFound": False,
            "conversationId": conversation_id,
            "targetProvider": target_provider,
        }

    rollout = _scan_rollout(thread["rolloutPath"])
    runtime_providers = read_runtime_provider_ids(thread["rolloutPath"])
    current_provider = thread["modelProvider"]
    non_openai_runtime = [
        provider
        for provider in runtime_providers
        if provider not in {"openai", "ollama", "lmstudio"}
    ]
    remote_compact_risk = (
        current_provider == "openai"
        and bool(thread["model"])
        and not is_official_model(thread["model"])
    )

    return {
        "threadFound": True,
        "conversationId": conversation_id,
        "title": thread["title"],
        "cwd": thread["cwd"],
        "model": thread["model"],
        "currentProvider": current_provider,
        "targetProvider": target_provider,
        "runtimeProviderIds": runtime_providers,
        "runtimeProviderMigrationRequired": bool(
            non_openai_runtime or current_provider != target_provider
        ),
        "remoteCompactRisk": remote_compact_risk,
        "sessionMetaUpdates": rollout["sessionMetaUpdates"],
        "rolloutSettingsUpdates": rollout["runtimeSettingsUpdates"],
        "rolloutExists": rollout["exists"],
        "dryRun": True,
    }


def _update_rollout(
    rollout_path: Path,
    target_provider: str,
) -> tuple[int, int, float, tuple[int, int] | None]:
    original_stamp = _rollout_stamp(rollout_path)
    original_mtime = rollout_path.stat().st_mtime
    temporary = rollout_path.with_suffix(rollout_path.suffix + f".{os.getpid()}.tmp")
    session_meta_updates = 0
    runtime_updates = 0

    with rollout_path.open("r", encoding="utf-8", errors="replace") as source:
        with temporary.open("w", encoding="utf-8", newline="\n") as target:
            for line in source:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    target.write(line)
                    continue

                payload = item.get("payload")
                if isinstance(payload, dict):
                    if item.get("type") == "session_meta":
                        if payload.get("model_provider") != target_provider:
                            payload["model_provider"] = target_provider
                            session_meta_updates += 1
                    elif (
                        item.get("type") == "event_msg"
                        and payload.get("type") == "thread_settings_applied"
                    ):
                        settings = payload.get("thread_settings")
                        if isinstance(settings, dict):
                            if settings.get("model_provider_id") != target_provider:
                                settings["model_provider_id"] = target_provider
                                runtime_updates += 1

                target.write(
                    json.dumps(
                        item,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )

    if _rollout_stamp(rollout_path) != original_stamp:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("Conversation rollout changed during migration")
    temporary.replace(rollout_path)
    os.utime(rollout_path, (original_mtime, original_mtime))
    return session_meta_updates, runtime_updates, original_mtime, _rollout_stamp(
        rollout_path
    )


def apply_thread_provider_migration(
    codex_home: Path,
    conversation_id: str,
    target_provider: str,
    min_idle_seconds: int = 0,
) -> dict:
    plan = plan_thread_provider_migration(
        codex_home=codex_home,
        conversation_id=conversation_id,
        target_provider=target_provider,
    )
    if not plan.get("threadFound"):
        raise ValueError(f"Conversation not found: {conversation_id}")
    if not plan.get("rolloutExists"):
        raise ValueError("Conversation rollout file was not found")

    thread = _load_thread(codex_home, conversation_id)
    assert thread is not None
    rollout_path = Path(thread["rolloutPath"])
    if min_idle_seconds > 0:
        age_seconds = time.time() - rollout_path.stat().st_mtime
        if age_seconds < min_idle_seconds:
            raise RuntimeError(
                "Conversation is still active; deferring migration "
                f"until it has been idle for {min_idle_seconds} seconds"
            )
    rollout_stamp_before = _rollout_stamp(rollout_path)

    stamp = (
        time.strftime("%Y%m%d-%H%M%S", time.localtime())
        + f"-{time.time_ns() % 1_000_000_000:09d}"
    )
    backup_directory = (
        codex_home
        / "backups"
        / "codex-thread-provider-migrate"
        / f"{conversation_id}-{stamp}"
    )
    backup_directory.mkdir(parents=True, exist_ok=False)

    rollout_backup = backup_directory / "rollout.jsonl"
    database_backup = backup_directory / "state_5.sqlite"
    shutil.copy2(rollout_path, rollout_backup)

    source_database = codex_home / "state_5.sqlite"
    source_connection = sqlite3.connect(source_database)
    backup_connection = sqlite3.connect(database_backup)
    rollout_modified = False
    database_modified = False
    modified_rollout_stamp: tuple[int, int] | None = None
    try:
        source_connection.backup(backup_connection)
    finally:
        backup_connection.close()
        source_connection.close()

    manifest_path = backup_directory / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "conversationId": conversation_id,
                "createdAt": time.time(),
                "targetProvider": target_provider,
                "sourceProvider": plan["currentProvider"],
                "rolloutPath": str(rollout_path),
                "databasePath": str(source_database),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    try:
        current_thread = _load_thread(codex_home, conversation_id)
        if (
            current_thread is None
            or current_thread["modelProvider"] != plan["currentProvider"]
            or current_thread["model"] != plan["model"]
            or Path(current_thread["rolloutPath"]) != rollout_path
        ):
            raise RuntimeError("Conversation state changed before migration")
        if _rollout_stamp(rollout_path) != rollout_stamp_before:
            raise RuntimeError("Conversation rollout changed before migration")

        (
            session_meta_updates,
            runtime_updates,
            _,
            modified_rollout_stamp,
        ) = _update_rollout(
            rollout_path,
            target_provider,
        )
        rollout_modified = True
        connection = sqlite3.connect(source_database)
        try:
            connection.execute("begin immediate")
            updated = connection.execute(
                "update threads set model_provider = ? "
                "where id = ? and model_provider = ? and model = ?",
                (
                    target_provider,
                    conversation_id,
                    plan["currentProvider"],
                    plan["model"],
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError(
                    "Conversation database state changed before migration"
                )
            connection.commit()
            database_modified = True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
    except Exception as exc:
        if rollout_modified and _rollout_stamp(rollout_path) != modified_rollout_stamp:
            raise RuntimeError(
                "Conversation rollout changed after migration; no automatic "
                f"restore was performed. Backup: {backup_directory}"
            ) from exc
        if rollout_modified:
            shutil.copy2(rollout_backup, rollout_path)
        if database_modified:
            source_connection = sqlite3.connect(source_database)
            backup_connection = sqlite3.connect(database_backup)
            try:
                backup_connection.backup(source_connection)
            finally:
                backup_connection.close()
                source_connection.close()
        raise

    return {
        **plan,
        "dryRun": False,
        "applied": True,
        "sessionMetaUpdates": session_meta_updates,
        "rolloutSettingsUpdates": runtime_updates,
        "backupDirectory": str(backup_directory),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-home", default=str(Path.home() / ".codex"))
    parser.add_argument("--conversation-id", required=True)
    parser.add_argument("--target-provider", default="custom")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.apply:
        result = apply_thread_provider_migration(
            codex_home=Path(args.codex_home),
            conversation_id=args.conversation_id,
            target_provider=args.target_provider,
        )
    else:
        result = plan_thread_provider_migration(
            codex_home=Path(args.codex_home),
            conversation_id=args.conversation_id,
            target_provider=args.target_provider,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
