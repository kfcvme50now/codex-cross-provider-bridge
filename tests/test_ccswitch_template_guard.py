from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_ccswitch_template_guard import update_provider_settings


BRIDGE_URL = "http://127.0.0.1:15722/v1"


class CcSwitchTemplateGuardTests(unittest.TestCase):
    def test_official_template_gets_custom_history_alias(self) -> None:
        raw = json.dumps({"auth": {}, "config": 'model = "gpt-5.5"\n'})

        updated, changed, alias = update_provider_settings(
            raw,
            category="official",
            bridge_url=BRIDGE_URL,
        )

        payload = json.loads(updated)
        self.assertTrue(changed)
        self.assertEqual(alias, "custom")
        self.assertIn("[model_providers.custom]", payload["config"])
        self.assertIn(f'base_url = "{BRIDGE_URL}"', payload["config"])
        self.assertIn('model = "gpt-5.5"', payload["config"])

    def test_third_party_template_gets_official_history_alias(self) -> None:
        raw = json.dumps(
            {
                "auth": {"OPENAI_API_KEY": "secret-is-preserved-not-returned"},
                "config": (
                    'model_provider = "custom"\n'
                    '[model_providers.custom]\n'
                    'base_url = "https://provider.example/v1"\n'
                    'wire_api = "responses"\n'
                ),
            }
        )

        updated, changed, alias = update_provider_settings(
            raw,
            category="third_party",
            bridge_url=BRIDGE_URL,
        )

        payload = json.loads(updated)
        self.assertTrue(changed)
        self.assertEqual(alias, "cc-switch-official")
        self.assertIn("[model_providers.custom]", payload["config"])
        self.assertIn(
            "[model_providers.cc-switch-official]",
            payload["config"],
        )
        self.assertEqual(
            payload["auth"]["OPENAI_API_KEY"],
            "secret-is-preserved-not-returned",
        )

    def test_update_is_idempotent(self) -> None:
        raw = json.dumps({"auth": {}, "config": ""})
        first, changed, _ = update_provider_settings(
            raw,
            category="official",
            bridge_url=BRIDGE_URL,
        )
        second, changed_again, _ = update_provider_settings(
            first,
            category="official",
            bridge_url=BRIDGE_URL,
        )

        self.assertTrue(changed)
        self.assertFalse(changed_again)
        self.assertEqual(first, second)

    def test_existing_unmanaged_alias_is_not_overwritten(self) -> None:
        raw = json.dumps(
            {
                "auth": {},
                "config": (
                    "[model_providers.custom]\n"
                    'base_url = "https://do-not-overwrite.example/v1"\n'
                ),
            }
        )

        with self.assertRaisesRegex(ValueError, "unmanaged"):
            update_provider_settings(
                raw,
                category="official",
                bridge_url=BRIDGE_URL,
            )


if __name__ == "__main__":
    unittest.main()
