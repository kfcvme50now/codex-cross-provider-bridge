from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codex_message_id_repair import prepare_rollout, repair

OLD = "resp_489b5e97-b66a-49a0-863e-495203e9440b_msg"
NEW = "msg__489b5e97-b66a-49a0-863e-495203e9440b_msg"


def fixture():
    records = [
        {"type": "turn_context", "payload": {"model": "deepseek-flash"}},
        {"type": "response_item", "payload": {"type": "message", "id": OLD,
            "role": "assistant", "content": [{"type": "output_text", "text": '淇濈暀鍘熸枃 ' + OLD + ' "id":"' + OLD + '"'}]}},
        {"type": "response_item", "payload": {"type": "function_call", "id": "fc_a", "call_id": OLD, "arguments": "{}"}},
        {"type": "event_msg", "payload": {"type": "agent_message", "item_id": OLD}},
    ]
    return b"".join((json.dumps(x, ensure_ascii=False) + "\r\n").encode() for x in records)


class MessageIdRepairTests(unittest.TestCase):
    def test_only_structural_message_ids_change(self):
        before = fixture()
        after, mapping = prepare_rollout(before)
        self.assertEqual(mapping, {OLD: NEW})
        a = [json.loads(x) for x in before.splitlines()]
        b = [json.loads(x) for x in after.splitlines()]
        self.assertEqual(a[0], b[0])
        self.assertEqual(a[1]["payload"]["content"], b[1]["payload"]["content"])
        self.assertEqual(a[2], b[2])
        self.assertEqual(b[3]["payload"]["item_id"], NEW)
        self.assertEqual(list(map(len, before.splitlines(True))), list(map(len, after.splitlines(True))))

    def test_idempotent(self):
        after, _ = prepare_rollout(fixture())
        self.assertEqual(prepare_rollout(after), (after, {}))

    def test_collision_rejected(self):
        extra = json.dumps({"type": "response_item", "payload": {"type": "message", "id": NEW}}).encode()
        with self.assertRaises(ValueError):
            prepare_rollout(fixture() + extra)

    def test_backups_projection_and_other_thread(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rollout = root / "thread.jsonl"
            rollout.write_bytes(fixture())
            with closing(sqlite3.connect(root / "state_5.sqlite")) as db, db:
                db.execute("create table threads(id text, rollout_path text)")
                db.execute("insert into threads values (?,?)", ("target", str(rollout)))
            with closing(sqlite3.connect(root / "thread_history_1.sqlite")) as db, db:
                db.execute("create table thread_items(thread_id text,item_id text,item_json text,primary key(thread_id,item_id))")
                db.execute("create table thread_turns(thread_id text,final_agent_item_id text)")
                for tid in ["target", "other"]:
                    db.execute("insert into thread_items values(?,?,?)", (tid, OLD, json.dumps({"id": OLD, "text": OLD})))
                    db.execute("insert into thread_turns values(?,?)", (tid, OLD))
            dry = repair(root, "target", root / "backup")
            self.assertFalse(dry["applied"])
            self.assertFalse((root / "backup").exists())
            with patch.object(Path, "replace", side_effect=PermissionError("Loaded rollout")):
                with self.assertRaises(PermissionError):
                    repair(root, "target", root / "blocked-backup", True, min_idle=0)
            self.assertEqual(rollout.read_bytes(), fixture())
            with closing(sqlite3.connect(root / "thread_history_1.sqlite")) as db:
                self.assertEqual(db.execute("select item_id from thread_items where thread_id='target'").fetchone()[0], OLD)
            result = repair(root, "target", root / "backup", True, min_idle=0)
            self.assertTrue(result["applied"])
            self.assertEqual((root / "backup/rollout-before.jsonl").read_bytes(), fixture())
            with closing(sqlite3.connect(root / "thread_history_1.sqlite")) as db, db:
                self.assertEqual(db.execute("select item_id from thread_items where thread_id='other'").fetchone()[0], OLD)
                row = db.execute("select item_id,item_json from thread_items where thread_id='target'").fetchone()
                self.assertEqual(row[0], NEW)
                self.assertEqual(json.loads(row[1]), {"id": NEW, "text": OLD})
                self.assertEqual(db.execute("select final_agent_item_id from thread_turns where thread_id='target'").fetchone()[0], NEW)


if __name__ == "__main__":
    unittest.main()
