from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_hook_manager import (
    install_lifecycle_hooks,
    restore_hook_backup,
    uninstall_lifecycle_hooks,
    wrapper_script_path,
)


class HookManagerTests(unittest.TestCase):
    def test_install_preserves_existing_hooks_and_uninstall_removes_only_managed_hooks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / ".codex"
            codex_home.mkdir()
            hooks_path = codex_home / "hooks.json"
            original = {
                "description": "User hooks",
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "python user_policy.py",
                                }
                            ],
                        }
                    ]
                },
            }
            hooks_path.write_text(
                json.dumps(original, indent=2) + "\n",
                encoding="utf-8",
            )
            lifecycle_script = root / "src" / "codex_lifecycle_hook.py"
            lifecycle_script.parent.mkdir()
            lifecycle_script.write_text("# managed hook fixture\n", encoding="utf-8")

            installed = install_lifecycle_hooks(
                codex_home=codex_home,
                policy_path=root / "state" / "lifecycle-policy.json",
                config_path=root / ".codex" / "config.toml",
                status_path=root / "state" / "lifecycle-status.json",
                lifecycle_script=lifecycle_script,
                python_executable=Path(sys.executable),
                apply=True,
            )

            self.assertEqual(installed["status"], "installed")
            hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
            self.assertEqual(
                hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"],
                "python user_policy.py",
            )
            self.assertEqual(len(hooks["hooks"]["PreCompact"]), 1)
            self.assertEqual(len(hooks["hooks"]["SessionStart"]), 1)
            self.assertEqual(len(hooks["hooks"]["UserPromptSubmit"]), 1)
            wrapper_path = wrapper_script_path(codex_home)
            prompt_command = hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
            self.assertEqual(prompt_command, f'"{wrapper_path}"')
            wrapper_text = wrapper_path.read_text(encoding="utf-8")
            self.assertIn("--route-repair-script", wrapper_text)
            self.assertIn("http://127.0.0.1:15722/v1", wrapper_text)

            second = install_lifecycle_hooks(
                codex_home=codex_home,
                policy_path=root / "state" / "lifecycle-policy.json",
                config_path=root / ".codex" / "config.toml",
                status_path=root / "state" / "lifecycle-status.json",
                lifecycle_script=lifecycle_script,
                python_executable=Path(sys.executable),
                apply=True,
            )
            self.assertEqual(second["status"], "already-installed")
            hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
            self.assertEqual(len(hooks["hooks"]["PreCompact"]), 1)
            hooks["hooks"]["PreCompact"][0]["hooks"][0]["command"] = (
                "python old_codex_lifecycle_hook.py"
            )
            hooks_path.write_text(
                json.dumps(hooks, indent=2) + "\n",
                encoding="utf-8",
            )

            updated = install_lifecycle_hooks(
                codex_home=codex_home,
                policy_path=root / "state" / "lifecycle-policy.json",
                config_path=root / ".codex" / "config.toml",
                status_path=root / "state" / "lifecycle-status.json",
                lifecycle_script=lifecycle_script,
                python_executable=Path(sys.executable),
                apply=True,
            )
            self.assertEqual(updated["status"], "updated")
            hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
            self.assertNotIn(
                "old_codex_lifecycle_hook.py",
                hooks["hooks"]["PreCompact"][0]["hooks"][0]["command"],
            )
            hooks["hooks"]["PreCompact"][0]["hooks"].append(
                {
                    "type": "command",
                    "command": "python user_compact_logger.py",
                }
            )
            hooks_path.write_text(
                json.dumps(hooks, indent=2) + "\n",
                encoding="utf-8",
            )

            uninstall_lifecycle_hooks(codex_home=codex_home, apply=True)
            hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
            self.assertFalse(wrapper_path.exists())
            self.assertIn("PreToolUse", hooks["hooks"])
            self.assertEqual(
                hooks["hooks"]["PreCompact"][0]["hooks"][0]["command"],
                "python user_compact_logger.py",
            )
            self.assertNotIn("SessionStart", hooks["hooks"])

            restore_hook_backup(
                backup_directory=Path(installed["backupDirectory"]),
                apply=True,
            )
            restored = json.loads(hooks_path.read_text(encoding="utf-8"))
            self.assertEqual(restored, original)


if __name__ == "__main__":
    unittest.main()
