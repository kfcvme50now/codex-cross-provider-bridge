from __future__ import annotations

import json
import sys
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_ccswitch_template_guard import update_provider_settings, repair_official_live_config


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
        config = tomllib.loads(payload["config"])
        for provider_id in ("custom", "cc-switch-official"):
            provider = config["model_providers"][provider_id]
            self.assertEqual(provider["name"], "OpenAI")
            self.assertNotIn("base_url", provider)
            self.assertTrue(provider["supports_websockets"])
            self.assertTrue(provider["requires_openai_auth"])
        self.assertIn('model = "gpt-5.5"', payload["config"])

    def test_official_upstream_template_undoes_client_bridge_projection(self):
        from codex_official_bridge_config import project_official_bridge
        projected=project_official_bridge('model="gpt-6-luna"\n')[0]
        updated,changed,_=update_provider_settings(json.dumps({'auth':{},'config':projected}),category='official')
        config=tomllib.loads(json.loads(updated)['config'])
        self.assertTrue(changed)
        self.assertEqual(config['model_provider'],'openai')
        self.assertTrue(all('base_url' not in v for v in config['model_providers'].values()))

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

    def test_upgrade_old_official_guard_preserves_both_history_buckets(self) -> None:
        raw = json.dumps({"config": 'model = "gpt-6.1-sol"\n'
            '# BEGIN codex-cross-provider-bridge-alias\n'
            '[model_providers.custom]\nname = "OpenAI cross-provider history"\n'
            f'base_url = "{BRIDGE_URL}"\nwire_api = "responses"\n'
            '# END codex-cross-provider-bridge-alias\n'})
        updated, changed, _ = update_provider_settings(raw, category="official")
        self.assertTrue(changed)
        config = tomllib.loads(json.loads(updated)["config"])
        self.assertEqual(config["model"], "gpt-6.1-sol")
        self.assertEqual(set(config["model_providers"]), {"custom", "cc-switch-official"})
        self.assertEqual(config['model_provider'], 'openai')
        self.assertTrue(all('base_url' not in p for p in config["model_providers"].values()))

    def test_second_unmanaged_alias_conflict_is_reported_before_update(self) -> None:
        raw = json.dumps({"config": '[model_providers.cc-switch-official]\nname = "User owned"\n'})
        with self.assertRaisesRegex(ValueError, "unmanaged.*cc-switch-official"):
            update_provider_settings(raw, category="official")

    def test_live_repair_survives_codex_comment_rewrite(self) -> None:
        config = ('model="gpt-6.1-sol"\n# BEGIN codex-cross-provider-bridge-alias\n'
           'service_tier="default"\nnotify=["local-handler"]\n'
           '[model_providers.custom]\nname="OpenAI cross-provider history"\n'
           f'base_url="{BRIDGE_URL}"\nwire_api="responses"\n'
           '[features]\njs_repl=false\n')
        updated, changed = repair_official_live_config(config)
        self.assertTrue(changed)
        parsed = tomllib.loads(updated)
        self.assertEqual(parsed['notify'], ['local-handler'])
        self.assertEqual(parsed['service_tier'], 'default')
        self.assertFalse(parsed['features']['js_repl'])
        self.assertEqual(set(parsed['model_providers']), {'custom','cc-switch-official'})
        self.assertFalse(repair_official_live_config(updated)[1])


if __name__ == "__main__":
    unittest.main()
