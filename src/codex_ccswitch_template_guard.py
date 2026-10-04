#!/usr/bin/env python3
"""Persist bridge-compatible history aliases in CC Switch provider templates."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import time
import tomllib
from codex_bridge_environment import default_cc_switch_db
from codex_bridge_environment import default_bridge_url
from pathlib import Path


DEFAULT_BRIDGE_URL = default_bridge_url()
BEGIN_MARKER = "# BEGIN codex-cross-provider-bridge-alias"
END_MARKER = "# END codex-cross-provider-bridge-alias"


def repair_official_live_config(config: str) -> tuple[str, bool]:
    """Repair owned aliases after Codex rewrites TOML and drops marker comments.

    Preserve root settings and unrelated tables. Do not copy a credential-bearing
    provider block or route native OpenAI sessions through an inactive proxy.
    """
    parsed = tomllib.loads(config)
    if parsed.get("model_provider", "openai") not in {"", "openai"}:
        raise ValueError("live official alias repair requires the native OpenAI route")
    providers = parsed.get("model_providers", {})
    for alias in ("custom", "cc-switch-official"):
        block = providers.get(alias)
        if block and not (
            (block.get("name") == "OpenAI cross-provider history"
             and block.get("base_url") == DEFAULT_BRIDGE_URL)
            or (block.get("name") == "OpenAI" and not block.get("base_url"))
        ):
            raise ValueError(f"refusing to overwrite unmanaged model_providers.{alias}")
    updated = config
    for alias in ("custom", "cc-switch-official"):
        pattern = re.compile(rf"(?ms)^\[model_providers\.{re.escape(alias)}\][^\n]*\n.*?(?=^\[|\Z)")
        updated = pattern.sub("", updated)
    for marker in (BEGIN_MARKER, END_MARKER):
        updated = re.sub(rf"(?m)^{re.escape(marker)}\r?\n?", "", updated)
    updated = updated.rstrip() + "\n\n" + _render_alias("custom", DEFAULT_BRIDGE_URL, official=True)
    result = tomllib.loads(updated)
    expected = dict(parsed)
    expected_providers = dict(providers)
    expected_providers.update(result["model_providers"])
    expected["model_providers"] = expected_providers
    if result != expected:
        raise ValueError("alias repair would modify unrelated TOML settings")
    return updated, updated != config


def _alias_for_category(category: str) -> str:
    if category == "official":
        return "custom"
    if category in {"third_party", "cn_official"}:
        return "cc-switch-official"
    raise ValueError(f"unsupported Codex provider category: {category}")


def _render_alias(alias: str, bridge_url: str, official: bool = False) -> str:
    if not re.fullmatch(r"http://127\.0\.0\.1:\d+/v1", bridge_url):
        raise ValueError("bridge URL must be a loopback http:// URL ending in /v1")
    if official:
        # Native OpenAI endpoint selection follows the saved login type.
        # A loopback alias breaks official history while Codex takeover is off.
        # Codex also uses the exact provider name to select backend capabilities.
        blocks = "".join(
            f"[model_providers.{provider}]\n"
            'name = "OpenAI"\n'
            'wire_api = "responses"\n'
            "requires_openai_auth = true\n"
            "supports_websockets = true\n"
            "supports_standalone_web_search = true\n"
            for provider in ("custom", "cc-switch-official")
        )
        return f"{BEGIN_MARKER}\n{blocks}{END_MARKER}\n"
    blocks = "".join(
        f"[model_providers.{provider}]\n"
        'name = "OpenAI cross-provider history"\n'
        f'base_url = "{bridge_url}"\n'
        'wire_api = "responses"\n'
        "requires_openai_auth = true\n"
        "supports_websockets = false\n"
        for provider in (alias,)
    )
    return f"{BEGIN_MARKER}\n{blocks}{END_MARKER}\n"


def update_provider_settings(
    raw_settings: str,
    category: str,
    bridge_url: str = DEFAULT_BRIDGE_URL,
) -> tuple[str, bool, str]:
    """Return settings JSON with one managed idle history-provider alias."""
    payload = json.loads(raw_settings or "{}")
    if not isinstance(payload, dict):
        raise ValueError("settings_config must contain a JSON object")
    config = payload.get("config") or ""
    if not isinstance(config, str):
        raise ValueError("settings_config.config must be a string")

    alias = _alias_for_category(category)
    replacement = _render_alias(alias, bridge_url, official=category == "official")
    managed_pattern = re.compile(
        rf"(?ms)^{re.escape(BEGIN_MARKER)}\r?\n.*?^{re.escape(END_MARKER)}\r?\n?"
    )
    managed = managed_pattern.search(config)
    unmanaged_config = managed_pattern.sub("", config)
    required_aliases = ("custom", "cc-switch-official") if category == "official" else (alias,)
    for required in required_aliases:
        if re.search(rf"(?m)^\s*\[model_providers\.{re.escape(required)}\]\s*$", unmanaged_config):
            raise ValueError(f"refusing to overwrite unmanaged model_providers.{required}")
    if managed:
        updated_config = config[: managed.start()] + replacement + config[managed.end() :]
    else:
        alias_pattern = re.compile(
            rf"(?m)^\s*\[model_providers\.{re.escape(alias)}\]\s*$"
        )
        if alias_pattern.search(config):
            raise ValueError(
                f"refusing to overwrite unmanaged model_providers.{alias}"
            )
        prefix = config.rstrip()
        updated_config = (prefix + "\n\n" if prefix else "") + replacement

    if category == "official":
        # CC Switch classifies any non-openai active provider / explicit base URL
        # as third-party and then drops incoming ChatGPT authorization. Keep its
        # upstream template native; only project the live client config to Bridge.
        active = tomllib.loads(updated_config).get("model_provider", "openai")
        if active not in {"", "openai", "cc-switch-official"}:
            raise ValueError("unmanaged active provider in official template")
        updated_config = re.sub(r'(?m)^model_provider\s*=\s*"[^"\n]*"[^\n]*\n?', '', updated_config)
        updated_config = 'model_provider = "openai"\n' + updated_config
    if updated_config == config:
        return raw_settings, False, alias
    payload["config"] = updated_config
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")), True, alias


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _backup_database(source: sqlite3.Connection, database_path: Path) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime())
    backup_dir = (
        database_path.parent
        / "backups"
        / "codex-cross-provider-template-guard"
        / f"{stamp}-{time.time_ns() % 1_000_000_000:09d}"
    )
    backup_dir.mkdir(parents=True, exist_ok=False)
    backup_path = backup_dir / database_path.name
    destination = sqlite3.connect(backup_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
    return backup_dir


def apply_template_guard(
    database_path: Path,
    bridge_url: str = DEFAULT_BRIDGE_URL,
    apply: bool = False,
) -> dict:
    connection = sqlite3.connect(database_path, timeout=15)
    try:
        rows = connection.execute(
            "select id, category, settings_config from providers "
            "where app_type = 'codex' order by id"
        ).fetchall()
        updates: list[tuple[str, str, str]] = []
        conflicts: list[dict[str, str]] = []
        for provider_id, category, raw_settings in rows:
            try:
                updated, changed, alias = update_provider_settings(
                    raw_settings or "{}",
                    category=str(category or ""),
                    bridge_url=bridge_url,
                )
            except ValueError as exc:
                conflicts.append({"providerId": provider_id, "error": str(exc)})
                continue
            if changed:
                updates.append((updated, provider_id, alias))

        if conflicts:
            return {
                "status": "conflict",
                "dryRun": not apply,
                "updatedProviderIds": [],
                "conflicts": conflicts,
                "backupDirectory": "",
            }
        if not updates:
            return {
                "status": "already-current",
                "dryRun": not apply,
                "updatedProviderIds": [],
                "conflicts": [],
                "backupDirectory": "",
            }
        if not apply:
            return {
                "status": "update-planned",
                "dryRun": True,
                "updatedProviderIds": [item[1] for item in updates],
                "aliases": {item[1]: item[2] for item in updates},
                "conflicts": [],
                "backupDirectory": "",
            }

        backup_dir = _backup_database(connection, database_path)
        try:
            connection.execute("begin immediate")
            connection.executemany(
                "update providers set settings_config = ? where id = ?",
                [(settings, provider_id) for settings, provider_id, _ in updates],
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

        manifest = {
            "schemaVersion": 1,
            "createdAt": time.time(),
            "databasePath": str(database_path),
            "backupSha256": _sha256(backup_dir / database_path.name),
            "updatedProviderIds": [item[1] for item in updates],
            "aliases": {item[1]: item[2] for item in updates},
        }
        (backup_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return {
            "status": "updated",
            "dryRun": False,
            "updatedProviderIds": [item[1] for item in updates],
            "aliases": {item[1]: item[2] for item in updates},
            "conflicts": [],
            "backupDirectory": str(backup_dir),
        }
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--database",
        default=str(default_cc_switch_db()),
    )
    parser.add_argument("--bridge-url", default=DEFAULT_BRIDGE_URL)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = apply_template_guard(
        database_path=Path(args.database),
        bridge_url=args.bridge_url,
        apply=args.apply,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["status"] == "conflict" else 0


if __name__ == "__main__":
    raise SystemExit(main())
