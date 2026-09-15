#!/usr/bin/env python3
"""Small stdio JSON-RPC client for the Codex app-server protocol."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from pathlib import Path
from typing import Any

from codex_executable import resolve_codex_command
from codex_internal import INTERNAL_ENV


class AppServerError(RuntimeError):
    pass


class AppServerClient:
    def __init__(
        self,
        codex_home: Path,
        timeout_seconds: int,
        command: list[str] | None = None,
        cwd: Path | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.command = command or [
            *resolve_codex_command(),
            "app-server",
            "--listen",
            "stdio://",
        ]
        self.cwd = cwd
        self.codex_home = codex_home
        self.process: subprocess.Popen[str] | None = None
        self.messages: queue.Queue[dict | None] = queue.Queue()
        self.pending: list[dict] = []
        self.next_id = 1
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr_tail = ""

    def __enter__(self) -> "AppServerClient":
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def start(self) -> None:
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(self.codex_home)
        environment[INTERNAL_ENV] = "1"
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=str(self.cwd) if self.cwd else None,
            env=environment,
        )
        self._reader_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader_thread.start()
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_thread.start()
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex_cross_provider_bridge",
                    "title": "Codex Cross-Provider Bridge",
                    "version": "1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        self.notify("initialized", {})

    def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for line in self.process.stdout:
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                self.messages.put(payload)
        self.messages.put(None)

    def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        chunks: list[str] = []
        size = 0
        for line in self.process.stderr:
            if size < 32768:
                chunks.append(line)
                size += len(line)
        self._stderr_tail = "".join(chunks)[-32768:]

    def _write(self, payload: dict) -> None:
        if self.process is None or self.process.stdin is None:
            raise AppServerError("App-server is not running")
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def notify(self, method: str, params: dict) -> None:
        self._write({"method": method, "params": params})

    def _next_message(self, timeout_seconds: float) -> dict:
        try:
            message = self.messages.get(timeout=timeout_seconds)
        except queue.Empty as exc:
            raise TimeoutError("Timed out waiting for app-server") from exc
        if message is None:
            raise AppServerError(
                f"App-server exited unexpectedly: {self._stderr_tail[-500:]}"
            )
        return message

    def request(self, method: str, params: dict) -> Any:
        request_id = self.next_id
        self.next_id += 1
        self._write({"method": method, "id": request_id, "params": params})
        while True:
            message = self._next_message(self.timeout_seconds)
            if message.get("id") == request_id:
                if "error" in message:
                    error = message.get("error") or {}
                    raise AppServerError(str(error.get("message") or error))
                return message.get("result")
            if "method" in message:
                self.pending.append(message)

    def wait_notification(
        self,
        method: str,
        timeout_seconds: float | None = None,
    ) -> dict:
        timeout = timeout_seconds or self.timeout_seconds
        for index, message in enumerate(self.pending):
            if message.get("method") == method:
                return self.pending.pop(index)
        while True:
            message = self._next_message(timeout)
            if message.get("method") == method:
                return message
            if "method" in message:
                self.pending.append(message)

    def close(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()
        self.process = None


def _category_from_error(message: str) -> str:
    lowered = message.lower()
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    if "not supported" in lowered or "unsupported model" in lowered:
        return "unsupported-model"
    if "auth" in lowered or "401" in lowered:
        return "authentication"
    if "rate" in lowered or "429" in lowered:
        return "rate-limit"
    if "connect" in lowered or "transport" in lowered:
        return "transport"
    if "config" in lowered:
        return "configuration"
    return "unknown"


def probe_provider_with_app_server(
    codex_home: Path,
    provider: str,
    model: str,
    timeout_seconds: int,
    command: list[str] | None = None,
) -> dict:
    try:
        with AppServerClient(
            codex_home=codex_home,
            timeout_seconds=timeout_seconds,
            command=command,
        ) as client:
            started = client.request(
                "thread/start",
                {
                    "ephemeral": True,
                    "modelProvider": provider,
                    "model": model,
                    "cwd": str(codex_home),
                },
            )
            thread_id = str((started.get("thread") or {}).get("id") or "")
            if not thread_id:
                raise AppServerError("App-server returned no probe thread ID")
            client.request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [
                        {
                            "type": "text",
                            "text": "Reply with exactly: bridge-probe-ok",
                        }
                    ],
                    "approvalPolicy": "never",
                    "sandboxPolicy": {
                        "type": "readOnly",
                        "networkAccess": False,
                    },
                },
            )
            notification = client.wait_notification("turn/completed")
            turn = notification.get("params", {}).get("turn") or {}
            if str(turn.get("status") or "") != "completed":
                return {
                    "ok": False,
                    "probeId": thread_id,
                    "errorCategory": "reported-failure",
                }
            return {
                "ok": True,
                "probeId": thread_id,
                "errorCategory": "",
            }
    except Exception as exc:
        return {
            "ok": False,
            "probeId": "",
            "errorCategory": _category_from_error(str(exc)),
        }


def _last_completed_turn(thread: dict) -> str:
    turns = thread.get("turns") or []
    completed = [
        str(turn.get("id"))
        for turn in turns
        if isinstance(turn, dict)
        and turn.get("id")
        and str(turn.get("status") or "") == "completed"
    ]
    return completed[-1] if completed else ""


def fork_thread_with_app_server(
    codex_home: Path,
    source_thread_id: str,
    provider: str,
    model: str,
    continue_prompt: str,
    timeout_seconds: int,
    command: list[str] | None = None,
) -> dict:
    with AppServerClient(
        codex_home=codex_home,
        timeout_seconds=timeout_seconds,
        command=command,
    ) as client:
        read_result = client.request(
            "thread/read",
            {"threadId": source_thread_id, "includeTurns": True},
        )
        source_thread = read_result.get("thread") or {}
        last_turn_id = _last_completed_turn(source_thread)
        params = {
            "threadId": source_thread_id,
            "modelProvider": provider,
            "model": model,
            "cwd": str(source_thread.get("cwd") or codex_home),
        }
        if last_turn_id:
            params["lastTurnId"] = last_turn_id
        forked = client.request("thread/fork", params)
        new_thread_id = str((forked.get("thread") or {}).get("id") or "")
        if not new_thread_id:
            raise AppServerError("App-server returned no forked thread ID")

        if continue_prompt:
            client.request(
                "turn/start",
                {
                    "threadId": new_thread_id,
                    "input": [{"type": "text", "text": continue_prompt}],
                    "model": model,
                    "approvalPolicy": "never",
                    "sandboxPolicy": {
                        "type": "readOnly",
                        "networkAccess": False,
                    },
                },
            )
            client.wait_notification("turn/completed")

        return {
            "newConversationId": new_thread_id,
            "lastTurnId": last_turn_id,
            "backend": "app-server",
        }
