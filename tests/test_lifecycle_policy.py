from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_lifecycle_policy import (
    decide_compact_action,
    load_lifecycle_policy,
    save_lifecycle_policy,
)
from codex_lifecycle_hook import (
    run_precompact_hook,
    run_session_start_hook,
    run_user_prompt_submit_hook,
)


def write_fixture(root: Path, model: str = "deepseek-flash") -> tuple[Path, Path, str]:
    codex_home = root / ".codex"
    codex_home.mkdir()
    conversation_id = "conversation-compact"
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
                },
            },
        },
    ]
    rollout.write_text(
        "".join(json.dumps(item) + "\n" for item in lines),
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

    config = codex_home / "config.toml"
    config.write_text(
        'model_provider = "custom"\n'
        f'model = "{model}"\n\n'
        '[model_providers.custom]\n'
        'name = "Third Party"\n'
        'base_url = "http://127.0.0.1:15721/v1"\n'
        'wire_api = "responses"\n',
        encoding="utf-8",
    )
    return codex_home, config, conversation_id


class LifecyclePolicyTests(unittest.TestCase):
    def test_repair_and_stop_targets_remote_compact_risk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home, config, conversation_id = write_fixture(Path(directory))

            decision = decide_compact_action(
                codex_home=codex_home,
                config_path=config,
                session_id=conversation_id,
                compact_mode="repair-and-stop",
                target_provider="",
                target_model="",
                auto_branch_enabled=False,
            )

            self.assertEqual(decision["action"], "repair-and-stop")
            self.assertEqual(decision["targetProvider"], "custom")
            self.assertEqual(decision["targetModel"], "deepseek-flash")
            self.assertEqual(decision["conversationTitle"], "Compact failure")
            self.assertEqual(decision["conversationCwd"], r"C:\work\example")
            self.assertTrue(decision["remoteCompactRisk"])

    def test_branch_mode_is_blocked_until_auto_branch_is_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home, config, conversation_id = write_fixture(Path(directory))

            decision = decide_compact_action(
                codex_home=codex_home,
                config_path=config,
                session_id=conversation_id,
                compact_mode="branch-only",
                target_provider="",
                target_model="",
                auto_branch_enabled=False,
            )

            self.assertEqual(decision["action"], "block-only")
            self.assertEqual(decision["reason"], "automatic-branch-disabled")

    def test_policy_round_trip_preserves_explicit_switchable_modes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"

            save_lifecycle_policy(
                path,
                {
                    "compactRepairMode": "repair-and-branch",
                    "autoBranchEnabled": True,
                    "branchBackend": "cli",
                    "postSwitchProbeMode": "app-server",
                    "postSwitchScope": "next",
                    "sessionStartMode": "repair",
                    "probeTimeoutSeconds": 25,
                },
            )
            policy = load_lifecycle_policy(path)

            self.assertEqual(policy["compactRepairMode"], "repair-and-branch")
            self.assertTrue(policy["autoBranchEnabled"])
            self.assertEqual(policy["branchBackend"], "cli")
            self.assertEqual(policy["postSwitchProbeMode"], "app-server")
            self.assertEqual(policy["postSwitchScope"], "next")
            self.assertEqual(policy["sessionStartMode"], "repair")
            self.assertEqual(policy["probeTimeoutSeconds"], 25)

    def test_precompact_repair_and_stop_blocks_then_migrates_safely(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home, config, conversation_id = write_fixture(root)
            status_path = root / "lifecycle-status.json"

            result = run_precompact_hook(
                event={
                    "session_id": conversation_id,
                    "hook_event_name": "PreCompact",
                    "trigger": "manual",
                    "turn_id": "turn-1",
                    "cwd": r"C:\work\example",
                    "model": "deepseek-flash",
                },
                policy={
                    "compactRepairMode": "repair-and-stop",
                    "autoBranchEnabled": False,
                    "branchBackend": "app-server",
                    "postSwitchProbeMode": "disabled",
                    "postSwitchScope": "preserve",
                    "sessionStartMode": "repair",
                    "probeTimeoutSeconds": 30,
                },
                codex_home=codex_home,
                config_path=config,
                status_path=status_path,
                apply=True,
            )

            self.assertFalse(result["continue"])
            self.assertIn("deepseek-flash", result["stopReason"])
            connection = sqlite3.connect(codex_home / "state_5.sqlite")
            try:
                provider = connection.execute(
                    "select model_provider from threads where id = ?",
                    (conversation_id,),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(provider, "custom")
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["event"], "PreCompact")
            self.assertEqual(status["action"], "repair-and-stop")
            self.assertEqual(status["result"], "repaired-and-stopped")

    def test_precompact_branch_mode_creates_additive_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home, config, conversation_id = write_fixture(root)
            status_path = root / "lifecycle-status.json"
            calls: list[dict] = []

            def fake_branch_runner(**kwargs: object) -> dict:
                calls.append(kwargs)
                return {
                    "status": "branched",
                    "newConversationId": "conversation-branch-copy",
                    "conversationTitle": "Compact failure",
                    "conversationCwd": r"C:\work\example",
                }

            result = run_precompact_hook(
                event={
                    "session_id": conversation_id,
                    "hook_event_name": "PreCompact",
                    "trigger": "auto",
                    "turn_id": "turn-1",
                    "cwd": r"C:\work\example",
                    "model": "deepseek-flash",
                },
                policy={
                    "compactRepairMode": "branch-only",
                    "autoBranchEnabled": True,
                    "branchBackend": "app-server",
                    "postSwitchProbeMode": "disabled",
                    "postSwitchScope": "preserve",
                    "sessionStartMode": "repair",
                    "probeTimeoutSeconds": 30,
                },
                codex_home=codex_home,
                config_path=config,
                status_path=status_path,
                apply=True,
                branch_runner=fake_branch_runner,
            )

            self.assertFalse(result["continue"])
            self.assertIn("conversation-branch-copy", result["systemMessage"])
            self.assertEqual(len(calls), 1)
            connection = sqlite3.connect(codex_home / "state_5.sqlite")
            try:
                provider = connection.execute(
                    "select model_provider from threads where id = ?",
                    (conversation_id,),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(provider, "openai")
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["result"], "branch-created")

    def test_session_start_repairs_risky_session_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home, config, conversation_id = write_fixture(root)
            status_path = root / "lifecycle-status.json"

            result = run_session_start_hook(
                event={
                    "session_id": conversation_id,
                    "hook_event_name": "SessionStart",
                    "source": "startup",
                    "cwd": r"C:\work\example",
                    "model": "deepseek-flash",
                },
                policy={
                    "compactRepairMode": "repair-and-stop",
                    "autoBranchEnabled": False,
                    "branchBackend": "app-server",
                    "postSwitchProbeMode": "disabled",
                    "postSwitchScope": "preserve",
                    "sessionStartMode": "repair",
                    "probeTimeoutSeconds": 30,
                },
                codex_home=codex_home,
                config_path=config,
                status_path=status_path,
                apply=True,
            )

            self.assertTrue(result["continue"])
            self.assertIn("Compact failure", result["systemMessage"])
            self.assertIn(r"C:\work\example", result["systemMessage"])
            connection = sqlite3.connect(codex_home / "state_5.sqlite")
            try:
                provider = connection.execute(
                    "select model_provider from threads where id = ?",
                    (conversation_id,),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(provider, "custom")
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["event"], "SessionStart")
            self.assertEqual(status["result"], "repaired")

    def test_session_start_can_verify_route_with_a_real_probe_runner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home, config, conversation_id = write_fixture(root)
            status_path = root / "lifecycle-status.json"
            calls: list[dict] = []

            def fake_probe(**kwargs: object) -> dict:
                calls.append(kwargs)
                return {
                    "ok": True,
                    "status": "verified",
                    "provider": "custom",
                    "model": "deepseek-flash",
                    "probeId": "probe-thread",
                    "errorCategory": "",
                }

            result = run_session_start_hook(
                event={
                    "session_id": conversation_id,
                    "hook_event_name": "SessionStart",
                    "source": "startup",
                    "cwd": r"C:\work\example",
                    "model": "deepseek-flash",
                },
                policy={
                    "compactRepairMode": "repair-and-stop",
                    "autoBranchEnabled": False,
                    "branchBackend": "app-server",
                    "postSwitchProbeMode": "cli",
                    "postSwitchScope": "next",
                    "sessionStartMode": "repair-and-probe",
                    "probeTimeoutSeconds": 30,
                },
                codex_home=codex_home,
                config_path=config,
                status_path=status_path,
                apply=True,
                probe_runner=fake_probe,
            )

            self.assertTrue(result["continue"])
            self.assertEqual(len(calls), 1)
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["result"], "repaired-and-verified")
            self.assertTrue(status["probe"]["ok"])


BRIDGE_URL = "http://127.0.0.1:15722/v1"
CC_SWITCH_URL = "http://127.0.0.1:15721/v1"


def write_route_config(
    path: Path,
    provider: str = "custom",
    base_url: str = CC_SWITCH_URL,
    model: str = "deepseek-flash",
) -> None:
    lines = [
        f'model_provider = "{provider}"',
        f'model = "{model}"',
        "",
        f"[model_providers.{provider}]",
        'name = "Third Party"',
    ]
    if base_url:
        lines.append(f'base_url = "{base_url}"')
    lines += ['wire_api = "responses"', "requires_openai_auth = true"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class RouteRepairHookTests(unittest.TestCase):
    def _run(
        self,
        root: Path,
        config_path: Path,
        policy: dict | None = None,
        runner=None,
        apply: bool = True,
    ) -> tuple[dict, Path, Path]:
        status_path = root / "state" / "lifecycle-status.json"
        result = run_user_prompt_submit_hook(
            event={
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "cwd": str(root),
                "model": "deepseek-flash",
                "prompt": "hello",
            },
            policy=policy or {"routeRepairMode": "repair"},
            codex_home=root / ".codex",
            config_path=config_path,
            status_path=status_path,
            apply=apply,
            bridge_url=BRIDGE_URL,
            route_repair_runner=runner,
        )
        return result, status_path, status_path.parent / "lifecycle-events.jsonl"

    def test_repairs_route_and_blocks_the_first_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            write_route_config(config_path)
            calls: list[tuple[Path, str]] = []

            def fake_repair(target: Path, bridge_url: str) -> dict:
                calls.append((target, bridge_url))
                write_route_config(target, base_url=bridge_url)
                return {"ok": True, "status": "repaired"}

            result, status_path, events_path = self._run(
                root,
                config_path,
                runner=fake_repair,
            )

            self.assertFalse(result["continue"])
            self.assertIn("again", result["stopReason"])
            self.assertEqual(len(calls), 1)
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["result"], "repaired")
            self.assertEqual(status["routeAfter"], BRIDGE_URL)
            self.assertIn("conversationTitle", status)
            self.assertIn("conversationCwd", status)
            events = events_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(events), 1)

            # The next prompt of the same session is left alone.
            second, _, _ = self._run(root, config_path, runner=fake_repair)
            self.assertTrue(second["continue"])
            self.assertEqual(len(calls), 1)

    def test_bridged_route_and_disabled_mode_stay_quiet(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            write_route_config(config_path, base_url=BRIDGE_URL)

            def unreachable_runner(_target: Path, _bridge_url: str) -> dict:
                raise AssertionError("repair must not run for a bridged route")

            result, status_path, _ = self._run(root, config_path, runner=unreachable_runner)
            self.assertTrue(result["continue"])
            self.assertEqual(
                json.loads(status_path.read_text(encoding="utf-8"))["result"],
                "route-ok",
            )

            write_route_config(config_path)
            disabled, _, _ = self._run(
                root,
                config_path,
                policy={"routeRepairMode": "disabled"},
                runner=unreachable_runner,
            )
            self.assertTrue(disabled["continue"])

    def test_official_route_is_never_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            write_route_config(config_path, provider="openai", base_url="", model="gpt-5.5")

            def unreachable_runner(_target: Path, _bridge_url: str) -> dict:
                raise AssertionError("official routes must not be repaired")

            result, status_path, events_path = self._run(root, config_path, runner=unreachable_runner)
            self.assertTrue(result["continue"])
            self.assertEqual(
                json.loads(status_path.read_text(encoding="utf-8"))["result"],
                "route-not-applicable",
            )
            self.assertFalse(events_path.exists())

    def test_inspect_mode_reports_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            write_route_config(config_path)

            def unreachable_runner(_target: Path, _bridge_url: str) -> dict:
                raise AssertionError("inspect mode must not repair")

            result, status_path, _ = self._run(
                root,
                config_path,
                policy={"routeRepairMode": "inspect"},
                runner=unreachable_runner,
            )
            self.assertTrue(result["continue"])
            self.assertIn("systemMessage", result)
            self.assertEqual(
                json.loads(status_path.read_text(encoding="utf-8"))["result"],
                "repair-planned",
            )

    def test_failed_repair_blocks_with_the_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            write_route_config(config_path)

            def failing_runner(_target: Path, _bridge_url: str) -> dict:
                return {"ok": False, "status": "repair-script-failed", "error": "pwsh exploded"}

            result, status_path, _ = self._run(root, config_path, runner=failing_runner)
            self.assertFalse(result["continue"])
            self.assertIn("pwsh exploded", result["stopReason"])
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["result"], "repair-failed")
            self.assertEqual(status["error"], "pwsh exploded")


if __name__ == "__main__":
    unittest.main()
