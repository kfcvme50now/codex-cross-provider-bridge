from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_branch_handoff import (
    list_branch_history,
    plan_branch_handoff,
    run_branch_handoff,
)


def write_fixture(root: Path) -> tuple[Path, Path, str]:
    codex_home = root / ".codex"
    codex_home.mkdir()
    conversation_id = "conversation-branch"
    rollout = codex_home / "rollout.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": conversation_id,
                    "model_provider": "openai",
                    "model": "deepseek-flash",
                    "cwd": r"C:\work\branch",
                },
            }
        )
        + "\n",
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
            "deepseek-flash",
            "Branch source",
            r"C:\work\branch",
            str(rollout),
        ),
    )
    connection.commit()
    connection.close()
    config = codex_home / "config.toml"
    config.write_text(
        'model_provider = "custom"\n'
        'model = "deepseek-flash"\n\n'
        '[model_providers.custom]\n'
        'name = "Third Party"\n'
        'base_url = "http://127.0.0.1:15721/v1"\n'
        'wire_api = "responses"\n',
        encoding="utf-8",
    )
    return codex_home, config, conversation_id


class BranchHandoffTests(unittest.TestCase):
    def test_plan_is_read_only_and_reports_human_readable_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home, config, conversation_id = write_fixture(Path(directory))

            plan = plan_branch_handoff(
                codex_home=codex_home,
                config_path=config,
                conversation_id=conversation_id,
                target_provider="",
                target_model="",
                backend="app-server",
            )

            self.assertTrue(plan["threadFound"])
            self.assertTrue(plan["remoteCompactRisk"])
            self.assertEqual(plan["targetProvider"], "custom")
            self.assertEqual(plan["targetModel"], "deepseek-flash")
            self.assertEqual(plan["conversationTitle"], "Branch source")
            self.assertEqual(plan["conversationCwd"], r"C:\work\branch")
            self.assertFalse(plan["dryRun"])

    def test_apply_records_new_branch_without_modifying_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home, config, conversation_id = write_fixture(root)
            history_path = root / "branch-history.jsonl"

            def fake_executor(plan: dict, continue_prompt: str) -> dict:
                self.assertEqual(continue_prompt, "")
                return {
                    "newConversationId": "conversation-branch-copy",
                    "backend": plan["backend"],
                }

            result = run_branch_handoff(
                codex_home=codex_home,
                config_path=config,
                conversation_id=conversation_id,
                target_provider="",
                target_model="",
                backend="app-server",
                continue_prompt="",
                history_path=history_path,
                apply=True,
                fork_executor=fake_executor,
            )

            self.assertEqual(result["status"], "branched")
            self.assertEqual(
                result["newConversationId"],
                "conversation-branch-copy",
            )
            connection = sqlite3.connect(codex_home / "state_5.sqlite")
            try:
                provider = connection.execute(
                    "select model_provider from threads where id = ?",
                    (conversation_id,),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(provider, "openai")
            history = list_branch_history(history_path)
            self.assertEqual(len(history), 1)
            self.assertEqual(
                history[0]["newConversationId"],
                "conversation-branch-copy",
            )
            self.assertEqual(history[0]["conversationTitle"], "Branch source")


if __name__ == "__main__":
    unittest.main()
