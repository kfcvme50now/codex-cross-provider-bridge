#!/usr/bin/env python3
"""Provider-neutral request bridge for Codex Responses conversations.

The bridge sits between Codex and CC Switch (or another HTTP Responses
upstream), rewrites only the outgoing request copy, and never modifies session
files or Codex databases.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import signal
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
PORTABILITY_ERROR_MARKERS = (
    b"expected an id that begins",
    b"encrypted content",
    b"could not be verified",
    b"previous_response_id",
    b"array too long",
)
MAX_BODY_BYTES = 256 * 1024 * 1024
DEFAULT_STATE_DIRECTORY = Path(__file__).resolve().parents[1] / "state"
DEFAULT_POLICY_FILE = DEFAULT_STATE_DIRECTORY / "policy.json"
DEFAULT_STATUS_FILE = DEFAULT_STATE_DIRECTORY / "status.json"


@dataclass
class SanitizeReport:
    removed_item_ids: int = 0
    removed_encrypted_reasoning: int = 0
    removed_previous_response_id: bool = False
    omitted_provider_items: int = 0


@dataclass
class PolicyState:
    scope: str = "all"
    history_scope: str = "all"
    conversation_ids: tuple[str, ...] = ()
    armed: bool = True
    target_conversation_id: str = ""
    updated_at: float = 0.0


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temporary.replace(path)


def load_policy(path: Path) -> PolicyState:
    if not path.exists():
        return PolicyState()
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    scope = str(payload.get("scope", "all")).lower()
    if scope not in {"all", "next", "conversation"}:
        scope = "all"
    conversation_ids = payload.get("conversationIds") or []
    if not isinstance(conversation_ids, list):
        conversation_ids = []
    return PolicyState(
        scope=scope,
        history_scope=str(payload.get("historyScope", "all")),
        conversation_ids=tuple(str(item) for item in conversation_ids if str(item).strip()),
        armed=bool(payload.get("armed", True)),
        target_conversation_id=str(payload.get("targetConversationId") or ""),
        updated_at=float(payload.get("updatedAt") or 0.0),
    )


def save_policy(path: Path, policy: PolicyState) -> None:
    policy.updated_at = time.time()
    _write_json_atomic(
        path,
        {
            "scope": policy.scope,
            "historyScope": policy.history_scope,
            "conversationIds": list(policy.conversation_ids),
            "armed": policy.armed,
            "targetConversationId": policy.target_conversation_id,
            "updatedAt": policy.updated_at,
        },
    )


def extract_conversation_id(headers: object, payload: object) -> tuple[str, str]:
    for header_name in ("session_id", "x-session-id"):
        value = headers.get(header_name) if hasattr(headers, "get") else None
        if value:
            return str(value).strip(), f"header:{header_name}"

    if isinstance(payload, dict):
        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            for key in ("session_id", "conversation_id", "thread_id"):
                value = metadata.get(key)
                if value:
                    return str(value).strip(), f"metadata:{key}"
        for key in ("session_id", "conversation_id", "thread_id", "prompt_cache_key"):
            value = payload.get(key)
            if value:
                return str(value).strip(), key
    return "", "unknown"


def lookup_conversation_metadata(
    codex_home: Path,
    conversation_id: str,
) -> dict[str, str]:
    if not conversation_id:
        return {"title": "", "cwd": "", "modelProvider": ""}
    database = codex_home / "state_5.sqlite"
    if not database.exists():
        return {"title": "", "cwd": "", "modelProvider": ""}

    try:
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        try:
            columns = {
                row[1] for row in connection.execute("pragma table_info(threads)")
            }
            if "id" not in columns:
                return {"title": "", "cwd": "", "modelProvider": ""}
            title_expression = "title" if "title" in columns else "''"
            cwd_expression = "cwd" if "cwd" in columns else "''"
            provider_expression = (
                "model_provider" if "model_provider" in columns else "''"
            )
            row = connection.execute(
                f"select {title_expression}, {cwd_expression}, "
                f"{provider_expression} from threads where id = ?",
                (conversation_id,),
            ).fetchone()
            if not row:
                return {"title": "", "cwd": "", "modelProvider": ""}
            return {
                "title": str(row[0] or ""),
                "cwd": str(row[1] or ""),
                "modelProvider": str(row[2] or ""),
            }
        finally:
            connection.close()
    except sqlite3.Error:
        return {"title": "", "cwd": "", "modelProvider": ""}


def payload_needs_repair(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("previous_response_id") is not None:
        return True
    items = payload.get("input")
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("id") is not None:
            return True
        if item.get("type") == "reasoning" and item.get("encrypted_content"):
            return True
        if item.get("type") == "item_reference":
            return True
    return False


def decide_scope(
    policy: PolicyState,
    conversation_id: str,
) -> tuple[bool, str]:
    """Return whether this request should be sanitized and the match reason."""
    if policy.scope == "all":
        return True, "all"

    if policy.scope == "next":
        if policy.armed:
            policy.armed = False
            policy.target_conversation_id = conversation_id
            return True, "next"
        return False, "next-consumed"

    if conversation_id and conversation_id in policy.conversation_ids:
        return True, "conversation-match"

    if policy.target_conversation_id and conversation_id:
        return (
            policy.target_conversation_id == conversation_id,
            "conversation-match" if policy.target_conversation_id == conversation_id else "conversation-miss",
        )

    if policy.armed and not policy.conversation_ids:
        policy.armed = False
        policy.target_conversation_id = conversation_id
        return True, "conversation-lock-next"

    return False, "conversation-not-armed"


def sanitize_payload(payload: object) -> tuple[object, SanitizeReport]:
    """Remove provider-owned continuation state while preserving visible history."""
    if not isinstance(payload, dict):
        return payload, SanitizeReport()

    cleaned = dict(payload)
    report = SanitizeReport()
    items = cleaned.get("input")

    if isinstance(items, list):
        sanitized_items = []
        for original in items:
            if not isinstance(original, dict):
                sanitized_items.append(original)
                continue

            item = dict(original)
            if "id" in item:
                item.pop("id")
                report.removed_item_ids += 1

            if item.get("type") == "reasoning":
                if isinstance(item.get("content"), list) and item["content"]:
                    item["content"] = []
                if "encrypted_content" in item:
                    item.pop("encrypted_content")
                    report.removed_encrypted_reasoning += 1

            sanitized_items.append(item)
        cleaned["input"] = sanitized_items

    if cleaned.get("previous_response_id") is not None:
        cleaned.pop("previous_response_id")
        report.removed_previous_response_id = True

    # The request contains a full local replay, so it must not depend on the
    # previous provider's server-side response state.
    cleaned["store"] = False
    return cleaned, report


def make_portable_payload(payload: object) -> tuple[object, SanitizeReport]:
    """Drop reasoning and references after an upstream still rejects replay."""
    if not isinstance(payload, dict):
        return payload, SanitizeReport()

    cleaned, base_report = sanitize_payload(payload)
    if not isinstance(cleaned, dict):
        return cleaned, base_report

    report = SanitizeReport(
        removed_item_ids=base_report.removed_item_ids,
        removed_encrypted_reasoning=base_report.removed_encrypted_reasoning,
        removed_previous_response_id=base_report.removed_previous_response_id,
    )
    items = cleaned.get("input")
    if isinstance(items, list):
        portable_items = []
        for item in items:
            if isinstance(item, dict) and item.get("type") in {
                "reasoning",
                "item_reference",
            }:
                report.omitted_provider_items += 1
                continue
            portable_items.append(item)
        cleaned["input"] = portable_items

    cleaned["store"] = False
    return cleaned, report


def is_retryable_portability_error(status: int, body: bytes) -> bool:
    """Recognize only explicit history-portability HTTP 400 responses."""
    if status != 400:
        return False

    lowered = body.lower()
    if b"input[" in lowered and b".id" in lowered:
        return True
    return any(marker in lowered for marker in PORTABILITY_ERROR_MARKERS)


def override_model(payload: object, model_override: str) -> object:
    if not model_override or not isinstance(payload, dict):
        return payload
    if not isinstance(payload.get("model"), str):
        return payload

    updated = dict(payload)
    updated["model"] = model_override
    return updated


def _is_responses_path(path: str) -> bool:
    normalized = urlsplit(path).path.rstrip("/")
    return normalized.endswith("/responses") or normalized.endswith(
        "/responses/compact"
    )


class BridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CodexCrossProviderBridge/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))
        sys.stderr.flush()

    def _read_request_body(self) -> bytes | None:
        length_text = self.headers.get("Content-Length", "0") or "0"
        try:
            length = int(length_text)
        except ValueError:
            self.send_error(400, "Invalid Content-Length")
            return None

        if length > MAX_BODY_BYTES:
            self.send_error(413, "Request body is too large")
            return None
        return self.rfile.read(length) if length else b""

    def _forward(
        self,
        method: str,
        path: str,
        body: bytes,
        headers: dict[str, str],
    ) -> tuple[http.client.HTTPResponse, http.client.HTTPConnection]:
        upstream = self.server.upstream  # type: ignore[attr-defined]
        connection_class = (
            http.client.HTTPSConnection
            if upstream.scheme == "https"
            else http.client.HTTPConnection
        )
        port = upstream.port or (443 if upstream.scheme == "https" else 80)
        connection = connection_class(upstream.hostname, port, timeout=600)
        connection.request(method, path, body=body or None, headers=headers)
        return connection.getresponse(), connection

    def _build_upstream_path(self) -> str:
        upstream = self.server.upstream  # type: ignore[attr-defined]
        incoming = urlsplit(self.path)
        base_path = upstream.path.rstrip("/")
        path = f"{base_path}/{incoming.path.lstrip('/')}" if base_path else incoming.path
        if upstream.query:
            separator = "&" if "?" in path else "?"
            path += separator + upstream.query
        if incoming.query:
            separator = "&" if "?" in path else "?"
            path += separator + incoming.query
        return path or "/"

    def _forward_response(
        self,
        response: http.client.HTTPResponse,
        buffered_body: bytes | None = None,
    ) -> None:
        self.send_response(response.status, response.reason)
        for key, value in response.getheaders():
            lower = key.lower()
            if lower in HOP_BY_HOP or lower == "content-length":
                continue
            self.send_header(key, value)
        self.send_header("Connection", "close")
        self.end_headers()

        if buffered_body is not None:
            self.wfile.write(buffered_body)
        else:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        self.close_connection = True

    def _handle(self) -> None:
        if urlsplit(self.path).path == "/__bridge/info":
            policy_path = self.server.policy_file  # type: ignore[attr-defined]
            policy = load_policy(policy_path)
            policy_file_text = ""
            policy_file_mtime = None
            if policy_path.exists():
                policy_file_text = policy_path.read_text(encoding="utf-8")
                policy_file_mtime = policy_path.stat().st_mtime
            body = json.dumps(
                {
                    "policyFile": str(policy_path),
                    "policyFileExists": policy_path.exists(),
                    "policyFileMtime": policy_file_mtime,
                    "policyFileText": policy_file_text,
                    "statusFile": str(self.server.status_file),  # type: ignore[attr-defined]
                    "codexHome": str(self.server.codex_home),  # type: ignore[attr-defined]
                    "policy": {
                        "scope": policy.scope,
                        "armed": policy.armed,
                        "conversationIds": list(policy.conversation_ids),
                        "targetConversationId": policy.target_conversation_id,
                    },
                },
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True
            return

        body = self._read_request_body()
        if body is None:
            return

        content_encoding = (self.headers.get("Content-Encoding") or "").lower().strip()
        if content_encoding and content_encoding != "identity":
            self.send_error(
                415,
                "Compressed request body is unsupported. "
                "Set [features] enable_request_compression = false in config.toml.",
            )
            return

        original_payload: object | None = None
        sanitized_payload: object | None = None
        report = SanitizeReport()
        conversation_id = ""
        conversation_id_source = "unknown"
        conversation_title = ""
        conversation_cwd = ""
        conversation_provider = ""
        needs_repair = False
        targeted = True
        match_reason = "non-responses"
        content_type = (self.headers.get("Content-Type") or "").lower()

        if body and "json" in content_type and _is_responses_path(self.path):
            try:
                original_payload = json.loads(body.decode("utf-8"))
                conversation_id, conversation_id_source = extract_conversation_id(
                    self.headers,
                    original_payload,
                )
                cached = self.server.metadata_cache.get(conversation_id)  # type: ignore[attr-defined]
                if not cached or time.time() - cached[0] > 10:
                    metadata = lookup_conversation_metadata(
                        self.server.codex_home,  # type: ignore[attr-defined]
                        conversation_id,
                    )
                    cached = (time.time(), metadata)
                    self.server.metadata_cache[conversation_id] = cached  # type: ignore[attr-defined]
                conversation_title = cached[1]["title"]
                conversation_cwd = cached[1]["cwd"]
                conversation_provider = cached[1]["modelProvider"]
                needs_repair = payload_needs_repair(original_payload)
                with self.server.policy_lock:  # type: ignore[attr-defined]
                    policy = load_policy(self.server.policy_file)  # type: ignore[attr-defined]
                    targeted, match_reason = decide_scope(policy, conversation_id)
                    save_policy(self.server.policy_file, policy)  # type: ignore[attr-defined]

                if targeted:
                    original_payload = override_model(
                        original_payload,
                        self.server.model_override,  # type: ignore[attr-defined]
                    )
                    sanitized_payload, report = sanitize_payload(original_payload)
                    body = json.dumps(
                        sanitized_payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self.send_error(400, f"Invalid JSON request body: {exc}")
                return

        request_headers: dict[str, str] = {}
        for key, value in self.headers.items():
            lower = key.lower()
            if lower in HOP_BY_HOP or lower in {
                "host",
                "content-length",
                "content-encoding",
                "accept-encoding",
            }:
                continue
            request_headers[key] = value
        if body:
            request_headers["Content-Length"] = str(len(body))
        request_headers["Accept-Encoding"] = "identity"

        upstream_response: http.client.HTTPResponse | None = None
        connection: http.client.HTTPConnection | None = None
        buffered_body: bytes | None = None
        retried = False
        retry_report = SanitizeReport()

        try:
            upstream_path = self._build_upstream_path()
            upstream_response, connection = self._forward(
                self.command,
                upstream_path,
                body,
                request_headers,
            )

            if (
                original_payload is not None
                and upstream_response.status == 400
                and self.command in {"POST", "PUT", "PATCH"}
            ):
                first_error = upstream_response.read()
                if is_retryable_portability_error(upstream_response.status, first_error):
                    connection.close()
                    portable, retry_report = make_portable_payload(original_payload)
                    portable_body = json.dumps(
                        portable,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    retry_headers = dict(request_headers)
                    retry_headers["Content-Length"] = str(len(portable_body))
                    upstream_response, connection = self._forward(
                        self.command,
                        upstream_path,
                        portable_body,
                        retry_headers,
                    )
                    retried = True
                else:
                    buffered_body = first_error

            self._forward_response(upstream_response, buffered_body)
            repair_status = (
                "repair-applied"
                if targeted and needs_repair
                else "not-needed"
                if targeted
                else "not-targeted"
            )
            with self.server.policy_lock:  # type: ignore[attr-defined]
                current_policy = load_policy(self.server.policy_file)  # type: ignore[attr-defined]
                request_index = self.server.request_count  # type: ignore[attr-defined]
                self.server.request_count += 1  # type: ignore[attr-defined]
                _write_json_atomic(
                    self.server.status_file,  # type: ignore[attr-defined]
                    {
                        "updatedAt": time.time(),
                        "scope": current_policy.scope,
                        "armed": current_policy.armed,
                        "conversationIds": list(current_policy.conversation_ids),
                        "targetConversationId": current_policy.target_conversation_id,
                        "requestIndex": request_index,
                        "lastRequest": {
                            "conversationId": conversation_id,
                            "conversationIdSource": conversation_id_source,
                            "conversationTitle": conversation_title,
                            "cwd": conversation_cwd,
                            "modelProvider": conversation_provider,
                            "targeted": targeted,
                            "matchReason": match_reason,
                            "needsRepair": needs_repair,
                            "repairStatus": repair_status,
                            "removedItemIds": report.removed_item_ids,
                            "removedEncryptedReasoning": report.removed_encrypted_reasoning,
                            "portableRetry": retried,
                            "upstreamStatus": upstream_response.status,
                        },
                    },
                )
            self.log_message(
                "%s %s -> %s; scope=%s; targeted=%s; "
                "conversation_id=%s; needs_repair=%s; removed_item_ids=%d; "
                "removed_encrypted_reasoning=%d; "
                "removed_previous_response_id=%s; portable_retry=%s; "
                "omitted_provider_items=%d",
                self.command,
                urlsplit(self.path).path,
                upstream_response.status,
                current_policy.scope,
                str(targeted).lower(),
                conversation_id or "-",
                str(needs_repair).lower(),
                report.removed_item_ids,
                report.removed_encrypted_reasoning,
                str(report.removed_previous_response_id).lower(),
                str(retried).lower(),
                retry_report.omitted_provider_items,
            )
        except (OSError, http.client.HTTPException) as exc:
            self.send_error(502, f"Upstream request failed: {exc}")
        finally:
            if connection is not None:
                connection.close()

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle
    do_OPTIONS = _handle


class BridgeServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        upstream_url: str,
        model_override: str = "",
        policy_file: Path = DEFAULT_POLICY_FILE,
        status_file: Path = DEFAULT_STATUS_FILE,
        codex_home: Path = Path.home() / ".codex",
    ) -> None:
        upstream = urlsplit(upstream_url)
        if upstream.scheme not in {"http", "https"} or not upstream.hostname:
            raise ValueError("upstream URL must be an absolute http:// or https:// URL")

        super().__init__(server_address, BridgeHandler)
        self.upstream = upstream
        self.model_override = model_override.strip()
        self.policy_file = Path(policy_file)
        self.status_file = Path(status_file)
        self.codex_home = Path(codex_home)
        self.policy_lock = threading.Lock()
        self.metadata_cache: dict[str, tuple[float, dict[str, str]]] = {}
        self.request_count = 0


def create_server(
    listen_host: str,
    listen_port: int,
    upstream_url: str,
    model_override: str = "",
    policy_file: Path = DEFAULT_POLICY_FILE,
    status_file: Path = DEFAULT_STATUS_FILE,
    codex_home: Path = Path.home() / ".codex",
) -> BridgeServer:
    if listen_host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("listen host must be loopback-only")
    return BridgeServer(
        (listen_host, listen_port),
        upstream_url=upstream_url,
        model_override=model_override,
        policy_file=policy_file,
        status_file=status_file,
        codex_home=codex_home,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sanitize Codex Responses history before forwarding upstream."
    )
    parser.add_argument("--listen", default="127.0.0.1:15722")
    parser.add_argument("--upstream", default="http://127.0.0.1:15721")
    parser.add_argument("--model-override", default="")
    parser.add_argument("--policy-file", default=str(DEFAULT_POLICY_FILE))
    parser.add_argument("--status-file", default=str(DEFAULT_STATUS_FILE))
    parser.add_argument("--codex-home", default=str(Path.home() / ".codex"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    listen_host, listen_port_text = args.listen.rsplit(":", 1)
    server = create_server(
        listen_host=listen_host,
        listen_port=int(listen_port_text),
        upstream_url=args.upstream,
        model_override=args.model_override,
        policy_file=Path(args.policy_file),
        status_file=Path(args.status_file),
        codex_home=Path(args.codex_home),
    )

    def stop(_signum: int, _frame: object) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)

    print(f"Codex cross-provider bridge listening on http://{args.listen}")
    print(f"Forwarding to {args.upstream}")
    if server.model_override:
        print(f"Rewriting request model to {server.model_override}")
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
