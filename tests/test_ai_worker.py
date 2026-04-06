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
    from worker.output_contracts import HumanHandoffResult, RouterResult, SpecialistResult, SpecialistSelectorResult
    from worker.pipeline import execute_triage_pipeline
    from worker.prompt_renderer import render_agent_prompt
    from worker.publication_policy import resolve_effective_publication_mode
    from worker.step_runner import StepRunError, build_codex_command, execute_step, prepare_step_run, write_run_manifest_snapshot
    from worker.triage import (
        _apply_success_result,
        _mark_failed,
        _prepare_run,
        build_requester_visible_fingerprint,
        process_ai_run,
    )

    return {
        "_apply_success_result": _apply_success_result,
        "_mark_failed": _mark_failed,
        "_prepare_run": _prepare_run,
        "build_codex_command": build_codex_command,
        "build_requester_visible_fingerprint": build_requester_visible_fingerprint,
        "execute_step": execute_step,
        "execute_triage_pipeline": execute_triage_pipeline,
        "heartbeat_loop": heartbeat_loop,
        "HumanHandoffResult": HumanHandoffResult,
        "load_agent_spec": load_agent_spec,
        "process_ai_run": process_ai_run,
        "prepare_step_run": prepare_step_run,
        "render_agent_prompt": render_agent_prompt,
        "resolve_effective_publication_mode": resolve_effective_publication_mode,
        "RouterResult": RouterResult,
        "SpecialistResult": SpecialistResult,
        "SpecialistSelectorResult": SpecialistSelectorResult,
        "StepRunError": StepRunError,
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
        requester_language=None,
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
    payload = {
        "route_target_id": "support",
        "routing_rationale": "The requester is asking for help using an existing workflow.",
    }
    payload.update(overrides)
    return payload


def _selector_payload(**overrides):
    payload = {
        "specialist_id": "bug",
        "selection_rationale": "The ticket needs debugging-oriented handling.",
    }
    payload.update(overrides)
    return payload


def _specialist_payload(**overrides):
    payload = {
        "requester_language": "en",
        "public_reply_markdown": "Please open Settings > Access and confirm the report role is enabled.",
        "internal_note_markdown": "High-confidence guidance backed by product behavior.",
        "response_confidence": "high",
        "risk_level": "low",
        "risk_reason": "The guidance is low-risk and operational.",
        "summary_internal": "Requester needs access guidance.",
        "publish_mode_recommendation": "auto_publish",
    }
    payload.update(overrides)
    return payload


def _build_route_target(
    *,
    route_target_id: str,
    kind: str,
    mode: str,
    specialist_id: str | None = None,
    candidate_specialist_ids: tuple[str, ...] = (),
    human_queue_status: str | None = None,
    allow_auto_publish: bool | None = None,
    allow_draft_for_human: bool = True,
    allow_manual_only: bool = True,
):
    return SimpleNamespace(
        id=route_target_id,
        label=route_target_id.replace("_", " ").title(),
        kind=kind,
        router_description=f"{route_target_id} description",
        handler=SimpleNamespace(
            human_queue_status=human_queue_status,
            specialist_selection=SimpleNamespace(
                mode=mode,
                specialist_id=specialist_id,
                candidate_specialist_ids=candidate_specialist_ids,
            ),
        ),
        publish_policy=SimpleNamespace(
            allow_auto_publish=(kind == "direct_ai") if allow_auto_publish is None else allow_auto_publish,
            min_response_confidence_for_auto_publish="high",
            max_risk_level_for_auto_publish="low",
            allow_draft_for_human=allow_draft_for_human,
            allow_manual_only=allow_manual_only,
        ),
    )


def _build_registry(*route_targets):
    router_spec = SimpleNamespace(id="router", version="1", kind="router", output_contract="router_result")
    selector_spec = SimpleNamespace(id="specialist-selector", version="1", kind="selector", output_contract="specialist_selector_result")
    specialists = {
        "support": SimpleNamespace(id="support", spec=SimpleNamespace(id="support", version="2", output_contract="specialist_result")),
        "bug": SimpleNamespace(id="bug", spec=SimpleNamespace(id="bug", version="2", output_contract="specialist_result")),
        "feature": SimpleNamespace(id="feature", spec=SimpleNamespace(id="feature", version="2", output_contract="specialist_result")),
    }
    route_targets_by_id = {route_target.id: route_target for route_target in route_targets}

    def require_enabled_route_target(route_target_id: str):
        return route_targets_by_id[route_target_id]

    def require_specialist(specialist_id: str):
        return specialists[specialist_id]

    def candidate_specialists_for_target(route_target_id: str):
        selection = route_targets_by_id[route_target_id].handler.specialist_selection
        return tuple(specialists[specialist_id] for specialist_id in selection.candidate_specialist_ids)

    return SimpleNamespace(
        router_spec=router_spec,
        selector_spec=selector_spec,
        require_enabled_route_target=require_enabled_route_target,
        require_specialist=require_specialist,
        candidate_specialists_for_target=candidate_specialists_for_target,
    )


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
        requester_language=None,
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
        target_route_target_id="support",
    )

    assert prepared.paths.prompt_path.read_text(encoding="utf-8") == prepared.prompt
    assert prepared.paths.schema_path.read_text(encoding="utf-8").startswith("{")
    assert str(context.ticket.id) in str(prepared.paths.run_dir)
    assert prepared.paths.step_dir.name.startswith("02-support")
    assert prepared.image_paths == [Path("/tmp/example.png")]


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
        target_route_target_id="support",
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
        target_route_target_id="support",
    )
    prepared.paths.final_output_path.write_text(json.dumps(_specialist_payload()), encoding="utf-8")
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
    assert result.output_payload["publish_mode_recommendation"] == "auto_publish"


@pytest.mark.parametrize(
    ("route_target", "outputs", "expected_selector", "expected_specialist", "expected_selected_id"),
    [
        (
            _build_route_target(route_target_id="support", kind="direct_ai", mode="fixed", specialist_id="support"),
            [
                _route_payload(route_target_id="support"),
                _specialist_payload(),
            ],
            False,
            True,
            "support",
        ),
        (
            _build_route_target(
                route_target_id="bug",
                kind="direct_ai",
                mode="auto",
                candidate_specialist_ids=("bug", "feature"),
            ),
            [
                _route_payload(route_target_id="bug"),
                _selector_payload(specialist_id="bug"),
                _specialist_payload(summary_internal="Likely bug path."),
            ],
            True,
            True,
            "bug",
        ),
        (
            _build_route_target(
                route_target_id="manual_review",
                kind="human_assist",
                mode="none",
                human_queue_status="waiting_on_dev_ti",
            ),
            [
                _route_payload(route_target_id="manual_review"),
            ],
            False,
            False,
            None,
        ),
        (
            _build_route_target(
                route_target_id="manual_review",
                kind="human_assist",
                mode="fixed",
                specialist_id="bug",
                human_queue_status="waiting_on_dev_ti",
            ),
            [
                _route_payload(route_target_id="manual_review"),
                _specialist_payload(),
            ],
            False,
            True,
            "bug",
        ),
        (
            _build_route_target(
                route_target_id="manual_review",
                kind="human_assist",
                mode="auto",
                candidate_specialist_ids=("bug", "feature"),
                human_queue_status="waiting_on_dev_ti",
            ),
            [
                _route_payload(route_target_id="manual_review"),
                _selector_payload(specialist_id="feature"),
                _specialist_payload(summary_internal="Escalation draft prepared."),
            ],
            True,
            True,
            "feature",
        ),
    ],
)
def test_execute_triage_pipeline_supports_registry_modes(
    monkeypatch,
    tmp_path,
    route_target,
    outputs,
    expected_selector,
    expected_specialist,
    expected_selected_id,
):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    context = _make_context()
    observed = {"manifest_updates": [], "prepared": []}
    registry = _build_registry(route_target)

    def fake_prepare_step_run(*args, **kwargs):
        observed["prepared"].append(
            {
                "step_index": kwargs["step_index"],
                "step_kind": kwargs["step_kind"],
                "spec_id": kwargs["spec"].id,
                "candidate_specialist_ids": kwargs.get("candidate_specialist_ids"),
            }
        )
        return SimpleNamespace(
            run_id=kwargs["run_id"],
            ticket_id=kwargs["ticket_id"],
            step_index=kwargs["step_index"],
            step_kind=kwargs["step_kind"],
            spec=kwargs["spec"],
            model_name=None,
            candidate_specialist_ids=kwargs.get("candidate_specialist_ids"),
            paths=SimpleNamespace(run_dir=tmp_path / "run", as_payload=lambda: {}),
        )

    def fake_execute_step(_settings, *, prepared):
        payload = outputs.pop(0)
        return SimpleNamespace(
            step_id=uuid.uuid4(),
            prepared=prepared,
            output_payload=payload,
        )

    monkeypatch.setattr("worker.pipeline.load_routing_registry", lambda: registry)
    monkeypatch.setattr("worker.pipeline.prepare_step_run", fake_prepare_step_run)
    monkeypatch.setattr("worker.pipeline.execute_step", fake_execute_step)
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

    assert result.route_target.id == route_target.id
    assert (result.selector_step is not None) is expected_selector
    assert (result.specialist_step is not None) is expected_specialist
    assert getattr(result.selected_specialist, "id", None) == expected_selected_id
    assert len(observed["manifest_updates"]) == len(observed["prepared"])


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
        requester_language=None,
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
    observed = {"requeue": 0}

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.triage.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.triage.load_ticket_context", lambda db, ticket_id: context)
    monkeypatch.setattr(
        "worker.triage.process_deferred_requeue",
        lambda db, ticket: observed.__setitem__("requeue", observed["requeue"] + 1),
    )

    prepared = symbols["_prepare_run"](settings, run_id=run.id)

    assert prepared is None
    assert run.status == "skipped"
    assert run.pipeline_version == "agent-pipeline-v1"
    assert run.ended_at is not None
    assert observed["requeue"] == 1


def _pipeline_result(
    *,
    route_target,
    specialist_payload: dict[str, object] | None,
    specialist_spec_id: str = "support",
):
    router_step = SimpleNamespace(
        step_id=uuid.uuid4(),
        prepared=SimpleNamespace(spec=SimpleNamespace(id="router"), model_name="gpt-router"),
        output_payload=_route_payload(route_target_id=route_target.id, routing_rationale="Router rationale."),
    )
    router_result = SimpleNamespace(route_target_id=route_target.id, routing_rationale="Router rationale.")
    if specialist_payload is None:
        specialist_step = None
        specialist_result = None
        final_step = router_step
    else:
        specialist_step = SimpleNamespace(
            step_id=uuid.uuid4(),
            prepared=SimpleNamespace(spec=SimpleNamespace(id=specialist_spec_id, output_contract="specialist_result"), model_name="gpt-specialist"),
            output_payload=specialist_payload,
        )
        specialist_result = _load_worker_symbols()["SpecialistResult"].model_validate(specialist_payload)
        final_step = specialist_step
    return SimpleNamespace(
        route_target=route_target,
        router_step=router_step,
        router_result=router_result,
        selector_step=None,
        selector_result=None,
        specialist_step=specialist_step,
        specialist_result=specialist_result,
        selected_specialist=SimpleNamespace(id=specialist_spec_id) if specialist_payload is not None else None,
        final_step=final_step,
    )


def test_apply_success_result_auto_publish_sets_route_target_and_final_fields(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        reference="T-000004",
        title="Permission issue",
        status="ai_triage",
        urgent=False,
        ai_confidence=0.5,
        impact_level="medium",
        development_needed=True,
        requester_language=None,
        last_processed_hash=None,
        last_ai_action=None,
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

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.triage.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.triage.load_ticket_context", lambda db, ticket_id: context)
    monkeypatch.setattr("worker.triage.publish_ai_internal_note", lambda *args, **kwargs: events.append("internal"))
    monkeypatch.setattr(
        "worker.triage.publish_ai_public_reply",
        lambda *args, **kwargs: events.append(f"public:{kwargs['last_ai_action']}"),
    )
    monkeypatch.setattr("worker.triage.create_ai_draft", lambda *args, **kwargs: events.append("draft"))
    monkeypatch.setattr("worker.triage.route_ticket_after_ai", lambda *args, **kwargs: events.append("route"))
    monkeypatch.setattr("worker.triage.process_deferred_requeue", lambda *args, **kwargs: events.append("requeue"))
    monkeypatch.setattr("worker.triage.write_run_manifest_snapshot", lambda *args, **kwargs: None)

    pipeline_result = _pipeline_result(
        route_target=_build_route_target(route_target_id="support", kind="direct_ai", mode="fixed", specialist_id="support"),
        specialist_payload=_specialist_payload(),
    )
    symbols["_apply_success_result"](settings, run_id=run.id, pipeline_result=pipeline_result)

    assert events == ["internal", "public:auto_public_reply", "requeue"]
    assert ticket.route_target_id == "support"
    assert ticket.requester_language == "en"
    assert ticket.ai_confidence == 0.5
    assert ticket.impact_level == "medium"
    assert ticket.development_needed is True
    assert ticket.last_processed_hash == publication_hash
    assert run.status == "succeeded"
    assert run.final_agent_spec_id == "support"
    assert run.final_output_contract == "specialist_result"
    assert run.final_output_json["publish_mode_recommendation"] == "auto_publish"


def test_apply_success_result_draft_for_human_keeps_direct_ai_ticket_in_ai_triage(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        reference="T-000005",
        title="Needs review",
        status="ai_triage",
        urgent=False,
        requester_language=None,
        last_processed_hash=None,
        last_ai_action=None,
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
    observed = {}

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.triage.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.triage.load_ticket_context", lambda db, ticket_id: context)
    monkeypatch.setattr("worker.triage.publish_ai_internal_note", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "worker.triage.create_ai_draft",
        lambda *args, **kwargs: observed.update({"next_status": kwargs["next_status"], "last_ai_action": kwargs["last_ai_action"]}),
    )
    monkeypatch.setattr("worker.triage.publish_ai_public_reply", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not publish")))
    monkeypatch.setattr("worker.triage.route_ticket_after_ai", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should draft")))
    monkeypatch.setattr("worker.triage.process_deferred_requeue", lambda *args, **kwargs: None)
    monkeypatch.setattr("worker.triage.write_run_manifest_snapshot", lambda *args, **kwargs: None)

    pipeline_result = _pipeline_result(
        route_target=_build_route_target(route_target_id="support", kind="direct_ai", mode="fixed", specialist_id="support"),
        specialist_payload=_specialist_payload(
            publish_mode_recommendation="draft_for_human",
            public_reply_markdown="Draft this reply for review.",
            internal_note_markdown="",
        ),
    )
    symbols["_apply_success_result"](settings, run_id=run.id, pipeline_result=pipeline_result)

    assert observed == {"next_status": "ai_triage", "last_ai_action": "draft_public_reply"}
    assert run.status == "human_review"
    assert run.final_output_contract == "specialist_result"


def test_apply_success_result_human_assist_none_synthesizes_terminal_handoff(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        reference="T-000006",
        title="Need a human",
        status="ai_triage",
        urgent=False,
        requester_language=None,
        last_processed_hash=None,
        last_ai_action=None,
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
    observed = {"route": None}

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.triage.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.triage.load_ticket_context", lambda db, ticket_id: context)
    monkeypatch.setattr("worker.triage.publish_ai_internal_note", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "worker.triage.route_ticket_after_ai",
        lambda *args, **kwargs: observed.update({"route": (kwargs["next_status"], kwargs["last_ai_action"])}),
    )
    monkeypatch.setattr("worker.triage.create_ai_draft", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not create draft")))
    monkeypatch.setattr("worker.triage.publish_ai_public_reply", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not publish")))
    monkeypatch.setattr("worker.triage.process_deferred_requeue", lambda *args, **kwargs: None)
    monkeypatch.setattr("worker.triage.write_run_manifest_snapshot", lambda *args, **kwargs: None)

    pipeline_result = _pipeline_result(
        route_target=_build_route_target(
            route_target_id="manual_review",
            kind="human_assist",
            mode="none",
            human_queue_status="waiting_on_dev_ti",
        ),
        specialist_payload=None,
    )
    symbols["_apply_success_result"](settings, run_id=run.id, pipeline_result=pipeline_result)

    assert observed["route"] == ("waiting_on_dev_ti", "manual_only")
    assert run.status == "human_review"
    assert run.final_agent_spec_id is None
    assert run.final_output_contract == "human_handoff_result"
    assert run.final_output_json["route_target_id"] == "manual_review"


def test_apply_success_result_human_assist_never_auto_publishes(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        reference="T-000007",
        title="Need manual queue",
        status="ai_triage",
        urgent=False,
        requester_language=None,
        last_processed_hash=None,
        last_ai_action=None,
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
    observed = {}

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.triage.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.triage.load_ticket_context", lambda db, ticket_id: context)
    monkeypatch.setattr("worker.triage.publish_ai_internal_note", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "worker.triage.create_ai_draft",
        lambda *args, **kwargs: observed.update({"next_status": kwargs["next_status"], "last_ai_action": kwargs["last_ai_action"]}),
    )
    monkeypatch.setattr("worker.triage.publish_ai_public_reply", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not auto publish")))
    monkeypatch.setattr("worker.triage.route_ticket_after_ai", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should draft")))
    monkeypatch.setattr("worker.triage.process_deferred_requeue", lambda *args, **kwargs: None)
    monkeypatch.setattr("worker.triage.write_run_manifest_snapshot", lambda *args, **kwargs: None)

    pipeline_result = _pipeline_result(
        route_target=_build_route_target(
            route_target_id="manual_review",
            kind="human_assist",
            mode="fixed",
            specialist_id="bug",
            human_queue_status="waiting_on_dev_ti",
        ),
        specialist_payload=_specialist_payload(
            publish_mode_recommendation="auto_publish",
            public_reply_markdown="Draft this update for the requester.",
        ),
        specialist_spec_id="bug",
    )
    symbols["_apply_success_result"](settings, run_id=run.id, pipeline_result=pipeline_result)

    assert observed == {"next_status": "waiting_on_dev_ti", "last_ai_action": "draft_public_reply"}
    assert run.status == "human_review"


def test_apply_success_result_direct_ai_manual_only_does_not_create_draft_when_policy_disables_drafts(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        reference="T-000007A",
        title="Need review without draft",
        status="ai_triage",
        urgent=False,
        requester_language=None,
        last_processed_hash=None,
        last_ai_action=None,
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
    observed = {"route": None}

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.triage.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.triage.load_ticket_context", lambda db, ticket_id: context)
    monkeypatch.setattr("worker.triage.publish_ai_internal_note", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "worker.triage.route_ticket_after_ai",
        lambda *args, **kwargs: observed.update({"route": (kwargs["next_status"], kwargs["last_ai_action"])}),
    )
    monkeypatch.setattr("worker.triage.create_ai_draft", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not create draft")))
    monkeypatch.setattr("worker.triage.publish_ai_public_reply", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not auto publish")))
    monkeypatch.setattr("worker.triage.process_deferred_requeue", lambda *args, **kwargs: None)
    monkeypatch.setattr("worker.triage.write_run_manifest_snapshot", lambda *args, **kwargs: None)

    pipeline_result = _pipeline_result(
        route_target=_build_route_target(
            route_target_id="support",
            kind="direct_ai",
            mode="fixed",
            specialist_id="support",
            allow_draft_for_human=False,
        ),
        specialist_payload=_specialist_payload(
            publish_mode_recommendation="draft_for_human",
            public_reply_markdown="Draft this reply for review.",
            internal_note_markdown="",
        ),
    )
    symbols["_apply_success_result"](settings, run_id=run.id, pipeline_result=pipeline_result)

    assert observed["route"] == ("ai_triage", "manual_only")
    assert run.status == "human_review"
    assert run.final_output_contract == "specialist_result"
    assert run.final_output_json["public_reply_markdown"] == "Draft this reply for review."


def test_apply_success_result_human_assist_manual_only_does_not_create_draft_when_policy_disables_drafts(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        reference="T-000007B",
        title="Need human review without draft",
        status="ai_triage",
        urgent=False,
        requester_language=None,
        last_processed_hash=None,
        last_ai_action=None,
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
    observed = {"route": None}

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.triage.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.triage.load_ticket_context", lambda db, ticket_id: context)
    monkeypatch.setattr("worker.triage.publish_ai_internal_note", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "worker.triage.route_ticket_after_ai",
        lambda *args, **kwargs: observed.update({"route": (kwargs["next_status"], kwargs["last_ai_action"])}),
    )
    monkeypatch.setattr("worker.triage.create_ai_draft", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not create draft")))
    monkeypatch.setattr("worker.triage.publish_ai_public_reply", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not auto publish")))
    monkeypatch.setattr("worker.triage.process_deferred_requeue", lambda *args, **kwargs: None)
    monkeypatch.setattr("worker.triage.write_run_manifest_snapshot", lambda *args, **kwargs: None)

    pipeline_result = _pipeline_result(
        route_target=_build_route_target(
            route_target_id="manual_review",
            kind="human_assist",
            mode="fixed",
            specialist_id="bug",
            human_queue_status="waiting_on_dev_ti",
            allow_draft_for_human=False,
        ),
        specialist_payload=_specialist_payload(
            publish_mode_recommendation="draft_for_human",
            public_reply_markdown="Draft this update for the requester.",
        ),
        specialist_spec_id="bug",
    )
    symbols["_apply_success_result"](settings, run_id=run.id, pipeline_result=pipeline_result)

    assert observed["route"] == ("waiting_on_dev_ti", "manual_only")
    assert run.status == "human_review"
    assert run.final_output_contract == "specialist_result"
    assert run.final_output_json["public_reply_markdown"] == "Draft this update for the requester."


def test_apply_success_result_supersedes_stale_run_without_publication(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        reference="T-000008",
        title="Stale input",
        status="ai_triage",
        urgent=False,
        requester_language=None,
        last_processed_hash=None,
        last_ai_action=None,
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
    monkeypatch.setattr("worker.triage.write_run_manifest_snapshot", lambda *args, **kwargs: None)

    pipeline_result = _pipeline_result(
        route_target=_build_route_target(route_target_id="support", kind="direct_ai", mode="fixed", specialist_id="support"),
        specialist_payload=_specialist_payload(),
    )
    symbols["_apply_success_result"](settings, run_id=run.id, pipeline_result=pipeline_result)

    assert run.status == "superseded"
    assert run.ended_at is not None
    assert observed == {"internal": 0, "public": 0, "requeue": 1}


def test_process_ai_run_marks_failed_when_publication_step_raises(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    prepared = SimpleNamespace(run_id=uuid.uuid4(), ticket_id=uuid.uuid4(), context=SimpleNamespace())
    pipeline_result = SimpleNamespace()
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
    monkeypatch.setattr("worker.triage.write_run_manifest_snapshot", lambda *args, **kwargs: None)

    symbols["_mark_failed"](settings, run_id=run.id, error_text="boom")

    assert run.status == "failed"
    assert run.pipeline_version == "agent-pipeline-v1"
    assert run.error_text == "boom"
    assert run.ended_at is not None
    assert ticket.status == "waiting_on_dev_ti"
    assert observed == {"failure_note": 1, "status_changes": 1, "requeue": 1}


def test_write_run_manifest_snapshot_serializes_route_target_metadata(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    run = SimpleNamespace(
        id=uuid.uuid4(),
        ticket_id=uuid.uuid4(),
        pipeline_version="agent-pipeline-v1",
        status="human_review",
        final_step_id=uuid.uuid4(),
        final_agent_spec_id="bug",
        final_output_contract="specialist_result",
        final_output_json=_specialist_payload(publish_mode_recommendation="draft_for_human"),
        error_text=None,
        ended_at=SimpleNamespace(isoformat=lambda: "2026-04-06T01:00:00+00:00"),
    )
    ticket = SimpleNamespace(id=run.ticket_id, route_target_id="manual_review", last_ai_action="draft_public_reply")
    steps = [
        SimpleNamespace(
            id=uuid.uuid4(),
            step_index=1,
            step_kind="router",
            agent_spec_id="router",
            agent_spec_version="1",
            output_contract="router_result",
            status="succeeded",
            model_name="gpt-router",
            prompt_path="/tmp/router-prompt.txt",
            schema_path="/tmp/router-schema.json",
            final_output_path="/tmp/router-final.json",
            stdout_jsonl_path="/tmp/router-stdout.jsonl",
            stderr_path="/tmp/router-stderr.txt",
            output_json=_route_payload(route_target_id="manual_review", routing_rationale="Needs human review."),
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            step_index=2,
            step_kind="selector",
            agent_spec_id="specialist-selector",
            agent_spec_version="1",
            output_contract="specialist_selector_result",
            status="succeeded",
            model_name="gpt-selector",
            prompt_path="/tmp/selector-prompt.txt",
            schema_path="/tmp/selector-schema.json",
            final_output_path="/tmp/selector-final.json",
            stdout_jsonl_path="/tmp/selector-stdout.jsonl",
            stderr_path="/tmp/selector-stderr.txt",
            output_json=_selector_payload(specialist_id="bug"),
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            step_index=3,
            step_kind="specialist",
            agent_spec_id="bug",
            agent_spec_version="2",
            output_contract="specialist_result",
            status="succeeded",
            model_name="gpt-bug",
            prompt_path="/tmp/bug-prompt.txt",
            schema_path="/tmp/bug-schema.json",
            final_output_path="/tmp/bug-final.json",
            stdout_jsonl_path="/tmp/bug-stdout.jsonl",
            stderr_path="/tmp/bug-stderr.txt",
            output_json=_specialist_payload(publish_mode_recommendation="draft_for_human"),
        ),
    ]
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
            name = getattr(model, "__name__", "")
            if name == "AIRun" and key == run.id:
                return run
            if name == "Ticket" and key == ticket.id:
                return ticket
            return None

        def execute(self, statement):
            return _FakeScalarResult(steps)

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

    metadata = observed["metadata"]
    assert metadata["route_target_id"] == "manual_review"
    assert metadata["route_target_label"] == "Manual Review"
    assert metadata["route_target_kind"] == "human_assist"
    assert metadata["selected_specialist_id"] == "bug"
    assert metadata["effective_publication_mode"] == "draft_for_human"
    assert observed["steps"][1]["selected_specialist_id"] == "bug"
    assert observed["steps"][2]["publish_mode_recommendation"] == "draft_for_human"


def test_write_run_manifest_snapshot_prefers_router_output_over_stale_ticket_route_target(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    run = SimpleNamespace(
        id=uuid.uuid4(),
        ticket_id=uuid.uuid4(),
        pipeline_version="agent-pipeline-v1",
        status="failed",
        final_step_id=None,
        final_agent_spec_id=None,
        final_output_contract=None,
        final_output_json=None,
        error_text="Selector failed",
        ended_at=SimpleNamespace(isoformat=lambda: "2026-04-06T01:05:00+00:00"),
    )
    ticket = SimpleNamespace(id=run.ticket_id, route_target_id="support", last_ai_action="auto_public_reply")
    steps = [
        SimpleNamespace(
            id=uuid.uuid4(),
            step_index=1,
            step_kind="router",
            agent_spec_id="router",
            agent_spec_version="1",
            output_contract="router_result",
            status="succeeded",
            model_name="gpt-router",
            prompt_path="/tmp/router-prompt.txt",
            schema_path="/tmp/router-schema.json",
            final_output_path="/tmp/router-final.json",
            stdout_jsonl_path="/tmp/router-stdout.jsonl",
            stderr_path="/tmp/router-stderr.txt",
            output_json=_route_payload(route_target_id="manual_review", routing_rationale="Needs human review."),
        ),
    ]
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
            name = getattr(model, "__name__", "")
            if name == "AIRun" and key == run.id:
                return run
            if name == "Ticket" and key == ticket.id:
                return ticket
            return None

        def execute(self, statement):
            return _FakeScalarResult(steps)

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

    metadata = observed["metadata"]
    assert metadata["route_target_id"] == "manual_review"
    assert metadata["route_target_label"] == "Manual Review"
    assert metadata["route_target_kind"] == "human_assist"


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
