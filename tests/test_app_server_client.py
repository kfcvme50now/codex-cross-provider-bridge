from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_app_server_client import (
    fork_thread_with_app_server,
    probe_provider_with_app_server,
)


FAKE_APP_SERVER = r'''
import json
import sys

for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        continue
    if method == "initialize":
        result = {"userAgent": "fake", "platformFamily": "windows", "platformOs": "windows"}
    elif method == "thread/start":
        result = {"thread": {"id": "probe-thread", "modelProvider": "custom"}}
    elif method == "turn/start":
        result = {"turn": {"id": "probe-turn", "status": "inProgress", "items": [], "error": None}}
        print(json.dumps({"id": request_id, "result": result}), flush=True)
        print(json.dumps({"method": "turn/completed", "params": {"turn": {"id": "probe-turn", "status": "completed"}}}), flush=True)
        continue
    elif method == "thread/read":
        result = {"thread": {"id": "source-thread", "turns": [{"id": "turn-1", "status": "completed"}, {"id": "turn-2", "status": "inProgress"}]}}
    elif method == "thread/fork":
        result = {"thread": {"id": "branch-thread", "forkedFromId": "source-thread"}}
    else:
        print(json.dumps({"id": request_id, "error": {"code": -32601, "message": "unknown"}}), flush=True)
        continue
    print(json.dumps({"id": request_id, "result": result}), flush=True)
'''


class AppServerClientTests(unittest.TestCase):
    def test_probe_uses_ephemeral_thread_and_reports_only_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / "fake_app_server.py"
            fake.write_text(textwrap.dedent(FAKE_APP_SERVER), encoding="utf-8")

            result = probe_provider_with_app_server(
                codex_home=root / ".codex",
                provider="custom",
                model="deepseek-flash",
                timeout_seconds=10,
                command=[sys.executable, str(fake)],
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["probeId"], "probe-thread")
            self.assertEqual(result["errorCategory"], "")
            self.assertNotIn("message", result)

    def test_branch_fork_uses_last_completed_turn_and_overrides_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / "fake_app_server.py"
            fake.write_text(textwrap.dedent(FAKE_APP_SERVER), encoding="utf-8")

            result = fork_thread_with_app_server(
                codex_home=root / ".codex",
                source_thread_id="source-thread",
                provider="custom",
                model="deepseek-flash",
                continue_prompt="",
                timeout_seconds=10,
                command=[sys.executable, str(fake)],
            )

            self.assertEqual(result["newConversationId"], "branch-thread")
            self.assertEqual(result["lastTurnId"], "turn-1")
            self.assertEqual(result["backend"], "app-server")


if __name__ == "__main__":
    unittest.main()
