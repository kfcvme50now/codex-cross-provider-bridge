from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_history_audit import audit_history
from codex_thread_provider_migrate import (
    apply_thread_provider_migration,
    plan_thread_provider_migration,
)


def write_fixture(root: Path, model: str = "deepseek-flash") -> tuple[Path, Path, str]:
    codex_home = root / ".codex"
    codex_home.mkdir()
    conversation_id = "conversation-compact-test"
    rollout = codex_home / "rollout.jsonl"

    lines = [
        {
            "type": "session_meta",
            "payload": {
                "id": conversation_id,
                "model_provider": "openai",
                "model": model,
                "cwd": r"C:\work\example",
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "thread_settings_applied",
                "thread_settings": {
                    "model_provider_id": "openai",
                    "model": model,
                    "cwd": r"C:\work\example",
                },
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "error": {
                    "message": (
                        "Error running remote compact task: "
                        "The 'deepseek-flash' model is not supported when "
                        "using Codex with a ChatGPT account."
                    )
                },
            },
        },
    ]
    rollout.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in lines),
        encoding="utf-8",
    )

    database = codex_home / "state_5.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        "create table threads ("
        "id text primary key, model_provider text, model text, title text, "
        "cwd text, rollout_path text"
        ")"
    )
    connection.execute(
        "insert into threads values (?, ?, ?, ?, ?, ?)",
        (
            conversation_id,
            "openai",
            model,
            "Compact failure",
            r"C:\work\example",
            str(rollout),
        ),
    )
    connection.commit()
    connection.close()
    return codex_home, rollout, conversation_id


class ThreadProviderMigrationTests(unittest.TestCase):
    def test_plan_detects_remote_compact_risk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home, _, conversation_id = write_fixture(root)

            plan = plan_thread_provider_migration(
                codex_home=codex_home,
                conversation_id=conversation_id,
                target_provider="custom",
            )

            self.assertTrue(plan["threadFound"])
            self.assertTrue(plan["remoteCompactRisk"])
            self.assertEqual(plan["currentProvider"], "openai")
            self.assertEqual(plan["targetProvider"], "custom")
            self.assertEqual(plan["rolloutSettingsUpdates"], 1)

    def test_plan_does_not_flag_official_reasoning_model_as_third_party(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home, _, conversation_id = write_fixture(root, model="o3-mini")

            plan = plan_thread_provider_migration(
                codex_home=codex_home,
                conversation_id=conversation_id,
                target_provider="custom",
            )

            self.assertFalse(plan["remoteCompactRisk"])

    def test_apply_updates_rollout_and_state_database_with_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home, rollout, conversation_id = write_fixture(root)

            result = apply_thread_provider_migration(
                codex_home=codex_home,
                conversation_id=conversation_id,
                target_provider="custom",
            )

            self.assertTrue(result["applied"])
            self.assertTrue(Path(result["backupDirectory"]).exists())

            lines = [
                json.loads(line)
                for line in rollout.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(lines[0]["payload"]["model_provider"], "custom")
            self.assertEqual(
                lines[1]["payload"]["thread_settings"]["model_provider_id"],
                "custom",
            )

            connection = sqlite3.connect(codex_home / "state_5.sqlite")
            try:
                provider = connection.execute(
                    "select model_provider from threads where id = ?",
                    (conversation_id,),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(provider, "custom")

    def test_apply_can_restore_from_manifest_provider_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home, rollout, conversation_id = write_fixture(root)

            migrated = apply_thread_provider_migration(
                codex_home=codex_home,
                conversation_id=conversation_id,
                target_provider="custom",
            )
            self.assertEqual(migrated["currentProvider"], "openai")

            restored = apply_thread_provider_migration(
                codex_home=codex_home,
                conversation_id=conversation_id,
                target_provider=migrated["currentProvider"],
            )

            self.assertTrue(restored["applied"])
            connection = sqlite3.connect(codex_home / "state_5.sqlite")
            try:
                provider = connection.execute(
                    "select model_provider from threads where id = ?",
                    (conversation_id,),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(provider, "openai")

            lines = [
                json.loads(line)
                for line in rollout.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(lines[0]["payload"]["model_provider"], "openai")

    def test_history_audit_flags_remote_compact_risk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home, _, conversation_id = write_fixture(root)
            config = codex_home / "config.toml"
            config.write_text(
                'model_provider = "custom"\n\n'
                '[model_providers.custom]\n'
                'name = "Third Party"\n',
                encoding="utf-8",
            )

            audit = audit_history(
                codex_home=codex_home,
                config_path=config,
                scope="conversation",
                conversation_id=conversation_id,
            )

            self.assertTrue(audit["remoteCompactRisk"])
            self.assertEqual(audit["runtimeProviderIds"], ["openai"])

    def test_migration_does_not_restore_over_concurrent_rollout_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home, rollout, conversation_id = write_fixture(root)
            concurrent_line = json.dumps(
                {
                    "type": "event_msg",
                    "payload": {"type": "concurrent-write", "value": "keep-me"},
                }
            )

            def racing_update(_rollout_path: Path, _target_provider: str):
                with rollout.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(concurrent_line + "\n")
                raise RuntimeError("Conversation rollout changed during migration")

            with patch(
                "codex_thread_provider_migrate._update_rollout",
                side_effect=racing_update,
            ):
                with self.assertRaisesRegex(RuntimeError, "changed during migration"):
                    apply_thread_provider_migration(
                        codex_home=codex_home,
                        conversation_id=conversation_id,
                        target_provider="custom",
                    )

            self.assertIn(concurrent_line, rollout.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
