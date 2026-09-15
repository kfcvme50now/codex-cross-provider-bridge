#!/usr/bin/env python3
"""Command-line control plane for lifecycle hooks and their policy."""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

from codex_hook_manager import (
    BACKUP_FOLDER,
    DEFAULT_BRIDGE_URL,
    inspect_lifecycle_hooks,
    install_lifecycle_hooks,
    restore_hook_backup,
    uninstall_lifecycle_hooks,
)
from codex_lifecycle_policy import (
    load_lifecycle_policy,
    save_lifecycle_policy,
)


def _list_backups(codex_home: Path) -> list[dict]:
    root = codex_home / "backups" / BACKUP_FOLDER
    if not root.exists():
        return []
    records = []
    for directory in root.iterdir():
        manifest_path = directory / "manifest.json"
        if not directory.is_dir() or not manifest_path.exists():
            continue
        with manifest_path.open("r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        records.append(
            {
                "backupDirectory": str(directory),
                "reason": str(manifest.get("reason") or ""),
                "createdAt": manifest.get("createdAt"),
                "hooksPath": str(manifest.get("hooksPath") or ""),
                "originalExists": bool(manifest.get("originalExists")),
            }
        )
    return sorted(records, key=lambda item: item.get("createdAt") or 0, reverse=True)


def _policy_backup_root(policy_path: Path) -> Path:
    return policy_path.parent / "lifecycle-policy-backups"


def _backup_policy_file(policy_path: Path) -> str:
    if not policy_path.exists():
        return ""
    root = _policy_backup_root(policy_path)
    root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    suffix = f"{time.time_ns() % 1_000_000_000:09d}"
    backup_file = root / f"policy-{stamp}-{suffix}.json"
    shutil.copy2(policy_path, backup_file)
    return str(backup_file)


def _list_policy_backups(policy_path: Path) -> list[dict]:
    root = _policy_backup_root(policy_path)
    if not root.exists():
        return []
    return [
        {
            "backupFile": str(path),
            "fileName": path.name,
            "size": path.stat().st_size,
            "updatedAt": path.stat().st_mtime,
        }
        for path in sorted(root.glob("policy-*.json"), reverse=True)
        if path.is_file()
    ]


def _restore_policy_backup(policy_path: Path, backup_file: Path, apply: bool) -> dict:
    restored_policy = load_lifecycle_policy(backup_file)
    if not apply:
        return {
            "status": "restore-planned",
            "backupFile": str(backup_file),
            "policyFile": str(policy_path),
            "dryRun": True,
        }
    pre_restore = ""
    if policy_path.exists():
        root = _policy_backup_root(policy_path)
        root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        suffix = f"{time.time_ns() % 1_000_000_000:09d}"
        pre_restore_path = root / f"policy-{stamp}-{suffix}.json"
        shutil.copy2(policy_path, pre_restore_path)
        pre_restore = str(pre_restore_path)
    save_lifecycle_policy(policy_path, restored_policy)
    return {
        "status": "restored",
        "backupFile": str(backup_file),
        "policyFile": str(policy_path),
        "preRestoreBackupFile": pre_restore,
        "dryRun": False,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=(
            "status",
            "install-hooks",
            "uninstall-hooks",
            "list-backups",
            "restore-hooks",
            "list-policy-backups",
            "restore-policy-backup",
            "set-policy",
        ),
    )
    parser.add_argument("--codex-home", default=str(Path.home() / ".codex"))
    parser.add_argument(
        "--config",
        default=str(Path.home() / ".codex" / "config.toml"),
    )
    parser.add_argument("--policy", required=True)
    parser.add_argument("--status-file", required=True)
    parser.add_argument("--lifecycle-script", required=True)
    parser.add_argument("--python-executable", default="")
    parser.add_argument("--backup-directory", default="")
    parser.add_argument("--backup-file", default="")
    parser.add_argument(
        "--compact-mode",
        choices=(
            "disabled",
            "inspect",
            "repair-and-continue",
            "repair-and-stop",
            "repair-and-branch",
            "branch-only",
            "block-only",
        ),
    )
    parser.add_argument(
        "--auto-branch",
        choices=("true", "false"),
    )
    parser.add_argument("--branch-backend", choices=("app-server", "cli"))
    parser.add_argument(
        "--post-switch-probe-mode",
        choices=("disabled", "cli", "app-server"),
    )
    parser.add_argument(
        "--post-switch-scope",
        choices=("preserve", "next", "all"),
    )
    parser.add_argument(
        "--session-start-mode",
        choices=("disabled", "repair", "repair-and-probe"),
    )
    parser.add_argument(
        "--route-repair-mode",
        choices=("disabled", "inspect", "repair"),
    )
    parser.add_argument("--bridge-url", default=DEFAULT_BRIDGE_URL)
    parser.add_argument("--probe-timeout-seconds", type=int)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    codex_home = Path(args.codex_home)
    policy_path = Path(args.policy)
    if args.action == "status":
        result = {
            "policy": load_lifecycle_policy(policy_path),
            "hooks": inspect_lifecycle_hooks(codex_home),
        }
    elif args.action == "list-backups":
        result = _list_backups(codex_home)
    elif args.action == "list-policy-backups":
        result = _list_policy_backups(policy_path)
    elif args.action == "restore-policy-backup":
        if not args.backup_file:
            raise SystemExit("--backup-file is required for restore-policy-backup")
        result = _restore_policy_backup(
            policy_path=policy_path,
            backup_file=Path(args.backup_file),
            apply=args.apply,
        )
    elif args.action == "set-policy":
        policy = load_lifecycle_policy(policy_path)
        if args.compact_mode is not None:
            policy["compactRepairMode"] = args.compact_mode
        if args.auto_branch is not None:
            policy["autoBranchEnabled"] = args.auto_branch == "true"
        if args.branch_backend is not None:
            policy["branchBackend"] = args.branch_backend
        if args.post_switch_probe_mode is not None:
            policy["postSwitchProbeMode"] = args.post_switch_probe_mode
        if args.post_switch_scope is not None:
            policy["postSwitchScope"] = args.post_switch_scope
        if args.session_start_mode is not None:
            policy["sessionStartMode"] = args.session_start_mode
        if args.route_repair_mode is not None:
            policy["routeRepairMode"] = args.route_repair_mode
        if args.probe_timeout_seconds is not None:
            policy["probeTimeoutSeconds"] = args.probe_timeout_seconds
        if args.apply:
            backup_file = _backup_policy_file(policy_path)
            saved = save_lifecycle_policy(policy_path, policy)
            result = {**saved, "backupFile": backup_file}
        else:
            result = {**policy, "dryRun": True}
    elif args.action == "install-hooks":
        if not args.python_executable:
            raise SystemExit("--python-executable is required for install-hooks")
        result = install_lifecycle_hooks(
            codex_home=codex_home,
            policy_path=policy_path,
            config_path=Path(args.config),
            status_path=Path(args.status_file),
            lifecycle_script=Path(args.lifecycle_script),
            python_executable=Path(args.python_executable),
            bridge_url=args.bridge_url,
            apply=args.apply,
        )
    elif args.action == "uninstall-hooks":
        result = uninstall_lifecycle_hooks(codex_home=codex_home, apply=args.apply)
    else:
        if not args.backup_directory:
            raise SystemExit("--backup-directory is required for restore-hooks")
        result = restore_hook_backup(
            backup_directory=Path(args.backup_directory),
            apply=args.apply,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
