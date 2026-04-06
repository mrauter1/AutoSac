from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import threading
import uuid

import pytest

from shared.config import Settings


def _make_settings(tmp_path: Path, *, codex_api_key: str | None = "test-key") -> Settings:
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
        codex_api_key=codex_api_key,
        codex_model="gpt-test",
        codex_timeout_seconds=3600,
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
    pytest.importorskip("pydantic")

    from shared.agent_specs import load_agent_spec
    from worker.main import heartbeat_loop
    from worker.output_contracts import RouterResult
    from worker.pipeline import execute_triage_pipeline
    from worker.prompt_renderer import render_agent_prompt
    from worker.step_runner import StepRunError, build_codex_command, execute_step, prepare_step_run, write_run_manifest_snapshot
    from worker.triage import (
        _apply_success_result,
        _mark_failed,
        _prepare_run,
        build_requester_visible_fingerprint,
        process_ai_run,
        resolve_triage_outcome,
    )
    from worker.triage_validation import TriageResultError, validate_triage_result

    return {
        "_apply_success_result": _apply_success_result,
        "_mark_failed": _mark_failed,
        "_prepare_run": _prepare_run,
        "build_codex_command": build_codex_command,
        "build_requester_visible_fingerprint": build_requester_visible_fingerprint,
        "execute_step": execute_step,
        "execute_triage_pipeline": execute_triage_pipeline,
        "heartbeat_loop": heartbeat_loop,
        "load_agent_spec": load_agent_spec,
        "process_ai_run": process_ai_run,
        "prepare_step_run": prepare_step_run,
        "render_agent_prompt": render_agent_prompt,
        "resolve_triage_outcome": resolve_triage_outcome,
        "RouterResult": RouterResult,
        "StepRunError": StepRunError,
        "TriageResultError": TriageResultError,
        "validate_triage_result": validate_triage_result,
        "write_run_manifest_snapshot": write_run_manifest_snapshot,
    }


def _make_context(
    *,
    ticket=None,
    public_body: str = "Public body",
    internal_body: str = "Internal body",
    attachment_sha: str = "sha-1",
    public_attachments=None,
    requester_role: str = "requester",
    requester_can_view_internal_messages: bool = False,
):
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
    attachment = SimpleNamespace(
        sha256=attachment_sha,
        stored_path="/tmp/example.png",
        mime_type="image/png",
        width=40,
        height=20,
    )
    return SimpleNamespace(
        ticket=ticket,
        requester_role=requester_role,
        requester_can_view_internal_messages=requester_can_view_internal_messages,
        public_messages=[public_message],
        internal_messages=[internal_message],
        public_attachments=public_attachments or [attachment],
    )


def _route_payload(**overrides):
    if "ticket_class" in overrides and "route_target_id" not in overrides:
        overrides["route_target_id"] = overrides.pop("ticket_class")
    payload = {
        "route_target_id": "support",
        "routing_rationale": "The requester is asking for help using an existing workflow.",
    }
    payload.update(overrides)
    return payload


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
        "answer_scope": "document_scoped",
        "evidence_status": "verified",
        "misuse_or_safety_risk": False,
        "human_review_reason": "",
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


def test_prepare_step_run_writes_prompt_and_schema(tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    context = _make_context()
    spec = symbols["load_agent_spec"]("support")
    router_result = symbols["RouterResult"].model_validate(_route_payload())

    prepared = symbols["prepare_step_run"](
        settings,
        run_id=uuid.uuid4(),
        ticket_id=context.ticket.id,
        step_index=2,
        step_kind="specialist",
        spec=spec,
        context=context,
        router_result=router_result,
        target_ticket_class="support",
    )

    assert prepared.paths.prompt_path.read_text(encoding="utf-8") == prepared.prompt
    assert prepared.paths.schema_path.read_text(encoding="utf-8").startswith("{")
    assert str(context.ticket.id) in str(prepared.paths.run_dir)
    assert prepared.paths.step_dir.name.startswith("02-support")
    assert prepared.image_paths == [Path("/tmp/example.png")]


def test_prepare_step_run_filters_non_image_attachments(tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    context = _make_context(
        public_attachments=[
            SimpleNamespace(
                sha256="img-sha",
                stored_path="/tmp/example.png",
                mime_type="image/png",
                width=40,
                height=20,
            ),
            SimpleNamespace(
                sha256="pdf-sha",
                stored_path="/tmp/report.pdf",
                mime_type="application/pdf",
                width=None,
                height=None,
            ),
        ]
    )
    spec = symbols["load_agent_spec"]("support")
    router_result = symbols["RouterResult"].model_validate(_route_payload())

    prepared = symbols["prepare_step_run"](
        settings,
        run_id=uuid.uuid4(),
        ticket_id=context.ticket.id,
        step_index=2,
        step_kind="specialist",
        spec=spec,
        context=context,
        router_result=router_result,
        target_ticket_class="support",
    )

    assert prepared.image_paths == [Path("/tmp/example.png")]


def test_prepare_step_run_excludes_spoofed_image_mime_without_verified_dimensions(tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    context = _make_context(
        public_attachments=[
            SimpleNamespace(
                sha256="fake-image-sha",
                stored_path="/tmp/not-really-an-image.bin",
                mime_type="image/png",
                width=None,
                height=None,
            )
        ]
    )
    spec = symbols["load_agent_spec"]("support")
    router_result = symbols["RouterResult"].model_validate(_route_payload())

    prepared = symbols["prepare_step_run"](
        settings,
        run_id=uuid.uuid4(),
        ticket_id=context.ticket.id,
        step_index=2,
        step_kind="specialist",
        spec=spec,
        context=context,
        router_result=router_result,
        target_ticket_class="support",
    )

    assert prepared.image_paths == []


def test_render_agent_prompt_includes_public_internal_and_router_context():
    symbols = _load_worker_symbols()
    context = _make_context(
        public_body="Requester sees an error",
        internal_body="Dev/TI suspects role drift",
        requester_role="dev_ti",
        requester_can_view_internal_messages=True,
    )
    spec = symbols["load_agent_spec"]("support")
    router_result = symbols["RouterResult"].model_validate(
        _route_payload(route_target_id="support", routing_rationale="Looks like usage help.")
    )

    prompt = symbols["render_agent_prompt"](
        spec,
        context=context,
        router_result=router_result,
        target_ticket_class="support",
    )

    assert prompt.startswith("$triage-support")
    assert "Public messages:" in prompt
    assert "Internal messages:" in prompt
    assert "Route target ID: support" in prompt
    assert "Requester sees an error" in prompt
    assert "Dev/TI suspects role drift" in prompt
    assert "dev_ti" in prompt
    assert "yes" in prompt


def test_build_codex_command_matches_required_contract(tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    context = _make_context()
    spec = symbols["load_agent_spec"]("support")
    router_result = symbols["RouterResult"].model_validate(_route_payload())
    prepared = symbols["prepare_step_run"](
        settings,
        run_id=uuid.uuid4(),
        ticket_id=context.ticket.id,
        step_index=2,
        step_kind="specialist",
        spec=spec,
        context=context,
        router_result=router_result,
        target_ticket_class="support",
    )

    command, env = symbols["build_codex_command"](settings, prepared=prepared)

    assert command[:8] == [
        "codex",
        "--ask-for-approval",
        "never",
        "exec",
        "--ephemeral",
        "--sandbox",
        "read-only",
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


def test_build_codex_command_omits_api_key_when_not_configured(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path, codex_api_key=None)
    context = _make_context()
    spec = symbols["load_agent_spec"]("support")
    router_result = symbols["RouterResult"].model_validate(_route_payload())
    prepared = symbols["prepare_step_run"](
        settings,
        run_id=uuid.uuid4(),
        ticket_id=context.ticket.id,
        step_index=2,
        step_kind="specialist",
        spec=spec,
        context=context,
        router_result=router_result,
        target_ticket_class="support",
    )

    monkeypatch.setenv("CODEX_API_KEY", "stale-parent-key")
    _command, env = symbols["build_codex_command"](settings, prepared=prepared)

    assert "CODEX_API_KEY" not in env


def test_execute_step_passes_prompt_via_stdin(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    context = _make_context()
    spec = symbols["load_agent_spec"]("support")
    router_result = symbols["RouterResult"].model_validate(_route_payload())
    prepared = symbols["prepare_step_run"](
        settings,
        run_id=uuid.uuid4(),
        ticket_id=context.ticket.id,
        step_index=2,
        step_kind="specialist",
        spec=spec,
        context=context,
        router_result=router_result,
        target_ticket_class="support",
    )
    prepared.paths.final_output_path.write_text(json.dumps(_valid_payload()), encoding="utf-8")
    observed = {}

    monkeypatch.setattr("worker.step_runner._create_running_step_row", lambda settings, prepared: uuid.uuid4())
    monkeypatch.setattr("worker.step_runner._update_step_row", lambda **kwargs: None)
    monkeypatch.setattr("worker.step_runner.write_step_manifest", lambda *args, **kwargs: None)

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["input"] = kwargs["input"]
        return SimpleNamespace(returncode=0, stdout='{"event":"ok"}\n', stderr="")

    monkeypatch.setattr("worker.step_runner.subprocess.run", fake_run)

    result = symbols["execute_step"](settings, prepared=prepared)

    assert observed["command"][-1] == "-"
    assert prepared.prompt not in observed["command"]
    assert observed["input"] == prepared.prompt
    assert result.output_payload["recommended_next_action"] == "auto_public_reply"


def test_execute_step_handles_timeout_streams_as_bytes(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    context = _make_context()
    spec = symbols["load_agent_spec"]("support")
    router_result = symbols["RouterResult"].model_validate(_route_payload())
    prepared = symbols["prepare_step_run"](
        settings,
        run_id=uuid.uuid4(),
        ticket_id=context.ticket.id,
        step_index=2,
        step_kind="specialist",
        spec=spec,
        context=context,
        router_result=router_result,
        target_ticket_class="support",
    )

    monkeypatch.setattr("worker.step_runner._create_running_step_row", lambda settings, prepared: uuid.uuid4())
    monkeypatch.setattr("worker.step_runner._update_step_row", lambda **kwargs: None)
    monkeypatch.setattr("worker.step_runner.write_step_manifest", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "worker.step_runner.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(
                cmd=["codex"],
                timeout=1,
                output=b'{"event":"partial"}\n',
                stderr=b"stderr-bytes",
            )
        ),
    )

    with pytest.raises(symbols["StepRunError"], match="timed out"):
        symbols["execute_step"](settings, prepared=prepared)

    assert prepared.paths.stdout_jsonl_path.read_text(encoding="utf-8") == '{"event":"partial"}\n'
    assert prepared.paths.stderr_path.read_text(encoding="utf-8") == "stderr-bytes"


def test_execute_triage_pipeline_flags_router_specialist_class_mismatch(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    context = _make_context()
    observed = {"manifest_updates": []}

    monkeypatch.setattr("worker.pipeline.prepare_step_run", lambda *args, **kwargs: SimpleNamespace(
        run_id=kwargs["run_id"],
        ticket_id=kwargs["ticket_id"],
        step_index=kwargs["step_index"],
        step_kind=kwargs["step_kind"],
        spec=kwargs["spec"],
        model_name=None,
        paths=SimpleNamespace(run_dir=tmp_path / "run", as_payload=lambda: {}),
    ))
    outputs = [
        SimpleNamespace(
            step_id=uuid.uuid4(),
            prepared=SimpleNamespace(
                step_index=1,
                step_kind="router",
                spec=SimpleNamespace(id="router", version="1", output_contract="router_result"),
                paths=SimpleNamespace(run_dir=tmp_path / "run", as_payload=lambda: {}),
            ),
            output_payload=_route_payload(ticket_class="support"),
        ),
        SimpleNamespace(
            step_id=uuid.uuid4(),
            prepared=SimpleNamespace(
                step_index=2,
                step_kind="specialist",
                spec=SimpleNamespace(id="support", version="1", output_contract="triage_result"),
                paths=SimpleNamespace(run_dir=tmp_path / "run", as_payload=lambda: {}),
            ),
            output_payload=_valid_payload(
                ticket_class="bug",
                answer_scope="general_reasoning",
                evidence_status="not_applicable",
                evidence_found=False,
                relevant_paths=[],
                public_reply_markdown="The internal team will review the likely bug path and follow up.",
                recommended_next_action="auto_confirm_and_route",
                internal_note_markdown="",
            ),
        ),
    ]
    monkeypatch.setattr("worker.pipeline.execute_step", lambda *args, **kwargs: outputs.pop(0))
    monkeypatch.setattr(
        "worker.pipeline.write_run_manifest_snapshot",
        lambda settings, run_id: observed["manifest_updates"].append(run_id),
    )

    result = symbols["execute_triage_pipeline"](
        settings,
        run_id=uuid.uuid4(),
        ticket_id=context.ticket.id,
        context=context,
    )

    assert result.force_human_review_reason is not None
    assert "router classified the ticket as support" in result.force_human_review_reason
    assert result.triage_result.ticket_class == "bug"
    assert len(observed["manifest_updates"]) == 2


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


def test_validate_triage_result_requires_document_sources_for_document_scoped_answer(tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)

    with pytest.raises(symbols["TriageResultError"], match="document_scoped answers must include relevant_paths"):
        symbols["validate_triage_result"](_valid_payload(relevant_paths=[]), settings)


def test_validate_triage_result_requires_human_review_reason_for_misuse_risk(tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)

    with pytest.raises(symbols["TriageResultError"], match="human_review_reason"):
        symbols["validate_triage_result"](
            _valid_payload(
                misuse_or_safety_risk=True,
                human_review_reason="",
            ),
            settings,
        )


def test_validate_triage_result_allows_general_reasoning_without_document_evidence(tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)

    result = symbols["validate_triage_result"](
        _valid_payload(
            ticket_class="bug",
            answer_scope="general_reasoning",
            evidence_status="not_applicable",
            evidence_found=False,
            relevant_paths=[],
            public_reply_markdown="Here is the most likely explanation based on the ticket details.",
            internal_note_markdown="",
        ),
        settings,
    )

    assert result.answer_scope == "general_reasoning"
    assert result.evidence_status == "not_applicable"


def test_validate_triage_result_requires_uncertainty_caveat_for_low_risk_guess(tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)

    with pytest.raises(symbols["TriageResultError"], match="not verified in manuals/ or app/"):
        symbols["validate_triage_result"](
            _valid_payload(
                evidence_found=False,
                evidence_status="not_found_low_risk_guess",
                public_reply_markdown="The most likely setup is that the report permission is disabled.",
            ),
            settings,
        )


def test_resolve_triage_outcome_allows_low_confidence_best_effort_reply(tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    ticket = SimpleNamespace(clarification_rounds=0)

    outcome = symbols["resolve_triage_outcome"](
        ticket,
        symbols["validate_triage_result"](
            _valid_payload(confidence=0.80, recommended_next_action="auto_public_reply"),
            settings,
        ),
        settings,
    )

    assert outcome.run_status == "succeeded"
    assert outcome.effective_action == "auto_public_reply"
    assert outcome.warning_text is None


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
        pipeline_version=None,
        final_step_id=None,
        final_agent_spec_id=None,
        final_output_contract=None,
        final_output_json=None,
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
    assert run.pipeline_version == "agent-pipeline-v1"
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
        pipeline_version=None,
        final_step_id=None,
        final_agent_spec_id=None,
        final_output_contract=None,
        final_output_json=None,
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
    assert run.pipeline_version == "agent-pipeline-v1"
    assert observed["requeue"] == 1
    assert observed["status_changes"] == 0


def test_apply_success_result_publishes_internal_note_before_public_action_and_sets_final_fields(monkeypatch, tmp_path):
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
        pipeline_version=None,
        final_step_id=None,
        final_agent_spec_id=None,
        final_output_contract=None,
        final_output_json=None,
        model_name=None,
    )
    fake_db = _FakeDb(run=run)
    events: list[str] = []
    observed = {"manifest_runs": []}

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
    monkeypatch.setattr(
        "worker.triage.write_run_manifest_snapshot",
        lambda settings, run_id: observed["manifest_runs"].append(run_id),
    )

    result = symbols["validate_triage_result"](_valid_payload(), settings)
    symbols["_apply_success_result"](
        settings,
        run_id=run.id,
        result=result,
        final_step_id=uuid.uuid4(),
        final_agent_spec_id="support",
        final_output_contract="triage_result",
        final_output_json=result.model_dump(),
        final_model_name="gpt-test",
    )

    assert events == ["classification", "internal", "public:auto_public_reply", "requeue"]
    assert ticket.last_processed_hash == publication_hash
    assert run.status == "succeeded"
    assert run.final_agent_spec_id == "support"
    assert run.final_output_contract == "triage_result"
    assert run.final_output_json["ticket_class"] == "support"
    assert run.model_name == "gpt-test"
    assert run.ended_at is not None
    assert observed["manifest_runs"] == [run.id]


def test_apply_success_result_force_human_review_creates_draft(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        reference="T-000006",
        title="Mismatch review",
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
        pipeline_version=None,
        final_step_id=None,
        final_agent_spec_id=None,
        final_output_contract=None,
        final_output_json=None,
        model_name=None,
    )
    fake_db = _FakeDb(run=run)
    events: list[str] = []
    observed = {"manifest_runs": []}

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.triage.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.triage.load_ticket_context", lambda db, ticket_id: context)
    monkeypatch.setattr("worker.triage.apply_ai_classification", lambda *args, **kwargs: events.append("classification"))
    monkeypatch.setattr(
        "worker.triage.publish_ai_internal_note",
        lambda *args, **kwargs: observed.setdefault("internal_note", kwargs["body_markdown"]),
    )
    monkeypatch.setattr("worker.triage.publish_ai_public_reply", lambda *args, **kwargs: events.append("public"))
    monkeypatch.setattr(
        "worker.triage.create_ai_draft",
        lambda *args, **kwargs: events.append("draft"),
    )
    monkeypatch.setattr("worker.triage.route_ticket_after_ai", lambda *args, **kwargs: events.append("route"))
    monkeypatch.setattr("worker.triage.process_deferred_requeue", lambda *args, **kwargs: events.append("requeue"))
    monkeypatch.setattr(
        "worker.triage.write_run_manifest_snapshot",
        lambda settings, run_id: observed["manifest_runs"].append(run_id),
    )

    result = symbols["validate_triage_result"](
        _valid_payload(
            ticket_class="bug",
            answer_scope="general_reasoning",
            evidence_status="not_applicable",
            evidence_found=False,
            relevant_paths=[],
            public_reply_markdown="The internal team will review the likely bug path and follow up.",
            recommended_next_action="auto_confirm_and_route",
            internal_note_markdown="",
        ),
        settings,
    )
    symbols["_apply_success_result"](
        settings,
        run_id=run.id,
        result=result,
        final_step_id=uuid.uuid4(),
        final_agent_spec_id="support",
        final_output_contract="triage_result",
        final_output_json=result.model_dump(),
        final_model_name="gpt-test",
        force_human_review_reason="Pipeline mismatch: router classified the ticket as support but the specialist classified it as bug.",
    )

    assert events == ["classification", "draft", "requeue"]
    assert run.status == "human_review"
    assert "Pipeline mismatch" in run.error_text
    assert "Additional review reason" in observed["internal_note"]
    assert observed["manifest_runs"] == [run.id]


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
    manifest_runs: list[uuid.UUID] = []

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
    monkeypatch.setattr(
        "worker.triage.write_run_manifest_snapshot",
        lambda settings, run_id: manifest_runs.append(run_id),
    )

    symbols["_apply_success_result"](
        settings,
        run_id=run.id,
        result=symbols["validate_triage_result"](_valid_payload(), settings),
        final_step_id=uuid.uuid4(),
        final_agent_spec_id="support",
        final_output_contract="triage_result",
        final_output_json=_valid_payload(),
        final_model_name="gpt-test",
    )

    assert run.status == "superseded"
    assert run.ended_at is not None
    assert observed == {"internal": 0, "public": 0, "requeue": 1}
    assert manifest_runs == [run.id]


def test_process_ai_run_marks_failed_when_publication_step_raises(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    prepared = SimpleNamespace(run_id=uuid.uuid4(), ticket_id=uuid.uuid4(), context=SimpleNamespace())
    pipeline_result = SimpleNamespace(
        triage_result=SimpleNamespace(),
        specialist_step=SimpleNamespace(
            step_id=uuid.uuid4(),
            prepared=SimpleNamespace(spec=SimpleNamespace(id="support", output_contract="triage_result"), model_name="gpt-test"),
            output_payload=_valid_payload(),
        ),
        force_human_review_reason=None,
    )
    observed = {}

    monkeypatch.setattr("worker.triage._prepare_run", lambda *args, **kwargs: prepared)
    monkeypatch.setattr("worker.triage.execute_triage_pipeline", lambda *args, **kwargs: pipeline_result)
    monkeypatch.setattr("worker.triage.write_run_manifest_snapshot", lambda *args, **kwargs: None)
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


def test_process_ai_run_marks_failed_when_step_errors(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    prepared = SimpleNamespace(run_id=uuid.uuid4(), ticket_id=uuid.uuid4(), context=SimpleNamespace())
    observed = {}

    monkeypatch.setattr("worker.triage._prepare_run", lambda *args, **kwargs: prepared)
    monkeypatch.setattr("worker.triage.write_run_manifest_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "worker.triage.execute_triage_pipeline",
        lambda *args, **kwargs: (_ for _ in ()).throw(symbols["StepRunError"]("router failed")),
    )
    monkeypatch.setattr(
        "worker.triage._mark_failed",
        lambda settings, run_id, error_text: observed.update({"run_id": run_id, "error_text": error_text}),
    )

    symbols["process_ai_run"](settings, run_id=prepared.run_id)

    assert observed["run_id"] == prepared.run_id
    assert observed["error_text"] == "router failed"


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
    manifest_runs: list[uuid.UUID] = []

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
    monkeypatch.setattr(
        "worker.triage.write_run_manifest_snapshot",
        lambda settings, run_id: manifest_runs.append(run_id),
    )

    symbols["_mark_failed"](settings, run_id=run.id, error_text="boom")

    assert run.status == "failed"
    assert run.pipeline_version == "agent-pipeline-v1"
    assert run.error_text == "boom"
    assert run.ended_at is not None
    assert ticket.status == "waiting_on_dev_ti"
    assert observed == {"failure_note": 1, "status_changes": 1, "requeue": 1}
    assert manifest_runs == [run.id]


def test_write_run_manifest_snapshot_serializes_terminal_run_state(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    run = SimpleNamespace(
        id=uuid.uuid4(),
        ticket_id=uuid.uuid4(),
        pipeline_version="agent-pipeline-v1",
        status="human_review",
        final_step_id=uuid.uuid4(),
        final_agent_spec_id="support",
        final_output_contract="triage_result",
        error_text="needs approval",
        ended_at=SimpleNamespace(isoformat=lambda: "2026-04-06T01:00:00+00:00"),
    )
    step = SimpleNamespace(
        id=uuid.uuid4(),
        step_index=2,
        step_kind="specialist",
        agent_spec_id="support",
        agent_spec_version="1",
        output_contract="triage_result",
        status="succeeded",
        model_name="gpt-test",
        prompt_path="/tmp/prompt.txt",
        schema_path="/tmp/schema.json",
        final_output_path="/tmp/final.json",
        stdout_jsonl_path="/tmp/stdout.jsonl",
        stderr_path="/tmp/stderr.txt",
    )
    observed = {}

    class _FakeScalarResult:
        def __init__(self, items):
            self._items = items

        def scalars(self):
            return self

        def __iter__(self):
            return iter(self._items)

    class _FakeManifestDb:
        def get(self, model, key):
            if getattr(model, "__name__", "") == "AIRun" and key == run.id:
                return run
            return None

        def execute(self, statement):
            return _FakeScalarResult([step])

    @contextmanager
    def fake_session_scope(_settings):
        yield _FakeManifestDb()

    monkeypatch.setattr("worker.step_runner.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.step_runner.build_run_dir", lambda settings, ticket_id, run_id: tmp_path / "run")
    monkeypatch.setattr(
        "worker.step_runner.write_run_manifest",
        lambda run_dir, **kwargs: observed.update({"run_dir": run_dir, **kwargs}),
    )

    symbols["write_run_manifest_snapshot"](settings, run_id=run.id)

    assert observed["status"] == "human_review"
    assert observed["pipeline_version"] == "agent-pipeline-v1"
    assert observed["final_output_contract"] == "triage_result"
    assert observed["error_text"] == "needs approval"
    assert observed["ended_at"] == "2026-04-06T01:00:00+00:00"
    assert observed["steps"][0]["status"] == "succeeded"


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
    assert bootstrap_state.value_json == {"version": "stage1-v3"}
    assert heartbeat_state.value_json["status"] == "alive"
