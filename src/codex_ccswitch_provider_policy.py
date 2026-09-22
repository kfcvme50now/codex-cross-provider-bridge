#!/usr/bin/env python3
"""Apply reversible, non-secret routing policy markers to CC Switch providers."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import time
from pathlib import Path


MANAGED_NOTE_PREFIX = "Routing disabled:"


def _updated_notes(notes: str, disabled: bool, reason: str) -> str:
    lines = [
        line
        for line in str(notes or "").splitlines()
        if not line.strip().startswith(MANAGED_NOTE_PREFIX)
    ]
    if disabled:
        display_reason = reason.replace("_", " ").strip() or "local policy"
        lines.append(
            f"{MANAGED_NOTE_PREFIX} {display_reason}; configuration retained for "
            "possible future reuse."
        )
    return "\n".join(line for line in lines if line.strip())


def set_routing_policy(
    database: Path,
    provider_id: str,
    *,
    disabled: bool,
    reason: str = "",
    apply: bool = False,
) -> dict[str, object]:
    """Update only routing metadata; provider settings and credentials stay untouched."""
    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            "select name, notes, meta, is_current, in_failover_queue "
            "from providers where id = ? and app_type = 'codex'",
            (provider_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Codex provider not found: {provider_id}")
        try:
            meta = json.loads(row[2] or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("provider meta is not valid JSON") from exc
        if not isinstance(meta, dict):
            raise ValueError("provider meta must be a JSON object")

        updated_meta = dict(meta)
        if disabled:
            updated_meta["routing_disabled"] = True
            updated_meta["routing_disabled_reason"] = reason or "local_policy"
        else:
            updated_meta.pop("routing_disabled", None)
            updated_meta.pop("routing_disabled_reason", None)

        notes = _updated_notes(str(row[1] or ""), disabled, reason)
        is_current = 0 if disabled else int(row[3] or 0)
        in_failover_queue = 0 if disabled else int(row[4] or 0)
        serialized_meta = json.dumps(
            updated_meta,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        changed = (
            notes != str(row[1] or "")
            or serialized_meta != str(row[2] or "{}")
            or is_current != int(row[3] or 0)
            or in_failover_queue != int(row[4] or 0)
        )
        if apply and changed:
            connection.execute(
                "update providers set notes = ?, meta = ?, is_current = ?, "
                "in_failover_queue = ? where id = ? and app_type = 'codex'",
                (
                    notes,
                    serialized_meta,
                    is_current,
                    in_failover_queue,
                    provider_id,
                ),
            )
            connection.commit()
        return {
            "providerId": provider_id,
            "providerName": str(row[0] or provider_id),
            "routingDisabled": disabled,
            "routingDisabledReason": reason if disabled else "",
            "isCurrent": bool(is_current),
            "inFailoverQueue": bool(in_failover_queue),
            "changed": changed,
            "applied": bool(apply and changed),
        }
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Disable or re-enable one retained CC Switch Codex provider without "
            "reading or deleting its settings_config."
        )
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path.home() / ".cc-switch" / "cc-switch.db",
    )
    parser.add_argument("--provider-id", required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--disable", action="store_true")
    action.add_argument("--enable", action="store_true")
    parser.add_argument("--reason", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--backup-root",
        type=Path,
        default=Path.home() / ".cc-switch" / "backups" / "provider-routing-policy",
    )
    args = parser.parse_args()

    backup = ""
    if args.apply:
        args.backup_root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_path = args.backup_root / f"cc-switch-{stamp}.db"
        shutil.copy2(args.database, backup_path)
        backup = str(backup_path)

    result = set_routing_policy(
        args.database,
        args.provider_id,
        disabled=bool(args.disable),
        reason=str(args.reason or ""),
        apply=bool(args.apply),
    )
    result["backup"] = backup
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
