from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_config_guard import inspect_config_route
from codex_provider_automation import (
    plan_provider_automation,
    run_provider_automation,
)


def _write_rollout(path: Path, conversation_id: str, provider: str, model: str) -> None:
    lines = [
        {
            "type": "session_meta",
            "payload": {
                "id": conversation_id,
                "model_provider": provider,
                "model": model,
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "thread_settings_applied",
                "thread_settings": {
                    "model_provider_id": provider,
                    "model": model,
                },
            },
        },
    ]
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in lines),
        encoding="utf-8",
    )


def write_automation_fixture(root: Path) -> tuple[Path, Path]:
    codex_home = root / ".codex"
    codex_home.mkdir()
    database = codex_home / "state_5.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        "create table threads ("
        "id text primary key, model_provider text, model text, title text, "
        "cwd text, rollout_path text"
        ")"
    )

    rows = [
        ("third-party-risk", "openai", "deepseek-flash"),
        ("official-safe", "openai", "gpt-5.5"),
        ("custom-safe", "custom", "deepseek-flash"),
    ]
    for conversation_id, provider, model in rows:
        rollout = codex_home / f"{conversation_id}.jsonl"
        _write_rollout(rollout, conversation_id, provider, model)
        connection.execute(
            "insert into threads values (?, ?, ?, ?, ?, ?)",
            (
                conversation_id,
                provider,
                model,
                conversation_id,
                str(root),
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
    return codex_home, config


class ProviderAutomationTests(unittest.TestCase):
    def test_plan_targets_only_third_party_model_on_openai_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home, config = write_automation_fixture(root)

            plan = plan_provider_automation(
                codex_home=codex_home,
                config_path=config,
                scope="all",
                conversation_id="",
                target_provider="custom",
                idle_seconds=0,
                max_items=10,
            )

            self.assertEqual(plan["candidateCount"], 1)
            self.assertEqual(
                plan["selected"][0]["conversationId"],
                "third-party-risk",
            )

    def test_recent_conversation_is_deferred(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home, config = write_automation_fixture(root)

            plan = plan_provider_automation(
                codex_home=codex_home,
                config_path=config,
                scope="all",
                conversation_id="",
                target_provider="custom",
                idle_seconds=3600,
                max_items=10,
            )

            self.assertEqual(plan["candidateCount"], 1)
            self.assertEqual(plan["selectedCount"], 0)
            self.assertEqual(plan["skipped"][0]["reason"], "recently-active")

    def test_current_conversation_can_be_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home, config = write_automation_fixture(root)

            plan = plan_provider_automation(
                codex_home=codex_home,
                config_path=config,
                scope="all",
                conversation_id="",
                target_provider="custom",
                idle_seconds=0,
                max_items=10,
                exclude_conversation_id="third-party-risk",
            )

            self.assertEqual(plan["candidateCount"], 0)
            self.assertEqual(plan["selectedCount"], 0)

    def test_apply_updates_only_safe_candidate_and_keeps_official_gpt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home, config = write_automation_fixture(root)

            result = run_provider_automation(
                codex_home=codex_home,
                config_path=config,
                scope="all",
                conversation_id="",
                target_provider="custom",
                idle_seconds=0,
                max_items=10,
                apply=True,
            )

            self.assertEqual(result["appliedCount"], 1)
            connection = sqlite3.connect(codex_home / "state_5.sqlite")
            try:
                providers = dict(
                    connection.execute(
                        "select id, model_provider from threads"
                    ).fetchall()
                )
            finally:
                connection.close()

            self.assertEqual(providers["third-party-risk"], "custom")
            self.assertEqual(providers["official-safe"], "openai")
            self.assertEqual(providers["custom-safe"], "custom")
            self.assertTrue(Path(result["migrated"][0]["backupDirectory"]).exists())

    def test_config_guard_refuses_official_gpt_and_accepts_local_third_party(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            official = root / "official.toml"
            official.write_text(
                'model_provider = "openai"\n'
                'model = "gpt-5.5"\n\n'
                '[model_providers.openai]\n'
                'name = "OpenAI"\n',
                encoding="utf-8",
            )
            third_party = root / "third-party.toml"
            third_party.write_text(
                'model_provider = "custom"\n'
                'model = "deepseek-flash"\n\n'
                '[model_providers.custom]\n'
                'name = "Third Party"\n'
                'base_url = "http://127.0.0.1:15721/v1"\n',
                encoding="utf-8",
            )

            official_result = inspect_config_route(official)
            third_party_result = inspect_config_route(third_party)

            self.assertFalse(official_result["eligibleForAutomaticBridgeRepair"])
            self.assertEqual(
                official_result["reason"],
                "official-model-or-provider",
            )
            self.assertTrue(
                third_party_result["eligibleForAutomaticBridgeRepair"]
            )

    def test_openai_is_rejected_as_an_automation_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home, config = write_automation_fixture(root)
            config.write_text(
                'model_provider = "custom"\n'
                'model = "deepseek-flash"\n\n'
                '[model_providers.custom]\n'
                'name = "Third Party"\n'
                'base_url = "http://127.0.0.1:15721/v1"\n\n'
                '[model_providers.openai]\n'
                'name = "OpenAI"\n',
                encoding="utf-8",
            )

            plan = plan_provider_automation(
                codex_home=codex_home,
                config_path=config,
                scope="all",
                conversation_id="",
                target_provider="openai",
                idle_seconds=0,
                max_items=10,
            )

            self.assertFalse(plan["targetProviderAvailable"])
            self.assertIn("official", plan["targetProviderRejectionReason"])
            self.assertEqual(plan["selectedCount"], 0)


if __name__ == "__main__":
    unittest.main()
