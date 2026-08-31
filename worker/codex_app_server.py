from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any, Callable
import uuid

from shared.config import Settings
from shared.db import session_scope
from shared.logging import log_worker_event
from shared.models import CodexConversation, CodexSession, CodexTurn, CodexTurnItem
from shared.security import utc_now
from worker.codex_inputs import (
    OrderedInputEvent,
    hash_input_events,
    local_image_input_items_for_events,
    render_ordered_input_events_for_codex,
)
from worker.step_runner import PreparedStepRun, StepRunError


_APP_SERVER_STREAM_JOIN_SECONDS = 5.0
_APP_SERVER_STDERR_MAX_LINES = 200
_APP_SERVER_STDERR_MAX_CHARS = 32_000
_APP_SERVER_WRITE_ERROR_CODE = -32000
_APP_SERVER_PROTOCOL_TEXT_MAX_CHARS = 12_000

FailureStatus = str
NotificationCallback = Callable[[dict[str, Any]], None]


class CodexAppServerError(StepRunError):
    """Raised when the app-server protocol or transport fails."""

    def __init__(
        self,
        message: str,
        *,
        failure_status: FailureStatus = "failed",
        error_code: str | None = None,
        stderr_text: str | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_status = failure_status
        self.error_code = error_code
        self.stderr_text = stderr_text or ""


class CodexAppServerRejectedError(CodexAppServerError):
    def __init__(self, message: str, *, error_code: str | None = None, stderr_text: str | None = None) -> None:
        super().__init__(message, failure_status="rejected", error_code=error_code, stderr_text=stderr_text)


class CodexAppServerTimedOutError(CodexAppServerError):
    def __init__(self, message: str, *, stderr_text: str | None = None) -> None:
        super().__init__(message, failure_status="timed_out", error_code="timeout", stderr_text=stderr_text)


class CodexAppServerInterruptedError(CodexAppServerError):
    def __init__(self, message: str, *, stderr_text: str | None = None) -> None:
        super().__init__(message, failure_status="interrupted", error_code="interrupted", stderr_text=stderr_text)


class CodexAppServerAmbiguousError(CodexAppServerError):
    def __init__(self, message: str, *, error_code: str | None = None, stderr_text: str | None = None) -> None:
        super().__init__(message, failure_status="ambiguous", error_code=error_code, stderr_text=stderr_text)


@dataclass(frozen=True)
class CodexAppServerCommandSpec:
    command: list[str]
    env: dict[str, str]
    runtime_codex_home: Path


@dataclass(frozen=True)
class CodexAppServerThread:
    thread_id: str
    resumed: bool
    response: dict[str, Any]


@dataclass(frozen=True)
class CodexAppServerTurn:
    thread_id: str
    turn_id: str
    response: dict[str, Any]


@dataclass(frozen=True)
class CodexAppServerSteerReceipt:
    thread_id: str
    expected_turn_id: str
    acknowledged_turn_id: str
    rpc_request_id: str
    response: dict[str, Any]


@dataclass(frozen=True)
class CodexAppServerFailureClassification:
    status: FailureStatus
    error_code: str | None
    message: str
    stderr_text: str


@dataclass
class _PendingRequest:
    method: str
    event: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] | None = None


def build_codex_app_server_command(settings: Settings) -> CodexAppServerCommandSpec:
    runtime_codex_home = settings.resolved_codex_home
    command = [
        settings.codex_bin,
        "app-server",
        "--stdio",
        "--strict-config",
        "-c",
        'sandbox_mode="read-only"',
        "-c",
        'web_search="disabled"',
        "-c",
        "tools.web_search=false",
        "--disable",
        "web_search_request",
        "--disable",
        "standalone_web_search",
    ]
    env = os.environ.copy()
    env["CODEX_HOME"] = str(runtime_codex_home)
    if settings.codex_api_key:
        env["CODEX_API_KEY"] = settings.codex_api_key
    else:
        env.pop("CODEX_API_KEY", None)
    return CodexAppServerCommandSpec(
        command=command,
        env=env,
        runtime_codex_home=runtime_codex_home,
    )


def classify_app_server_failure(error: BaseException, *, stderr_text: str = "") -> CodexAppServerFailureClassification:
    if isinstance(error, CodexAppServerError):
        return CodexAppServerFailureClassification(
            status=error.failure_status,
            error_code=error.error_code,
            message=str(error),
            stderr_text=error.stderr_text or stderr_text,
        )
    return CodexAppServerFailureClassification(
        status="failed",
        error_code=error.__class__.__name__,
        message=str(error),
        stderr_text=stderr_text,
    )


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _bounded_protocol_value(value: Any, *, max_chars: int = _APP_SERVER_PROTOCOL_TEXT_MAX_CHARS) -> Any:
    if isinstance(value, str):
        if len(value) <= max_chars:
            return value
        return {
            "text_truncated": True,
            "original_length": len(value),
            "prefix": value[:max_chars],
        }
    if isinstance(value, list):
        return [_bounded_protocol_value(item, max_chars=max_chars) for item in value]
    if isinstance(value, dict):
        return {str(key): _bounded_protocol_value(item, max_chars=max_chars) for key, item in value.items()}
    return value


def _bounded_protocol_message(message: dict[str, Any]) -> dict[str, Any]:
    bounded = _bounded_protocol_value(message)
    return bounded if isinstance(bounded, dict) else {"raw_value": bounded}


def _coerce_user_input(input_payload: str | list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    if isinstance(input_payload, str):
        return [{"type": "text", "text": input_payload, "text_elements": []}]
    return [dict(item) for item in input_payload]


def _extract_thread_id(result: dict[str, Any]) -> str:
    thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
    thread_id = thread.get("id") or result.get("threadId") or result.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise CodexAppServerAmbiguousError("App-server response did not include a thread id.", error_code="missing_thread_id")
    return thread_id


def _extract_turn_id(result: dict[str, Any]) -> str:
    turn = result.get("turn") if isinstance(result.get("turn"), dict) else {}
    turn_id = turn.get("id") or result.get("turnId") or result.get("turn_id")
    if not isinstance(turn_id, str) or not turn_id.strip():
        raise CodexAppServerAmbiguousError("App-server response did not include a turn id.", error_code="missing_turn_id")
    return turn_id


def app_server_input_for_events(
    events: tuple[OrderedInputEvent, ...],
    *,
    trusted_attachment_root: Path | None = None,
    max_attachment_bytes: int | None = None,
) -> list[dict[str, Any]]:
    rendered = render_ordered_input_events_for_codex(
        events,
        trusted_attachment_root=trusted_attachment_root,
        max_attachment_bytes=max_attachment_bytes,
    )
    if not rendered:
        return []
    payload = {
        "kind": "autosac_ordered_input_delta",
        "input_hash": hash_input_events(events),
        "events": rendered,
    }
    items: list[dict[str, Any]] = [{"type": "text", "text": _json_dumps(payload), "text_elements": []}]
    items.extend(
        local_image_input_items_for_events(
            events,
            trusted_attachment_root=trusted_attachment_root,
            max_attachment_bytes=max_attachment_bytes,
        )
    )
    return items


def _server_request_rejection(method: str) -> dict[str, Any]:
    return {
        "code": _APP_SERVER_WRITE_ERROR_CODE,
        "message": f"AutoSac rejects unexpected app-server request {method}.",
    }


def _is_meaningful_protocol_item(method: str) -> bool:
    return (
        method in {
            "thread/started",
            "thread/status/changed",
            "turn/started",
            "turn/completed",
            "turn/diff/updated",
            "turn/plan/updated",
            "item/started",
            "item/completed",
            "rawResponseItem/completed",
            "rawResponse/completed",
            "item/agentMessage/delta",
            "item/reasoning/summaryTextDelta",
            "item/reasoning/summaryPartAdded",
            "item/reasoning/textDelta",
            "item/commandExecution/outputDelta",
            "item/commandExecution/terminalInteraction",
            "item/fileChange/outputDelta",
            "item/fileChange/patchUpdated",
            "error",
        }
        or method.startswith("command/")
        or method.startswith("process/")
    )


class CodexAppServerClient:
    """Run-scoped JSON-RPC stdio client for `codex app-server --stdio`."""

    def __init__(
        self,
        settings: Settings,
        *,
        command_spec: CodexAppServerCommandSpec | None = None,
        cwd: Path | None = None,
        response_timeout_seconds: float = 30.0,
        on_protocol_item: NotificationCallback | None = None,
        on_thread_id: Callable[[str], None] | None = None,
        on_turn_id: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self.command_spec = command_spec or build_codex_app_server_command(settings)
        self.cwd = cwd or settings.triage_workspace_dir
        self.response_timeout_seconds = response_timeout_seconds
        self.on_protocol_item = on_protocol_item
        self.on_thread_id = on_thread_id
        self.on_turn_id = on_turn_id
        self._process: subprocess.Popen | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._request_index = 0
        self._pending: dict[str, _PendingRequest] = {}
        self._stderr_lines: deque[str] = deque(maxlen=_APP_SERVER_STDERR_MAX_LINES)
        self._stderr_chars = 0
        self._fatal_error: CodexAppServerError | None = None
        self._closed = threading.Event()
        self._completed_turns: dict[str, dict[str, Any]] = {}
        self._completed_event = threading.Event()

    @property
    def process(self) -> subprocess.Popen | None:
        return self._process

    @property
    def stderr_text(self) -> str:
        text = "".join(self._stderr_lines)
        if len(text) <= _APP_SERVER_STDERR_MAX_CHARS:
            return text
        return text[-_APP_SERVER_STDERR_MAX_CHARS:]

    def start(self) -> None:
        if self._process is not None:
            return
        try:
            process = subprocess.Popen(
                self.command_spec.command,
                cwd=self.cwd,
                env=self.command_spec.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except OSError as exc:
            raise CodexAppServerError(f"Failed to launch codex app-server: {exc}", failure_status="failed") from exc
        self._process = process
        assert process.stdout is not None and process.stderr is not None
        self._stdout_thread = threading.Thread(
            target=self._stdout_reader,
            args=(process.stdout,),
            name=f"codex-app-server-stdout-{process.pid}",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._stderr_reader,
            args=(process.stderr,),
            name=f"codex-app-server-stderr-{process.pid}",
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    def initialize(self, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        self.start()
        response = self.request(
            "initialize",
            {
                "clientInfo": {"name": "autosac-worker", "version": "1"},
                "capabilities": {"notifications": {}},
            },
            timeout_seconds=timeout_seconds,
        )
        self.notify("initialized")
        return response

    def start_or_resume_thread(
        self,
        *,
        stored_thread_id: str | None,
        prepared: PreparedStepRun,
        timeout_seconds: float | None = None,
    ) -> CodexAppServerThread:
        if stored_thread_id:
            result = self.request(
                "thread/resume",
                self._thread_resume_params(stored_thread_id, prepared=prepared),
                timeout_seconds=timeout_seconds,
            )
            thread_id = _extract_thread_id(result)
            if thread_id != stored_thread_id:
                raise CodexAppServerAmbiguousError(
                    f"App-server resumed thread {thread_id}, expected {stored_thread_id}.",
                    error_code="thread_mismatch",
                )
            if self.on_thread_id:
                self.on_thread_id(thread_id)
            return CodexAppServerThread(thread_id=thread_id, resumed=True, response=result)
        result = self.request(
            "thread/start",
            self._thread_start_params(prepared=prepared),
            timeout_seconds=timeout_seconds,
        )
        thread_id = _extract_thread_id(result)
        if self.on_thread_id:
            self.on_thread_id(thread_id)
        return CodexAppServerThread(thread_id=thread_id, resumed=False, response=result)

    def start_turn(
        self,
        *,
        thread_id: str,
        input_payload: str | list[dict[str, Any]] | tuple[dict[str, Any], ...],
        prepared: PreparedStepRun,
        timeout_seconds: float | None = None,
    ) -> CodexAppServerTurn:
        user_input = _coerce_user_input(input_payload)
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": user_input,
            "cwd": str(self.settings.triage_workspace_dir),
            "approvalPolicy": "never",
            "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
        }
        if prepared.model_name:
            params["model"] = prepared.model_name
        if prepared.schema_json:
            params["outputSchema"] = json.loads(prepared.schema_json)
        result = self.request("turn/start", params, timeout_seconds=timeout_seconds)
        turn_id = _extract_turn_id(result)
        if self.on_turn_id:
            self.on_turn_id(turn_id)
        return CodexAppServerTurn(thread_id=thread_id, turn_id=turn_id, response=result)

    def steer_turn(
        self,
        *,
        thread_id: str,
        expected_turn_id: str,
        input_payload: str | list[dict[str, Any]] | tuple[dict[str, Any], ...],
        timeout_seconds: float | None = None,
    ) -> CodexAppServerSteerReceipt:
        user_input = _coerce_user_input(input_payload)
        params = {
            "threadId": thread_id,
            "input": user_input,
            "expectedTurnId": expected_turn_id,
        }
        rpc_id, result = self.request_with_id("turn/steer", params, timeout_seconds=timeout_seconds)
        acknowledged_turn_id = _extract_turn_id(result)
        if acknowledged_turn_id != expected_turn_id:
            raise CodexAppServerRejectedError(
                f"App-server accepted steer for turn {acknowledged_turn_id}, expected {expected_turn_id}.",
                error_code="expected_turn_mismatch",
            )
        return CodexAppServerSteerReceipt(
            thread_id=thread_id,
            expected_turn_id=expected_turn_id,
            acknowledged_turn_id=acknowledged_turn_id,
            rpc_request_id=rpc_id,
            response=result,
        )

    def interrupt_turn(self, *, thread_id: str, turn_id: str, timeout_seconds: float | None = None) -> dict[str, Any] | None:
        try:
            return self.request(
                "turn/interrupt",
                {"threadId": thread_id, "turnId": turn_id},
                timeout_seconds=timeout_seconds,
            )
        except CodexAppServerError:
            return None

    def supervise_until_completed(
        self,
        *,
        thread_id: str,
        turn_id: str,
        deadline: float,
        on_poll: Callable[[], None] | None = None,
        poll_interval_seconds: float = 0.05,
    ) -> dict[str, Any]:
        next_poll_at = time.monotonic()
        while True:
            self._raise_if_failed()
            completed = self._completed_turns.get(turn_id)
            if completed is not None:
                params = completed.get("params") if isinstance(completed.get("params"), dict) else {}
                if params.get("threadId") != thread_id:
                    raise CodexAppServerAmbiguousError(
                        f"turn/completed for {turn_id} had unexpected thread id {params.get('threadId')}.",
                        error_code="turn_completed_thread_mismatch",
                        stderr_text=self.stderr_text,
                    )
                turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                native_status = turn.get("status")
                if native_status == "interrupted":
                    raise CodexAppServerInterruptedError(
                        f"codex app-server turn {turn_id} was interrupted.",
                        stderr_text=self.stderr_text,
                    )
                if native_status == "failed":
                    error = turn.get("error") if isinstance(turn.get("error"), dict) else {}
                    raise CodexAppServerError(
                        str(error.get("message") or f"codex app-server turn {turn_id} failed."),
                        failure_status="failed",
                        error_code="turn_failed",
                        stderr_text=self.stderr_text,
                    )
                if native_status not in {None, "completed"}:
                    raise CodexAppServerAmbiguousError(
                        f"turn/completed for {turn_id} had unexpected native status {native_status}.",
                        error_code="unexpected_turn_status",
                        stderr_text=self.stderr_text,
                    )
                return completed
            if on_poll is not None and time.monotonic() >= next_poll_at:
                on_poll()
                next_poll_at = time.monotonic() + max(0.01, poll_interval_seconds)
            if self._process is not None and self._process.poll() is not None:
                raise CodexAppServerAmbiguousError(
                    f"codex app-server exited before turn/completed for {turn_id}.",
                    error_code="process_exited",
                    stderr_text=self.stderr_text,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.interrupt_turn(thread_id=thread_id, turn_id=turn_id, timeout_seconds=2.0)
                raise CodexAppServerTimedOutError(
                    f"Timed out waiting for turn/completed for {turn_id}.",
                    stderr_text=self.stderr_text,
                )
            self._completed_event.wait(timeout=min(0.05, remaining))
            self._completed_event.clear()

    def request(self, method: str, params: dict[str, Any] | None, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        _rpc_id, response = self.request_with_id(method, params, timeout_seconds=timeout_seconds)
        return response

    def request_with_id(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[str, dict[str, Any]]:
        self.start()
        rpc_id = self._next_request_id()
        pending = _PendingRequest(method=method)
        with self._pending_lock:
            self._pending[rpc_id] = pending
        try:
            self._record_outbound_item(method, params or {}, rpc_id=rpc_id)
            self._write_json({"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params or {}})
            self._wait_for_response(rpc_id, pending, timeout_seconds=timeout_seconds)
            assert pending.response is not None
            if "error" in pending.response:
                error = pending.response.get("error")
                error_dict = error if isinstance(error, dict) else {}
                message = str(error_dict.get("message") or f"App-server rejected {method}.")
                code = str(error_dict.get("code")) if error_dict.get("code") is not None else None
                raise CodexAppServerRejectedError(message, error_code=code, stderr_text=self.stderr_text)
            result = pending.response.get("result")
            if result is None:
                return rpc_id, {}
            if not isinstance(result, dict):
                raise CodexAppServerAmbiguousError(
                    f"Malformed app-server response for {method}: result must be an object.",
                    error_code="malformed_response",
                    stderr_text=self.stderr_text,
                )
            return rpc_id, result
        finally:
            with self._pending_lock:
                self._pending.pop(rpc_id, None)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.start()
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._record_outbound_item(method, params or {}, rpc_id=None)
        self._write_json(message)

    def close(self) -> None:
        cleanup_started = time.monotonic()
        pid = getattr(self._process, "pid", None)
        process_group_cleanup_attempted = False
        leader_returncode = None
        self._closed.set()
        process = self._process
        if process is not None:
            from worker.persistent_codex import _close_pipe, _terminate_process_group

            _close_pipe(getattr(process, "stdin", None))
            if process.poll() is None:
                process_group_cleanup_attempted = True
                leader_returncode = _terminate_process_group(process)
            else:
                leader_returncode = process.poll()
        for thread in (self._stdout_thread, self._stderr_thread):
            if thread is not None:
                thread.join(timeout=_APP_SERVER_STREAM_JOIN_SECONDS)
        cleanup_duration_ms = int((time.monotonic() - cleanup_started) * 1000)
        stdout_thread_alive = bool(self._stdout_thread and self._stdout_thread.is_alive())
        stderr_thread_alive = bool(self._stderr_thread and self._stderr_thread.is_alive())
        cleanup_payload = {
            "pid": pid,
            "process_group_cleanup_attempted": process_group_cleanup_attempted,
            "cleanup_duration_ms": cleanup_duration_ms,
            "leader_returncode": leader_returncode,
            "stdout_thread_alive": stdout_thread_alive,
            "stderr_thread_alive": stderr_thread_alive,
            "orphan_process_cleanup_evidence": process_group_cleanup_attempted,
        }
        log_worker_event("codex_app_server_transport_cleanup", **cleanup_payload)
        self._record_lifecycle_item("process/cleanup", cleanup_payload)

    def __enter__(self) -> CodexAppServerClient:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _thread_start_params(self, *, prepared: PreparedStepRun) -> dict[str, Any]:
        params: dict[str, Any] = {
            "cwd": str(self.settings.triage_workspace_dir),
            "approvalPolicy": "never",
            "sandbox": "read-only",
        }
        if prepared.model_name:
            params["model"] = prepared.model_name
        return params

    def _thread_resume_params(self, stored_thread_id: str, *, prepared: PreparedStepRun) -> dict[str, Any]:
        params = self._thread_start_params(prepared=prepared)
        params["threadId"] = stored_thread_id
        return params

    def _next_request_id(self) -> str:
        with self._pending_lock:
            self._request_index += 1
            return f"autosac-{self._request_index}"

    def _write_json(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise CodexAppServerError("codex app-server stdin is not available.", failure_status="failed")
        line = _json_dumps(message) + "\n"
        try:
            with self._write_lock:
                process.stdin.write(line)
                process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise CodexAppServerAmbiguousError(
                f"Failed to write to codex app-server stdin: {exc}",
                error_code="stdin_write_failed",
                stderr_text=self.stderr_text,
            ) from exc

    def _wait_for_response(
        self,
        rpc_id: str,
        pending: _PendingRequest,
        *,
        timeout_seconds: float | None,
    ) -> None:
        timeout = self.response_timeout_seconds if timeout_seconds is None else timeout_seconds
        deadline = time.monotonic() + timeout
        while not pending.event.is_set():
            self._raise_if_failed()
            if self._process is not None and self._process.poll() is not None:
                if self._stderr_thread is not None:
                    self._stderr_thread.join(timeout=0.2)
                raise CodexAppServerAmbiguousError(
                    f"codex app-server exited before response to {pending.method}.",
                    error_code="process_exited",
                    stderr_text=self.stderr_text,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexAppServerTimedOutError(
                    f"Timed out waiting for app-server response to {pending.method}.",
                    stderr_text=self.stderr_text,
                )
            pending.event.wait(timeout=min(0.05, remaining))
        self._raise_if_failed()

    def _raise_if_failed(self) -> None:
        if self._fatal_error is not None:
            raise self._fatal_error

    def _record_outbound_item(self, method: str, params: dict[str, Any], *, rpc_id: str | None) -> None:
        if self.on_protocol_item is None:
            return
        self.on_protocol_item(
            _bounded_protocol_message({
                "jsonrpc": "2.0",
                "direction": "client_to_server",
                "id": rpc_id,
                "method": method,
                "params": params,
            })
        )

    def _record_lifecycle_item(self, method: str, params: dict[str, Any]) -> None:
        if self.on_protocol_item is None:
            return
        try:
            self.on_protocol_item(
                _bounded_protocol_message(
                    {
                        "jsonrpc": "2.0",
                        "direction": "client_internal",
                        "method": method,
                        "params": params,
                    }
                )
            )
        except Exception:
            return

    def _stdout_reader(self, stdout) -> None:
        try:
            for line in stdout:
                if self._closed.is_set():
                    return
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    message = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    self._mark_fatal(
                        CodexAppServerAmbiguousError(
                            f"codex app-server emitted malformed JSON: {exc}",
                            error_code="malformed_json",
                            stderr_text=self.stderr_text,
                        )
                    )
                    return
                if not isinstance(message, dict):
                    self._mark_fatal(
                        CodexAppServerAmbiguousError(
                            "codex app-server emitted a non-object JSON-RPC message.",
                            error_code="malformed_jsonrpc",
                            stderr_text=self.stderr_text,
                        )
                    )
                    return
                self._handle_message(message)
        except Exception as exc:  # pragma: no cover - defensive transport boundary
            if not self._closed.is_set():
                self._mark_fatal(
                    CodexAppServerAmbiguousError(
                        f"codex app-server stdout reader failed: {exc}",
                        error_code="stdout_reader_failed",
                        stderr_text=self.stderr_text,
                    )
                )

    def _stderr_reader(self, stderr) -> None:
        try:
            for line in stderr:
                if self._closed.is_set():
                    return
                self._stderr_lines.append(line)
                self._stderr_chars += len(line)
                while self._stderr_chars > _APP_SERVER_STDERR_MAX_CHARS and self._stderr_lines:
                    removed = self._stderr_lines.popleft()
                    self._stderr_chars -= len(removed)
        except Exception:
            return

    def _handle_message(self, message: dict[str, Any]) -> None:
        if "id" in message and "method" not in message:
            rpc_id = str(message.get("id"))
            with self._pending_lock:
                pending = self._pending.get(rpc_id)
            if pending is None:
                self._mark_fatal(
                    CodexAppServerAmbiguousError(
                        f"Received app-server response for unknown request id {rpc_id}.",
                        error_code="unknown_response_id",
                        stderr_text=self.stderr_text,
                    )
                )
                return
            pending.response = message
            pending.event.set()
            return
        method = message.get("method")
        if not isinstance(method, str) or not method:
            self._mark_fatal(
                CodexAppServerAmbiguousError(
                    "Received app-server message without a method or response id.",
                    error_code="malformed_jsonrpc",
                    stderr_text=self.stderr_text,
                )
            )
            return
        if "id" in message:
            self._reject_server_request(message, method=method)
            return
        self._handle_notification(message, method=method)

    def _reject_server_request(self, message: dict[str, Any], *, method: str) -> None:
        rpc_id = message.get("id")
        try:
            self._write_json({"jsonrpc": "2.0", "id": rpc_id, "error": _server_request_rejection(method)})
        except CodexAppServerError:
            pass
        self._mark_fatal(
            CodexAppServerRejectedError(
                f"Unexpected interactive app-server request rejected: {method}.",
                error_code=method,
                stderr_text=self.stderr_text,
            )
        )

    def _handle_notification(self, message: dict[str, Any], *, method: str) -> None:
        if self.on_protocol_item is not None and _is_meaningful_protocol_item(method):
            self.on_protocol_item(_bounded_protocol_message(message))
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method == "thread/started":
            thread = params.get("thread") if isinstance(params.get("thread"), dict) else {}
            thread_id = thread.get("id")
            if isinstance(thread_id, str) and thread_id.strip() and self.on_thread_id is not None:
                self.on_thread_id(thread_id)
        elif method == "turn/started":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            turn_id = turn.get("id")
            if isinstance(turn_id, str) and turn_id.strip() and self.on_turn_id is not None:
                self.on_turn_id(turn_id)
        elif method == "turn/completed":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            turn_id = turn.get("id")
            if isinstance(turn_id, str) and turn_id.strip():
                self._completed_turns[turn_id] = message
                self._completed_event.set()
        elif method == "error":
            error = params.get("error") if isinstance(params.get("error"), dict) else {}
            message_text = str(error.get("message") or "codex app-server emitted an error notification.")
            self._mark_fatal(
                CodexAppServerAmbiguousError(
                    message_text,
                    error_code="error_notification",
                    stderr_text=self.stderr_text,
                )
            )

    def _mark_fatal(self, error: CodexAppServerError) -> None:
        if self._fatal_error is None:
            self._fatal_error = error
        with self._pending_lock:
            pending_requests = list(self._pending.values())
        for pending in pending_requests:
            pending.event.set()
        self._completed_event.set()


@dataclass
class CodexAppServerTurnPersistence:
    settings: Settings
    prepared: PreparedStepRun
    step_id: uuid.UUID
    turn_id: uuid.UUID
    session_id: uuid.UUID
    conversation_id: uuid.UUID
    next_item_index: int = 1
    lock: threading.Lock = field(default_factory=threading.Lock)

    def persist_protocol_item(self, message: dict[str, Any]) -> None:
        with self.lock:
            item_index = self.next_item_index
            self.next_item_index += 1
        method = str(message.get("method") or message.get("item_kind") or "protocol_item")
        codex_item_id = _extract_codex_item_id(message)
        bounded_message = _bounded_protocol_message(message)
        with session_scope(self.settings) as db:
            from worker.persistent_codex import _load_locked_owned_runtime_records
            from worker.run_ownership import RunOwnershipLost

            try:
                run, session, turn, step = _load_locked_owned_runtime_records(
                    db,
                    prepared=self.prepared,
                    persistent=self,
                )
            except RunOwnershipLost:
                self._persist_retired_protocol_item(
                    db,
                    item_index=item_index,
                    method=method,
                    codex_item_id=codex_item_id,
                    message=bounded_message,
                )
                return
            db.add(
                CodexTurnItem(
                    turn_id=turn.id,
                    item_index=item_index,
                    item_kind=method,
                    codex_item_id=codex_item_id,
                    payload_json=bounded_message,
                )
            )
            now = utc_now()
            if method == "thread/started":
                native_thread_id = _extract_thread_id_from_protocol_message(message)
                if native_thread_id:
                    _persist_thread_id(db, session=session, turn=turn, conversation_id=self.conversation_id, thread_id=native_thread_id)
            elif method == "turn/started":
                native_turn_id = _extract_turn_id_from_protocol_message(message)
                if native_turn_id:
                    turn.native_turn_id = native_turn_id
                    if turn.accepted_at is None:
                        turn.accepted_at = now
            elif method == "turn/completed":
                native_turn_id = _extract_turn_id_from_protocol_message(message)
                if native_turn_id and turn.native_turn_id is None:
                    turn.native_turn_id = native_turn_id
                turn.steering_closed_at = turn.steering_closed_at or now
            run.last_heartbeat_at = now
            session.lease_heartbeat_at = now
            session.lease_expires_at = now + self._lease_delta()
            step.ended_at = None

    def _persist_retired_protocol_item(
        self,
        db,
        *,
        item_index: int,
        method: str,
        codex_item_id: str | None,
        message: dict[str, Any],
    ) -> None:
        turn = db.get(CodexTurn, self.turn_id)
        session = db.get(CodexSession, self.session_id)
        if turn is None or session is None:
            raise StepRunError("Late app-server protocol item could not be linked to its retired persistent turn.")
        if getattr(turn, "status", None) in {"prepared", "running"} and getattr(session, "ended_at", None) is None:
            raise StepRunError("Late app-server protocol item arrived for a still-active persistent turn without lease ownership.")
        marked_message = {
            **message,
            "autosac_recovery": {
                "late_output_from_retired_session": True,
                "publishable": False,
                "session_status": getattr(session, "status", None),
                "turn_status": getattr(turn, "status", None),
            },
        }
        db.add(
            CodexTurnItem(
                turn_id=turn.id,
                item_index=item_index,
                item_kind=method,
                codex_item_id=codex_item_id,
                payload_json=marked_message,
            )
        )

    def persist_thread_id(self, thread_id: str) -> None:
        with session_scope(self.settings) as db:
            from worker.persistent_codex import _load_locked_owned_runtime_records

            _run, session, turn, _step = _load_locked_owned_runtime_records(
                db,
                prepared=self.prepared,
                persistent=self,
            )
            _persist_thread_id(db, session=session, turn=turn, conversation_id=self.conversation_id, thread_id=thread_id)

    def persist_turn_id(self, native_turn_id: str) -> None:
        with session_scope(self.settings) as db:
            from worker.persistent_codex import _load_locked_owned_runtime_records

            _run, _session, turn, _step = _load_locked_owned_runtime_records(
                db,
                prepared=self.prepared,
                persistent=self,
            )
            turn.native_turn_id = native_turn_id
            turn.accepted_at = turn.accepted_at or utc_now()

    def _lease_delta(self):
        from datetime import timedelta

        return timedelta(seconds=self.settings.ai_run_stale_timeout_seconds)


def _extract_codex_item_id(message: dict[str, Any]) -> str | None:
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    for key in ("item", "threadItem", "rawItem"):
        item = params.get(key)
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            return item["id"]
    for key in ("itemId", "item_id"):
        item_id = params.get(key) or message.get(key)
        if isinstance(item_id, str):
            return item_id
    return None


def _extract_thread_id_from_protocol_message(message: dict[str, Any]) -> str | None:
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    thread = params.get("thread") if isinstance(params.get("thread"), dict) else {}
    value = thread.get("id") or params.get("threadId") or params.get("thread_id")
    return value if isinstance(value, str) and value.strip() else None


def _extract_turn_id_from_protocol_message(message: dict[str, Any]) -> str | None:
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
    value = turn.get("id") or params.get("turnId") or params.get("turn_id")
    return value if isinstance(value, str) and value.strip() else None


def _persist_thread_id(
    db,
    *,
    session: CodexSession,
    turn,
    conversation_id: uuid.UUID,
    thread_id: str,
) -> None:
    if session.thread_id is None:
        session.thread_id = thread_id
    elif session.thread_id != thread_id:
        conversation = db.get(CodexConversation, conversation_id)
        if conversation is not None:
            conversation.status = "recovery_required"
        raise CodexAppServerAmbiguousError(
            f"Persistent session {session.id} thread id changed from {session.thread_id} to {thread_id}.",
            error_code="thread_mismatch",
        )
    session.status = "active"
    session.started_at = session.started_at or utc_now()
    turn.transport_kind = "app_server"
