from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_provider_probe import (
    maybe_probe_after_switch,
    run_configured_probe,
    route_fingerprint,
)


class ProviderProbeTests(unittest.TestCase):
    def test_cli_probe_reports_success_without_persisting_response_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / "fake_codex.py"
            fake.write_text(
                "import json\n"
                "print(json.dumps({'type': 'thread.started', 'thread_id': 'probe-thread'}))\n"
                "print(json.dumps({'type': 'turn.completed'}))\n",
                encoding="utf-8",
            )
            config = root / "config.toml"
            config.write_text(
                'model_provider = "custom"\n'
                'model = "deepseek-flash"\n\n'
                '[model_providers.custom]\n'
                'name = "Third Party"\n'
                'base_url = "http://127.0.0.1:15721/v1"\n'
                'wire_api = "responses"\n',
                encoding="utf-8",
            )

            result = run_configured_probe(
                config_path=config,
                codex_home=root / ".codex",
                mode="cli",
                timeout_seconds=10,
                command=[sys.executable, str(fake)],
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["provider"], "custom")
            self.assertEqual(result["model"], "deepseek-flash")
            self.assertEqual(result["probeId"], "probe-thread")
            self.assertNotIn("text", result)

    def test_cli_probe_classifies_unsupported_model_without_raw_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / "fake_codex.py"
            fake.write_text(
                "import json, sys\n"
                "print(json.dumps({'type': 'turn.failed', 'error': {"
                "'message': \"The 'deepseek-flash' model is not supported\"}}))\n"
                "sys.exit(1)\n",
                encoding="utf-8",
            )
            config = root / "config.toml"
            config.write_text(
                'model_provider = "openai"\n'
                'model = "deepseek-flash"\n\n'
                '[model_providers.openai]\n'
                'name = "OpenAI"\n',
                encoding="utf-8",
            )

            result = run_configured_probe(
                config_path=config,
                codex_home=root / ".codex",
                mode="cli",
                timeout_seconds=10,
                command=[sys.executable, str(fake)],
            )

            self.assertFalse(result["ok"])
            self.assertEqual(result["errorCategory"], "unsupported-model")
            self.assertNotIn("not supported", json.dumps(result))
            self.assertNotIn("errorMessage", result)

    def test_switch_detection_probes_once_per_route_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text(
                'model_provider = "custom"\n'
                'model = "deepseek-flash"\n\n'
                '[model_providers.custom]\n'
                'name = "Third Party"\n'
                'base_url = "http://127.0.0.1:15721/v1"\n',
                encoding="utf-8",
            )
            state_path = root / "probe-state.json"
            status_path = root / "probe-status.json"
            calls: list[dict] = []

            def fake_probe(**kwargs: object) -> dict:
                calls.append(kwargs)
                return {
                    "ok": True,
                    "provider": "custom",
                    "model": "deepseek-flash",
                    "probeId": "probe-once",
                }

            first = maybe_probe_after_switch(
                config_path=config,
                codex_home=root / ".codex",
                mode="cli",
                scope="next",
                timeout_seconds=10,
                state_path=state_path,
                status_path=status_path,
                probe_runner=fake_probe,
                apply=True,
            )
            second = maybe_probe_after_switch(
                config_path=config,
                codex_home=root / ".codex",
                mode="cli",
                scope="next",
                timeout_seconds=10,
                state_path=state_path,
                status_path=status_path,
                probe_runner=fake_probe,
                apply=True,
            )

            self.assertEqual(first["status"], "probed")
            self.assertEqual(second["status"], "unchanged")
            self.assertEqual(len(calls), 1)
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["fingerprint"], route_fingerprint(config))
            self.assertEqual(saved["lastScope"], "next")


if __name__ == "__main__":
    unittest.main()
