#!/usr/bin/env python3
"""Install, remove, and restore the lifecycle hooks managed by this project."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path


MARKER = "codex_lifecycle_hook.py"
WRAPPER_MARKER = "codex-lifecycle-hook"
BACKUP_FOLDER = "codex-cross-provider-hooks"
DEFAULT_BRIDGE_URL = "http://127.0.0.1:15722/v1"
MANAGED_EVENTS = ("PreCompact", "SessionStart", "UserPromptSubmit")


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temporary.replace(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_hooks(path: Path) -> dict:
    if not path.exists():
        return {"hooks": {}}
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("hooks.json must contain a JSON object")
    hooks = payload.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks.json hooks field must be an object")
    return payload


def _create_backup(
    codex_home: Path,
    hooks_path: Path,
    reason: str,
) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    backup_directory = (
        codex_home
        / "backups"
        / BACKUP_FOLDER
        / f"{stamp}-{time.time_ns() % 1_000_000_000:09d}"
    )
    backup_directory.mkdir(parents=True, exist_ok=False)
    exists = hooks_path.exists()
    backup_file = backup_directory / "hooks.json"
    digest = ""
    if exists:
        shutil.copy2(hooks_path, backup_file)
        digest = _sha256(backup_file)
    _write_json_atomic(
        backup_directory / "manifest.json",
        {
            "schemaVersion": 1,
            "reason": reason,
            "createdAt": time.time(),
            "hooksPath": str(hooks_path),
            "originalExists": exists,
            "sha256": digest,
        },
    )
    return backup_directory


def _quote_command_part(value: Path) -> str:
    return '"' + str(value).replace('"', '\\"') + '"'


def _managed_command(
    python_executable: Path,
    lifecycle_script: Path,
    policy_path: Path,
    codex_home: Path,
    config_path: Path,
    status_path: Path,
    bridge_url: str = DEFAULT_BRIDGE_URL,
) -> str:
    return " ".join(
        [
            _quote_command_part(python_executable),
            _quote_command_part(lifecycle_script),
            "--policy",
            _quote_command_part(policy_path),
            "--codex-home",
            _quote_command_part(codex_home),
            "--config",
            _quote_command_part(config_path),
            "--status-file",
            _quote_command_part(status_path),
            "--bridge-url",
            bridge_url,
            "--route-repair-script",
            _quote_command_part(route_repair_script_for(lifecycle_script)),
            "--apply",
        ]
    )


def route_repair_script_for(lifecycle_script: Path) -> Path:
    return (
        lifecycle_script.resolve().parents[1]
        / "scripts"
        / "Repair-Codex-CCSwitchProviderAlias.ps1"
    )


def wrapper_script_path(codex_home: Path) -> Path:
    suffix = ".cmd" if os.name == "nt" else ".sh"
    return codex_home / f"codex-lifecycle-hook{suffix}"


def write_wrapper_script(path: Path, command: str) -> None:
    """Point Codex at a script instead of an inline command line.

    A single script path avoids shell quoting of the multi-argument Python
    command. The hook definition leaves that path unquoted because Codex's
    Windows hook launcher treats a leading quote as part of the executable
    name. The managed wrapper path itself never contains spaces.
    """
    if path.suffix == ".cmd":
        content = "@echo off\r\n" + command + "\r\n"
    else:
        content = "#!/bin/sh\n" + command + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(content)


def _managed_groups(command: str) -> dict[str, list[dict]]:
    common = {
        "type": "command",
        "command": command,
        "commandWindows": command,
        "timeout": 180,
    }
    return {
        "PreCompact": [
            {
                "matcher": "manual|auto",
                "hooks": [
                    {
                        **common,
                        "statusMessage": "Checking provider state before compaction",
                    }
                ],
            }
        ],
        "SessionStart": [
            {
                "matcher": "startup|resume|clear|compact",
                "hooks": [
                    {
                        **common,
                        "statusMessage": "Checking provider state for this session",
                    }
                ],
            }
        ],
        "UserPromptSubmit": [
            {
                "hooks": [
                    {
                        **common,
                        "statusMessage": "Checking the Codex route",
                    }
                ],
            }
        ],
    }


def _handler_command(handler: object) -> str:
    if not isinstance(handler, dict):
        return ""
    return str(handler.get("command") or handler.get("commandWindows") or "")


def _is_managed_handler(handler: object) -> bool:
    command = _handler_command(handler)
    return MARKER in command or WRAPPER_MARKER in command


def _contains_managed_hook(group: object) -> bool:
    if not isinstance(group, dict):
        return False
    handlers = group.get("hooks")
    if not isinstance(handlers, list):
        return False
    return any(_is_managed_handler(handler) for handler in handlers)


def _is_installed(payload: dict) -> bool:
    hooks = payload.get("hooks") or {}
    return any(
        _contains_managed_hook(group)
        for groups in hooks.values()
        if isinstance(groups, list)
        for group in groups
    )


def _is_current_install(payload: dict, command: str) -> bool:
    hooks = payload.get("hooks") or {}
    managed_events = set()
    managed_group_count = 0
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not _contains_managed_hook(group):
                continue
            managed_group_count += 1
            managed_events.add(event)
            handlers = group.get("hooks") or []
            managed_handlers = [
                handler for handler in handlers if _is_managed_handler(handler)
            ]
            if len(managed_handlers) != 1:
                return False
            handler = managed_handlers[0]
            if (
                handler.get("command") != command
                or handler.get("commandWindows") != command
            ):
                return False
    return (
        managed_events == set(MANAGED_EVENTS)
        and managed_group_count == len(MANAGED_EVENTS)
    )


def _remove_managed_hooks(payload: dict) -> None:
    hooks = payload.get("hooks") or {}
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        remaining_groups = []
        for group in groups:
            if not _contains_managed_hook(group):
                remaining_groups.append(group)
                continue
            handlers = group.get("hooks") or []
            remaining_handlers = [
                handler for handler in handlers if not _is_managed_handler(handler)
            ]
            if remaining_handlers:
                updated_group = dict(group)
                updated_group["hooks"] = remaining_handlers
                remaining_groups.append(updated_group)
        if remaining_groups:
            hooks[event] = remaining_groups
        else:
            hooks.pop(event, None)


def inspect_lifecycle_hooks(codex_home: Path) -> dict:
    hooks_path = codex_home / "hooks.json"
    backup_root = codex_home / "backups" / BACKUP_FOLDER
    backup_count = 0
    if backup_root.exists():
        backup_count = sum(
            1
            for path in backup_root.iterdir()
            if path.is_dir() and (path / "manifest.json").exists()
        )
    try:
        payload = _load_hooks(hooks_path)
        installed = _is_installed(payload)
        error = ""
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        installed = False
        error = str(exc)
    return {
        "hooksPath": str(hooks_path),
        "hooksFileExists": hooks_path.exists(),
        "managedHooksInstalled": installed,
        "backupCount": backup_count,
        "error": error,
    }


def install_lifecycle_hooks(
    codex_home: Path,
    policy_path: Path,
    config_path: Path,
    status_path: Path,
    lifecycle_script: Path,
    python_executable: Path,
    apply: bool,
    bridge_url: str = DEFAULT_BRIDGE_URL,
) -> dict:
    hooks_path = codex_home / "hooks.json"
    payload = _load_hooks(hooks_path)
    inner_command = _managed_command(
        python_executable=python_executable,
        lifecycle_script=lifecycle_script,
        policy_path=policy_path,
        codex_home=codex_home,
        config_path=config_path,
        status_path=status_path,
        bridge_url=bridge_url,
    )
    wrapper_path = wrapper_script_path(codex_home)
    command = str(wrapper_path)
    installed = _is_installed(payload)
    if installed and _is_current_install(payload, command):
        return {
            "status": "already-installed",
            "hooksPath": str(hooks_path),
            "wrapperPath": str(wrapper_path),
            "backupDirectory": "",
            "dryRun": not apply,
        }
    if not lifecycle_script.exists():
        raise FileNotFoundError(f"Lifecycle script not found: {lifecycle_script}")
    if not python_executable.exists():
        raise FileNotFoundError(
            f"Python executable not found: {python_executable}"
        )
    if not apply:
        return {
            "status": "update-planned" if installed else "planned",
            "hooksPath": str(hooks_path),
            "wrapperPath": str(wrapper_path),
            "backupDirectory": "",
            "dryRun": True,
        }

    backup_directory = _create_backup(
        codex_home=codex_home,
        hooks_path=hooks_path,
        reason="pre-hook-update" if installed else "pre-hook-install",
    )
    if installed:
        _remove_managed_hooks(payload)
    write_wrapper_script(wrapper_path, inner_command)
    hooks = payload.setdefault("hooks", {})
    for event, groups in _managed_groups(command).items():
        current = hooks.setdefault(event, [])
        if not isinstance(current, list):
            raise ValueError(f"hooks.{event} must be an array")
        current.extend(groups)
    _write_json_atomic(hooks_path, payload)
    return {
        "status": "updated" if installed else "installed",
        "hooksPath": str(hooks_path),
        "wrapperPath": str(wrapper_path),
        "backupDirectory": str(backup_directory),
        "dryRun": False,
    }


def uninstall_lifecycle_hooks(codex_home: Path, apply: bool) -> dict:
    hooks_path = codex_home / "hooks.json"
    payload = _load_hooks(hooks_path)
    if not _is_installed(payload):
        return {
            "status": "not-installed",
            "hooksPath": str(hooks_path),
            "backupDirectory": "",
            "dryRun": not apply,
        }
    if not apply:
        return {
            "status": "planned",
            "hooksPath": str(hooks_path),
            "backupDirectory": "",
            "dryRun": True,
        }

    backup_directory = _create_backup(
        codex_home=codex_home,
        hooks_path=hooks_path,
        reason="pre-hook-uninstall",
    )
    _remove_managed_hooks(payload)
    _write_json_atomic(hooks_path, payload)
    wrapper_path = wrapper_script_path(codex_home)
    try:
        wrapper_path.unlink()
    except FileNotFoundError:
        pass
    return {
        "status": "uninstalled",
        "hooksPath": str(hooks_path),
        "wrapperPath": str(wrapper_path),
        "backupDirectory": str(backup_directory),
        "dryRun": False,
    }


def restore_hook_backup(backup_directory: Path, apply: bool) -> dict:
    manifest_path = backup_directory / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Backup manifest not found: {manifest_path}")
    with manifest_path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    hooks_path = Path(str(manifest.get("hooksPath") or ""))
    if not hooks_path:
        raise ValueError("Backup manifest does not contain hooksPath")

    backup_file = backup_directory / "hooks.json"
    original_exists = bool(manifest.get("originalExists"))
    expected_digest = str(manifest.get("sha256") or "")
    if original_exists:
        if not backup_file.exists():
            raise FileNotFoundError(f"Hook backup not found: {backup_file}")
        if expected_digest and _sha256(backup_file) != expected_digest:
            raise ValueError("Hook backup SHA256 does not match its manifest")

    if not apply:
        return {
            "status": "restore-planned",
            "hooksPath": str(hooks_path),
            "preRestoreBackupDirectory": "",
            "dryRun": True,
        }
    if not original_exists and hooks_path.exists():
        payload = _load_hooks(hooks_path)
        hooks = payload.get("hooks") or {}
        if any(hooks.values()):
            raise RuntimeError(
                "Refusing to delete hooks.json because it contains unmanaged hooks"
            )

    codex_home = hooks_path.parent
    pre_restore = _create_backup(
        codex_home=codex_home,
        hooks_path=hooks_path,
        reason="pre-hook-restore",
    )
    if original_exists:
        temporary = hooks_path.with_suffix(hooks_path.suffix + f".{os.getpid()}.tmp")
        shutil.copy2(backup_file, temporary)
        temporary.replace(hooks_path)
    elif hooks_path.exists():
        hooks_path.unlink()

    return {
        "status": "restored",
        "hooksPath": str(hooks_path),
        "preRestoreBackupDirectory": str(pre_restore),
        "dryRun": False,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install", "uninstall", "restore"))
    parser.add_argument("--codex-home", default=str(Path.home() / ".codex"))
    parser.add_argument("--policy", default="")
    parser.add_argument("--config", default="")
    parser.add_argument("--status-file", default="")
    parser.add_argument("--lifecycle-script", default="")
    parser.add_argument("--python-executable", default="")
    parser.add_argument("--backup-directory", default="")
    parser.add_argument("--bridge-url", default=DEFAULT_BRIDGE_URL)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    codex_home = Path(args.codex_home)
    if args.action == "install":
        result = install_lifecycle_hooks(
            codex_home=codex_home,
            policy_path=Path(args.policy),
            config_path=Path(args.config),
            status_path=Path(args.status_file),
            lifecycle_script=Path(args.lifecycle_script),
            python_executable=Path(args.python_executable or sys.executable),
            bridge_url=args.bridge_url,
            apply=args.apply,
        )
    elif args.action == "uninstall":
        result = uninstall_lifecycle_hooks(codex_home=codex_home, apply=args.apply)
    else:
        if not args.backup_directory:
            raise SystemExit("--backup-directory is required for restore")
        result = restore_hook_backup(
            Path(args.backup_directory),
            apply=args.apply,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
