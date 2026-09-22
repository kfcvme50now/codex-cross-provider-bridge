#!/usr/bin/env python3
"""Provider-neutral request bridge for Codex Responses conversations.

The bridge sits between Codex and CC Switch (or another HTTP Responses
upstream), rewrites only the outgoing request copy, and never modifies session
files or Codex databases.
"""

from __future__ import annotations

import argparse
import gzip
import http.client
import io
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

try:
    import zstandard
except ImportError:  # pragma: no cover - exercised only on incomplete installs
    zstandard = None


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
DEFAULT_UPSTREAM_HEADER_TIMEOUT_SECONDS = 120
DEFAULT_UPSTREAM_IDLE_TIMEOUT_SECONDS = 120
DEFAULT_PROVIDER_CIRCUIT_SECONDS = 60
DEFAULT_STATE_DIRECTORY = Path(__file__).resolve().parents[1] / "state"
DEFAULT_POLICY_FILE = DEFAULT_STATE_DIRECTORY / "policy.json"
DEFAULT_STATUS_FILE = DEFAULT_STATE_DIRECTORY / "status.json"
DEFAULT_CC_SWITCH_DB = Path.home() / ".cc-switch" / "cc-switch.db"
DEFAULT_PRESERVE_STATE_PROVIDER_IDS = ("default", "anyrouter-codex-gpt6")
DEFAULT_HEALTH_GUARD_PROVIDER_IDS = ("anyrouter-codex-gpt6",)


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


@dataclass(frozen=True)
class ProviderRuntimeState:
    provider_id: str
    name: str
    is_healthy: bool | None
    consecutive_failures: int
    routing_disabled: bool
    routing_disabled_reason: str


class RequestBodyDecodeError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def decode_request_body(body: bytes, content_encoding: str) -> bytes:
    encoding = content_encoding.lower().strip()
    if not encoding or encoding == "identity":
        return body
    try:
        if encoding == "zstd":
            if zstandard is None:
                raise RequestBodyDecodeError(
                    "request_decompression_unavailable",
                    "zstd request support is unavailable; install the zstandard package",
                )
            decoded = zstandard.ZstdDecompressor().decompress(
                body,
                max_output_size=MAX_BODY_BYTES + 1,
            )
        elif encoding == "gzip":
            with gzip.GzipFile(fileobj=io.BytesIO(body), mode="rb") as stream:
                decoded = stream.read(MAX_BODY_BYTES + 1)
        else:
            raise RequestBodyDecodeError(
                "unsupported_content_encoding",
                f"unsupported request Content-Encoding: {encoding}",
            )
    except RequestBodyDecodeError:
        raise
    except Exception as exc:  # zstd and gzip expose different decode exceptions
        raise RequestBodyDecodeError(
            "invalid_compressed_request",
            f"could not decode {encoding} request body",
        ) from exc
    if len(decoded) > MAX_BODY_BYTES:
        raise RequestBodyDecodeError(
            "decompressed_request_too_large",
            "decompressed request body exceeds the bridge limit",
        )
    return decoded


def lookup_current_provider(database: Path) -> ProviderRuntimeState | None:
    """Read only non-secret routing and health metadata from CC Switch."""
    if not database.exists():
        return None
    try:
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            row = connection.execute(
                "select p.id, p.name, p.meta, h.is_healthy, "
                "coalesce(h.consecutive_failures, 0) "
                "from providers p left join provider_health h "
                "on h.provider_id = p.id and h.app_type = p.app_type "
                "where p.app_type = 'codex' and p.is_current = 1 limit 1"
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    if not row:
        return None
    try:
        meta = json.loads(row[2] or "{}")
    except (TypeError, json.JSONDecodeError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    health = None if row[3] is None else bool(row[3])
    return ProviderRuntimeState(
        provider_id=str(row[0] or ""),
        name=str(row[1] or row[0] or ""),
        is_healthy=health,
        consecutive_failures=int(row[4] or 0),
        routing_disabled=bool(meta.get("routing_disabled", False)),
        routing_disabled_reason=str(meta.get("routing_disabled_reason") or ""),
    )


def provider_block_reason(
    provider: ProviderRuntimeState | None,
    health_guard_provider_ids: set[str] | frozenset[str],
) -> tuple[str, str] | None:
    if provider is None:
        return None
    if provider.routing_disabled:
        reason = provider.routing_disabled_reason or "disabled by local routing policy"
        return "provider_disabled", reason
    if (
        provider.provider_id in health_guard_provider_ids
        and provider.is_healthy is False
    ):
        return (
            "provider_unavailable",
            f"CC Switch marked this provider unhealthy after "
            f"{provider.consecutive_failures} consecutive failures",
        )
    return None


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temporary.replace(path)


def load_status_last_request(path: Path) -> object:
    """Keep the previous request record visible after a bridge restart."""
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict):
                return payload.get("lastRequest")
    except (OSError, json.JSONDecodeError):
        pass
    return None


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
        return {"title": "", "cwd": "", "modelProvider": "", "model": ""}
    database = codex_home / "state_5.sqlite"
    if not database.exists():
        return {"title": "", "cwd": "", "modelProvider": "", "model": ""}

    try:
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        try:
            columns = {
                row[1] for row in connection.execute("pragma table_info(threads)")
            }
            if "id" not in columns:
                return {"title": "", "cwd": "", "modelProvider": "", "model": ""}
            title_expression = "title" if "title" in columns else "''"
            cwd_expression = "cwd" if "cwd" in columns else "''"
            provider_expression = (
                "model_provider" if "model_provider" in columns else "''"
            )
            model_expression = "model" if "model" in columns else "''"
            row = connection.execute(
                f"select {title_expression}, {cwd_expression}, "
                f"{provider_expression}, {model_expression} "
                "from threads where id = ?",
                (conversation_id,),
            ).fetchone()
            if not row:
                return {"title": "", "cwd": "", "modelProvider": "", "model": ""}
            return {
                "title": str(row[0] or ""),
                "cwd": str(row[1] or ""),
                "modelProvider": str(row[2] or ""),
                "model": str(row[3] or ""),
            }
        finally:
            connection.close()
    except sqlite3.Error:
        return {"title": "", "cwd": "", "modelProvider": "", "model": ""}


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
        header_timeout = self.server.upstream_header_timeout  # type: ignore[attr-defined]
        connection = connection_class(
            upstream.hostname,
            port,
            timeout=header_timeout or None,
        )
        try:
            connection.request(method, path, body=body or None, headers=headers)
            # getresponse() clears connection.sock when the response closes the
            # connection, so the socket has to be captured first to apply the
            # idle timeout to the body reads.
            upstream_socket = connection.sock
            response = connection.getresponse()
        except (OSError, http.client.HTTPException):
            connection.close()
            raise
        idle_timeout = self.server.upstream_idle_timeout  # type: ignore[attr-defined]
        if upstream_socket is not None:
            upstream_socket.settimeout(idle_timeout or None)
        return response, connection

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

    def _send_error_quietly(self, code: int, message: str) -> None:
        try:
            self.send_error(code, message)
        except OSError:
            pass

    def _send_json_error(self, status: int, code: str, message: str) -> None:
        body = json.dumps(
            {"error": {"code": code, "message": message}},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except OSError:
            pass
        self.close_connection = True

    def _relay_failure_event(self, outcome: str, detail: dict[str, object]) -> None:
        """Best-effort SSE error so a stalled stream is not an endless wait."""
        if outcome == "upstream-idle-timeout":
            code = "upstream_stalled"
            message = "upstream produced no data for %ss" % detail.get(
                "silenceSeconds", "?"
            )
        elif outcome == "upstream-empty-response":
            code = "upstream_empty_response"
            message = "upstream closed a successful stream without any events"
        else:
            code = "upstream_terminated"
            message = "upstream ended the stream unexpectedly"
        payload = json.dumps(
            {
                "type": "error",
                "code": code,
                "message": (
                    f"Cross-provider bridge aborted this request: {message}. "
                    "The upstream provider never completed a response; retry or switch provider."
                ),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        try:
            self.wfile.write(
                b"event: error\ndata: " + payload + b"\n\n"
            )
            self.wfile.flush()
        except OSError:
            pass

    def _forward_response(
        self,
        response: http.client.HTTPResponse,
        buffered_body: bytes | None = None,
    ) -> tuple[str, dict[str, object]]:
        content_type = (response.getheader("Content-Type") or "").lower()
        self.send_response(response.status, response.reason)
        for key, value in response.getheaders():
            lower = key.lower()
            if lower in HOP_BY_HOP or lower == "content-length":
                continue
            self.send_header(key, value)
        self.send_header("Connection", "close")
        self.end_headers()

        if buffered_body is not None:
            try:
                self.wfile.write(buffered_body)
            except OSError:
                return "client-aborted", {"bytesRelayed": 0}
            self.close_connection = True
            return "completed", {"bytesRelayed": len(buffered_body)}

        bytes_relayed = 0
        silence_started = time.monotonic()
        while True:
            try:
                # read1() returns as soon as any data is available; read() would
                # block until the full block arrives and lose partial data when
                # the idle timeout fires.
                chunk = response.read1(65536)
            except TimeoutError:
                detail = {
                    "silenceSeconds": round(time.monotonic() - silence_started, 3),
                    "bytesRelayed": bytes_relayed,
                }
                if "event-stream" in content_type:
                    self._relay_failure_event("upstream-idle-timeout", detail)
                self.close_connection = True
                return "upstream-idle-timeout", detail
            except (OSError, http.client.HTTPException) as exc:
                detail = {
                    "error": type(exc).__name__,
                    "bytesRelayed": bytes_relayed,
                }
                if "event-stream" in content_type:
                    self._relay_failure_event("upstream-error", detail)
                self.close_connection = True
                return "upstream-error", detail
            if not chunk:
                if bytes_relayed == 0 and "event-stream" in content_type:
                    detail = {"bytesRelayed": 0}
                    self._relay_failure_event("upstream-empty-response", detail)
                    self.close_connection = True
                    return "upstream-empty-response", detail
                self.close_connection = True
                return "completed", {"bytesRelayed": bytes_relayed}
            bytes_relayed += len(chunk)
            silence_started = time.monotonic()
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
            except OSError:
                self.close_connection = True
                return "client-aborted", {"bytesRelayed": bytes_relayed}

    def _publish_status(self) -> str:
        """Rewrite the status file from current in-memory bridge state."""
        server = self.server  # type: ignore[attr-defined]
        with server.policy_lock:
            policy = load_policy(server.policy_file)
            _write_json_atomic(
                server.status_file,
                {
                    "updatedAt": time.time(),
                    "scope": policy.scope,
                    "armed": policy.armed,
                    "conversationIds": list(policy.conversation_ids),
                    "targetConversationId": policy.target_conversation_id,
                    "requestIndex": server.request_count,
                    "inFlight": list(server.in_flight.values()) or None,
                    "lastRequest": server.last_request,
                },
            )
            return policy.scope

    def _begin_request(self, descriptor: dict[str, object]) -> int:
        """Record a request before it is forwarded so a stall stays visible."""
        server = self.server  # type: ignore[attr-defined]
        with server.policy_lock:
            key = server.request_serial
            server.request_serial += 1
            server.in_flight[key] = descriptor
        self._in_flight_key = key
        self._publish_status()
        return key

    def _end_request(self, final_status: dict[str, object]) -> str:
        server = self.server  # type: ignore[attr-defined]
        key = getattr(self, "_in_flight_key", None)
        with server.policy_lock:
            if key is not None:
                server.in_flight.pop(key, None)
            server.last_request = final_status
            server.request_count += 1
        return self._publish_status()

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
        try:
            body = decode_request_body(body, content_encoding)
        except RequestBodyDecodeError as exc:
            self._send_json_error(400, exc.code, str(exc))
            return

        original_payload: object | None = None
        sanitized_payload: object | None = None
        report = SanitizeReport()
        conversation_id = ""
        conversation_id_source = "unknown"
        conversation_title = ""
        conversation_cwd = ""
        conversation_provider = ""
        conversation_model = ""
        model_before = ""
        model_after = ""
        needs_repair = False
        targeted = True
        match_reason = "non-responses"
        content_type = (self.headers.get("Content-Type") or "").lower()
        active_provider = lookup_current_provider(
            self.server.cc_switch_db  # type: ignore[attr-defined]
        )
        provider_state_preserved = False

        if body and "json" in content_type and _is_responses_path(self.path):
            try:
                original_payload = json.loads(body.decode("utf-8"))
                conversation_id, conversation_id_source = extract_conversation_id(
                    self.headers,
                    original_payload,
                )
                needs_repair = payload_needs_repair(original_payload)
                cached = self.server.metadata_cache.get(conversation_id)  # type: ignore[attr-defined]
                if needs_repair or not cached or time.time() - cached[0] > 10:
                    metadata = lookup_conversation_metadata(
                        self.server.codex_home,  # type: ignore[attr-defined]
                        conversation_id,
                    )
                    cached = (time.time(), metadata)
                    self.server.metadata_cache[conversation_id] = cached  # type: ignore[attr-defined]
                conversation_title = cached[1]["title"]
                conversation_cwd = cached[1]["cwd"]
                conversation_provider = cached[1]["modelProvider"]
                conversation_model = cached[1]["model"]
                with self.server.policy_lock:  # type: ignore[attr-defined]
                    policy = load_policy(self.server.policy_file)  # type: ignore[attr-defined]
                    targeted, match_reason = decide_scope(policy, conversation_id)
                    save_policy(self.server.policy_file, policy)  # type: ignore[attr-defined]

                if targeted:
                    if isinstance(original_payload, dict):
                        model_before = str(original_payload.get("model") or "")
                    effective_model_override = self.server.model_override  # type: ignore[attr-defined]
                    if not effective_model_override and needs_repair:
                        effective_model_override = conversation_model
                    original_payload = override_model(
                        original_payload,
                        effective_model_override,
                    )
                    if isinstance(original_payload, dict):
                        model_after = str(original_payload.get("model") or "")
                    preserve_ids = self.server.preserve_state_provider_ids  # type: ignore[attr-defined]
                    provider_state_preserved = bool(
                        needs_repair
                        and active_provider is not None
                        and active_provider.provider_id in preserve_ids
                    )
                    if provider_state_preserved:
                        sanitized_payload = original_payload
                    else:
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

        repair_status = (
            "provider-state-preserved"
            if provider_state_preserved
            else "repair-applied"
            if targeted and needs_repair
            else "not-needed"
            if targeted
            else "not-targeted"
        )
        request_descriptor: dict[str, object] = {
            "conversationId": conversation_id,
            "conversationIdSource": conversation_id_source,
            "conversationTitle": conversation_title,
            "cwd": conversation_cwd,
            "modelProvider": conversation_provider,
            "conversationModel": conversation_model,
            "modelBefore": model_before,
            "modelAfter": model_after,
            "targeted": targeted,
            "matchReason": match_reason,
            "needsRepair": needs_repair,
            "repairStatus": repair_status,
            "activeProviderId": active_provider.provider_id if active_provider else "",
            "activeProviderName": active_provider.name if active_provider else "",
            "requestPath": urlsplit(self.path).path,
            "startedAt": time.time(),
        }

        blocked = provider_block_reason(
            active_provider,
            self.server.health_guard_provider_ids,  # type: ignore[attr-defined]
        )
        circuit: dict[str, object] | None = None
        if active_provider is not None:
            circuit = self.server.get_open_provider_circuit(  # type: ignore[attr-defined]
                active_provider.provider_id
            )
        if blocked is None and circuit is not None:
            blocked = (
                "provider_circuit_open",
                "the local circuit is open after an unexpected upstream failure",
            )
        if blocked is not None:
            code, reason = blocked
            request_descriptor["outcome"] = "provider-blocked"
            request_descriptor["retrySuppressed"] = True
            request_descriptor["errorCategory"] = code
            request_descriptor["outcomeDetail"] = {
                "reason": reason,
                "consecutiveFailures": (
                    active_provider.consecutive_failures if active_provider else 0
                ),
            }
            if circuit is not None:
                request_descriptor["circuitOpenUntil"] = circuit["openUntil"]
            self._begin_request(request_descriptor)
            self._send_json_error(
                424,
                code,
                f"Request stopped locally: provider "
                f"{active_provider.name if active_provider else 'unknown'} is unavailable "
                f"({reason}). No upstream request was sent.",
            )
            self._end_request(request_descriptor)
            self.log_message(
                "%s %s -> 424; outcome=provider-blocked; provider=%s; category=%s",
                self.command,
                urlsplit(self.path).path,
                active_provider.provider_id if active_provider else "-",
                code,
            )
            return

        upstream_response: http.client.HTTPResponse | None = None
        connection: http.client.HTTPConnection | None = None
        buffered_body: bytes | None = None
        retried = False
        retry_report = SanitizeReport()
        outcome = "upstream-error"
        outcome_detail: dict[str, object] = {}
        upstream_status: int | None = None

        self._begin_request(request_descriptor)

        try:
            upstream_path = self._build_upstream_path()
            try:
                upstream_response, connection = self._forward(
                    self.command,
                    upstream_path,
                    body,
                    request_headers,
                )
            except TimeoutError:
                outcome = "upstream-headers-timeout"
                outcome_detail = {
                    "headerTimeoutSeconds": self.server.upstream_header_timeout,  # type: ignore[attr-defined]
                }
                if (
                    active_provider is not None
                    and active_provider.provider_id
                    in self.server.health_guard_provider_ids  # type: ignore[attr-defined]
                ):
                    circuit = self.server.open_provider_circuit(  # type: ignore[attr-defined]
                        active_provider.provider_id,
                        "upstream-headers-timeout",
                    )
                    outcome = "provider-upstream-error"
                    outcome_detail["circuitOpenUntil"] = circuit["openUntil"]
                    self._send_json_error(
                        424,
                        "provider_upstream_error",
                        "Request stopped: the provider did not answer before the "
                        "header timeout. The local circuit is now open; no automatic "
                        "upstream retry will be attempted.",
                    )
                else:
                    self._send_error_quietly(
                        504,
                        "Upstream did not send response headers before the bridge "
                        "timeout. The provider may be stalled.",
                    )
                return

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

            guarded_provider = bool(
                active_provider is not None
                and active_provider.provider_id
                in self.server.health_guard_provider_ids  # type: ignore[attr-defined]
            )
            if guarded_provider and not 200 <= upstream_response.status < 300:
                if buffered_body is None:
                    buffered_body = upstream_response.read()
                upstream_status = upstream_response.status
                circuit = self.server.open_provider_circuit(  # type: ignore[attr-defined]
                    active_provider.provider_id,
                    f"upstream-http-{upstream_response.status}",
                )
                outcome = "provider-upstream-error"
                outcome_detail = {
                    "upstreamStatus": upstream_response.status,
                    "circuitOpenUntil": circuit["openUntil"],
                    "retrySuppressed": True,
                }
                self._send_json_error(
                    424,
                    "provider_upstream_error",
                    f"Request stopped: provider {active_provider.name} returned "
                    f"HTTP {upstream_response.status}. The local circuit is now open; "
                    "no automatic upstream retry will be attempted.",
                )
                return

            declared_length = (upstream_response.getheader("Content-Length") or "").strip()
            if guarded_provider and (
                upstream_response.status == 204 or declared_length == "0"
            ):
                upstream_status = upstream_response.status
                circuit = self.server.open_provider_circuit(  # type: ignore[attr-defined]
                    active_provider.provider_id,
                    "upstream-empty-response",
                )
                outcome = "provider-upstream-error"
                outcome_detail = {
                    "upstreamStatus": upstream_response.status,
                    "emptyResponse": True,
                    "circuitOpenUntil": circuit["openUntil"],
                    "retrySuppressed": True,
                }
                self._send_json_error(
                    424,
                    "provider_empty_response",
                    f"Request stopped: provider {active_provider.name} returned an "
                    "empty success response. The local circuit is now open; no "
                    "automatic upstream retry will be attempted.",
                )
                return

            outcome, outcome_detail = self._forward_response(
                upstream_response,
                buffered_body,
            )
            upstream_status = upstream_response.status
            if guarded_provider and outcome in {
                "upstream-idle-timeout",
                "upstream-error",
                "upstream-empty-response",
            }:
                circuit = self.server.open_provider_circuit(  # type: ignore[attr-defined]
                    active_provider.provider_id,
                    outcome,
                )
                outcome_detail["circuitOpenUntil"] = circuit["openUntil"]
                outcome_detail["retrySuppressed"] = True
        except (OSError, http.client.HTTPException) as exc:
            guarded_provider = bool(
                active_provider is not None
                and active_provider.provider_id
                in self.server.health_guard_provider_ids  # type: ignore[attr-defined]
            )
            outcome = "provider-upstream-error" if guarded_provider else "upstream-error"
            outcome_detail = {"error": type(exc).__name__}
            if guarded_provider:
                circuit = self.server.open_provider_circuit(  # type: ignore[attr-defined]
                    active_provider.provider_id,
                    type(exc).__name__,
                )
                outcome_detail["circuitOpenUntil"] = circuit["openUntil"]
                outcome_detail["retrySuppressed"] = True
                self._send_json_error(
                    424,
                    "provider_upstream_error",
                    f"Request stopped: provider {active_provider.name} ended the "
                    "connection unexpectedly. The local circuit is now open; no "
                    "automatic upstream retry will be attempted.",
                )
            else:
                self._send_error_quietly(502, f"Upstream request failed: {exc}")
        finally:
            if connection is not None:
                connection.close()
            final_status = dict(request_descriptor)
            final_status["outcome"] = outcome
            final_status["upstreamStatus"] = upstream_status
            final_status["portableRetry"] = retried
            final_status["removedItemIds"] = report.removed_item_ids
            final_status["removedEncryptedReasoning"] = (
                report.removed_encrypted_reasoning
            )
            if outcome == "provider-upstream-error" or outcome_detail.get(
                "retrySuppressed"
            ):
                final_status["errorCategory"] = "provider_upstream_error"
                final_status["retrySuppressed"] = True
            if outcome_detail.get("circuitOpenUntil") is not None:
                final_status["circuitOpenUntil"] = outcome_detail["circuitOpenUntil"]
            if outcome_detail:
                final_status["outcomeDetail"] = outcome_detail
            policy_scope = self._end_request(final_status)
            self.log_message(
                "%s %s -> %s; outcome=%s; scope=%s; targeted=%s; "
                "conversation_id=%s; needs_repair=%s; removed_item_ids=%d; "
                "removed_encrypted_reasoning=%d; "
                "removed_previous_response_id=%s; portable_retry=%s; "
                "omitted_provider_items=%d; detail=%s",
                self.command,
                urlsplit(self.path).path,
                upstream_status if upstream_status is not None else "-",
                outcome,
                policy_scope,
                str(targeted).lower(),
                conversation_id or "-",
                str(needs_repair).lower(),
                report.removed_item_ids,
                report.removed_encrypted_reasoning,
                str(report.removed_previous_response_id).lower(),
                str(retried).lower(),
                retry_report.omitted_provider_items,
                json.dumps(outcome_detail, ensure_ascii=False) if outcome_detail else "-",
            )

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
        cc_switch_db: Path = DEFAULT_CC_SWITCH_DB,
        preserve_state_provider_ids: tuple[str, ...] = DEFAULT_PRESERVE_STATE_PROVIDER_IDS,
        health_guard_provider_ids: tuple[str, ...] = DEFAULT_HEALTH_GUARD_PROVIDER_IDS,
        provider_circuit_seconds: int = DEFAULT_PROVIDER_CIRCUIT_SECONDS,
        upstream_header_timeout: int = DEFAULT_UPSTREAM_HEADER_TIMEOUT_SECONDS,
        upstream_idle_timeout: int = DEFAULT_UPSTREAM_IDLE_TIMEOUT_SECONDS,
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
        self.cc_switch_db = Path(cc_switch_db)
        self.preserve_state_provider_ids = frozenset(preserve_state_provider_ids)
        self.health_guard_provider_ids = frozenset(health_guard_provider_ids)
        self.provider_circuit_seconds = max(1, int(provider_circuit_seconds))
        self.provider_circuit_lock = threading.Lock()
        self.provider_circuits: dict[str, dict[str, object]] = {}
        self.upstream_header_timeout = max(0, int(upstream_header_timeout))
        self.upstream_idle_timeout = max(0, int(upstream_idle_timeout))
        self.policy_lock = threading.Lock()
        self.metadata_cache: dict[str, tuple[float, dict[str, str]]] = {}
        self.request_count = 0
        self.request_serial = 0
        self.in_flight: dict[int, dict[str, object]] = {}
        self.last_request: object = load_status_last_request(self.status_file)

    def get_open_provider_circuit(self, provider_id: str) -> dict[str, object] | None:
        now = time.time()
        with self.provider_circuit_lock:
            circuit = self.provider_circuits.get(provider_id)
            if circuit is None:
                return None
            if float(circuit.get("openUntil") or 0) <= now:
                self.provider_circuits.pop(provider_id, None)
                return None
            return dict(circuit)

    def open_provider_circuit(
        self,
        provider_id: str,
        reason: str,
    ) -> dict[str, object]:
        circuit = {
            "reason": reason,
            "openedAt": time.time(),
            "openUntil": time.time() + self.provider_circuit_seconds,
        }
        with self.provider_circuit_lock:
            self.provider_circuits[provider_id] = circuit
        return dict(circuit)


def create_server(
    listen_host: str,
    listen_port: int,
    upstream_url: str,
    model_override: str = "",
    policy_file: Path = DEFAULT_POLICY_FILE,
    status_file: Path = DEFAULT_STATUS_FILE,
    codex_home: Path = Path.home() / ".codex",
    cc_switch_db: Path = DEFAULT_CC_SWITCH_DB,
    preserve_state_provider_ids: tuple[str, ...] = DEFAULT_PRESERVE_STATE_PROVIDER_IDS,
    health_guard_provider_ids: tuple[str, ...] = DEFAULT_HEALTH_GUARD_PROVIDER_IDS,
    provider_circuit_seconds: int = DEFAULT_PROVIDER_CIRCUIT_SECONDS,
    upstream_header_timeout: int = DEFAULT_UPSTREAM_HEADER_TIMEOUT_SECONDS,
    upstream_idle_timeout: int = DEFAULT_UPSTREAM_IDLE_TIMEOUT_SECONDS,
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
        cc_switch_db=cc_switch_db,
        preserve_state_provider_ids=preserve_state_provider_ids,
        health_guard_provider_ids=health_guard_provider_ids,
        provider_circuit_seconds=provider_circuit_seconds,
        upstream_header_timeout=upstream_header_timeout,
        upstream_idle_timeout=upstream_idle_timeout,
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
    parser.add_argument("--cc-switch-db", default=str(DEFAULT_CC_SWITCH_DB))
    parser.add_argument(
        "--preserve-state-provider",
        action="append",
        dest="preserve_state_providers",
        help=(
            "CC Switch provider ID allowed to try existing provider-owned state first. "
            "Repeat for multiple providers."
        ),
    )
    parser.add_argument(
        "--health-guard-provider",
        action="append",
        dest="health_guard_providers",
        help=(
            "CC Switch provider ID blocked locally while its health state is unhealthy. "
            "Repeat for multiple providers."
        ),
    )
    parser.add_argument(
        "--provider-circuit-seconds",
        type=int,
        default=DEFAULT_PROVIDER_CIRCUIT_SECONDS,
        help="Seconds to suppress guarded-provider retries after an upstream failure.",
    )
    parser.add_argument(
        "--upstream-header-timeout",
        type=int,
        default=DEFAULT_UPSTREAM_HEADER_TIMEOUT_SECONDS,
        help="Seconds to wait for upstream response headers; 0 disables the limit.",
    )
    parser.add_argument(
        "--upstream-idle-timeout",
        type=int,
        default=DEFAULT_UPSTREAM_IDLE_TIMEOUT_SECONDS,
        help="Seconds of upstream silence tolerated mid-stream; 0 disables the limit.",
    )
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
        cc_switch_db=Path(args.cc_switch_db),
        preserve_state_provider_ids=tuple(
            args.preserve_state_providers or DEFAULT_PRESERVE_STATE_PROVIDER_IDS
        ),
        health_guard_provider_ids=tuple(
            args.health_guard_providers or DEFAULT_HEALTH_GUARD_PROVIDER_IDS
        ),
        provider_circuit_seconds=args.provider_circuit_seconds,
        upstream_header_timeout=args.upstream_header_timeout,
        upstream_idle_timeout=args.upstream_idle_timeout,
    )

    def stop(_signum: int, _frame: object) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)

    print(f"Codex cross-provider bridge listening on http://{args.listen}")
    print(f"Forwarding to {args.upstream}")
    print(
        "Upstream limits: headers=%ss idle=%ss (0 disables)"
        % (server.upstream_header_timeout, server.upstream_idle_timeout)
    )
    if server.model_override:
        print(f"Rewriting request model to {server.model_override}")
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
