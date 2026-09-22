from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_ccswitch_provider_policy import set_routing_policy


class ProviderRoutingPolicyTests(unittest.TestCase):
    def _database(self, root: Path) -> Path:
        database = root / "cc-switch.db"
        connection = sqlite3.connect(database)
        connection.execute(
            "create table providers ("
            "id text, app_type text, name text, settings_config text, notes text, "
            "meta text, is_current integer, in_failover_queue integer"
            ")"
        )
        connection.execute(
            "insert into providers values (?, 'codex', ?, ?, ?, ?, 1, 1)",
            (
                "provider-a",
                "Provider A",
                '{"auth":{"OPENAI_API_KEY":"keep-unread"}}',
                "Existing note",
                json.dumps({"existing": "kept"}),
            ),
        )
        connection.commit()
        connection.close()
        return database

    def test_disable_preserves_settings_and_adds_reversible_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(Path(directory))

            result = set_routing_policy(
                database,
                "provider-a",
                disabled=True,
                reason="subscription_expired",
                apply=True,
            )

            self.assertTrue(result["changed"])
            connection = sqlite3.connect(database)
            row = connection.execute(
                "select settings_config, notes, meta, is_current, in_failover_queue "
                "from providers where id='provider-a' and app_type='codex'"
            ).fetchone()
            connection.close()
            self.assertEqual(
                row[0], '{"auth":{"OPENAI_API_KEY":"keep-unread"}}'
            )
            self.assertIn("Existing note", row[1])
            self.assertIn("Routing disabled: subscription expired", row[1])
            self.assertEqual(json.loads(row[2])["existing"], "kept")
            self.assertTrue(json.loads(row[2])["routing_disabled"])
            self.assertEqual(row[3:], (0, 0))

    def test_enable_removes_only_managed_marker_and_does_not_activate_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(Path(directory))
            set_routing_policy(
                database,
                "provider-a",
                disabled=True,
                reason="subscription_expired",
                apply=True,
            )

            result = set_routing_policy(
                database,
                "provider-a",
                disabled=False,
                apply=True,
            )

            self.assertTrue(result["changed"])
            connection = sqlite3.connect(database)
            row = connection.execute(
                "select notes, meta, is_current, in_failover_queue "
                "from providers where id='provider-a' and app_type='codex'"
            ).fetchone()
            connection.close()
            self.assertEqual(row[0], "Existing note")
            self.assertEqual(json.loads(row[1]), {"existing": "kept"})
            self.assertEqual(row[2:], (0, 0))

    def test_preview_does_not_modify_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(Path(directory))
            result = set_routing_policy(
                database,
                "provider-a",
                disabled=True,
                reason="maintenance",
                apply=False,
            )
            self.assertTrue(result["changed"])
            connection = sqlite3.connect(database)
            row = connection.execute(
                "select meta, is_current, in_failover_queue from providers "
                "where id='provider-a' and app_type='codex'"
            ).fetchone()
            connection.close()
            self.assertEqual(json.loads(row[0]), {"existing": "kept"})
            self.assertEqual(row[1:], (1, 1))


if __name__ == "__main__":
    unittest.main()
