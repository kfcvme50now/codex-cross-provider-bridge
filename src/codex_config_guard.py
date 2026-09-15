#!/usr/bin/env python3
"""Inspect whether the live Codex route is safe for bridge automation."""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path

from codex_history_audit import is_official_model


LOOPBACK_CC_SWITCH_URL = "http://127.0.0.1:15721/v1"


def inspect_config_route(config_path: Path) -> dict:
    with config_path.open("rb") as handle:
        config = tomllib.load(handle)

    active_provider = str(config.get("model_provider") or "")
    model = str(config.get("model") or "")
    providers = config.get("model_providers") or {}
    if not isinstance(providers, dict):
        providers = {}

    active_block = providers.get(active_provider) or {}
    if not isinstance(active_block, dict):
        active_block = {}
    base_url = str(active_block.get("base_url") or "").rstrip("/")
    is_local_route = base_url == LOOPBACK_CC_SWITCH_URL
    is_official = active_provider == "openai" or is_official_model(model)

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
        default=str(Path.home() / ".codex" / "config.toml"),
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
