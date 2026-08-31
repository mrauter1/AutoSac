from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
from types import SimpleNamespace
import uuid

import pytest

from shared.config import Settings


def _make_settings(tmp_path: Path, *, codex_bin: str = "codex") -> Settings:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        app_base_url="http://localhost:8000",
        app_secret_key="test-secret",
        database_url="postgresql+psycopg://triage:triage@localhost:5432/triage",
        uploads_dir=workspace_dir / "attachments_store",
        triage_workspace_dir=workspace_dir,
        repo_mount_dir=workspace_dir / "app",
        manuals_mount_dir=workspace_dir / "manuals",
        codex_bin=codex_bin,
        codex_api_key="test-key",
        default_codex_model="gpt-test",
        default_codex_effort="medium",
        codex_timeout_seconds=3600,
        worker_poll_seconds=10,
        auto_support_reply_min_confidence=0.85,
        auto_confirm_intent_min_confidence=0.90,
        max_images_per_message=3,
        max_image_bytes=5 * 1024 * 1024,
        session_default_hours=12,
        session_remember_days=30,
        codex_conversations_enabled=True,
        codex_app_server_specialist_transport_enabled=True,
        codex_home=workspace_dir / ".codex",
    )


def _prepared(tmp_path: Path):
    schema = {
        "type": "object",
        "properties": {"summary_internal": {"type": "string"}},
        "required": ["summary_internal"],
    }
    return SimpleNamespace(
        model_name="gpt-test",
        reasoning_effort="medium",
        schema_json=json.dumps(schema),
    )


def _command_for_script(tmp_path: Path, script: str, *args: str):
    script_path = tmp_path / f"fake_app_server_{uuid.uuid4().hex}.py"
    script_path.write_text(script, encoding="utf-8")
    env = os.environ.copy()
    env["CODEX_HOME"] = str(tmp_path / "codex-home")
    return [sys.executable, str(script_path), *args], env


_FAKE_SERVER = r'''
from __future__ import annotations
import json
import os
import signal
import subprocess
import sys
import time

mode = sys.argv[1]
log_path = sys.argv[2]
child_pid_path = sys.argv[3] if len(sys.argv) > 3 else None

def write(payload):
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()

def log(payload):
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")

def read_message():
    line = sys.stdin.readline()
    if not line:
        return None
    payload = json.loads(line)
    log(payload)
    return payload

if mode == "malformed":
    print("{not-json", flush=True)
    time.sleep(1)
    sys.exit(0)

if mode == "loss":
    sys.stderr.write("fatal app-server loss\n")
    sys.stderr.flush()
    sys.exit(7)

child = None
if mode == "timeout":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    if child_pid_path:
        with open(child_pid_path, "w", encoding="utf-8") as handle:
            handle.write(str(child.pid))

while True:
    message = read_message()
    if message is None:
        break
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialized":
        continue
    if method == "initialize":
        write({"jsonrpc": "2.0", "id": request_id, "result": {"userAgent": "fake", "codexHome": os.environ.get("CODEX_HOME"), "platformFamily": "unix", "platformOs": "linux"}})
    elif method == "server/diagnostics":
        if message.get("params", {}).get("name") == "slow":
            time.sleep(0.15)
        write({"jsonrpc": "2.0", "id": request_id, "result": {"name": message.get("params", {}).get("name")}})
    elif method == "thread/start":
        gated = {"historyMode", "experimentalRawEvents", "excludeTurns"} & set(message.get("params", {}))
        if gated:
            write({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "gated fields require experimentalApi capability: " + ",".join(sorted(gated))}})
            continue
        write({"jsonrpc": "2.0", "method": "thread/started", "params": {"thread": {"id": "thread-new"}}})
        write({"jsonrpc": "2.0", "id": request_id, "result": {"thread": {"id": "thread-new"}}})
    elif method == "thread/resume":
        gated = {"historyMode", "experimentalRawEvents", "excludeTurns"} & set(message.get("params", {}))
        if gated:
            write({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "gated fields require experimentalApi capability: " + ",".join(sorted(gated))}})
            continue
        write({"jsonrpc": "2.0", "id": request_id, "result": {"thread": {"id": message["params"]["threadId"]}}})
    elif method == "turn/start":
        if mode == "approval":
            write({"jsonrpc": "2.0", "id": "server-approval-1", "method": "item/commandExecution/requestApproval", "params": {"threadId": message["params"]["threadId"], "turnId": "turn-1", "itemId": "item-1", "approvalId": None, "command": ["echo", "x"], "cwd": ".", "reason": None, "parsedCmd": []}})
            read_message()
        if mode == "interactive":
            write({"jsonrpc": "2.0", "id": "server-interactive-1", "method": "item/commandExecution/terminalInteraction", "params": {"threadId": message["params"]["threadId"], "turnId": "turn-1", "itemId": "item-1", "input": "continue"}})
            read_message()
        write({"jsonrpc": "2.0", "method": "turn/started", "params": {"threadId": message["params"]["threadId"], "turn": {"id": "turn-1", "items": [], "itemsView": "all", "status": "inProgress", "error": None, "startedAt": 1, "completedAt": None, "durationMs": None}}})
        write({"jsonrpc": "2.0", "method": "item/agentMessage/delta", "params": {"threadId": message["params"]["threadId"], "turnId": "turn-1", "itemId": "agent-early", "delta": "{\"summary_internal\":\"too early\"}"}})
        write({"jsonrpc": "2.0", "id": request_id, "result": {"turn": {"id": "turn-1"}}})
        if mode == "interrupted":
            write({"jsonrpc": "2.0", "method": "turn/completed", "params": {"threadId": message["params"]["threadId"], "turn": {"id": "turn-1", "items": [], "itemsView": "all", "status": "interrupted", "error": None, "startedAt": 1, "completedAt": 2, "durationMs": 1000}}})
        if mode == "timeout":
            while True:
                later = read_message()
                if later is None:
                    break
                if later.get("method") == "turn/interrupt":
                    write({"jsonrpc": "2.0", "id": later.get("id"), "result": {}})
                    break
            time.sleep(60)
    elif method == "turn/steer":
        write({"jsonrpc": "2.0", "id": request_id, "result": {"turnId": message["params"]["expectedTurnId"]}})
        if mode == "ok":
            write({"jsonrpc": "2.0", "method": "turn/completed", "params": {"threadId": message["params"]["threadId"], "turn": {"id": message["params"]["expectedTurnId"], "items": [], "itemsView": "all", "status": "completed", "error": None, "startedAt": 1, "completedAt": 2, "durationMs": 1000}}})
    else:
        write({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "unknown"}})
'''


def _read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _wait_for_log_entry(path: Path, predicate, *, timeout_seconds: float = 1.0) -> list[dict]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        entries = [entry for entry in _read_log(path) if predicate(entry)]
        if entries:
            return entries
        if time.monotonic() >= deadline:
            return entries
        time.sleep(0.02)


def _client(tmp_path: Path, mode: str, *, child_pid_path: Path | None = None, items=None):
    from worker.codex_app_server import CodexAppServerClient, CodexAppServerCommandSpec

    log_path = tmp_path / f"{mode}.jsonl"
    args = [mode, str(log_path)]
    if child_pid_path is not None:
        args.append(str(child_pid_path))
    command, env = _command_for_script(tmp_path, _FAKE_SERVER, *args)
    settings = _make_settings(tmp_path)
    client = CodexAppServerClient(
        settings,
        command_spec=CodexAppServerCommandSpec(
            command=command,
            env=env,
            runtime_codex_home=Path(env["CODEX_HOME"]),
        ),
        response_timeout_seconds=1.0,
        on_protocol_item=items.append if items is not None else None,
    )
    return client, log_path


def _bundle_attachment(path: Path, *, is_image: bool, size_bytes: int | None = None, mime_type: str | None = None):
    return {
        "attachment_id": str(uuid.uuid4()),
        "message_id": str(uuid.uuid4()),
        "visibility": "public",
        "original_filename": path.name,
        "mime_type": mime_type or ("image/png" if is_image else "application/pdf"),
        "sha256": "sha-test",
        "size_bytes": path.stat().st_size if size_bytes is None and path.exists() else size_bytes,
        "width": 10 if is_image else None,
        "height": 10 if is_image else None,
        "representation_status": "supported",
        "representation_errors": (),
        "safe_input": {
            "kind": "file_path",
            "stored_path": str(path),
            "is_image": is_image,
        },
    }


def _bundle_event(*attachments: dict):
    event_id = uuid.uuid4()
    return SimpleNamespace(
        event_kind="ticket_message",
        source_kind="ticket_message",
        source_id=event_id,
        dedupe_key=f"ticket-message:{event_id}",
        payload_json={
            "message_id": str(event_id),
            "dedupe_key": f"ticket-message:{event_id}",
            "ticket_id": str(uuid.uuid4()),
            "author_type": "requester",
            "visibility": "public",
            "source": "requester_reply",
            "body_text": "hello",
            "body": {"text": "hello", "markdown": "hello"},
            "attachments": attachments,
            "bundle": {
                "logical_input": "ticket_message_with_attachments",
                "attachment_count": len(attachments),
                "representation_status": "supported",
                "representation_errors": (),
            },
            "causal": {"ai_run_id": None, "codex_turn_outcome_id": None},
        },
        order_key=(2, str(event_id)),
    )


def test_app_server_client_initializes_starts_threads_turns_steers_correlates_and_fences_completion(tmp_path):
    from worker.codex_app_server import app_server_input_for_events

    items: list[dict] = []
    client, log_path = _client(tmp_path, "ok", items=items)
    try:
        init = client.initialize()
        assert init["userAgent"] == "fake"
        thread = client.start_or_resume_thread(
            stored_thread_id=None,
            prepared=_prepared(tmp_path),
        )
        turn = client.start_turn(
            thread_id=thread.thread_id,
            input_payload="Initial specialist prompt",
            prepared=_prepared(tmp_path),
        )
        time.sleep(0.05)
        assert any(item["method"] == "item/agentMessage/delta" for item in items)
        assert not any(item["method"] == "turn/completed" for item in items)
        steer = client.steer_turn(
            thread_id=thread.thread_id,
            expected_turn_id=turn.turn_id,
            input_payload="Steered content",
        )
        completed = client.supervise_until_completed(
            thread_id=thread.thread_id,
            turn_id=turn.turn_id,
            deadline=time.monotonic() + 1.0,
        )
        assert steer.acknowledged_turn_id == turn.turn_id
        assert completed["method"] == "turn/completed"
        assert any(item["method"] == "turn/completed" for item in items)
        log = _read_log(log_path)
        assert [entry.get("method") for entry in log[:3]] == ["initialize", "initialized", "thread/start"]
        thread_start = next(entry for entry in log if entry.get("method") == "thread/start")
        assert not ({"historyMode", "experimentalRawEvents", "excludeTurns"} & set(thread_start["params"]))
        assert thread_start["params"]["model"] == "gpt-test"
        turn_start = next(entry for entry in log if entry.get("method") == "turn/start")
        assert turn_start["params"]["model"] == "gpt-test"
        assert turn_start["params"]["effort"] == "medium"
        assert turn_start["params"]["outputSchema"]["type"] == "object"
        steer_requests = [entry for entry in log if entry.get("method") == "turn/steer"]
        assert len(steer_requests) == 1
        assert set(steer_requests[0]["params"]) == {"expectedTurnId", "input", "threadId"}

        event_id = uuid.uuid4()
        rendered = app_server_input_for_events(
            (
                SimpleNamespace(
                    event_kind="ticket_message",
                    source_kind="ticket_message",
                    source_id=event_id,
                    dedupe_key=f"ticket-message:{event_id}",
                    payload_json={
                        "bundle": {"representation_status": "supported", "representation_errors": ()},
                        "attachments": (),
                        "body_text": "hello",
                    },
                ),
            )
        )
        assert rendered[0]["type"] == "text"
        assert "autosac_ordered_input_delta" in rendered[0]["text"]
    finally:
        client.close()
        assert client.process is not None
        assert client.process.poll() is not None


def test_app_server_input_for_events_emits_text_envelope_and_native_images(tmp_path):
    from worker.codex_app_server import app_server_input_for_events

    settings = _make_settings(tmp_path)
    ticket_dir = settings.uploads_dir / "ticket-1"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    image_path = ticket_dir / "shot.png"
    image_path.write_bytes(b"image-bytes")
    document_path = ticket_dir / "notes.pdf"
    document_path.write_bytes(b"pdf-bytes")

    rendered = app_server_input_for_events(
        (
            _bundle_event(
                _bundle_attachment(image_path, is_image=True),
                _bundle_attachment(document_path, is_image=False, mime_type="application/pdf"),
            ),
        ),
        trusted_attachment_root=settings.uploads_dir,
        max_attachment_bytes=settings.max_image_bytes,
    )

    assert [item["type"] for item in rendered] == ["text", "localImage"]
    assert rendered[1]["path"] == str(image_path.resolve())
    envelope = json.loads(rendered[0]["text"])
    assert envelope["kind"] == "autosac_ordered_input_delta"
    assert envelope["events"][0]["attachments"][0]["safe_input"]["stored_path"] == str(image_path)
    assert envelope["events"][0]["attachments"][1]["safe_input"]["stored_path"] == str(document_path)


@pytest.mark.parametrize(
    ("label", "attachment_factory", "error_match"),
    [
        (
            "missing",
            lambda uploads_dir, outside_dir: _bundle_attachment(
                uploads_dir / "ticket-missing" / "missing.png",
                is_image=True,
                size_bytes=5,
            ),
            "unavailable",
        ),
        (
            "escaped_symlink",
            lambda uploads_dir, outside_dir: _bundle_attachment(
                (uploads_dir / "ticket-2" / "escaped.png"),
                is_image=True,
            ),
            "trusted upload boundary",
        ),
        (
            "oversize",
            lambda uploads_dir, outside_dir: _bundle_attachment(
                uploads_dir / "ticket-3" / "large.pdf",
                is_image=False,
                size_bytes=32,
                mime_type="application/pdf",
            ),
            "size limit",
        ),
    ],
)
def test_app_server_input_for_events_rejects_invalid_attachment_bundles(tmp_path, label, attachment_factory, error_match):
    from worker.codex_app_server import app_server_input_for_events
    from worker.codex_inputs import UnsupportedInputBundleError

    settings = _make_settings(tmp_path)
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir(parents=True, exist_ok=True)

    if label == "missing":
        (settings.uploads_dir / "ticket-missing").mkdir(parents=True, exist_ok=True)
    elif label == "escaped_symlink":
        target = outside_dir / "escaped.png"
        target.write_bytes(b"escape")
        escaped = settings.uploads_dir / "ticket-2" / "escaped.png"
        escaped.parent.mkdir(parents=True, exist_ok=True)
        escaped.symlink_to(target)
    elif label == "oversize":
        large = settings.uploads_dir / "ticket-3" / "large.pdf"
        large.parent.mkdir(parents=True, exist_ok=True)
        large.write_bytes(b"x" * 32)

    event = _bundle_event(attachment_factory(settings.uploads_dir, outside_dir))

    with pytest.raises(UnsupportedInputBundleError, match=error_match):
        app_server_input_for_events(
            (event,),
            trusted_attachment_root=settings.uploads_dir,
            max_attachment_bytes=16,
        )


def test_app_server_input_for_events_rejects_unreadable_and_mixed_invalid_bundle(tmp_path, monkeypatch):
    from worker.codex_app_server import app_server_input_for_events
    from worker.codex_inputs import UnsupportedInputBundleError

    settings = _make_settings(tmp_path)
    ticket_dir = settings.uploads_dir / "ticket-4"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    good_image = ticket_dir / "good.png"
    good_image.write_bytes(b"good-image")
    unreadable_document = ticket_dir / "locked.pdf"
    unreadable_document.write_bytes(b"locked")

    original_open = Path.open

    def fake_open(self, *args, **kwargs):
        if self.resolve(strict=False) == unreadable_document.resolve(strict=False):
            raise OSError("permission denied")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fake_open)
    event = _bundle_event(
        _bundle_attachment(good_image, is_image=True),
        _bundle_attachment(unreadable_document, is_image=False, mime_type="application/pdf"),
    )

    with pytest.raises(UnsupportedInputBundleError, match="unavailable"):
        app_server_input_for_events(
            (event,),
            trusted_attachment_root=settings.uploads_dir,
            max_attachment_bytes=settings.max_image_bytes,
        )


def test_app_server_client_resumes_stored_thread(tmp_path):
    client, log_path = _client(tmp_path, "ok")
    try:
        client.initialize()
        thread = client.start_or_resume_thread(
            stored_thread_id="thread-existing",
            prepared=_prepared(tmp_path),
        )
        client.start_turn(
            thread_id=thread.thread_id,
            input_payload="Resume with pinned settings",
            prepared=_prepared(tmp_path),
        )
        assert thread.thread_id == "thread-existing"
        assert thread.resumed is True
        log = _read_log(log_path)
        methods = [entry.get("method") for entry in log]
        assert "thread/resume" in methods
        assert "thread/fork" not in methods
        thread_resume = next(entry for entry in log if entry.get("method") == "thread/resume")
        assert not ({"historyMode", "experimentalRawEvents", "excludeTurns"} & set(thread_resume["params"]))
        assert thread_resume["params"]["threadId"] == "thread-existing"
        assert thread_resume["params"]["model"] == "gpt-test"
        turn_start = next(entry for entry in log if entry.get("method") == "turn/start")
        assert turn_start["params"]["threadId"] == "thread-existing"
        assert turn_start["params"]["model"] == "gpt-test"
        assert turn_start["params"]["effort"] == "medium"
    finally:
        client.close()


def test_app_server_client_correlates_out_of_order_responses(tmp_path):
    client, _log_path = _client(tmp_path, "ok")
    try:
        client.initialize()
        results = {}

        def call(name):
            results[name] = client.request("server/diagnostics", {"name": name})

        slow = threading.Thread(target=call, args=("slow",))
        fast = threading.Thread(target=call, args=("fast",))
        slow.start()
        fast.start()
        slow.join(timeout=1.0)
        fast.join(timeout=1.0)
        assert results["slow"] == {"name": "slow"}
        assert results["fast"] == {"name": "fast"}
    finally:
        client.close()


def test_app_server_client_rejects_unexpected_approval_request(tmp_path):
    from worker.codex_app_server import CodexAppServerRejectedError

    client, log_path = _client(tmp_path, "approval")
    try:
        client.initialize()
        thread = client.start_or_resume_thread(stored_thread_id=None, prepared=_prepared(tmp_path))
        with pytest.raises(CodexAppServerRejectedError):
            client.start_turn(thread_id=thread.thread_id, input_payload="prompt", prepared=_prepared(tmp_path))
        rejection = _wait_for_log_entry(
            log_path,
            lambda entry: entry.get("id") == "server-approval-1" and "error" in entry,
        )
        assert rejection
    finally:
        client.close()


def test_app_server_client_rejects_unexpected_interactive_request(tmp_path):
    from worker.codex_app_server import CodexAppServerRejectedError

    client, log_path = _client(tmp_path, "interactive")
    try:
        client.initialize()
        thread = client.start_or_resume_thread(stored_thread_id=None, prepared=_prepared(tmp_path))
        with pytest.raises(CodexAppServerRejectedError):
            client.start_turn(thread_id=thread.thread_id, input_payload="prompt", prepared=_prepared(tmp_path))
        rejection = _wait_for_log_entry(
            log_path,
            lambda entry: entry.get("id") == "server-interactive-1" and "error" in entry,
        )
        assert rejection
    finally:
        client.close()
        assert client.process is not None
        assert client.process.poll() is not None


def test_app_server_client_malformed_output_is_ambiguous_and_process_is_cleaned(tmp_path):
    from worker.codex_app_server import CodexAppServerAmbiguousError

    client, _log_path = _client(tmp_path, "malformed")
    with pytest.raises(CodexAppServerAmbiguousError) as error:
        client.initialize(timeout_seconds=0.5)
    assert error.value.failure_status == "ambiguous"
    client.close()
    assert client.process is not None
    assert client.process.poll() is not None


def test_app_server_client_process_loss_captures_stderr_and_classifies_ambiguous(tmp_path):
    from worker.codex_app_server import CodexAppServerAmbiguousError

    client, _log_path = _client(tmp_path, "loss")
    with pytest.raises(CodexAppServerAmbiguousError) as error:
        client.initialize(timeout_seconds=0.5)
    assert error.value.failure_status == "ambiguous"
    assert "fatal app-server loss" in error.value.stderr_text
    client.close()
    assert client.process is not None
    assert client.process.poll() is not None


def test_app_server_client_timeout_interrupts_and_cleans_process_group(tmp_path):
    from worker.codex_app_server import CodexAppServerTimedOutError

    child_pid_path = tmp_path / "child.pid"
    client, log_path = _client(tmp_path, "timeout", child_pid_path=child_pid_path)
    try:
        client.initialize()
        thread = client.start_or_resume_thread(stored_thread_id=None, prepared=_prepared(tmp_path))
        turn = client.start_turn(thread_id=thread.thread_id, input_payload="prompt", prepared=_prepared(tmp_path))
        with pytest.raises(CodexAppServerTimedOutError) as error:
            client.supervise_until_completed(
                thread_id=thread.thread_id,
                turn_id=turn.turn_id,
                deadline=time.monotonic() + 0.1,
            )
        assert error.value.failure_status == "timed_out"
        assert any(entry.get("method") == "turn/interrupt" for entry in _read_log(log_path))
    finally:
        client.close()
    assert client.process is not None
    assert client.process.poll() is not None
    if child_pid_path.exists():
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        for _ in range(20):
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            os.kill(child_pid, signal.SIGKILL)
            raise AssertionError(f"child process {child_pid} remained live after cleanup")


def test_app_server_client_interrupted_completion_classifies_and_cleans_process(tmp_path):
    from worker.codex_app_server import CodexAppServerInterruptedError

    client, _log_path = _client(tmp_path, "interrupted")
    try:
        client.initialize()
        thread = client.start_or_resume_thread(stored_thread_id=None, prepared=_prepared(tmp_path))
        turn = client.start_turn(thread_id=thread.thread_id, input_payload="prompt", prepared=_prepared(tmp_path))
        with pytest.raises(CodexAppServerInterruptedError) as error:
            client.supervise_until_completed(
                thread_id=thread.thread_id,
                turn_id=turn.turn_id,
                deadline=time.monotonic() + 1.0,
            )
        assert error.value.failure_status == "interrupted"
    finally:
        client.close()
        assert client.process is not None
        assert client.process.poll() is not None


def test_app_server_turn_persistence_stores_items_and_updates_thread_and_turn(monkeypatch, tmp_path):
    from worker import codex_app_server
    from worker.codex_app_server import CodexAppServerTurnPersistence

    added = []
    run = SimpleNamespace(id=uuid.uuid4(), last_heartbeat_at=None)
    session = SimpleNamespace(
        id=uuid.uuid4(),
        thread_id=None,
        status="pending",
        started_at=None,
        lease_heartbeat_at=None,
        lease_expires_at=None,
    )
    turn = SimpleNamespace(
        id=uuid.uuid4(),
        native_turn_id=None,
        accepted_at=None,
        transport_kind="exec",
        steering_closed_at=None,
    )
    step = SimpleNamespace(id=uuid.uuid4(), ended_at=None)
    conversation = SimpleNamespace(id=uuid.uuid4(), status="active")

    class Db:
        def add(self, item):
            added.append(item)

        def get(self, model, key):
            return conversation

    @contextmanager
    def fake_scope(_settings):
        yield Db()

    monkeypatch.setattr(codex_app_server, "session_scope", fake_scope)

    import worker.persistent_codex as persistent_codex

    observed_runtime_identities = []

    def load_runtime(db, prepared, persistent):
        observed_runtime_identities.append(
            (persistent.step_id, persistent.turn_id, persistent.session_id, persistent.conversation_id)
        )
        return run, session, turn, step

    monkeypatch.setattr(persistent_codex, "_load_locked_owned_runtime_records", load_runtime)

    persistence = CodexAppServerTurnPersistence(
        settings=_make_settings(tmp_path),
        prepared=SimpleNamespace(run_id=run.id, worker_instance_id="worker-1"),
        step_id=step.id,
        turn_id=turn.id,
        session_id=session.id,
        conversation_id=conversation.id,
    )
    persistence.persist_protocol_item(
        {"method": "thread/started", "params": {"thread": {"id": "thread-1"}}}
    )
    persistence.persist_protocol_item(
        {"method": "turn/started", "params": {"threadId": "thread-1", "turn": {"id": "turn-1"}}}
    )
    persistence.persist_protocol_item(
        {"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {"id": "turn-1"}}}
    )
    persistence.persist_thread_id("thread-1")
    persistence.persist_turn_id("turn-1")

    assert [item.item_kind for item in added] == ["thread/started", "turn/started", "turn/completed"]
    assert observed_runtime_identities == [
        (step.id, turn.id, session.id, conversation.id),
    ] * 5
    assert session.thread_id == "thread-1"
    assert turn.native_turn_id == "turn-1"
    assert turn.accepted_at is not None
    assert turn.steering_closed_at is not None
    assert turn.transport_kind == "app_server"


def test_app_server_turn_persistence_retains_late_retired_session_output(monkeypatch, tmp_path):
    from worker import codex_app_server
    from worker.codex_app_server import CodexAppServerTurnPersistence
    from worker.run_ownership import RunOwnershipLost

    added = []
    run_id = uuid.uuid4()
    session = SimpleNamespace(
        id=uuid.uuid4(),
        status="replaced",
        ended_at=time.time(),
    )
    turn = SimpleNamespace(
        id=uuid.uuid4(),
        status="ambiguous",
    )

    class Db:
        def add(self, item):
            added.append(item)

        def get(self, model, key):
            if getattr(model, "__name__", "") == "CodexTurn" and key == turn.id:
                return turn
            if getattr(model, "__name__", "") == "CodexSession" and key == session.id:
                return session
            return None

    @contextmanager
    def fake_scope(_settings):
        yield Db()

    monkeypatch.setattr(codex_app_server, "session_scope", fake_scope)

    import worker.persistent_codex as persistent_codex

    def lose_ownership(*args, **kwargs):
        raise RunOwnershipLost("retired")

    monkeypatch.setattr(persistent_codex, "_load_locked_owned_runtime_records", lose_ownership)

    persistence = CodexAppServerTurnPersistence(
        settings=_make_settings(tmp_path),
        prepared=SimpleNamespace(run_id=run_id, worker_instance_id="worker-1"),
        step_id=uuid.uuid4(),
        turn_id=turn.id,
        session_id=session.id,
        conversation_id=uuid.uuid4(),
    )
    persistence.persist_protocol_item(
        {"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {"id": "turn-late"}}}
    )

    assert len(added) == 1
    assert added[0].item_kind == "turn/completed"
    assert added[0].payload_json["autosac_recovery"] == {
        "late_output_from_retired_session": True,
        "publishable": False,
        "session_status": "replaced",
        "turn_status": "ambiguous",
    }


def test_app_server_failure_classification_preserves_step_status_boundaries():
    from worker.codex_app_server import (
        CodexAppServerAmbiguousError,
        CodexAppServerError,
        CodexAppServerInterruptedError,
        CodexAppServerRejectedError,
        CodexAppServerTimedOutError,
        classify_app_server_failure,
    )

    cases = [
        (CodexAppServerError("failed", failure_status="failed"), "failed"),
        (CodexAppServerRejectedError("rejected"), "rejected"),
        (CodexAppServerTimedOutError("timed out"), "timed_out"),
        (CodexAppServerInterruptedError("interrupted"), "interrupted"),
        (CodexAppServerAmbiguousError("ambiguous"), "ambiguous"),
    ]
    for error, expected_status in cases:
        classification = classify_app_server_failure(error, stderr_text="stderr")
        assert classification.status == expected_status
