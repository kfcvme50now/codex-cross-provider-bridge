#!/usr/bin/env python3
"""Inspect whether the live Codex route is safe for bridge automation."""

from __future__ import annotations

import argparse
import json
import tomllib
from codex_bridge_environment import default_bridge_url, default_cc_switch_url, official_bridge_url, responses_url
from codex_bridge_environment import default_config_path
from pathlib import Path

LOOPBACK_CC_SWITCH_URL = responses_url(default_cc_switch_url())


def inspect_config_route(config_path: Path, bridge_url=None, cc_switch_url=None) -> dict:
    with config_path.open("rb") as handle:
        config = tomllib.load(handle)

    active_provider = str(config.get("model_provider") or "")
    model = str(config.get("model") or "")
    providers = config.get("model_providers") or {}
    if not isinstance(providers, dict):
        providers = {}

    active_block = providers.get(active_provider or "openai") or {}
    if not isinstance(active_block, dict):
        active_block = {}
    base_url = str(active_block.get("base_url") or "").rstrip("/")
    is_local_route = base_url == responses_url(cc_switch_url or default_cc_switch_url())
    # A GPT model name does not identify the transport or credential owner.
    # Third-party Responses routes often use the same names as OpenAI.
    is_official = active_provider in {"", "openai"} or (
        active_block.get("name") == "OpenAI" and base_url in {"", official_bridge_url(bridge_url or default_bridge_url())}
    )

    if not active_provider:
        reason = "active-provider-missing"
    elif active_provider == "openai" or is_official:
        reason = "official-model-or-provider"
    elif not is_local_route:
        reason = "active-route-is-not-cc-switch-loopback"
    else:
        reason = "eligible"

    return {
        "configPath": str(config_path),
        "activeProvider": active_provider,
        "model": model,
        "baseUrl": base_url,
        "providerIds": sorted(providers),
        "usesCcSwitchLoopback": is_local_route,
        "officialModelOrProvider": is_official,
        "eligibleForAutomaticBridgeRepair": reason == "eligible",
        "reason": reason,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default=str(default_config_path()),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print(
        json.dumps(
            inspect_config_route(Path(args.config)),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
