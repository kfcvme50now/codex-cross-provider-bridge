"""Project native ChatGPT-account Codex requests onto the dedicated bridge route."""
import re
import tomllib
from codex_bridge_environment import default_bridge_url, default_cc_switch_url, official_bridge_url, responses_url

OFFICIAL_BRIDGE_URL = official_bridge_url(default_bridge_url())
ALIASES = ("custom", "cc-switch-official")


def project_official_bridge(config, bridge_url=OFFICIAL_BRIDGE_URL, cc_switch_url=None):
    bridge_url = official_bridge_url(bridge_url)
    cc_switch_url = responses_url(cc_switch_url or default_cc_switch_url())
    parsed = tomllib.loads(config)
    if parsed.get("model_provider", "openai") not in {"", "openai", "cc-switch-official"}:
        raise ValueError("Official bridge projection requires the active OpenAI provider")
    blocks = parsed.get("model_providers", {})
    desired = {"name": "OpenAI", "base_url": bridge_url, "wire_api": "responses",
               "requires_openai_auth": True, "supports_websockets": False,
               "supports_standalone_web_search": True}
    if parsed.get("model_provider") == "cc-switch-official" and all(blocks.get(k) == desired for k in ALIASES):
        return config, False
    updated = config
    root_end = re.search(r"(?m)^\[", updated)
    root = updated[:root_end.start()] if root_end else updated
    rest = updated[root_end.start():] if root_end else ""
    root = re.sub(r'(?m)^model_provider\s*=\s*"[^"\n]*"[^\n]*\n?', '', root)
    updated = 'model_provider = "cc-switch-official"\n' + root + rest
    for alias in ALIASES:
        existing = blocks.get(alias, {})
        if existing and not (
            existing.get("name") == "OpenAI" and existing.get("base_url", "").rstrip("/") in {
                "", bridge_url.rstrip("/"), cc_switch_url.rstrip("/")
            }
        ):
            raise ValueError(f"Unmanaged provider block: {alias}")
        pattern = rf"(?ms)^\[model_providers\.{re.escape(alias)}\][^\n]*\n.*?(?=^\[|\Z)"
        updated = re.sub(pattern, "", updated)
    for marker in ("# BEGIN codex-cross-provider-bridge-alias", "# END codex-cross-provider-bridge-alias"):
        updated = re.sub(rf"(?m)^{re.escape(marker)}\r?\n?", "", updated)
    updated = updated.rstrip() + "\n\n# BEGIN codex-cross-provider-bridge-alias\n"
    for alias in ALIASES:
        updated += f"[model_providers.{alias}]\n"
        for key, value in desired.items():
            updated += f'{key} = ' + (str(value).lower() if isinstance(value, bool) else f'"{value}"') + "\n"
    updated += "# END codex-cross-provider-bridge-alias\n"
    expected = dict(parsed)
    expected["model_provider"] = "cc-switch-official"
    expected["model_providers"] = {**blocks, **{key: desired for key in ALIASES}}
    if tomllib.loads(updated) != expected:
        raise ValueError("Projection would change unrelated configuration")
    return updated, True
