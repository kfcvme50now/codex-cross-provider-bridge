"""Maintain known CC Switch routes inside the existing bridge process.

Only live client configuration is wrapped. Upstream provider templates retain
their real URL, so CC Switch protocol conversion and request overrides still run.
"""
import copy
import json
import logging
from pathlib import Path
import re
import socket
import sqlite3
import threading
import time
import tomllib

from codex_official_bridge_config import project_official_bridge
from codex_bridge_environment import default_bridge_url, default_cc_switch_url, endpoint, official_bridge_url, responses_url

BRIDGE_URL = default_bridge_url()
CC_URL = responses_url(default_cc_switch_url())


def project_known_route(config, database, bridge_url=BRIDGE_URL, cc_switch_url=CC_URL):
    official_url = official_bridge_url(bridge_url)
    cc_switch_url = responses_url(cc_switch_url)
    parsed = tomllib.loads(config)
    active = parsed.get("model_provider") or "openai"
    blocks = parsed.get("model_providers", {})
    block = blocks.get(active, {})
    base = block.get("base_url", "").rstrip("/")
    if active in {"openai", "cc-switch-official"} and block.get("name", "OpenAI") == "OpenAI":
        return project_official_bridge(config, official_url, cc_switch_url)[0], "official"
    if not database.exists():
        return config, "unmanaged"
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        row = connection.execute("select category,settings_config from providers where app_type='codex' and is_current=1 limit 1").fetchone()
    finally:
        connection.close()
    if not row or row[0] not in {"third_party", "cn_official"}:
        return config, "unmanaged"
    template = tomllib.loads(json.loads(row[1]).get("config") or "")
    source = template.get("model_providers", {}).get(template.get("model_provider"), {})
    if not base or base not in {source.get("base_url", "").rstrip("/"), cc_switch_url, bridge_url}:
        return config, "unmanaged"
    expected = copy.deepcopy(parsed)
    updated = config
    replacements = {active: {**block, "base_url": bridge_url, "supports_websockets": False}}
    for alias in ("cc-switch-official",):
        if alias == active:
            continue
        old = blocks.get(alias, {})
        if old and old.get("name") not in {"OpenAI", "OpenAI cross-provider history", source.get("name")}:
            raise ValueError("Unmanaged history alias must not be overwritten")
        replacements[alias] = {"name": "OpenAI cross-provider history", "base_url": bridge_url,
            "wire_api": "responses", "requires_openai_auth": True, "supports_websockets": False}
    for alias, values in replacements.items():
        if blocks.get(alias) == values:
            continue
        pattern = re.compile(rf"(?ms)^\[model_providers\.{re.escape(alias)}\][^\n]*\n.*?(?=^\[|\Z)")
        # Preserve arbitrary custom provider fields by editing its scalar route fields only.
        match = pattern.search(updated)
        if alias == active and match:
            original = match.group()
            replacement = re.sub(r'(?m)^base_url\s*=\s*"[^"\n]*"[^\n]*', f'base_url = "{bridge_url}"', original)
            replacement = re.sub(r'(?m)^supports_websockets\s*=\s*(?:true|false)[^\n]*\n?', '', replacement)
            replacement = replacement.rstrip() + '\nsupports_websockets = false\n\n'
        else:
            replacement = f"[model_providers.{alias}]\n" + ''.join(k + ' = ' + (str(v).lower() if isinstance(v, bool) else json.dumps(v)) + '\n' for k, v in values.items()) + '\n'
        if match:
            updated = updated[:match.start()] + replacement + updated[match.end():]
        else:
            updated = updated.rstrip() + '\n\n' + replacement
        expected.setdefault("model_providers", {})[alias] = values
    if tomllib.loads(updated) != expected:
        raise ValueError("Route projection would change unrelated configuration")
    return updated, "cc-switch"


def write_projection(config_path, before, after, backup_root):
    if before == after:
        return False
    archive = backup_root / str(time.time_ns())
    archive.mkdir(parents=True, exist_ok=False)
    (archive / 'config.toml').write_text(before, encoding='utf-8')
    temporary = config_path.with_suffix('.route-project.tmp')
    temporary.write_text(after, encoding='utf-8')
    if config_path.read_text(encoding='utf-8') != before:
        temporary.unlink()
        return False
    temporary.replace(config_path)
    return True


def start_route_maintenance(server):
    """One background thread in the bridge, with no additional service/process."""
    stopped = threading.Event()
    address = getattr(server, 'server_address', None)
    bridge_url = f'http://127.0.0.1:{address[1]}/v1' if address else BRIDGE_URL
    upstream = getattr(server, 'upstream', None)
    cc_switch_url = responses_url(upstream.geturl()) if upstream else CC_URL
    def run():
        previous = None
        last_error = None
        while not stopped.wait(2):
            try:
                policy_path = server.policy_file.parent / 'lifecycle-policy.json'
                if not policy_path.exists() or not json.loads(policy_path.read_text()).get('portableHistoryViaBridge'):
                    continue
                config_path = server.codex_home / 'config.toml'
                before = config_path.read_text(encoding='utf-8')
                # Wait for a stable snapshot across two polls after CC Switch writes.
                if before != previous:
                    previous = before
                    continue
                after, route = project_known_route(before, server.cc_switch_db, bridge_url, cc_switch_url)
                if route == 'cc-switch':
                    with socket.create_connection(endpoint(cc_switch_url), timeout=1):
                        pass
                if write_projection(config_path, before, after, server.codex_home / 'backups/automatic-route-projection'):
                    logging.getLogger('codex.bridge').info('automatic route projection applied: %s', route)
                    previous = after
                last_error = None
            except Exception as exc:
                marker = type(exc).__name__
                if marker != last_error:
                    logging.getLogger('codex.bridge').warning('automatic route projection deferred: %s', marker)
                last_error = marker
    thread = threading.Thread(target=run, name='codex-route-maintenance', daemon=True)
    thread.start()
    return stopped
