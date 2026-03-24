from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import uuid

import pytest

from shared.config import Settings


def _make_settings(tmp_path: Path) -> Settings:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        app_base_url="http://localhost:8000",
        app_secret_key="test-secret",
        database_url="postgresql+psycopg://triage:triage@localhost:5432/triage",
        uploads_dir=tmp_path / "uploads",
        triage_workspace_dir=workspace_dir,
        repo_mount_dir=workspace_dir / "app",
        manuals_mount_dir=workspace_dir / "manuals",
        codex_bin="codex",
        codex_api_key="test-key",
        codex_model="gpt-test",
        codex_timeout_seconds=75,
        worker_poll_seconds=10,
        auto_support_reply_min_confidence=0.85,
        auto_confirm_intent_min_confidence=0.90,
        max_images_per_message=3,
        max_image_bytes=5 * 1024 * 1024,
        session_default_hours=12,
        session_remember_days=30,
    )


def _load_worker_symbols():
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("argon2")
    pytest.importorskip("pydantic")

    from worker.main import heartbeat_loop
    from worker.codex_runner import (
        CodexRunError,
        build_codex_command,
        build_triage_prompt,
        execute_codex_run,
        prepare_codex_run,
    )
    from worker.triage import (
        _apply_success_result,
        _mark_failed,
        _prepare_run,
        TriageResultError,
        build_requester_visible_fingerprint,
        process_ai_run,
        validate_triage_result,
    )

    return {
        "_apply_success_result": _apply_success_result,
        "_mark_failed": _mark_failed,
        "_prepare_run": _prepare_run,
        "CodexRunError": CodexRunError,
        "TriageResultError": TriageResultError,
        "build_codex_command": build_codex_command,
        "build_requester_visible_fingerprint": build_requester_visible_fingerprint,
        "build_triage_prompt": build_triage_prompt,
        "execute_codex_run": execute_codex_run,
        "heartbeat_loop": heartbeat_loop,
        "prepare_codex_run": prepare_codex_run,
        "process_ai_run": process_ai_run,
        "validate_triage_result": validate_triage_result,
    }


def _make_context(*, ticket=None, public_body: str = "Public body", internal_body: str = "Internal body", attachment_sha: str = "sha-1"):
    ticket = ticket or SimpleNamespace(
        id=uuid.uuid4(),
        reference="T-000001",
        title="Need access",
        status="new",
        urgent=False,
        last_processed_hash=None,
        clarification_rounds=0,
        requeue_requested=False,
        requeue_trigger=None,
    )
    public_message = SimpleNamespace(
        author_type="requester",
        source="ticket_create",
        created_at=SimpleNamespace(isoformat=lambda: "2026-03-23T00:00:00+00:00"),
        body_text=public_body,
    )
    internal_message = SimpleNamespace(
        author_type="dev_ti",
        source="human_internal_note",
        created_at=SimpleNamespace(isoformat=lambda: "2026-03-23T00:01:00+00:00"),
        body_text=internal_body,
    )
    attachment = SimpleNamespace(sha256=attachment_sha, stored_path="/tmp/example.png")
    return SimpleNamespace(
        ticket=ticket,
        public_messages=[public_message],
        internal_messages=[internal_message],
        public_attachments=[attachment],
    )


def _valid_payload(**overrides):
    payload = {
        "ticket_class": "support",
        "confidence": 0.93,
        "impact_level": "medium",
        "requester_language": "en",
        "summary_short": "Need access to the report view",
        "summary_internal": "Requester cannot reach the report page and likely needs permission guidance.",
        "development_needed": False,
        "needs_clarification": False,
        "clarifying_questions": [],
        "incorrect_or_conflicting_details": [],
        "evidence_found": True,
        "relevant_paths": [{"path": "manuals/access.md", "reason": "Contains role setup steps."}],
        "recommended_next_action": "auto_public_reply",
        "auto_public_reply_allowed": True,
        "public_reply_markdown": "Please open Settings > Access and confirm the report role is enabled.",
        "internal_note_markdown": "High-confidence access/config answer backed by manuals/access.md.",
    }
    payload.update(overrides)
    return payload


class _FakeDb:
    def __init__(self, *, run=None, ticket=None):
        self.run = run
        self.ticket = ticket

    def get(self, model, key):
        name = getattr(model, "__name__", "")
        if name == "AIRun" and self.run is not None and self.run.id == key:
            return self.run
        if name == "Ticket" and self.ticket is not None and self.ticket.id == key:
            return self.ticket
        return None


class _FakeWorkerStateResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeWorkerStateDb:
    def __init__(self):
        self.objects = {}
        self.pending = {}
        self.added = []
        self.executed = []
        self.flush_calls = 0

    def get(self, model, key):
        return self.objects.get((getattr(model, "__name__", ""), key))

    def add(self, item):
        self.added.append(item)
        item_name = type(item).__name__
        key = getattr(item, "key", None)
        if key is not None:
            self.pending[(item_name, key)] = item

    def execute(self, statement):
        self.executed.append(statement)
        keys = [
            (key,)
            for (model_name, key), _value in {**self.objects, **self.pending}.items()
            if model_name == "SystemState"
        ]
        return _FakeWorkerStateResult(keys)

    def flush(self):
        self.flush_calls += 1
        self.objects.update(self.pending)
        self.pending.clear()


def test_requester_visible_fingerprint_excludes_internal_messages():
    symbols = _load_worker_symbols()
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        reference="T-000002",
        title="Same public content",
        status="waiting_on_user",
        urgent=True,
        last_processed_hash=None,
        clarification_rounds=0,
        requeue_requested=False,
        requeue_trigger=None,
    )
    first = _make_context(ticket=ticket, internal_body="First internal note")
    second = _make_context(ticket=ticket, internal_body="Different internal note")

    assert symbols["build_requester_visible_fingerprint"](first) == symbols["build_requester_visible_fingerprint"](second)


def test_validate_triage_result_enforces_auto_reply_threshold(tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)

    with pytest.raises(RuntimeError):
        symbols["validate_triage_result"](
            _valid_payload(confidence=0.80, recommended_next_action="auto_public_reply"),
            settings,
        )


def test_prepare_codex_run_writes_prompt_and_schema(tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    context = _make_context()

    prepared = symbols["prepare_codex_run"](
        settings,
        ticket_id=context.ticket.id,
        run_id=uuid.uuid4(),
        context=context,
    )

    assert prepared.prompt_path.read_text(encoding="utf-8") == prepared.prompt
    assert prepared.schema_path.read_text(encoding="utf-8").startswith("{")
    assert str(context.ticket.id) in str(prepared.run_dir)
    assert prepared.image_paths == [Path("/tmp/example.png")]


def test_build_triage_prompt_includes_public_and_internal_context():
    symbols = _load_worker_symbols()
    context = _make_context(public_body="Requester sees an error", internal_body="Dev/TI suspects role drift")

    prompt = symbols["build_triage_prompt"](context)

    assert "Public messages:" in prompt
    assert "Internal messages:" in prompt
    assert "Requester sees an error" in prompt
    assert "Dev/TI suspects role drift" in prompt


def test_build_codex_command_matches_required_contract(tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    context = _make_context()
    prepared = symbols["prepare_codex_run"](
        settings,
        ticket_id=context.ticket.id,
        run_id=uuid.uuid4(),
        context=context,
    )

    command, env = symbols["build_codex_command"](settings, prepared=prepared)

    assert command[:8] == [
        "codex",
        "exec",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--ask-for-approval",
        "never",
        "--json",
    ]
    assert "--output-schema" in command
    assert "--output-last-message" in command
    assert '-c' in command
    assert 'web_search="disabled"' in command
    assert "--model" in command
    assert "--image" in command
    assert command[-1] == "-"
    assert prepared.prompt not in command
    assert env["CODEX_API_KEY"] == "test-key"


def test_execute_codex_run_passes_prompt_via_stdin(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    context = _make_context()
    prepared = symbols["prepare_codex_run"](
        settings,
        ticket_id=context.ticket.id,
        run_id=uuid.uuid4(),
        context=context,
    )
    prepared.final_output_path.write_text(json.dumps(_valid_payload()), encoding="utf-8")
    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["input"] = kwargs["input"]
        return SimpleNamespace(returncode=0, stdout='{"event":"ok"}\n', stderr="")

    monkeypatch.setattr("worker.codex_runner.subprocess.run", fake_run)

    artifacts = symbols["execute_codex_run"](settings, prepared=prepared)

    assert observed["command"][-1] == "-"
    assert prepared.prompt not in observed["command"]
    assert observed["input"] == prepared.prompt
    assert artifacts.output_payload["recommended_next_action"] == "auto_public_reply"


def test_prepare_run_skips_when_last_processed_hash_matches(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        reference="T-000003",
        title="Duplicate content",
        status="ai_triage",
        urgent=False,
        last_processed_hash="",
        clarification_rounds=0,
        requeue_requested=False,
        requeue_trigger=None,
    )
    context = _make_context(ticket=ticket)
    ticket.last_processed_hash = symbols["build_requester_visible_fingerprint"](context)
    run = SimpleNamespace(
        id=uuid.uuid4(),
        ticket_id=ticket.id,
        status="running",
        triggered_by="new_ticket",
        input_hash=None,
        model_name=None,
        prompt_path=None,
        schema_path=None,
        final_output_path=None,
        stdout_jsonl_path=None,
        stderr_path=None,
        error_text=None,
        ended_at=None,
    )
    fake_db = _FakeDb(run=run)
    observed = {"requeue": 0, "status_changes": 0}

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.triage.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.triage.load_ticket_context", lambda db, ticket_id: context)
    monkeypatch.setattr(
        "worker.triage.prepare_codex_run",
        lambda *args, **kwargs: SimpleNamespace(
            prompt_path=tmp_path / "prompt.txt",
            schema_path=tmp_path / "schema.json",
            final_output_path=tmp_path / "final.json",
            stdout_jsonl_path=tmp_path / "stdout.jsonl",
            stderr_path=tmp_path / "stderr.txt",
        ),
    )
    monkeypatch.setattr(
        "worker.triage.process_deferred_requeue",
        lambda db, ticket: observed.__setitem__("requeue", observed["requeue"] + 1),
    )
    monkeypatch.setattr(
        "worker.triage.record_status_change",
        lambda *args, **kwargs: observed.__setitem__("status_changes", observed["status_changes"] + 1),
    )

    prepared = symbols["_prepare_run"](settings, run_id=run.id)

    assert prepared is None
    assert run.status == "skipped"
    assert run.ended_at is not None
    assert observed["requeue"] == 1
    assert observed["status_changes"] == 0


def test_prepare_run_skip_does_not_change_non_ai_triage_status(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        reference="T-000003",
        title="Duplicate content",
        status="waiting_on_dev_ti",
        urgent=False,
        last_processed_hash="same-hash",
        clarification_rounds=0,
        requeue_requested=False,
        requeue_trigger=None,
    )
    context = _make_context(ticket=ticket)
    run = SimpleNamespace(
        id=uuid.uuid4(),
        ticket_id=ticket.id,
        status="running",
        triggered_by="new_ticket",
        input_hash=None,
        model_name=None,
        prompt_path=None,
        schema_path=None,
        final_output_path=None,
        stdout_jsonl_path=None,
        stderr_path=None,
        error_text=None,
        ended_at=None,
    )
    fake_db = _FakeDb(run=run)
    observed = {"requeue": 0, "status_changes": 0}

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.triage.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.triage.load_ticket_context", lambda db, ticket_id: context)
    monkeypatch.setattr("worker.triage.build_requester_visible_fingerprint", lambda context: "same-hash")
    monkeypatch.setattr(
        "worker.triage.prepare_codex_run",
        lambda *args, **kwargs: SimpleNamespace(
            prompt_path=tmp_path / "prompt.txt",
            schema_path=tmp_path / "schema.json",
            final_output_path=tmp_path / "final.json",
            stdout_jsonl_path=tmp_path / "stdout.jsonl",
            stderr_path=tmp_path / "stderr.txt",
        ),
    )
    monkeypatch.setattr(
        "worker.triage.process_deferred_requeue",
        lambda db, ticket: observed.__setitem__("requeue", observed["requeue"] + 1),
    )
    monkeypatch.setattr(
        "worker.triage.record_status_change",
        lambda *args, **kwargs: observed.__setitem__("status_changes", observed["status_changes"] + 1),
    )

    prepared = symbols["_prepare_run"](settings, run_id=run.id)

    assert prepared is None
    assert ticket.status == "waiting_on_dev_ti"
    assert run.status == "skipped"
    assert observed["requeue"] == 1
    assert observed["status_changes"] == 0


def test_apply_success_result_publishes_internal_note_before_public_action(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        reference="T-000004",
        title="Permission issue",
        status="ai_triage",
        urgent=False,
        last_processed_hash=None,
        clarification_rounds=0,
        requeue_requested=False,
        requeue_trigger=None,
    )
    context = _make_context(ticket=ticket)
    publication_hash = symbols["build_requester_visible_fingerprint"](context)
    run = SimpleNamespace(
        id=uuid.uuid4(),
        ticket_id=ticket.id,
        status="running",
        input_hash=publication_hash,
        ended_at=None,
        error_text=None,
    )
    fake_db = _FakeDb(run=run)
    events: list[str] = []

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.triage.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.triage.load_ticket_context", lambda db, ticket_id: context)
    monkeypatch.setattr("worker.triage.apply_ai_classification", lambda *args, **kwargs: events.append("classification"))
    monkeypatch.setattr("worker.triage.publish_ai_internal_note", lambda *args, **kwargs: events.append("internal"))
    monkeypatch.setattr(
        "worker.triage.publish_ai_public_reply",
        lambda *args, **kwargs: events.append(f"public:{kwargs['last_ai_action']}"),
    )
    monkeypatch.setattr("worker.triage.create_ai_draft", lambda *args, **kwargs: events.append("draft"))
    monkeypatch.setattr("worker.triage.route_ticket_after_ai", lambda *args, **kwargs: events.append("route"))
    monkeypatch.setattr("worker.triage.process_deferred_requeue", lambda *args, **kwargs: events.append("requeue"))

    symbols["_apply_success_result"](
        settings,
        run_id=run.id,
        result=symbols["validate_triage_result"](_valid_payload(), settings),
    )

    assert events == ["classification", "internal", "public:auto_public_reply", "requeue"]
    assert ticket.last_processed_hash == publication_hash
    assert run.status == "succeeded"
    assert run.ended_at is not None


def test_validate_triage_result_requires_questions_for_clarification(tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)

    with pytest.raises(symbols["TriageResultError"], match="at least one clarifying question"):
        symbols["validate_triage_result"](
            _valid_payload(
                recommended_next_action="ask_clarification",
                needs_clarification=True,
                clarifying_questions=[],
                auto_public_reply_allowed=False,
            ),
            settings,
        )


def test_validate_triage_result_rejects_clarification_with_auto_public_flag(tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)

    with pytest.raises(symbols["TriageResultError"], match="auto_public_reply_allowed=false"):
        symbols["validate_triage_result"](
            _valid_payload(
                recommended_next_action="ask_clarification",
                needs_clarification=True,
                clarifying_questions=["Which report is affected?"],
                auto_public_reply_allowed=True,
            ),
            settings,
        )


def test_validate_triage_result_rejects_route_dev_ti_with_public_reply(tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)

    with pytest.raises(symbols["TriageResultError"], match="empty public reply"):
        symbols["validate_triage_result"](
            _valid_payload(
                recommended_next_action="route_dev_ti",
                auto_public_reply_allowed=False,
            ),
            settings,
        )


def test_validate_triage_result_rejects_unknown_auto_confirm(tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)

    with pytest.raises(symbols["TriageResultError"], match="unknown tickets cannot use automatic public actions"):
        symbols["validate_triage_result"](
            _valid_payload(
                ticket_class="unknown",
                recommended_next_action="auto_confirm_and_route",
                confidence=0.95,
            ),
            settings,
        )


def test_validate_triage_result_rejects_bug_auto_public_action(tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)

    with pytest.raises(
        symbols["TriageResultError"],
        match="automatic public actions are only supported for support and access_config tickets",
    ):
        symbols["validate_triage_result"](
            _valid_payload(
                ticket_class="bug",
                recommended_next_action="auto_confirm_and_route",
                confidence=0.95,
            ),
            settings,
        )


def test_validate_triage_result_allows_access_config_auto_confirm(tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)

    result = symbols["validate_triage_result"](
        _valid_payload(
            ticket_class="access_config",
            recommended_next_action="auto_confirm_and_route",
            confidence=0.95,
        ),
        settings,
    )

    assert result.recommended_next_action == "auto_confirm_and_route"


def test_validate_triage_result_allows_route_dev_ti_without_public_reply(tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)

    result = symbols["validate_triage_result"](
        _valid_payload(
            recommended_next_action="route_dev_ti",
            auto_public_reply_allowed=False,
            public_reply_markdown="",
            confidence=0.40,
            evidence_found=False,
        ),
        settings,
    )

    assert result.recommended_next_action == "route_dev_ti"


def test_apply_success_result_supersedes_stale_run_without_publication(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        reference="T-000005",
        title="Stale input",
        status="ai_triage",
        urgent=False,
        last_processed_hash=None,
        clarification_rounds=0,
        requeue_requested=True,
        requeue_trigger="requester_reply",
    )
    context = _make_context(ticket=ticket)
    run = SimpleNamespace(
        id=uuid.uuid4(),
        ticket_id=ticket.id,
        status="running",
        input_hash="old-hash",
        ended_at=None,
        error_text=None,
    )
    fake_db = _FakeDb(run=run)
    observed = {"internal": 0, "public": 0, "requeue": 0}

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.triage.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.triage.load_ticket_context", lambda db, ticket_id: context)
    monkeypatch.setattr(
        "worker.triage.publish_ai_internal_note",
        lambda *args, **kwargs: observed.__setitem__("internal", observed["internal"] + 1),
    )
    monkeypatch.setattr(
        "worker.triage.publish_ai_public_reply",
        lambda *args, **kwargs: observed.__setitem__("public", observed["public"] + 1),
    )
    monkeypatch.setattr(
        "worker.triage.process_deferred_requeue",
        lambda *args, **kwargs: observed.__setitem__("requeue", observed["requeue"] + 1),
    )

    symbols["_apply_success_result"](
        settings,
        run_id=run.id,
        result=symbols["validate_triage_result"](_valid_payload(), settings),
    )

    assert run.status == "superseded"
    assert run.ended_at is not None
    assert observed == {"internal": 0, "public": 0, "requeue": 1}


def test_process_ai_run_marks_failed_when_publication_step_raises(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    prepared = SimpleNamespace(run_id=uuid.uuid4(), prepared_codex_run=SimpleNamespace())
    observed = {}

    monkeypatch.setattr("worker.triage._prepare_run", lambda *args, **kwargs: prepared)
    monkeypatch.setattr(
        "worker.triage.execute_codex_run",
        lambda *args, **kwargs: SimpleNamespace(output_payload=_valid_payload()),
    )
    monkeypatch.setattr(
        "worker.triage.validate_triage_result",
        lambda payload, settings: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "worker.triage._apply_success_result",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("publish failed")),
    )
    monkeypatch.setattr(
        "worker.triage._mark_failed",
        lambda settings, run_id, error_text: observed.update({"run_id": run_id, "error_text": error_text}),
    )

    symbols["process_ai_run"](settings, run_id=prepared.run_id)

    assert observed["run_id"] == prepared.run_id
    assert observed["error_text"] == "Unexpected worker error: publish failed"


def test_process_ai_run_marks_failed_when_codex_errors(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    prepared = SimpleNamespace(run_id=uuid.uuid4(), prepared_codex_run=SimpleNamespace())
    observed = {}

    monkeypatch.setattr("worker.triage._prepare_run", lambda *args, **kwargs: prepared)
    monkeypatch.setattr(
        "worker.triage.execute_codex_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(symbols["CodexRunError"]("codex failed")),
    )
    monkeypatch.setattr(
        "worker.triage._mark_failed",
        lambda settings, run_id, error_text: observed.update({"run_id": run_id, "error_text": error_text}),
    )

    symbols["process_ai_run"](settings, run_id=prepared.run_id)

    assert observed["run_id"] == prepared.run_id
    assert observed["error_text"] == "codex failed"


def test_mark_failed_publishes_internal_failure_note_and_routes_ticket(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        reference="T-000010",
        title="Worker failure",
        status="new",
        urgent=False,
    )
    run = SimpleNamespace(
        id=uuid.uuid4(),
        ticket_id=ticket.id,
        status="running",
        ended_at=None,
        error_text=None,
    )
    fake_db = _FakeDb(run=run, ticket=ticket)
    observed = {"failure_note": 0, "status_changes": 0, "requeue": 0}

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.triage.session_scope", fake_session_scope)
    monkeypatch.setattr(
        "worker.triage.publish_ai_failure_note",
        lambda *args, **kwargs: observed.__setitem__("failure_note", observed["failure_note"] + 1),
    )
    monkeypatch.setattr(
        "worker.triage.record_status_change",
        lambda db, ticket, to_status, **kwargs: (
            observed.__setitem__("status_changes", observed["status_changes"] + 1),
            setattr(ticket, "status", to_status),
        ),
    )
    monkeypatch.setattr(
        "worker.triage.process_deferred_requeue",
        lambda *args, **kwargs: observed.__setitem__("requeue", observed["requeue"] + 1),
    )

    symbols["_mark_failed"](settings, run_id=run.id, error_text="boom")

    assert run.status == "failed"
    assert run.error_text == "boom"
    assert run.ended_at is not None
    assert ticket.status == "waiting_on_dev_ti"
    assert observed == {"failure_note": 1, "status_changes": 1, "requeue": 1}


def test_heartbeat_loop_emits_while_stop_event_controls_exit(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    stop_event = threading.Event()
    observed = {"heartbeats": 0}

    def fake_emit_worker_heartbeat(_settings):
        observed["heartbeats"] += 1
        stop_event.set()

    monkeypatch.setattr("worker.main.emit_worker_heartbeat", fake_emit_worker_heartbeat)

    symbols["heartbeat_loop"](settings, stop_event=stop_event, interval_seconds=0)

    assert observed["heartbeats"] == 1


def test_emit_worker_heartbeat_initializes_system_state_defaults(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("argon2")
    from worker.main import emit_worker_heartbeat

    settings = _make_settings(tmp_path)
    fake_db = _FakeWorkerStateDb()

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.main.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.main.log_worker_event", lambda *args, **kwargs: None)

    emit_worker_heartbeat(settings)

    bootstrap_state = fake_db.objects[("SystemState", "bootstrap_version")]
    heartbeat_state = fake_db.objects[("SystemState", "worker_heartbeat")]
    assert fake_db.flush_calls == 1
    assert bootstrap_state.value_json == {"version": "stage1-v1"}
    assert heartbeat_state.value_json["status"] == "alive"
