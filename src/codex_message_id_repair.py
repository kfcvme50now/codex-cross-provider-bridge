"""Explicit, backed-up repair of legacy response-style message IDs in one idle thread.

Never change model provenance, text, tool call IDs, routing, or other threads.
Length-preserving IDs keep the desktop's rollout byte offsets valid.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import copy
import hashlib
import json
import os
from codex_bridge_environment import default_codex_home
from pathlib import Path
import re
import sqlite3
import time


LEGACY_ID = re.compile(r"resp_[0-9a-fA-F-]{36}_msg\Z")
ID_KEYS = {"id", "item_id", "first_user_item_id", "final_agent_item_id"}


def transform(value, mapping):
    if isinstance(value, dict):
        return {k: mapping.get(v, v) if k in ID_KEYS and isinstance(v, str)
                else transform(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [transform(v, mapping) for v in value]
    return value


def prepare_rollout(raw):
    lines = raw.splitlines(keepends=True)
    records = [json.loads(line) for line in lines]
    mapping = {}
    for record in records:
        item = record.get("payload", {})
        old = item.get("id", "")
        if (record.get("type") == "response_item" and item.get("type") == "message"
                and isinstance(old, str) and LEGACY_ID.fullmatch(old)):
            mapping[old] = "msg__" + old[5:]
    # Refuse a collision with any existing structural ID, including event records.
    def ids(value):
        if isinstance(value, dict):
            for k, v in value.items():
                if k in ID_KEYS and isinstance(v, str):
                    yield v
                else:
                    yield from ids(v)
        elif isinstance(value, list):
            for v in value:
                yield from ids(v)
    if set(mapping.values()) & set(ids(records)):
        raise ValueError("Replacement ID already exists")
    pattern = re.compile(rb'("(?:id|item_id|first_user_item_id|final_agent_item_id)"\s*:\s*")([^"\\]*)(")')
    encoded = {k.encode(): v.encode() for k, v in mapping.items()}
    result = []
    for line, record in zip(lines, records):
        updated = pattern.sub(lambda m: m[1] + encoded.get(m[2], m[2]) + m[3], line)
        if len(updated) != len(line) or json.loads(updated) != transform(record, mapping):
            raise ValueError("Non-ID content or byte offsets would change")
        result.append(updated)
    return b"".join(result), mapping


def repair(home, thread_id, archive, apply=False, min_idle=60):
    with closing(sqlite3.connect((home / "state_5.sqlite").as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("select rollout_path from threads where id=?", (thread_id,)).fetchone()
    if not row:
        raise ValueError("Thread not found")
    path = Path(row[0])
    before = path.read_bytes()
    after, mapping = prepare_rollout(before)
    summary = {"threadId": thread_id, "messageIds": len(mapping), "bytesPreserved": len(before) == len(after),
               "applied": False, "inferenceRequested": False}
    if not apply or not mapping:
        return summary
    if time.time() - path.stat().st_mtime < min_idle:
        raise RuntimeError("Thread is not idle; retry after its turn has stopped")
    archive.mkdir(parents=True, exist_ok=False)
    (archive / "rollout-before.jsonl").write_bytes(before)
    database = home / "thread_history_1.sqlite"
    db = sqlite3.connect(database)
    db.row_factory = sqlite3.Row
    temporary = path.with_suffix(path.suffix + f".id-repair-{os.getpid()}.tmp")
    wrote = False
    committed = False
    try:
        db.execute("BEGIN IMMEDIATE")
        backups = {}
        tables = {r[0] for r in db.execute("select name from sqlite_master where type='table'")}
        for table in ("thread_items", "thread_turns", "thread_realtime_items"):
            if table not in tables:
                continue
            backups[table] = []
            for row in db.execute(f"select rowid AS repair_rowid,* from {table} where thread_id=?", (thread_id,)).fetchall():
                original = dict(row)
                updated = copy.deepcopy(original)
                for key in ID_KEYS:
                    if key in updated:
                        updated[key] = mapping.get(updated[key], updated[key])
                if "item_json" in updated:
                    item = json.loads(updated["item_json"])
                    changed = transform(item, mapping)
                    if changed != item:
                        updated["item_json"] = json.dumps(changed, ensure_ascii=False, separators=(",", ":"))
                changed_keys = [k for k in original if original[k] != updated[k]]
                if changed_keys:
                    backups[table].append(original)
                    db.execute(f"update {table} set " + ",".join(k + "=?" for k in changed_keys) + " where rowid=? AND thread_id=?",
                               [updated[k] for k in changed_keys] + [original["repair_rowid"], thread_id])
        (archive / "projection-before.json").write_text(json.dumps(backups, ensure_ascii=False), encoding="utf-8")
        temporary.write_bytes(after)
        if path.read_bytes() != before:
            raise RuntimeError("Thread changed during repair; no rollout changes applied")
        temporary.replace(path)
        wrote = True
        db.commit()
        committed = True
        summary.update(applied=True, archive=str(archive), projectionRows=sum(map(len, backups.values())),
                       beforeSha256=hashlib.sha256(before).hexdigest(), afterSha256=hashlib.sha256(after).hexdigest())
        (archive / "manifest.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return summary
    except BaseException:
        db.rollback()
        if wrote and not committed and path.read_bytes() == after:
            temporary.write_bytes(before)
            temporary.replace(path)
        raise
    finally:
        temporary.unlink(missing_ok=True)
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", type=Path, default=default_codex_home())
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(repair(args.codex_home, args.thread_id, args.archive, args.apply), ensure_ascii=False))
