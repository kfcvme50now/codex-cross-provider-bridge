from __future__ import annotations

import http.client
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_cross_provider_bridge import (
    PolicyState,
    decide_scope,
    extract_conversation_id,
    is_retryable_portability_error,
    make_portable_payload,
    payload_needs_repair,
    lookup_conversation_metadata,
    sanitize_payload,
)
from codex_history_audit import audit_history


def sample_payload() -> dict:
    return {
        "model": "gpt-6-astra",
        "previous_response_id": "resp_from_other_provider",
        "store": True,
        "input": [
            {
                "type": "message",
                "id": "msg_valid",
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            },
            {
                "type": "reasoning",
                "id": "rs_resp_gen-123",
                "summary": [],
                "content": [{"type": "reasoning_text", "text": "foreign"}],
                "encrypted_content": "opaque-ciphertext",
            },
            {
                "type": "message",
                "id": "resp_gen-123_msg",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "world"}],
            },
            {
                "type": "function_call",
                "id": "fc_123",
                "call_id": "call_123",
                "name": "lookup",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "id": "fco_123",
                "call_id": "call_123",
                "output": "result",
            },
        ],
    }


class SanitizePayloadTests(unittest.TestCase):
    def test_removes_cross_provider_state_and_preserves_portable_history(self) -> None:
        payload, report = sanitize_payload(sample_payload())

        self.assertNotIn("previous_response_id", payload)
        self.assertIs(payload["store"], False)
        self.assertEqual(report.removed_item_ids, 5)
        self.assertEqual(report.removed_encrypted_reasoning, 1)

        items = payload["input"]
        self.assertEqual([item["type"] for item in items], [
            "message",
            "reasoning",
            "message",
            "function_call",
            "function_call_output",
        ])
        self.assertTrue(all("id" not in item for item in items))
        self.assertEqual(items[0]["content"][0]["text"], "hello")
        self.assertEqual(items[2]["content"][0]["text"], "world")
        self.assertEqual(items[3]["call_id"], "call_123")
        self.assertEqual(items[4]["output"], "result")
        self.assertNotIn("encrypted_content", items[1])
        self.assertEqual(items[1]["content"], [])

    def test_portable_payload_omits_reasoning_and_item_references(self) -> None:
        payload = sample_payload()
        payload["input"].append({"type": "item_reference", "id": "ref_123"})

        portable, report = make_portable_payload(payload)

        self.assertNotIn("previous_response_id", portable)
        self.assertIs(portable["store"], False)
        self.assertGreaterEqual(report.omitted_provider_items, 2)
        self.assertNotIn("reasoning", [item["type"] for item in portable["input"]])
        self.assertNotIn("item_reference", [item["type"] for item in portable["input"]])

    def test_retryable_error_detection_is_narrow(self) -> None:
        self.assertTrue(
            is_retryable_portability_error(
                400,
                b'{"error":{"param":"input[4].id","code":"invalid_value"}}',
            )
        )
        self.assertTrue(
            is_retryable_portability_error(400, b"encrypted content could not be verified")
        )
        self.assertFalse(is_retryable_portability_error(401, b"invalid input id"))
        self.assertFalse(is_retryable_portability_error(500, b"invalid input id"))
        self.assertFalse(is_retryable_portability_error(400, b"rate limit"))

    def test_scope_all_targets_every_request(self) -> None:
        targeted, reason = decide_scope(PolicyState(scope="all"), "conversation-a")
        self.assertTrue(targeted)
        self.assertEqual(reason, "all")

    def test_scope_next_is_consumed_once(self) -> None:
        policy = PolicyState(scope="next", armed=True)
        targeted, reason = decide_scope(policy, "conversation-a")
        self.assertTrue(targeted)
        self.assertEqual(reason, "next")
        self.assertFalse(policy.armed)

        targeted, reason = decide_scope(policy, "conversation-b")
        self.assertFalse(targeted)
        self.assertEqual(reason, "next-consumed")

    def test_scope_conversation_can_lock_next_session(self) -> None:
        policy = PolicyState(scope="conversation", armed=True)
        targeted, reason = decide_scope(policy, "conversation-a")
        self.assertTrue(targeted)
        self.assertEqual(reason, "conversation-lock-next")
        self.assertEqual(policy.target_conversation_id, "conversation-a")

        targeted, _ = decide_scope(policy, "conversation-b")
        self.assertFalse(targeted)
        targeted, _ = decide_scope(policy, "conversation-a")
        self.assertTrue(targeted)

    def test_extracts_session_identity_and_repair_need(self) -> None:
        conversation_id, source = extract_conversation_id(
            {"session_id": "session-123"},
            sample_payload(),
        )
        self.assertEqual(conversation_id, "session-123")
        self.assertEqual(source, "header:session_id")
        self.assertTrue(payload_needs_repair(sample_payload()))
        self.assertFalse(
            payload_needs_repair(
                {
                    "input": [
                        {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "clean"}],
                        }
                    ]
                }
            )
        )


class _MockResponsesHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).requests.append(payload)

        items = payload.get("input") or []
        has_item_id = any(isinstance(item, dict) and "id" in item for item in items)
        if has_item_id or payload.get("previous_response_id"):
            body = b'{"error":{"message":"bad response","param":"input[4].id","code":"invalid_value"}}'
            self.send_response(400)
        elif any(isinstance(item, dict) and item.get("type") == "reasoning" for item in items):
            body = b'{"error":{"message":"encrypted content could not be verified"}}'
            self.send_response(400)
        else:
            body = b'{"type":"response.completed","response":{"output":[]}}'
            self.send_response(200)

        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class BridgeIntegrationTests(unittest.TestCase):
    def test_legacy_compact_endpoint_is_sanitized(self) -> None:
        _MockResponsesHandler.requests = []
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), _MockResponsesHandler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()

        try:
            server, bridge_thread, temporary_policy = start_bridge(upstream.server_port)
            try:
                body = json.dumps(sample_payload()).encode("utf-8")
                connection = http.client.HTTPConnection(
                    "127.0.0.1",
                    server.server_port,
                    timeout=5,
                )
                connection.request(
                    "POST",
                    "/v1/responses/compact",
                    body=body,
                    headers={
                        "Content-Type": "application/json",
                        "Content-Length": str(len(body)),
                    },
                )
                response = connection.getresponse()
                response.read()
                connection.close()
                self.assertEqual(response.status, 200)
                self.assertTrue(_MockResponsesHandler.requests)
            finally:
                server.shutdown()
                server.server_close()
                bridge_thread.join(timeout=5)
                temporary_policy.cleanup()
        finally:
            upstream.shutdown()
            upstream.server_close()
            upstream_thread.join(timeout=5)

    def test_retries_with_portable_history_after_opaque_state_rejection(self) -> None:
        _MockResponsesHandler.requests = []
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), _MockResponsesHandler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()

        try:
            server, bridge_thread, temporary_policy = start_bridge(upstream.server_port)
            try:
                body = json.dumps(sample_payload()).encode("utf-8")
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                connection.request(
                    "POST",
                    "/v1/responses",
                    body=body,
                    headers={
                        "Content-Type": "application/json",
                        "Content-Length": str(len(body)),
                    },
                )
                response = connection.getresponse()
                response_body = response.read()
                connection.close()

                self.assertEqual(response.status, 200, response_body)
                self.assertEqual(len(_MockResponsesHandler.requests), 2)
                self.assertEqual(
                    [item["type"] for item in _MockResponsesHandler.requests[1]["input"]],
                    ["message", "message", "function_call", "function_call_output"],
                )
            finally:
                server.shutdown()
                server.server_close()
                bridge_thread.join(timeout=5)
                temporary_policy.cleanup()
        finally:
            upstream.shutdown()
            upstream.server_close()
            upstream_thread.join(timeout=5)

    def test_next_scope_is_consumed_by_the_bridge_handler(self) -> None:
        _MockResponsesHandler.requests = []
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), _MockResponsesHandler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()

        try:
            server, bridge_thread, temporary_policy = start_bridge(upstream.server_port)
            policy_path = Path(temporary_policy.name) / "policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "scope": "next",
                        "conversationIds": [],
                        "armed": True,
                        "targetConversationId": "",
                    }
                ),
                encoding="utf-8",
            )
            try:
                for prompt in ("first", "second"):
                    body = json.dumps(
                        {
                            "model": "gpt-6-astra",
                            "input": [
                                {
                                    "type": "message",
                                    "id": "resp_foreign_msg",
                                    "role": "user",
                                    "content": [{"type": "input_text", "text": prompt}],
                                }
                            ],
                        }
                    ).encode("utf-8")
                    connection = http.client.HTTPConnection(
                        "127.0.0.1",
                        server.server_port,
                        timeout=5,
                    )
                    connection.request(
                        "POST",
                        "/v1/responses",
                        body=body,
                        headers={
                            "Content-Type": "application/json",
                            "Content-Length": str(len(body)),
                            "session_id": f"conversation-{prompt}",
                        },
                    )
                    response = connection.getresponse()
                    response.read()
                    connection.close()
                    self.assertEqual(response.status, 200)

                stored_policy = json.loads(policy_path.read_text(encoding="utf-8"))
                self.assertFalse(stored_policy["armed"])
                status = json.loads(
                    (Path(temporary_policy.name) / "status.json").read_text(encoding="utf-8")
                )
                self.assertEqual(status["lastRequest"]["matchReason"], "next-consumed")
                self.assertFalse(status["lastRequest"]["targeted"])
            finally:
                server.shutdown()
                server.server_close()
                bridge_thread.join(timeout=5)
                temporary_policy.cleanup()
        finally:
            upstream.shutdown()
            upstream.server_close()
            upstream_thread.join(timeout=5)


class HistoryAuditTests(unittest.TestCase):
    def test_conversation_scope_reports_missing_provider_alias(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state_5.sqlite"
            connection = sqlite3.connect(database)
            connection.execute(
                "create table threads ("
                "id text primary key, model_provider text, title text, cwd text, rollout_path text"
                ")"
            )
            connection.execute(
                "insert into threads values (?, ?, ?, ?, ?)",
                (
                    "conversation-a",
                    "cc-switch-official",
                    "Handoff verification",
                    r"C:\work\example",
                    "rollout-a.jsonl",
                ),
            )
            connection.execute(
                "insert into threads values (?, ?, ?, ?, ?)",
                (
                    "conversation-b",
                    "custom",
                    "Other conversation",
                    r"C:\work\other",
                    "rollout-b.jsonl",
                ),
            )
            connection.commit()
            connection.close()

            config = root / "config.toml"
            config.write_text(
                'model_provider = "custom"\n\n[model_providers.custom]\n',
                encoding="utf-8",
            )

            result = audit_history(
                codex_home=root,
                config_path=config,
                scope="conversation",
                conversation_id="conversation-a",
            )

            self.assertTrue(result["threadFound"])
            self.assertEqual(result["providerIds"], ["cc-switch-official"])
            self.assertEqual(result["missingProviderIds"], ["cc-switch-official"])
            self.assertTrue(result["historyRepairRequired"])
            self.assertEqual(result["threads"][0]["title"], "Handoff verification")
            self.assertEqual(result["threads"][0]["cwd"], r"C:\work\example")

            metadata = lookup_conversation_metadata(root, "conversation-a")
            self.assertEqual(metadata["title"], "Handoff verification")
            self.assertEqual(metadata["cwd"], r"C:\work\example")


def start_bridge(upstream_port: int):
    from codex_cross_provider_bridge import create_server

    temporary_policy = tempfile.TemporaryDirectory()
    server = create_server(
        listen_host="127.0.0.1",
        listen_port=0,
        upstream_url=f"http://127.0.0.1:{upstream_port}",
        policy_file=Path(temporary_policy.name) / "policy.json",
        status_file=Path(temporary_policy.name) / "status.json",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, temporary_policy


if __name__ == "__main__":
    unittest.main()
