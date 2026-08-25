from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace
import threading
import time
import uuid

import pytest

from shared.config import Settings
from shared.contracts import WORKSPACE_BOOTSTRAP_VERSION
from shared.routing_registry import RoutingRegistryError


def _make_settings(tmp_path: Path, *, codex_api_key: str | None = "test-key") -> Settings:
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
    from worker.main import ActiveRunTracker, WorkerIdentity, emit_worker_heartbeat, heartbeat_loop
    from worker.queue import claim_oldest_pending_run, recover_stale_runs
    from worker.output_contracts import HumanHandoffResult, RouterResult, SpecialistResult, SpecialistSelectorResult
    from worker.pipeline import PersistentCodexNonQuiescentCleanupError, execute_triage_pipeline
    from worker.prompt_renderer import render_agent_prompt
    from worker.publication_policy import resolve_effective_publication_mode
    from worker.run_ownership import RunOwnershipLost
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
        "ActiveRunTracker": ActiveRunTracker,
        "build_codex_command": build_codex_command,
        "build_requester_visible_fingerprint": build_requester_visible_fingerprint,
        "claim_oldest_pending_run": claim_oldest_pending_run,
        "emit_worker_heartbeat": emit_worker_heartbeat,
        "execute_step": execute_step,
        "execute_triage_pipeline": execute_triage_pipeline,
        "heartbeat_loop": heartbeat_loop,
        "HumanHandoffResult": HumanHandoffResult,
        "load_agent_spec": load_agent_spec,
        "process_ai_run": process_ai_run,
        "prepare_step_run": prepare_step_run,
        "PersistentCodexNonQuiescentCleanupError": PersistentCodexNonQuiescentCleanupError,
        "recover_stale_runs": recover_stale_runs,
        "render_agent_prompt": render_agent_prompt,
        "resolve_effective_publication_mode": resolve_effective_publication_mode,
        "RouterResult": RouterResult,
        "RunOwnershipLost": RunOwnershipLost,
        "SpecialistResult": SpecialistResult,
        "SpecialistSelectorResult": SpecialistSelectorResult,
        "StepRunError": StepRunError,
        "WorkerIdentity": WorkerIdentity,
        "write_run_manifest_snapshot": write_run_manifest_snapshot,
    }


def _make_context(
    *,
    ticket=None,
    public_body: str = "Public body",
    internal_body: str = "Internal body",
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
    return SimpleNamespace(
        ticket=ticket,
        requester_role=requester_role,
        requester_can_view_internal_messages=requester_can_view_internal_messages,
        public_messages=[public_message],
        internal_messages=[internal_message],
        public_attachments=list(public_attachments or []),
    )


def _make_attachment(
    tmp_path: Path,
    *,
    filename: str,
    contents: bytes,
    mime_type: str,
    sha256: str,
    width: int | None = None,
    height: int | None = None,
):
    source_path = tmp_path / "source_attachments" / filename
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(contents)
    return SimpleNamespace(
        id=uuid.uuid4(),
        original_filename=filename,
        stored_path=str(source_path),
        mime_type=mime_type,
        size_bytes=len(contents),
        sha256=sha256,
        width=width,
        height=height,
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
        "support": SimpleNamespace(
            id="support",
            display_name="support",
            spec=SimpleNamespace(id="support", version="2", output_contract="specialist_result"),
        ),
        "bug": SimpleNamespace(
            id="bug",
            display_name="bug",
            spec=SimpleNamespace(id="bug", version="2", output_contract="specialist_result"),
        ),
        "feature": SimpleNamespace(
            id="feature",
            display_name="feature",
            spec=SimpleNamespace(id="feature", version="2", output_contract="specialist_result"),
        ),
        "software-architect": SimpleNamespace(
            id="software-architect",
            display_name="software-architect",
            spec=SimpleNamespace(id="software-architect", version="2", output_contract="specialist_result"),
        ),
        "software-data-engineer": SimpleNamespace(
            id="software-data-engineer",
            display_name="software-data-engineer",
            spec=SimpleNamespace(id="software-data-engineer", version="1", output_contract="specialist_result"),
        ),
    }
    route_targets_by_id = {route_target.id: route_target for route_target in route_targets}

    def require_enabled_route_target(route_target_id: str):
        return route_targets_by_id[route_target_id]

    def require_enabled_route_target_for_requester(route_target_id: str, requester_role: str):
        return route_targets_by_id[route_target_id]

    def require_specialist(specialist_id: str):
        return specialists[specialist_id]

    def require_enabled_specialist(specialist_id: str):
        return specialists[specialist_id]

    def resolve_forced_manual_rerun_choice(*, route_target_id: str, specialist_id: str):
        route_target = route_targets_by_id[route_target_id]
        return SimpleNamespace(
            route_target_id=route_target.id,
            route_target_label=route_target.label,
            specialist_id=specialist_id,
            specialist_display_name=specialists[specialist_id].id,
        )

    def candidate_specialists_for_target(route_target_id: str, requester_role: str | None = None):
        selection = route_targets_by_id[route_target_id].handler.specialist_selection
        return tuple(specialists[specialist_id] for specialist_id in selection.candidate_specialist_ids)

    return SimpleNamespace(
        router_spec=router_spec,
        selector_spec=selector_spec,
        require_enabled_route_target=require_enabled_route_target,
        require_enabled_route_target_for_requester=require_enabled_route_target_for_requester,
        require_specialist=require_specialist,
        require_enabled_specialist=require_enabled_specialist,
        require_route_target=require_enabled_route_target,
        resolve_forced_manual_rerun_choice=resolve_forced_manual_rerun_choice,
        candidate_specialists_for_target=candidate_specialists_for_target,
    )


class _FakeDb:
    def __init__(self, *, run=None, ticket=None):
        self.run = run
        self.ticket = ticket

    def execute(self, statement):
        entity = statement.column_descriptions[0].get("entity") if getattr(statement, "column_descriptions", None) else None
        if getattr(entity, "__name__", "") != "AIRun" or self.run is None:
            return _FakeWorkerStateResult([])
        for criterion in getattr(statement, "_where_criteria", ()):
            left = getattr(criterion, "left", None)
            right = getattr(criterion, "right", None)
            key = getattr(left, "name", None)
            if key is None or not hasattr(right, "value") or not hasattr(self.run, key):
                continue
            if getattr(self.run, key) != right.value:
                return _FakeWorkerStateResult([])
        return _FakeWorkerStateResult([self.run])

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

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self

    def __iter__(self):
        return iter(self._rows)


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
        entity = statement.column_descriptions[0].get("entity") if getattr(statement, "column_descriptions", None) else None
        if getattr(entity, "__name__", "") == "CodexSession":
            return _FakeWorkerStateResult([])
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


class _ClaimRunResult:
    def __init__(self, run):
        self._run = run

    def scalar_one_or_none(self):
        return self._run


class _ClaimRunDb:
    def __init__(self, run):
        self.run = run

    def execute(self, statement):
        return _ClaimRunResult(self.run)


class _QueueRecoveryDb:
    def __init__(self, *, stale_runs, steps_by_run_id, tickets_by_id):
        self.stale_runs = list(stale_runs)
        self.steps_by_run_id = steps_by_run_id
        self.tickets_by_id = tickets_by_id
        self.executed = []

    def execute(self, statement):
        self.executed.append(statement)
        entity = statement.column_descriptions[0].get("entity") if getattr(statement, "column_descriptions", None) else None
        entity_name = getattr(entity, "__name__", "")
        if entity_name == "AIRun":
            return _FakeWorkerStateResult(self.stale_runs)
        if entity_name == "AIRunStep":
            run_id = None
            for value in getattr(statement, "_where_criteria", ()):
                right = getattr(value, "right", None)
                if hasattr(right, "value"):
                    run_id = right.value
                    break
            return _FakeWorkerStateResult(self.steps_by_run_id.get(run_id, []))
        return _FakeWorkerStateResult([])

    def get(self, model, key):
        name = getattr(model, "__name__", "")
        if name == "Ticket":
            return self.tickets_by_id.get(key)
        return None


class _PersistentQueueRecoveryDb(_QueueRecoveryDb):
    def __init__(
        self,
        *,
        stale_runs,
        steps_by_run_id,
        tickets_by_id,
        turns_by_run_id,
        sessions_by_id,
        conversations_by_id,
    ):
        super().__init__(stale_runs=stale_runs, steps_by_run_id=steps_by_run_id, tickets_by_id=tickets_by_id)
        self.turns_by_run_id = turns_by_run_id
        self.sessions_by_id = sessions_by_id
        self.conversations_by_id = conversations_by_id
        self.added = []

    def _criterion_value(self, statement, column_name: str):
        for criterion in getattr(statement, "_where_criteria", ()):
            left = getattr(criterion, "left", None)
            right = getattr(criterion, "right", None)
            if getattr(left, "name", None) == column_name and hasattr(right, "value"):
                return right.value
        return None

    def execute(self, statement):
        self.executed.append(statement)
        descriptions = statement.column_descriptions if getattr(statement, "column_descriptions", None) else []
        first_name = descriptions[0].get("name") if descriptions else None
        entity = descriptions[0].get("entity") if descriptions else None
        if first_name == "coalesce":
            return _FakePersistentScalarResult(0)
        entity_name = getattr(entity, "__name__", "")
        if entity_name in {"AIRun", "AIRunStep"}:
            return super().execute(statement)
        if entity_name == "CodexTurn":
            run_id = self._criterion_value(statement, "ai_run_id")
            return _FakeWorkerStateResult(self.turns_by_run_id.get(run_id, []))
        if entity_name == "CodexSession":
            session_id = self._criterion_value(statement, "id")
            session = self.sessions_by_id.get(session_id)
            return _FakeWorkerStateResult([session] if session is not None else [])
        return _FakeWorkerStateResult([])

    def get(self, model, key):
        name = getattr(model, "__name__", "")
        if name == "CodexConversation":
            return self.conversations_by_id.get(key)
        return super().get(model, key)

    def add(self, item):
        self.added.append(item)


class _FakePersistentDb:
    def __init__(self):
        self.added = []
        self.conversation = None

    def add(self, item):
        self.added.append(item)

    def flush(self):
        return None

    def get(self, model, key):
        if getattr(model, "__name__", "") == "CodexConversation":
            return self.conversation
        return None

    def execute(self, statement):
        query_text = str(statement)
        if "count(*)" in query_text or "coalesce(max(codex_turns.turn_index)" in query_text:
            return _FakePersistentScalarResult(0)
        return _FakeWorkerStateResult([])


class _FakePipeProcess:
    def __init__(
        self,
        *,
        stdout_text: str,
        stderr_text: str = "",
        timeout_on_first_wait: bool = False,
        returncode: int = 0,
    ):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO(stdout_text)
        self.stderr = io.StringIO(stderr_text)
        self._timeout_on_first_wait = timeout_on_first_wait
        self._wait_calls = 0
        self.killed = False
        self.returncode = returncode

    def wait(self, timeout=None):
        self._wait_calls += 1
        if self._timeout_on_first_wait and self._wait_calls == 1:
            raise subprocess.TimeoutExpired(cmd=["codex"], timeout=timeout)
        return 124 if self.killed else self.returncode

    def kill(self):
        self.killed = True

    def terminate(self):
        self.killed = True


class _BlockingStdin:
    def __init__(self):
        self.write_started = threading.Event()
        self.closed = threading.Event()

    def write(self, _value):
        self.write_started.set()
        self.closed.wait(timeout=30)
        raise OSError("stdin write interrupted")

    def close(self):
        self.closed.set()


class _BrokenPipeStdin:
    def write(self, _value):
        raise BrokenPipeError("stdin pipe closed")

    def close(self):
        raise BrokenPipeError("stdin pipe closed")


class _BlockingReadPipe:
    def __init__(self, initial_lines=()):
        self._lines = list(initial_lines)
        self.closed = threading.Event()

    def readline(self):
        if self._lines:
            return self._lines.pop(0)
        self.closed.wait(timeout=30)
        return ""

    def close(self):
        self.closed.set()


class _NonClosingBlockingReadPipe:
    def __init__(self):
        self.read_started = threading.Event()
        self.release_read = threading.Event()
        self.close_called = threading.Event()

    def readline(self):
        self.read_started.set()
        self.release_read.wait(timeout=30)
        return ""

    def close(self):
        self.close_called.set()


class _BlockingWriteHandle:
    def __init__(self):
        self.write_started = threading.Event()
        self.write_finished = threading.Event()
        self.release_write = threading.Event()
        self.lines = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def write(self, line):
        self.write_started.set()
        self.release_write.wait(timeout=30)
        self.lines.append(line)
        self.write_finished.set()

    def flush(self):
        return None


class _DeadlineFakeProcess:
    def __init__(
        self,
        *,
        stdin=None,
        stdout=None,
        stderr=None,
        returncode: int = 0,
        timeout_wait: bool = False,
    ):
        self.stdin = stdin if stdin is not None else io.StringIO()
        self.stdout = stdout if stdout is not None else io.StringIO("")
        self.stderr = stderr if stderr is not None else io.StringIO("")
        self.returncode = returncode
        self.timeout_wait = timeout_wait
        self.terminated = False
        self.killed = False
        self.wait_calls = []

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.timeout_wait:
            if timeout:
                time.sleep(min(float(timeout), 0.05))
            raise subprocess.TimeoutExpired(cmd=["codex"], timeout=timeout)
        return 143 if self.terminated else self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class _FakePersistentScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value

    def scalar_one_or_none(self):
        return self._value


def _prepare_persistent_test_step(tmp_path: Path, *, timeout_seconds: float | None = None):
    symbols = _load_worker_symbols()
    settings = Settings(**{**_make_settings(tmp_path).__dict__, "codex_conversations_enabled": True})
    context = _make_context()
    prepared = symbols["prepare_step_run"](
        settings,
        run_id=uuid.uuid4(),
        ticket_id=context.ticket.id,
        worker_instance_id="worker-test",
        step_index=2,
        step_kind="specialist",
        spec=symbols["load_agent_spec"]("support"),
        context=context,
        router_result=symbols["RouterResult"].model_validate(_route_payload()),
        target_route_target_id="support",
    )
    if timeout_seconds is not None:
        prepared = replace(prepared, timeout_seconds=timeout_seconds)
    return symbols, settings, prepared


def _persistent_step_for_test(persistent_codex, tmp_path: Path, *, resumed: bool = False):
    return persistent_codex.PreparedPersistentSpecialistStep(
        step_id=uuid.uuid4(),
        turn_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        command_spec=persistent_codex.PersistentCommandSpec(
            command=["codex", "exec"],
            env={},
            runtime_codex_home=tmp_path / ".codex" / "ticket-prod",
            resumed=resumed,
        ),
    )


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
    attachment = _make_attachment(
        tmp_path,
        filename="Example Screenshot.png",
        contents=b"fake image data",
        mime_type="image/png",
        sha256="sha-image",
        width=40,
        height=20,
    )
    context = _make_context(public_attachments=[attachment])
    spec = symbols["load_agent_spec"]("support")
    router_result = symbols["RouterResult"].model_validate(_route_payload())

    prepared = symbols["prepare_step_run"](
        settings,
        run_id=uuid.uuid4(),
        ticket_id=context.ticket.id,
        worker_instance_id="worker-test",
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
    assert len(prepared.image_paths) == 1
    assert prepared.image_paths[0].is_file()
    assert prepared.image_paths[0].parent == prepared.paths.run_dir / "attachments"
    assert prepared.image_paths[0].read_bytes() == b"fake image data"
    assert prepared.public_attachments[0].original_filename == "Example Screenshot.png"
    assert prepared.public_attachments[0].workspace_path.startswith(f"runs/{context.ticket.id}/")
    assert prepared.public_attachments[0].absolute_path == str(prepared.image_paths[0].resolve())
    assert "Attachment workspace root:" in prepared.prompt
    assert "Public attachments:" in prepared.prompt
    assert "Example Screenshot.png" in prepared.prompt
    assert prepared.public_attachments[0].workspace_path in prepared.prompt


def test_prepare_step_run_projects_non_image_attachment_into_workspace_prompt_without_image_flag(tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    attachment = _make_attachment(
        tmp_path,
        filename="Quarterly Report.xls",
        contents=b"fake spreadsheet",
        mime_type="application/vnd.ms-excel",
        sha256="sha-xls",
    )
    context = _make_context(public_attachments=[attachment])
    spec = symbols["load_agent_spec"]("support")
    router_result = symbols["RouterResult"].model_validate(_route_payload())

    prepared = symbols["prepare_step_run"](
        settings,
        run_id=uuid.uuid4(),
        ticket_id=context.ticket.id,
        worker_instance_id="worker-test",
        step_index=2,
        step_kind="specialist",
        spec=spec,
        context=context,
        router_result=router_result,
        target_route_target_id="support",
    )

    assert prepared.image_paths == []
    assert prepared.public_attachments[0].is_image is False
    assert Path(prepared.public_attachments[0].absolute_path).is_file()
    assert Path(prepared.public_attachments[0].absolute_path).parent == prepared.paths.run_dir / "attachments"
    assert Path(prepared.public_attachments[0].absolute_path).read_bytes() == b"fake spreadsheet"
    assert "Quarterly Report.xls" in prepared.prompt
    assert prepared.public_attachments[0].workspace_path in prepared.prompt
    assert "image_attachment=no" in prepared.prompt


def test_prepare_step_run_skips_missing_attachment_file_without_failing(tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    missing_path = tmp_path / "source_attachments" / "03 - MARCO.xlsx"
    attachment = SimpleNamespace(
        id=uuid.uuid4(),
        original_filename="03 - MARCO.xlsx",
        stored_path=str(missing_path),
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=123,
        sha256="sha-missing",
        width=None,
        height=None,
    )
    context = _make_context(public_attachments=[attachment])
    spec = symbols["load_agent_spec"]("support")
    router_result = symbols["RouterResult"].model_validate(_route_payload())

    prepared = symbols["prepare_step_run"](
        settings,
        run_id=uuid.uuid4(),
        ticket_id=context.ticket.id,
        worker_instance_id="worker-test",
        step_index=2,
        step_kind="specialist",
        spec=spec,
        context=context,
        router_result=router_result,
        target_route_target_id="support",
    )

    assert prepared.public_attachments == ()
    assert prepared.image_paths == []
    assert "Public attachments:" in prepared.prompt
    assert "(none)" in prepared.prompt


def test_prepare_step_run_keeps_existing_attachments_when_some_files_are_missing(tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    present_attachment = _make_attachment(
        tmp_path,
        filename="Quarterly Report.xls",
        contents=b"fake spreadsheet",
        mime_type="application/vnd.ms-excel",
        sha256="sha-xls",
    )
    missing_attachment = SimpleNamespace(
        id=uuid.uuid4(),
        original_filename="03 - MARCO.xlsx",
        stored_path=str(tmp_path / "source_attachments" / "03 - MARCO.xlsx"),
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=456,
        sha256="sha-missing",
        width=None,
        height=None,
    )
    context = _make_context(public_attachments=[present_attachment, missing_attachment])
    spec = symbols["load_agent_spec"]("support")
    router_result = symbols["RouterResult"].model_validate(_route_payload())

    prepared = symbols["prepare_step_run"](
        settings,
        run_id=uuid.uuid4(),
        ticket_id=context.ticket.id,
        worker_instance_id="worker-test",
        step_index=2,
        step_kind="specialist",
        spec=spec,
        context=context,
        router_result=router_result,
        target_route_target_id="support",
    )

    assert len(prepared.public_attachments) == 1
    assert prepared.public_attachments[0].original_filename == "Quarterly Report.xls"
    assert "Quarterly Report.xls" in prepared.prompt
    assert "03 - MARCO.xlsx" not in prepared.prompt


def test_prepare_step_run_rejects_symlinked_attachment_dir_outside_workspace_before_copy(tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    attachment = _make_attachment(
        tmp_path,
        filename="evidence.txt",
        contents=b"sensitive evidence",
        mime_type="text/plain",
        sha256="sha-text",
    )
    context = _make_context(public_attachments=[attachment])
    spec = symbols["load_agent_spec"]("support")
    router_result = symbols["RouterResult"].model_validate(_route_payload())
    run_id = uuid.uuid4()
    run_dir = settings.runs_dir / str(context.ticket.id) / str(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "attachments").symlink_to(outside_dir, target_is_directory=True)

    with pytest.raises(symbols["StepRunError"], match="escaped the workspace"):
        symbols["prepare_step_run"](
            settings,
            run_id=run_id,
            ticket_id=context.ticket.id,
            worker_instance_id="worker-test",
            step_index=2,
            step_kind="specialist",
            spec=spec,
            context=context,
            router_result=router_result,
            target_route_target_id="support",
        )

    assert list(outside_dir.iterdir()) == []


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
        worker_instance_id="worker-test",
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
    assert env["CODEX_HOME"] == str(settings.resolved_codex_home)


def test_build_persistent_codex_command_includes_initial_exec_controls(tmp_path):
    pytest.importorskip("sqlalchemy")

    from worker.persistent_codex import build_persistent_codex_command

    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    settings = Settings(**{**settings.__dict__, "codex_conversations_enabled": True})
    context = _make_context()
    spec = symbols["load_agent_spec"]("support")
    router_result = symbols["RouterResult"].model_validate(_route_payload())
    prepared = symbols["prepare_step_run"](
        settings,
        run_id=uuid.uuid4(),
        ticket_id=context.ticket.id,
        worker_instance_id="worker-test",
        step_index=2,
        step_kind="specialist",
        spec=spec,
        context=context,
        router_result=router_result,
        target_route_target_id="support",
    )

    command_spec = build_persistent_codex_command(
        settings,
        prepared=prepared,
        thread_id=None,
    )

    assert command_spec.command[:4] == ["codex", "--ask-for-approval", "never", "exec"]
    assert "--sandbox" in command_spec.command
    assert "resume" not in command_spec.command
    assert "--strict-config" in command_spec.command
    assert 'sandbox_mode="read-only"' in command_spec.command
    assert 'web_search="disabled"' in command_spec.command
    assert "web_search_request" in command_spec.command
    assert "standalone_web_search" in command_spec.command
    assert str(prepared.paths.schema_path) in command_spec.command
    assert str(prepared.paths.final_output_path) in command_spec.command
    assert command_spec.command[-1] == "-"
    assert command_spec.env["CODEX_HOME"] == str(settings.resolved_codex_home)
    assert command_spec.resumed is False


def test_build_persistent_codex_command_uses_explicit_thread_id_on_resume(tmp_path):
    pytest.importorskip("sqlalchemy")

    from worker.persistent_codex import build_persistent_codex_command

    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    settings = Settings(**{**settings.__dict__, "codex_conversations_enabled": True})
    context = _make_context()
    spec = symbols["load_agent_spec"]("support")
    router_result = symbols["RouterResult"].model_validate(_route_payload())
    prepared = symbols["prepare_step_run"](
        settings,
        run_id=uuid.uuid4(),
        ticket_id=context.ticket.id,
        worker_instance_id="worker-test",
        step_index=2,
        step_kind="specialist",
        spec=spec,
        context=context,
        router_result=router_result,
        target_route_target_id="support",
    )

    command_spec = build_persistent_codex_command(
        settings,
        prepared=prepared,
        thread_id="thread-123",
    )

    assert command_spec.command[:5] == ["codex", "--ask-for-approval", "never", "exec", "resume"]
    assert "--sandbox" not in command_spec.command
    assert "thread-123" in command_spec.command
    assert command_spec.command[-2:] == ["thread-123", "-"]
    assert command_spec.resumed is True


def test_persistent_lease_conflict_allows_expired_owner_recovery():
    pytest.importorskip("sqlalchemy")

    from worker.persistent_codex import _conversation_lease_conflict_reason

    now = datetime(2026, 8, 24, tzinfo=timezone.utc)
    session = SimpleNamespace(
        lease_owner_run_id=uuid.uuid4(),
        lease_worker_instance_id="worker-other",
        lease_expires_at=now - timedelta(seconds=5),
    )
    reason = _conversation_lease_conflict_reason(
        session,
        run_id=uuid.uuid4(),
        worker_instance_id="worker-test",
        now=now,
    )

    assert reason is None


def test_persistent_stdout_event_persists_thread_started_immediately(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")

    from worker import persistent_codex

    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    context = _make_context()
    spec = symbols["load_agent_spec"]("support")
    router_result = symbols["RouterResult"].model_validate(_route_payload())
    prepared = symbols["prepare_step_run"](
        settings,
        run_id=uuid.uuid4(),
        ticket_id=context.ticket.id,
        worker_instance_id="worker-test",
        step_index=2,
        step_kind="specialist",
        spec=spec,
        context=context,
        router_result=router_result,
        target_route_target_id="support",
    )
    persistent = persistent_codex.PreparedPersistentSpecialistStep(
        step_id=uuid.uuid4(),
        turn_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        command_spec=persistent_codex.PersistentCommandSpec(
            command=["codex", "exec"],
            env={},
            runtime_codex_home=tmp_path / ".codex" / "ticket-prod",
            resumed=False,
        ),
    )
    run = SimpleNamespace(id=prepared.run_id, last_heartbeat_at=None)
    session = SimpleNamespace(
        id=persistent.session_id,
        lease_owner_run_id=prepared.run_id,
        lease_worker_instance_id=prepared.worker_instance_id,
        thread_id=None,
        status="pending",
        started_at=None,
        lease_heartbeat_at=None,
        lease_expires_at=None,
    )
    turn = SimpleNamespace(id=persistent.turn_id, accepted_at=None)
    step = SimpleNamespace(id=persistent.step_id, ended_at=None)
    fake_db = _FakePersistentDb()
    fake_db.conversation = SimpleNamespace(id=persistent.conversation_id, status="active")
    captured = []

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr(persistent_codex, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        persistent_codex,
        "_load_locked_owned_runtime_records",
        lambda db, *, prepared, persistent: (run, session, turn, step),
    )
    monkeypatch.setattr(
        persistent_codex,
        "_append_turn_outcome",
        lambda db, *, turn_id, outcome_kind, payload_json: captured.append((outcome_kind, payload_json)),
    )

    state = persistent_codex.PersistentStreamState()
    persistent_codex._record_stdout_event(
        settings,
        prepared=prepared,
        persistent=persistent,
        state=state,
        raw_line='{"type":"thread.started","thread_id":"thread-1"}',
    )

    assert session.thread_id == "thread-1"
    assert session.status == "active"
    assert turn.accepted_at is None
    assert state.accepted is False
    persistent_codex._record_stdout_event(
        settings,
        prepared=prepared,
        persistent=persistent,
        state=state,
        raw_line='{"type":"turn.started"}',
    )
    assert turn.accepted_at is not None
    assert state.accepted is True
    assert state.next_item_index == 3
    assert fake_db.added[0].item_kind == "thread.started"
    assert captured == [("accepted", {"event_type": "turn.started", "thread_id": "thread-1"})]


def test_prepare_persistent_specialist_step_rejects_unexpired_conversation_overlap(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")

    from worker import persistent_codex

    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    settings = Settings(**{**settings.__dict__, "codex_conversations_enabled": True})
    context = _make_context()
    spec = symbols["load_agent_spec"]("support")
    router_result = symbols["RouterResult"].model_validate(_route_payload())
    prepared = symbols["prepare_step_run"](
        settings,
        run_id=uuid.uuid4(),
        ticket_id=context.ticket.id,
        worker_instance_id="worker-test",
        step_index=2,
        step_kind="specialist",
        spec=spec,
        context=context,
        router_result=router_result,
        target_route_target_id="support",
    )
    fake_db = _FakePersistentDb()
    run = SimpleNamespace(id=prepared.run_id, worker_pid=1234, last_heartbeat_at=None)
    conversation = SimpleNamespace(id=uuid.uuid4(), status="active")
    session = SimpleNamespace(
        id=uuid.uuid4(),
        conversation_id=conversation.id,
        thread_id=None,
        lease_owner_run_id=uuid.uuid4(),
        lease_worker_instance_id="worker-stale",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        lease_acquired_at=None,
        lease_heartbeat_at=None,
        status="active",
        started_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
        ended_at=None,
    )

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr(persistent_codex, "session_scope", fake_session_scope)
    monkeypatch.setattr(persistent_codex, "load_owned_running_run", lambda db, **kwargs: run)
    monkeypatch.setattr(persistent_codex, "_load_locked_conversation", lambda db, **kwargs: conversation)
    monkeypatch.setattr(persistent_codex, "_load_locked_active_session", lambda db, **kwargs: session)
    prompt_state = SimpleNamespace(
        conversation_id=conversation.id,
        active_session_id=session.id,
        recovery_required=False,
    )
    with pytest.raises(symbols["StepRunError"], match="already leased"):
        persistent_codex.prepare_persistent_specialist_step(
            settings,
            prepared=prepared,
            prompt_state=prompt_state,
        )
    assert session.status == "active"
    assert session.ended_at is None


def test_execute_persistent_specialist_step_bounds_blocked_prompt_delivery(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")

    from worker import persistent_codex

    symbols, settings, prepared = _prepare_persistent_test_step(tmp_path, timeout_seconds=0.05)
    prepared = replace(prepared, prompt="x" * (1024 * 1024))
    persistent = _persistent_step_for_test(persistent_codex, tmp_path)
    blocking_stdin = _BlockingStdin()
    fake_process = _DeadlineFakeProcess(
        stdin=blocking_stdin,
        stdout=io.StringIO(""),
        stderr=io.StringIO(""),
        timeout_wait=True,
    )
    finalized = {}

    monkeypatch.setattr(persistent_codex, "_STREAM_JOIN_SECONDS", 0.01)
    monkeypatch.setattr(persistent_codex, "_PROCESS_TERMINATION_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(persistent_codex, "_PROCESS_KILL_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(persistent_codex, "_WRITER_CLEANUP_JOIN_SECONDS", 0.01)
    monkeypatch.setattr(
        persistent_codex,
        "prepare_persistent_specialist_step",
        lambda settings, prepared, prompt_state: persistent,
    )
    monkeypatch.setattr(persistent_codex.subprocess, "Popen", lambda *args, **kwargs: fake_process)
    monkeypatch.setattr(
        persistent_codex,
        "_finalize_persistent_step",
        lambda settings, **kwargs: finalized.update(kwargs),
    )

    started_at = time.monotonic()
    with pytest.raises(symbols["StepRunError"], match="timed out before Codex completion"):
        persistent_codex.execute_persistent_specialist_step(
            settings,
            prepared=prepared,
            prompt_state=SimpleNamespace(),
        )
    elapsed = time.monotonic() - started_at

    assert elapsed < 1.0
    assert blocking_stdin.write_started.is_set()
    assert blocking_stdin.closed.is_set()
    assert fake_process.terminated is True
    assert finalized["turn_status"] == "timed_out"
    assert finalized["step_status"] == "failed"
    assert finalized["output_payload"] is None


def test_execute_persistent_specialist_step_cleans_pipe_holding_descendant_before_finalize(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")

    from worker import persistent_codex

    symbols, settings, prepared = _prepare_persistent_test_step(tmp_path)
    prepared.paths.final_output_path.write_text(json.dumps(_specialist_payload()), encoding="utf-8")
    persistent = _persistent_step_for_test(persistent_codex, tmp_path, resumed=True)
    stdout = _BlockingReadPipe(['{"type":"turn.completed"}\n'])
    fake_process = _DeadlineFakeProcess(stdout=stdout, stderr=io.StringIO(""), returncode=0)
    finalized = {}
    record_calls = []

    monkeypatch.setattr(persistent_codex, "_STREAM_JOIN_SECONDS", 0.01)
    monkeypatch.setattr(persistent_codex, "_PROCESS_TERMINATION_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(persistent_codex, "_PROCESS_KILL_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(
        persistent_codex,
        "prepare_persistent_specialist_step",
        lambda settings, prepared, prompt_state: persistent,
    )
    monkeypatch.setattr(persistent_codex.subprocess, "Popen", lambda *args, **kwargs: fake_process)

    def record_event(settings, *, prepared, persistent, state, raw_line):
        record_calls.append(raw_line)
        state.accepted = True
        state.completed = True

    def finalize(settings, **kwargs):
        assert stdout.closed.is_set()
        assert fake_process.terminated is True
        finalized.update(kwargs)

    monkeypatch.setattr(persistent_codex, "_record_stdout_event", record_event)
    monkeypatch.setattr(persistent_codex, "_finalize_persistent_step", finalize)

    with pytest.raises(symbols["StepRunError"], match="output streams did not close"):
        persistent_codex.execute_persistent_specialist_step(
            settings,
            prepared=prepared,
            prompt_state=SimpleNamespace(),
        )

    assert record_calls == ['{"type":"turn.completed"}']
    assert finalized["step_status"] == "failed"
    assert finalized["output_payload"] is None


def test_execute_persistent_specialist_step_does_not_finalize_with_live_output_pump(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")

    from worker import persistent_codex

    symbols, settings, prepared = _prepare_persistent_test_step(tmp_path)
    prepared.paths.final_output_path.write_text(json.dumps(_specialist_payload()), encoding="utf-8")
    persistent = _persistent_step_for_test(persistent_codex, tmp_path, resumed=True)
    stdout = _NonClosingBlockingReadPipe()
    fake_process = _DeadlineFakeProcess(stdout=stdout, stderr=io.StringIO(""), returncode=0)
    finalized = {}

    monkeypatch.setattr(persistent_codex, "_STREAM_JOIN_SECONDS", 0.01)
    monkeypatch.setattr(persistent_codex, "_PROCESS_TERMINATION_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(persistent_codex, "_PROCESS_KILL_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(
        persistent_codex,
        "prepare_persistent_specialist_step",
        lambda settings, prepared, prompt_state: persistent,
    )
    monkeypatch.setattr(persistent_codex.subprocess, "Popen", lambda *args, **kwargs: fake_process)
    monkeypatch.setattr(
        persistent_codex,
        "_record_stdout_event",
        lambda *args, **kwargs: pytest.fail("blocked pump should not persist stdout events"),
    )
    monkeypatch.setattr(
        persistent_codex,
        "_finalize_persistent_step",
        lambda settings, **kwargs: finalized.update(kwargs),
    )

    started_at = time.monotonic()
    try:
        with pytest.raises(
            symbols["PersistentCodexNonQuiescentCleanupError"],
            match="output streams did not close after cleanup",
        ):
            persistent_codex.execute_persistent_specialist_step(
                settings,
                prepared=prepared,
                prompt_state=SimpleNamespace(),
            )
        assert stdout.read_started.is_set()
        assert stdout.close_called.is_set()
        assert fake_process.terminated is True
        assert finalized == {}
        assert time.monotonic() - started_at < 1.0
    finally:
        stdout.release_read.set()


def _pid_is_running(pid: int) -> bool:
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        state = proc_stat.read_text(encoding="utf-8").split()[2]
    except (FileNotFoundError, IndexError, OSError):
        state = None
    if state == "Z":
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_terminate_process_group_kills_pipe_holding_descendant_after_leader_exits():
    pytest.importorskip("sqlalchemy")

    from worker import persistent_codex

    leader_script = r"""
import subprocess
import sys
import time

child_script = '''
import os
import signal
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
print(os.getpid(), flush=True)
time.sleep(30)
'''

subprocess.Popen([sys.executable, "-c", child_script])
time.sleep(0.05)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", leader_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert process.stdout is not None
    child_pid = int(process.stdout.readline().strip())
    process.wait(timeout=1)

    try:
        returned = persistent_codex._terminate_process_group(
            process,
            grace_seconds=0.05,
            kill_wait_seconds=0.2,
        )
        deadline = time.monotonic() + 1.0
        while _pid_is_running(child_pid) and time.monotonic() < deadline:
            time.sleep(0.01)

        assert returned == 0
        assert not _pid_is_running(child_pid)
    finally:
        if _pid_is_running(child_pid):
            os.kill(child_pid, signal.SIGKILL)


def test_execute_persistent_specialist_step_does_not_finalize_during_pre_record_stdout_write(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")

    from worker import persistent_codex

    symbols, settings, prepared = _prepare_persistent_test_step(tmp_path)
    prepared.paths.final_output_path.write_text(json.dumps(_specialist_payload()), encoding="utf-8")
    persistent = _persistent_step_for_test(persistent_codex, tmp_path, resumed=True)
    stdout = _BlockingReadPipe(['{"type":"turn.completed"}\n'])
    fake_process = _DeadlineFakeProcess(stdout=stdout, stderr=io.StringIO(""), returncode=0)
    blocking_handle = _BlockingWriteHandle()
    finalized = {}
    record_calls = []
    original_path_open = Path.open

    monkeypatch.setattr(persistent_codex, "_STREAM_JOIN_SECONDS", 0.01)
    monkeypatch.setattr(persistent_codex, "_PROCESS_TERMINATION_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(persistent_codex, "_PROCESS_KILL_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(
        persistent_codex,
        "prepare_persistent_specialist_step",
        lambda settings, prepared, prompt_state: persistent,
    )
    monkeypatch.setattr(persistent_codex.subprocess, "Popen", lambda *args, **kwargs: fake_process)
    monkeypatch.setattr(
        persistent_codex,
        "_record_stdout_event",
        lambda settings, *, prepared, persistent, state, raw_line: record_calls.append((raw_line, state.stop_streams.is_set())),
    )
    monkeypatch.setattr(
        persistent_codex,
        "_finalize_persistent_step",
        lambda settings, **kwargs: finalized.update(kwargs),
    )

    def fake_path_open(self, *args, **kwargs):
        if self == prepared.paths.stdout_jsonl_path:
            return blocking_handle
        return original_path_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fake_path_open)

    started_at = time.monotonic()
    with pytest.raises(symbols["PersistentCodexNonQuiescentCleanupError"], match="persistence did not quiesce"):
        persistent_codex.execute_persistent_specialist_step(
            settings,
            prepared=prepared,
            prompt_state=SimpleNamespace(),
        )

    assert blocking_handle.write_started.is_set()
    assert fake_process.terminated is True
    assert finalized == {}
    assert time.monotonic() - started_at < 1.0
    blocking_handle.release_write.set()
    assert blocking_handle.write_finished.wait(timeout=1)
    assert record_calls == []


def test_execute_persistent_specialist_step_broken_pipe_uses_strict_failure_path(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")

    from worker import persistent_codex

    symbols, settings, prepared = _prepare_persistent_test_step(tmp_path)
    prepared.paths.final_output_path.write_text(json.dumps(_specialist_payload()), encoding="utf-8")
    persistent = _persistent_step_for_test(persistent_codex, tmp_path)
    fake_process = _DeadlineFakeProcess(stdin=_BrokenPipeStdin(), stdout=io.StringIO(""), stderr=io.StringIO(""))
    finalized = {}

    monkeypatch.setattr(
        persistent_codex,
        "prepare_persistent_specialist_step",
        lambda settings, prepared, prompt_state: persistent,
    )
    monkeypatch.setattr(persistent_codex.subprocess, "Popen", lambda *args, **kwargs: fake_process)
    monkeypatch.setattr(
        persistent_codex,
        "_finalize_persistent_step",
        lambda settings, **kwargs: finalized.update(kwargs),
    )

    with pytest.raises(symbols["StepRunError"], match="process supervision failed"):
        persistent_codex.execute_persistent_specialist_step(
            settings,
            prepared=prepared,
            prompt_state=SimpleNamespace(),
        )

    assert finalized["output_payload"] is None
    assert finalized["step_status"] == "failed"
    assert finalized["turn_status"] == "failed"


def test_execute_persistent_specialist_step_broken_pipe_after_acceptance_is_ambiguous(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")

    from worker import persistent_codex

    symbols, settings, prepared = _prepare_persistent_test_step(tmp_path)
    prepared.paths.final_output_path.write_text(json.dumps(_specialist_payload()), encoding="utf-8")
    persistent = _persistent_step_for_test(persistent_codex, tmp_path, resumed=True)
    fake_process = _DeadlineFakeProcess(
        stdin=_BrokenPipeStdin(),
        stdout=io.StringIO('{"type":"turn.started"}\n'),
        stderr=io.StringIO(""),
    )
    finalized = {}

    monkeypatch.setattr(
        persistent_codex,
        "prepare_persistent_specialist_step",
        lambda settings, prepared, prompt_state: persistent,
    )
    monkeypatch.setattr(persistent_codex.subprocess, "Popen", lambda *args, **kwargs: fake_process)
    monkeypatch.setattr(
        persistent_codex,
        "_record_stdout_event",
        lambda settings, *, prepared, persistent, state, raw_line: setattr(state, "accepted", True),
    )
    monkeypatch.setattr(
        persistent_codex,
        "_finalize_persistent_step",
        lambda settings, **kwargs: finalized.update(kwargs),
    )

    with pytest.raises(symbols["StepRunError"], match="process supervision failed"):
        persistent_codex.execute_persistent_specialist_step(
            settings,
            prepared=prepared,
            prompt_state=SimpleNamespace(),
        )

    assert finalized["output_payload"] is None
    assert finalized["step_status"] == "failed"
    assert finalized["turn_status"] == "ambiguous"


def test_execute_persistent_specialist_step_accepts_successful_structured_completion(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")

    from worker import persistent_codex

    symbols, settings, prepared = _prepare_persistent_test_step(tmp_path)
    prepared.paths.final_output_path.write_text(json.dumps(_specialist_payload()), encoding="utf-8")
    persistent = _persistent_step_for_test(persistent_codex, tmp_path)
    fake_process = _DeadlineFakeProcess(
        stdout=io.StringIO(
            '{"type":"thread.started","thread_id":"thread-1"}\n'
            '{"type":"turn.started"}\n'
            '{"type":"turn.completed"}\n'
        ),
        stderr=io.StringIO(""),
        returncode=0,
    )
    finalized = {}

    monkeypatch.setattr(
        persistent_codex,
        "prepare_persistent_specialist_step",
        lambda settings, prepared, prompt_state: persistent,
    )
    monkeypatch.setattr(persistent_codex.subprocess, "Popen", lambda *args, **kwargs: fake_process)

    def record_event(settings, *, prepared, persistent, state, raw_line):
        event = json.loads(raw_line)
        if event["type"] == "thread.started":
            state.thread_id = event["thread_id"]
        if event["type"] == "turn.started":
            state.accepted = True
        if event["type"] == "turn.completed":
            state.completed = True

    monkeypatch.setattr(persistent_codex, "_record_stdout_event", record_event)
    monkeypatch.setattr(
        persistent_codex,
        "_finalize_persistent_step",
        lambda settings, **kwargs: finalized.update(kwargs),
    )

    result = persistent_codex.execute_persistent_specialist_step(
        settings,
        prepared=prepared,
        prompt_state=SimpleNamespace(),
    )

    assert result.output_payload["publish_mode_recommendation"] == "auto_publish"
    assert finalized["step_status"] == "succeeded"
    assert finalized["turn_status"] == "completed"
    assert finalized["error_text"] is None


def test_execute_persistent_specialist_step_marks_timeout_after_acceptance_as_ambiguous(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")

    from worker import persistent_codex

    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    settings = Settings(**{**settings.__dict__, "codex_conversations_enabled": True})
    context = _make_context()
    spec = symbols["load_agent_spec"]("support")
    router_result = symbols["RouterResult"].model_validate(_route_payload())
    prepared = symbols["prepare_step_run"](
        settings,
        run_id=uuid.uuid4(),
        ticket_id=context.ticket.id,
        worker_instance_id="worker-test",
        step_index=2,
        step_kind="specialist",
        spec=spec,
        context=context,
        router_result=router_result,
        target_route_target_id="support",
    )
    persistent = persistent_codex.PreparedPersistentSpecialistStep(
        step_id=uuid.uuid4(),
        turn_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        command_spec=persistent_codex.PersistentCommandSpec(
            command=["codex", "exec"],
            env={},
            runtime_codex_home=tmp_path / ".codex" / "ticket-prod",
            resumed=False,
        ),
    )
    finalized = {}

    monkeypatch.setattr(
        persistent_codex,
        "prepare_persistent_specialist_step",
        lambda settings, prepared, prompt_state: persistent,
    )
    monkeypatch.setattr(
        persistent_codex.subprocess,
        "Popen",
        lambda *args, **kwargs: _FakePipeProcess(
            stdout_text='{"type":"thread.started","thread_id":"thread-1"}\n{"type":"turn.started"}\n',
            timeout_on_first_wait=True,
        ),
    )
    monkeypatch.setattr(
        persistent_codex,
        "_record_stdout_event",
        lambda settings, *, prepared, persistent, state, raw_line: setattr(state, "accepted", True),
    )
    monkeypatch.setattr(
        persistent_codex,
        "_finalize_persistent_step",
        lambda settings, **kwargs: finalized.update(kwargs),
    )

    with pytest.raises(symbols["StepRunError"], match="timed out after Codex accepted"):
        persistent_codex.execute_persistent_specialist_step(
            settings,
            prepared=prepared,
            prompt_state=SimpleNamespace(),
        )

    assert finalized["turn_status"] == "ambiguous"
    assert finalized["step_status"] == "failed"


def test_classify_persistent_failure_distinguishes_resume_recovery_and_preaccept_failure():
    pytest.importorskip("sqlalchemy")

    from worker.persistent_codex import classify_persistent_failure

    assert classify_persistent_failure(
        accepted=False,
        timed_out=False,
        stderr_text="thread_not_found",
    ) == (
        "failed",
        "Persistent specialist turn could not resume because the stored thread was not found.",
    )
    assert classify_persistent_failure(
        accepted=False,
        timed_out=False,
        stderr_text="plain failure",
    ) == (
        "failed",
        "Persistent specialist turn failed before Codex acceptance.",
    )


@pytest.mark.parametrize(
    ("returncode", "completed", "expected_error"),
    [
        (7, True, "exited with status 7"),
        (0, False, "without a durable turn.completed"),
    ],
)
def test_persistent_success_gate_rejects_valid_final_when_transport_is_not_terminal(
    monkeypatch,
    tmp_path,
    returncode,
    completed,
    expected_error,
):
    pytest.importorskip("sqlalchemy")

    from worker import persistent_codex

    symbols = _load_worker_symbols()
    settings = Settings(**{**_make_settings(tmp_path).__dict__, "codex_conversations_enabled": True})
    context = _make_context()
    prepared = symbols["prepare_step_run"](
        settings,
        run_id=uuid.uuid4(),
        ticket_id=context.ticket.id,
        worker_instance_id="worker-test",
        step_index=2,
        step_kind="specialist",
        spec=symbols["load_agent_spec"]("support"),
        context=context,
        router_result=symbols["RouterResult"].model_validate(_route_payload()),
        target_route_target_id="support",
    )
    prepared.paths.final_output_path.write_text(json.dumps(_specialist_payload()), encoding="utf-8")
    persistent = persistent_codex.PreparedPersistentSpecialistStep(
        step_id=uuid.uuid4(),
        turn_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        command_spec=persistent_codex.PersistentCommandSpec(
            command=["codex", "exec"],
            env={},
            runtime_codex_home=settings.resolved_codex_home,
            resumed=False,
        ),
    )
    finalized = {}

    monkeypatch.setattr(
        persistent_codex,
        "prepare_persistent_specialist_step",
        lambda settings, prepared, prompt_state: persistent,
    )
    monkeypatch.setattr(
        persistent_codex.subprocess,
        "Popen",
        lambda *args, **kwargs: _FakePipeProcess(
            stdout_text='{"type":"turn.completed"}\n',
            returncode=returncode,
        ),
    )

    def record_event(settings, *, prepared, persistent, state, raw_line):
        state.accepted = True
        state.completed = completed
        state.thread_id = "thread-1"

    monkeypatch.setattr(persistent_codex, "_record_stdout_event", record_event)
    monkeypatch.setattr(
        persistent_codex,
        "_finalize_persistent_step",
        lambda settings, **kwargs: finalized.update(kwargs),
    )

    with pytest.raises(symbols["StepRunError"], match=expected_error):
        persistent_codex.execute_persistent_specialist_step(
            settings,
            prepared=prepared,
            prompt_state=SimpleNamespace(),
        )

    assert finalized["step_status"] == "failed"
    assert finalized["turn_status"] == "ambiguous"
    assert finalized["output_payload"] is None


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
        worker_instance_id="worker-test",
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


def test_execute_step_persists_normalized_specialist_output(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    context = _make_context()
    spec = symbols["load_agent_spec"]("support")
    router_result = symbols["RouterResult"].model_validate(_route_payload())
    prepared = symbols["prepare_step_run"](
        settings,
        run_id=uuid.uuid4(),
        ticket_id=context.ticket.id,
        worker_instance_id="worker-test",
        step_index=2,
        step_kind="specialist",
        spec=spec,
        context=context,
        router_result=router_result,
        target_route_target_id="support",
    )
    prepared.paths.final_output_path.write_text(
        json.dumps(
            _specialist_payload(
                publish_mode_recommendation="manual_only",
                public_reply_markdown="Here is a safe requester-facing draft.",
                internal_note_markdown="",
                response_confidence="medium",
                risk_level="medium",
            )
        ),
        encoding="utf-8",
    )
    observed = {}

    monkeypatch.setattr("worker.step_runner._create_running_step_row", lambda settings, prepared: uuid.uuid4())
    monkeypatch.setattr("worker.step_runner._update_step_row", lambda **kwargs: observed.update({"output_payload": kwargs["output_payload"]}))
    monkeypatch.setattr("worker.step_runner.write_step_manifest", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "worker.step_runner.subprocess.run",
        lambda command, **kwargs: SimpleNamespace(returncode=0, stdout='{"event":"ok"}\n', stderr=""),
    )

    result = symbols["execute_step"](settings, prepared=prepared)

    assert result.output_payload["publish_mode_recommendation"] == "draft_for_human"
    assert observed["output_payload"]["publish_mode_recommendation"] == "draft_for_human"


def test_execute_step_writes_selected_specialist_registration_id_to_step_manifest(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    context = _make_context()
    spec = symbols["load_agent_spec"]("support")
    router_result = symbols["RouterResult"].model_validate(_route_payload())
    prepared = symbols["prepare_step_run"](
        settings,
        run_id=uuid.uuid4(),
        ticket_id=context.ticket.id,
        worker_instance_id="worker-test",
        step_index=2,
        step_kind="specialist",
        spec=spec,
        context=context,
        router_result=router_result,
        target_route_target_id="support",
        selected_specialist_id="support-primary",
    )
    prepared.paths.final_output_path.write_text(json.dumps(_specialist_payload()), encoding="utf-8")
    observed = {"metadata": []}

    monkeypatch.setattr("worker.step_runner._create_running_step_row", lambda settings, prepared: uuid.uuid4())
    monkeypatch.setattr("worker.step_runner._update_step_row", lambda **kwargs: None)
    monkeypatch.setattr(
        "worker.step_runner.write_step_manifest",
        lambda *args, **kwargs: observed["metadata"].append(kwargs["metadata"]),
    )

    def fake_run(command, **kwargs):
        return SimpleNamespace(returncode=0, stdout='{"event":"ok"}\n', stderr="")

    monkeypatch.setattr("worker.step_runner.subprocess.run", fake_run)

    symbols["execute_step"](settings, prepared=prepared)

    assert observed["metadata"][-1]["selected_specialist_id"] == "support-primary"


def test_execute_step_raises_when_run_ownership_is_lost_before_step_completion(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    context = _make_context()
    spec = symbols["load_agent_spec"]("support")
    router_result = symbols["RouterResult"].model_validate(_route_payload())
    prepared = symbols["prepare_step_run"](
        settings,
        run_id=uuid.uuid4(),
        ticket_id=context.ticket.id,
        worker_instance_id="worker-test",
        step_index=2,
        step_kind="specialist",
        spec=spec,
        context=context,
        router_result=router_result,
        target_route_target_id="support",
    )
    prepared.paths.final_output_path.write_text(json.dumps(_specialist_payload()), encoding="utf-8")
    observed = {"manifests": 0}

    monkeypatch.setattr("worker.step_runner._create_running_step_row", lambda settings, prepared: uuid.uuid4())
    monkeypatch.setattr(
        "worker.step_runner._update_step_row",
        lambda **kwargs: (_ for _ in ()).throw(symbols["RunOwnershipLost"]("lost")),
    )
    monkeypatch.setattr(
        "worker.step_runner.write_step_manifest",
        lambda *args, **kwargs: observed.__setitem__("manifests", observed["manifests"] + 1),
    )
    monkeypatch.setattr(
        "worker.step_runner.subprocess.run",
        lambda command, **kwargs: SimpleNamespace(returncode=0, stdout='{"event":"ok"}\n', stderr=""),
    )

    with pytest.raises(symbols["RunOwnershipLost"], match="lost"):
        symbols["execute_step"](settings, prepared=prepared)

    assert observed["manifests"] == 0


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
        worker_instance_id="worker-test",
        context=context,
    )

    assert result.route_target.id == route_target.id
    assert (result.selector_step is not None) is expected_selector
    assert (result.specialist_step is not None) is expected_specialist
    assert getattr(result.selected_specialist, "id", None) == expected_selected_id
    assert len(observed["manifest_updates"]) == len(observed["prepared"])


def test_execute_triage_pipeline_supports_forced_specialist_reruns(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    context = _make_context()
    route_target = _build_route_target(route_target_id="software_architect", kind="direct_ai", mode="fixed", specialist_id="software-architect")
    registry = _build_registry(route_target)
    specialist = SimpleNamespace(
        id="software-architect",
        display_name="Software Architect",
        spec=SimpleNamespace(id="software-architect-spec", version="2", output_contract="specialist_result"),
        enabled=True,
    )
    registry.require_specialist = lambda specialist_id: specialist
    registry.resolve_forced_manual_rerun_choice = lambda *, route_target_id, specialist_id: SimpleNamespace(
        route_target_id=route_target_id,
        route_target_label="Software Architect",
        specialist_id=specialist_id,
        specialist_display_name="Software Architect",
    )
    observed = {"synthetic_router": [], "prepared": [], "manifest_updates": []}

    def fake_record_synthetic_step_success(*args, **kwargs):
        observed["synthetic_router"].append(kwargs)
        return SimpleNamespace(
            step_id=uuid.uuid4(),
            prepared=SimpleNamespace(spec=kwargs["spec"], model_name=None),
            output_payload=kwargs["output_payload"],
        )

    def fake_prepare_step_run(*args, **kwargs):
        observed["prepared"].append(kwargs)
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
        return SimpleNamespace(
            step_id=uuid.uuid4(),
            prepared=prepared,
            output_payload=_specialist_payload(summary_internal="Architect assessment complete."),
        )

    monkeypatch.setattr("worker.pipeline.load_routing_registry", lambda: registry)
    monkeypatch.setattr("worker.pipeline.record_synthetic_step_success", fake_record_synthetic_step_success)
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
        worker_instance_id="worker-test",
        context=context,
        forced_route_target_id="software_architect",
        forced_specialist_id="software-architect",
    )

    assert result.route_target.id == "software_architect"
    assert result.selector_step is None
    assert result.selected_specialist.id == "software-architect"
    assert observed["synthetic_router"][0]["output_payload"]["route_target_id"] == "software_architect"
    assert observed["synthetic_router"][0]["selected_specialist_id"] == "software-architect"
    assert observed["prepared"][0]["selected_specialist_id"] == "software-architect"
    assert len(observed["manifest_updates"]) == 2


def test_execute_triage_pipeline_allows_software_data_engineer_for_internal_requesters(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    context = _make_context(requester_role="dev_ti")
    observed = {"manifest_updates": [], "prepared": []}
    route_target = _build_route_target(
        route_target_id="software_data_engineer",
        kind="direct_ai",
        mode="fixed",
        specialist_id="software-data-engineer",
        allow_auto_publish=False,
    )
    registry = _build_registry(route_target)

    def fake_prepare_step_run(*args, **kwargs):
        observed["prepared"].append(
            {
                "step_index": kwargs["step_index"],
                "step_kind": kwargs["step_kind"],
                "selected_specialist_id": kwargs.get("selected_specialist_id"),
            }
        )
        return SimpleNamespace(
            step_index=kwargs["step_index"],
            step_kind=kwargs["step_kind"],
            spec=kwargs["spec"],
            model_name=None,
            candidate_specialist_ids=kwargs.get("candidate_specialist_ids"),
            paths=SimpleNamespace(run_dir=tmp_path / "run", as_payload=lambda: {}),
        )

    def fake_execute_step(_settings, *, prepared):
        if prepared.step_kind == "router":
            return SimpleNamespace(
                step_id=uuid.uuid4(),
                prepared=prepared,
                output_payload=_route_payload(
                    route_target_id="software_data_engineer",
                    routing_rationale="The internal requester wants concrete implementation text.",
                ),
            )
        return SimpleNamespace(
            step_id=uuid.uuid4(),
            prepared=prepared,
            output_payload=_specialist_payload(
                public_reply_markdown="```diff\n--- a/app/example.py\n+++ b/app/example.py\n@@\n- old\n+ new\n```",
                publish_mode_recommendation="draft_for_human",
                summary_internal="Prepared the proposed implementation diff.",
            ),
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
        worker_instance_id="worker-test",
        context=context,
    )

    assert result.route_target.id == "software_data_engineer"
    assert result.selected_specialist.id == "software-data-engineer"
    assert observed["prepared"][1]["selected_specialist_id"] == "software-data-engineer"
    assert len(observed["manifest_updates"]) == 2


def test_execute_triage_pipeline_rejects_software_data_engineer_for_external_requesters(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    context = _make_context(requester_role="requester")
    route_target = _build_route_target(
        route_target_id="software_data_engineer",
        kind="direct_ai",
        mode="fixed",
        specialist_id="software-data-engineer",
    )
    registry = _build_registry(route_target)

    def require_enabled_route_target_for_requester(route_target_id: str, requester_role: str):
        raise RoutingRegistryError(f"Route target {route_target_id} is not eligible for requester role {requester_role}")

    def fake_prepare_step_run(*args, **kwargs):
        return SimpleNamespace(
            step_index=kwargs["step_index"],
            step_kind=kwargs["step_kind"],
            spec=kwargs["spec"],
            model_name=None,
            candidate_specialist_ids=kwargs.get("candidate_specialist_ids"),
            paths=SimpleNamespace(run_dir=tmp_path / "run", as_payload=lambda: {}),
        )

    def fake_execute_step(_settings, *, prepared):
        return SimpleNamespace(
            step_id=uuid.uuid4(),
            prepared=prepared,
            output_payload=_route_payload(
                route_target_id="software_data_engineer",
                routing_rationale="The requester asked for code changes.",
            ),
        )

    registry.require_enabled_route_target_for_requester = require_enabled_route_target_for_requester
    monkeypatch.setattr("worker.pipeline.load_routing_registry", lambda: registry)
    monkeypatch.setattr("worker.pipeline.prepare_step_run", fake_prepare_step_run)
    monkeypatch.setattr("worker.pipeline.execute_step", fake_execute_step)
    monkeypatch.setattr("worker.pipeline.write_run_manifest_snapshot", lambda *args, **kwargs: None)

    with pytest.raises(RoutingRegistryError, match="software_data_engineer is not eligible for requester role requester"):
        symbols["execute_triage_pipeline"](
            settings,
            run_id=uuid.uuid4(),
            ticket_id=context.ticket.id,
            worker_instance_id="worker-test",
            context=context,
        )


def test_execute_triage_pipeline_supports_forced_software_data_engineer_reruns_for_external_requesters(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    context = _make_context(requester_role="requester")
    observed = {"synthetic_router": [], "manifest_updates": []}
    route_target = _build_route_target(
        route_target_id="software_data_engineer",
        kind="direct_ai",
        mode="fixed",
        specialist_id="software-data-engineer",
        allow_auto_publish=False,
    )
    registry = _build_registry(route_target)
    registry.resolve_forced_manual_rerun_choice = lambda *, route_target_id, specialist_id: SimpleNamespace(
        route_target_id=route_target_id,
        route_target_label="Software & Data Engineer",
        specialist_id=specialist_id,
        specialist_display_name="software-data-engineer",
    )

    def fake_record_synthetic_step_success(*args, **kwargs):
        observed["synthetic_router"].append(kwargs)
        return SimpleNamespace(
            step_id=uuid.uuid4(),
            prepared=SimpleNamespace(spec=kwargs["spec"], model_name=None),
            output_payload=kwargs["output_payload"],
        )

    def fake_prepare_step_run(*args, **kwargs):
        return SimpleNamespace(
            step_index=kwargs["step_index"],
            step_kind=kwargs["step_kind"],
            spec=kwargs["spec"],
            model_name=None,
            candidate_specialist_ids=kwargs.get("candidate_specialist_ids"),
            paths=SimpleNamespace(run_dir=tmp_path / "run", as_payload=lambda: {}),
        )

    def fake_execute_step(_settings, *, prepared):
        return SimpleNamespace(
            step_id=uuid.uuid4(),
            prepared=prepared,
            output_payload=_specialist_payload(
                public_reply_markdown="```diff\n--- a/shared/example.py\n+++ b/shared/example.py\n@@\n- before\n+ after\n```",
                publish_mode_recommendation="draft_for_human",
                summary_internal="Prepared forced engineer rerun output.",
            ),
        )

    monkeypatch.setattr("worker.pipeline.load_routing_registry", lambda: registry)
    monkeypatch.setattr("worker.pipeline.record_synthetic_step_success", fake_record_synthetic_step_success)
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
        worker_instance_id="worker-test",
        context=context,
        forced_route_target_id="software_data_engineer",
        forced_specialist_id="software-data-engineer",
    )

    assert result.route_target.id == "software_data_engineer"
    assert result.selected_specialist.id == "software-data-engineer"
    assert observed["synthetic_router"][0]["output_payload"]["route_target_id"] == "software_data_engineer"
    assert len(observed["manifest_updates"]) == 2


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

    prepared = symbols["_prepare_run"](settings, run_id=run.id, worker_instance_id="worker-test")

    assert prepared is None
    assert run.status == "skipped"
    assert run.pipeline_version == "agent-pipeline-v1"
    assert run.ended_at is not None
    assert observed["requeue"] == 1


def test_prepare_run_preserves_forced_specialist_override(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        reference="T-000003A",
        title="Architect review",
        status="ai_triage",
        urgent=False,
        last_processed_hash=None,
        clarification_rounds=0,
        requeue_requested=False,
        requeue_trigger=None,
        requester_language=None,
    )
    context = _make_context(ticket=ticket)
    run = SimpleNamespace(
        id=uuid.uuid4(),
        ticket_id=ticket.id,
        status="running",
        triggered_by="manual_rerun",
        input_hash=None,
        model_name=None,
        pipeline_version=None,
        final_step_id=None,
        final_agent_spec_id=None,
        final_output_contract=None,
        final_output_json=None,
        error_text=None,
        ended_at=None,
        forced_route_target_id="software_architect",
        forced_specialist_id="software-architect",
    )
    fake_db = _FakeDb(run=run)

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.triage.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.triage.load_ticket_context", lambda db, ticket_id: context)

    prepared = symbols["_prepare_run"](settings, run_id=run.id, worker_instance_id="worker-test")

    assert prepared is not None
    assert prepared.forced_route_target_id == "software_architect"
    assert prepared.forced_specialist_id == "software-architect"


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
    symbols["_apply_success_result"](
        settings,
        run_id=run.id,
        worker_instance_id="worker-test",
        pipeline_result=pipeline_result,
    )

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


def test_apply_success_result_internal_requester_manual_only_with_public_reply_auto_publishes(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        reference="T-000004A",
        title="Internal requester needs technical direction",
        status="ai_triage",
        urgent=False,
        requester_language=None,
        last_processed_hash=None,
        last_ai_action=None,
        clarification_rounds=0,
        requeue_requested=False,
        requeue_trigger=None,
    )
    context = _make_context(
        ticket=ticket,
        requester_role="dev_ti",
        requester_can_view_internal_messages=True,
    )
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
    observed = {"internal": 0, "public": None}

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
        lambda *args, **kwargs: observed.__setitem__(
            "public",
            (kwargs["next_status"], kwargs["last_ai_action"], kwargs["body_markdown"]),
        ),
    )
    monkeypatch.setattr("worker.triage.create_ai_draft", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not create draft")))
    monkeypatch.setattr("worker.triage.route_ticket_after_ai", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not route manual-only")))
    monkeypatch.setattr("worker.triage.process_deferred_requeue", lambda *args, **kwargs: None)
    monkeypatch.setattr("worker.triage.write_run_manifest_snapshot", lambda *args, **kwargs: None)

    pipeline_result = _pipeline_result(
        route_target=_build_route_target(route_target_id="support", kind="direct_ai", mode="fixed", specialist_id="support"),
        specialist_payload=_specialist_payload(
            publish_mode_recommendation="manual_only",
            public_reply_markdown="Share this directly with the internal requester.",
            internal_note_markdown="Context for ops.",
        ),
    )
    symbols["_apply_success_result"](
        settings,
        run_id=run.id,
        worker_instance_id="worker-test",
        pipeline_result=pipeline_result,
    )

    assert observed["internal"] == 1
    assert observed["public"] == ("waiting_on_user", "auto_public_reply", "Share this directly with the internal requester.")
    assert run.status == "succeeded"
    assert run.final_output_contract == "specialist_result"
    assert run.final_output_json["publish_mode_recommendation"] == "auto_publish"


def test_apply_success_result_internal_requester_without_public_reply_auto_publishes_internal_note(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        reference="T-000004B",
        title="Internal requester needs internal-only guidance",
        status="ai_triage",
        urgent=False,
        requester_language=None,
        last_processed_hash=None,
        last_ai_action=None,
        clarification_rounds=0,
        requeue_requested=False,
        requeue_trigger=None,
    )
    context = _make_context(
        ticket=ticket,
        requester_role="admin",
        requester_can_view_internal_messages=True,
    )
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
    observed = {"public": None}

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.triage.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.triage.load_ticket_context", lambda db, ticket_id: context)
    monkeypatch.setattr("worker.triage.publish_ai_internal_note", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not duplicate internal note")))
    monkeypatch.setattr(
        "worker.triage.publish_ai_public_reply",
        lambda *args, **kwargs: observed.__setitem__(
            "public",
            (kwargs["next_status"], kwargs["last_ai_action"], kwargs["body_markdown"]),
        ),
    )
    monkeypatch.setattr("worker.triage.create_ai_draft", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not create draft")))
    monkeypatch.setattr("worker.triage.route_ticket_after_ai", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not route manual-only")))
    monkeypatch.setattr("worker.triage.process_deferred_requeue", lambda *args, **kwargs: None)
    monkeypatch.setattr("worker.triage.write_run_manifest_snapshot", lambda *args, **kwargs: None)

    pipeline_result = _pipeline_result(
        route_target=_build_route_target(route_target_id="support", kind="direct_ai", mode="fixed", specialist_id="support"),
        specialist_payload=_specialist_payload(
            publish_mode_recommendation="manual_only",
            public_reply_markdown="",
            internal_note_markdown="Keep this guidance internal only.",
        ),
    )
    symbols["_apply_success_result"](
        settings,
        run_id=run.id,
        worker_instance_id="worker-test",
        pipeline_result=pipeline_result,
    )

    assert observed["public"] == ("waiting_on_user", "auto_public_reply", "Keep this guidance internal only.")
    assert run.status == "succeeded"
    assert run.final_output_contract == "specialist_result"
    assert run.final_output_json["publish_mode_recommendation"] == "auto_publish"
    assert run.final_output_json["public_reply_markdown"] == "Keep this guidance internal only."
    assert run.final_output_json["internal_note_markdown"] == ""


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
    symbols["_apply_success_result"](
        settings,
        run_id=run.id,
        worker_instance_id="worker-test",
        pipeline_result=pipeline_result,
    )

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
    symbols["_apply_success_result"](
        settings,
        run_id=run.id,
        worker_instance_id="worker-test",
        pipeline_result=pipeline_result,
    )

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
    symbols["_apply_success_result"](
        settings,
        run_id=run.id,
        worker_instance_id="worker-test",
        pipeline_result=pipeline_result,
    )

    assert observed == {"next_status": "waiting_on_dev_ti", "last_ai_action": "draft_public_reply"}
    assert run.status == "human_review"


def test_apply_success_result_internal_requester_human_assist_with_public_reply_auto_publishes(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        reference="T-000007A",
        title="Internal requester needs human-assist specialist output",
        status="ai_triage",
        urgent=False,
        requester_language=None,
        last_processed_hash=None,
        last_ai_action=None,
        clarification_rounds=0,
        requeue_requested=False,
        requeue_trigger=None,
    )
    context = _make_context(
        ticket=ticket,
        requester_role="dev_ti",
        requester_can_view_internal_messages=True,
    )
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
    observed = {"internal": 0, "public": None}

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
        lambda *args, **kwargs: observed.__setitem__("public", (kwargs["next_status"], kwargs["last_ai_action"])),
    )
    monkeypatch.setattr("worker.triage.create_ai_draft", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not draft")))
    monkeypatch.setattr("worker.triage.route_ticket_after_ai", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not route to human queue")))
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
            publish_mode_recommendation="draft_for_human",
            public_reply_markdown="Share this architecture assessment with the internal requester.",
        ),
        specialist_spec_id="bug",
    )
    symbols["_apply_success_result"](
        settings,
        run_id=run.id,
        worker_instance_id="worker-test",
        pipeline_result=pipeline_result,
    )

    assert observed["internal"] == 1
    assert observed["public"] == ("waiting_on_user", "auto_public_reply")
    assert run.status == "succeeded"
    assert run.final_output_contract == "specialist_result"
    assert run.final_output_json["publish_mode_recommendation"] == "auto_publish"


def test_apply_success_result_internal_requester_human_assist_without_specialist_keeps_human_review(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        reference="T-000007B",
        title="Internal requester needs handoff-only result",
        status="ai_triage",
        urgent=False,
        requester_language=None,
        last_processed_hash=None,
        last_ai_action=None,
        clarification_rounds=0,
        requeue_requested=False,
        requeue_trigger=None,
    )
    context = _make_context(
        ticket=ticket,
        requester_role="admin",
        requester_can_view_internal_messages=True,
    )
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
    observed = {"internal": 0, "route": None}

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
        "worker.triage.route_ticket_after_ai",
        lambda *args, **kwargs: observed.__setitem__("route", (kwargs["next_status"], kwargs["last_ai_action"])),
    )
    monkeypatch.setattr("worker.triage.publish_ai_public_reply", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not publish synthesized handoff")))
    monkeypatch.setattr("worker.triage.create_ai_draft", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not create draft")))
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
    symbols["_apply_success_result"](
        settings,
        run_id=run.id,
        worker_instance_id="worker-test",
        pipeline_result=pipeline_result,
    )

    assert observed["internal"] == 1
    assert observed["route"] == ("waiting_on_dev_ti", "manual_only")
    assert run.status == "human_review"
    assert run.final_agent_spec_id is None
    assert run.final_output_contract == "human_handoff_result"
    assert run.final_output_json["route_target_id"] == "manual_review"
    assert run.final_output_json["public_reply_markdown"] == ""
    assert run.final_output_json["internal_note_markdown"]


def test_apply_success_result_internal_requester_software_architect_auto_publishes_despite_route_policy(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        reference="T-000007C",
        title="Need architecture direction",
        status="ai_triage",
        urgent=False,
        requester_language=None,
        last_processed_hash=None,
        last_ai_action=None,
        clarification_rounds=0,
        requeue_requested=False,
        requeue_trigger=None,
    )
    context = _make_context(
        ticket=ticket,
        requester_role="admin",
        requester_can_view_internal_messages=True,
    )
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
    observed = {"public": None}

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.triage.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.triage.load_ticket_context", lambda db, ticket_id: context)
    monkeypatch.setattr("worker.triage.publish_ai_internal_note", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "worker.triage.publish_ai_public_reply",
        lambda *args, **kwargs: observed.__setitem__("public", (kwargs["next_status"], kwargs["last_ai_action"])),
    )
    monkeypatch.setattr("worker.triage.create_ai_draft", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not draft")))
    monkeypatch.setattr("worker.triage.route_ticket_after_ai", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not route manually")))
    monkeypatch.setattr("worker.triage.process_deferred_requeue", lambda *args, **kwargs: None)
    monkeypatch.setattr("worker.triage.write_run_manifest_snapshot", lambda *args, **kwargs: None)

    pipeline_result = _pipeline_result(
        route_target=_build_route_target(
            route_target_id="software_architect",
            kind="direct_ai",
            mode="fixed",
            specialist_id="software-architect",
            allow_auto_publish=False,
        ),
        specialist_payload=_specialist_payload(
            publish_mode_recommendation="draft_for_human",
            public_reply_markdown="Here is the recommended architecture change.",
            internal_note_markdown="",
        ),
        specialist_spec_id="software-architect",
    )
    symbols["_apply_success_result"](
        settings,
        run_id=run.id,
        worker_instance_id="worker-test",
        pipeline_result=pipeline_result,
    )

    assert observed["public"] == ("waiting_on_user", "auto_public_reply")
    assert run.status == "succeeded"
    assert run.final_agent_spec_id == "software-architect"
    assert run.final_output_json["publish_mode_recommendation"] == "auto_publish"


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
    symbols["_apply_success_result"](
        settings,
        run_id=run.id,
        worker_instance_id="worker-test",
        pipeline_result=pipeline_result,
    )

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
    symbols["_apply_success_result"](
        settings,
        run_id=run.id,
        worker_instance_id="worker-test",
        pipeline_result=pipeline_result,
    )

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
    symbols["_apply_success_result"](
        settings,
        run_id=run.id,
        worker_instance_id="worker-test",
        pipeline_result=pipeline_result,
    )

    assert run.status == "superseded"
    assert run.ended_at is not None
    assert observed == {"internal": 0, "public": 0, "requeue": 1}


def test_apply_success_result_raises_when_run_is_no_longer_owned(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    run = SimpleNamespace(
        id=uuid.uuid4(),
        ticket_id=uuid.uuid4(),
        status="failed",
        worker_instance_id="worker-test",
        input_hash="old-hash",
        ended_at=datetime.now(timezone.utc),
        error_text="recovered",
    )
    fake_db = _FakeDb(run=run)
    observed = {"internal": 0, "public": 0, "draft": 0, "route": 0}

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.triage.session_scope", fake_session_scope)
    monkeypatch.setattr(
        "worker.triage.publish_ai_internal_note",
        lambda *args, **kwargs: observed.__setitem__("internal", observed["internal"] + 1),
    )
    monkeypatch.setattr(
        "worker.triage.publish_ai_public_reply",
        lambda *args, **kwargs: observed.__setitem__("public", observed["public"] + 1),
    )
    monkeypatch.setattr(
        "worker.triage.create_ai_draft",
        lambda *args, **kwargs: observed.__setitem__("draft", observed["draft"] + 1),
    )
    monkeypatch.setattr(
        "worker.triage.route_ticket_after_ai",
        lambda *args, **kwargs: observed.__setitem__("route", observed["route"] + 1),
    )

    pipeline_result = _pipeline_result(
        route_target=_build_route_target(route_target_id="support", kind="direct_ai", mode="fixed", specialist_id="support"),
        specialist_payload=_specialist_payload(),
    )

    with pytest.raises(symbols["RunOwnershipLost"], match="finalization"):
        symbols["_apply_success_result"](
            settings,
            run_id=run.id,
            worker_instance_id="worker-test",
            pipeline_result=pipeline_result,
        )

    assert observed == {"internal": 0, "public": 0, "draft": 0, "route": 0}
    assert run.status == "failed"


def test_process_ai_run_marks_failed_when_publication_step_raises(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    prepared = SimpleNamespace(
        run_id=uuid.uuid4(),
        ticket_id=uuid.uuid4(),
        worker_instance_id="worker-test",
        context=SimpleNamespace(),
    )
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
        lambda settings, run_id, worker_instance_id, error_text: observed.update(
            {"run_id": run_id, "worker_instance_id": worker_instance_id, "error_text": error_text}
        ),
    )

    symbols["process_ai_run"](settings, run_id=prepared.run_id, worker_instance_id="worker-test")

    assert observed["run_id"] == prepared.run_id
    assert observed["worker_instance_id"] == "worker-test"
    assert observed["error_text"] == "Unexpected worker error: publish failed"


def test_process_ai_run_retains_nonquiescent_persistent_cleanup_for_stale_recovery(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        status="ai_triage",
        requeue_requested=False,
        requeue_trigger=None,
        requeue_requested_by_user_id=None,
        requeue_forced_route_target_id=None,
        requeue_forced_specialist_id=None,
        updated_at=None,
    )
    run = SimpleNamespace(
        id=uuid.uuid4(),
        ticket_id=ticket.id,
        status="running",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=11),
        started_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        last_heartbeat_at=None,
        ended_at=None,
        error_text=None,
        triggered_by="new_ticket",
        requested_by_user_id=None,
        forced_route_target_id=None,
        forced_specialist_id=None,
        recovery_attempt_count=0,
    )
    step = SimpleNamespace(
        ai_run_id=run.id,
        step_index=2,
        status="running",
        error_text=None,
        ended_at=None,
    )
    conversation = SimpleNamespace(id=uuid.uuid4(), status="active")
    session = SimpleNamespace(
        id=uuid.uuid4(),
        conversation_id=conversation.id,
        thread_id="thread-from-prior-turn",
        status="active",
        ended_at=None,
        lease_owner_run_id=run.id,
        lease_worker_instance_id="worker-test",
        lease_acquired_at=datetime.now(timezone.utc),
        lease_heartbeat_at=datetime.now(timezone.utc),
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    turn = SimpleNamespace(
        id=uuid.uuid4(),
        ai_run_id=run.id,
        conversation_id=conversation.id,
        session_id=session.id,
        accepted_at=None,
        status="running",
        ended_at=None,
    )
    prepared = SimpleNamespace(
        run_id=run.id,
        ticket_id=ticket.id,
        worker_instance_id="worker-test",
        context=SimpleNamespace(),
    )

    class PersistentRecoveryDb:
        def __init__(self):
            self.added = []

        def execute(self, statement):
            entity = statement.column_descriptions[0].get("entity") if getattr(statement, "column_descriptions", None) else None
            first_name = statement.column_descriptions[0]["name"] if getattr(statement, "column_descriptions", None) else None
            if first_name == "coalesce":
                return _FakePersistentScalarResult(0)
            entity_name = getattr(entity, "__name__", "")
            if entity_name == "AIRun":
                return _FakeWorkerStateResult([run])
            if entity_name == "AIRunStep":
                return _FakeWorkerStateResult([step])
            if entity_name == "CodexTurn":
                return _FakeWorkerStateResult([turn])
            if entity_name == "CodexSession":
                return _FakeWorkerStateResult([session])
            return _FakeWorkerStateResult([])

        def get(self, model, key):
            name = getattr(model, "__name__", "")
            if name == "Ticket" and key == ticket.id:
                return ticket
            if name == "CodexConversation" and key == conversation.id:
                return conversation
            return None

        def add(self, item):
            self.added.append(item)

    fake_db = PersistentRecoveryDb()
    observed = {
        "triage_logs": [],
        "queue_logs": [],
        "failure_notes": [],
        "status_changes": [],
        "manifest_run_ids": [],
    }

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    def raise_nonquiescent(*args, **kwargs):
        raise symbols["PersistentCodexNonQuiescentCleanupError"](
            "Codex output stream persistence did not quiesce after cleanup"
        )

    monkeypatch.setattr("worker.triage._prepare_run", lambda *args, **kwargs: prepared)
    monkeypatch.setattr("worker.triage.execute_triage_pipeline", raise_nonquiescent)
    monkeypatch.setattr("worker.triage._apply_success_result", lambda *args, **kwargs: pytest.fail("unexpected success"))
    monkeypatch.setattr("worker.triage._mark_failed", lambda *args, **kwargs: pytest.fail("premature failure handling"))
    monkeypatch.setattr("worker.triage.publish_ai_failure_note", lambda *args, **kwargs: pytest.fail("premature failure note"))
    monkeypatch.setattr("worker.triage.publish_ai_internal_note", lambda *args, **kwargs: pytest.fail("premature internal note"))
    monkeypatch.setattr("worker.triage.record_status_change", lambda *args, **kwargs: pytest.fail("premature status route"))
    monkeypatch.setattr("worker.triage.route_ticket_after_ai", lambda *args, **kwargs: pytest.fail("premature route"))
    monkeypatch.setattr("worker.triage.process_deferred_requeue", lambda *args, **kwargs: pytest.fail("premature deferred requeue"))
    monkeypatch.setattr("worker.triage.write_run_manifest_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "worker.triage.log_worker_event",
        lambda event, **kwargs: observed["triage_logs"].append((event, kwargs)),
    )
    monkeypatch.setattr("worker.queue.session_scope", fake_session_scope)
    monkeypatch.setattr(
        "worker.queue.log_worker_event",
        lambda event, **kwargs: observed["queue_logs"].append((event, kwargs)),
    )
    monkeypatch.setattr("worker.queue.process_deferred_requeue", lambda *args, **kwargs: pytest.fail("unexpected deferred requeue"))
    monkeypatch.setattr("worker.queue.create_pending_ai_run", lambda *args, **kwargs: pytest.fail("unexpected stale-prompt retry"))
    monkeypatch.setattr(
        "worker.queue.publish_ai_failure_note",
        lambda db, ticket, ai_run_id, body_markdown, created_at=None: observed["failure_notes"].append(
            {"ai_run_id": ai_run_id, "body": body_markdown}
        ),
    )
    monkeypatch.setattr(
        "worker.queue.record_status_change",
        lambda db, ticket, to_status, changed_by_type, changed_at, **kwargs: observed["status_changes"].append(
            (ticket.status, to_status, changed_by_type)
        )
        or setattr(ticket, "status", to_status),
    )
    monkeypatch.setattr(
        "worker.queue.write_run_manifest_snapshot",
        lambda _settings, run_id: observed["manifest_run_ids"].append(run_id),
    )

    symbols["process_ai_run"](settings, run_id=run.id, worker_instance_id="worker-test")

    assert run.status == "running"
    assert run.ended_at is None
    assert step.status == "running"
    assert turn.status == "running"
    assert session.status == "active"
    assert session.lease_owner_run_id == run.id
    assert conversation.status == "active"
    assert observed["failure_notes"] == []
    assert observed["status_changes"] == []
    assert observed["triage_logs"][0][0] == "persistent_codex_cleanup_retained_for_stale_recovery"
    stale_before = datetime.now(timezone.utc) - timedelta(seconds=settings.ai_run_stale_timeout_seconds)
    assert run.last_heartbeat_at is None
    assert run.started_at < stale_before

    recovered_count = symbols["recover_stale_runs"](settings)

    assert recovered_count == 1
    assert run.status == "failed"
    assert run.ended_at is not None
    assert "stale" in run.error_text.lower()
    assert step.status == "failed"
    assert step.ended_at is not None
    assert turn.status == "interrupted"
    assert turn.ended_at is not None
    assert session.status == "replaced"
    assert session.ended_at is not None
    assert session.lease_owner_run_id is None
    assert session.lease_worker_instance_id is None
    assert session.lease_expires_at is None
    assert conversation.status == "recovery_required"
    outcomes = [item for item in fake_db.added if item.__class__.__name__ == "CodexTurnOutcome"]
    assert len(outcomes) == 1
    assert outcomes[0].outcome_kind == "interrupted"
    assert ticket.status == "waiting_on_dev_ti"
    assert len(observed["failure_notes"]) == 1
    assert "not retried automatically" in observed["failure_notes"][0]["body"]
    assert observed["status_changes"] == [("ai_triage", "waiting_on_dev_ti", "system")]
    assert observed["manifest_run_ids"] == [run.id]


def test_process_ai_run_nonquiescent_accepted_turn_recovers_as_ambiguous(monkeypatch, tmp_path):
    from worker import persistent_codex

    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        status="ai_triage",
        requeue_requested=False,
        requeue_trigger=None,
        requeue_requested_by_user_id=None,
        requeue_forced_route_target_id=None,
        requeue_forced_specialist_id=None,
        updated_at=None,
    )
    run = SimpleNamespace(
        id=uuid.uuid4(),
        ticket_id=ticket.id,
        status="running",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=11),
        started_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        last_heartbeat_at=None,
        ended_at=None,
        error_text=None,
        triggered_by="new_ticket",
        requested_by_user_id=None,
        forced_route_target_id=None,
        forced_specialist_id=None,
        recovery_attempt_count=0,
    )
    step = SimpleNamespace(
        ai_run_id=run.id,
        step_index=2,
        status="running",
        error_text=None,
        ended_at=None,
    )
    conversation = SimpleNamespace(id=uuid.uuid4(), status="active")
    session = SimpleNamespace(
        id=uuid.uuid4(),
        conversation_id=conversation.id,
        thread_id="thread-accepted-uncertain",
        status="active",
        ended_at=None,
        lease_owner_run_id=run.id,
        lease_worker_instance_id="worker-test",
        lease_acquired_at=datetime.now(timezone.utc),
        lease_heartbeat_at=datetime.now(timezone.utc),
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    turn = SimpleNamespace(
        id=uuid.uuid4(),
        ai_run_id=run.id,
        conversation_id=conversation.id,
        session_id=session.id,
        accepted_at=datetime.now(timezone.utc) - timedelta(minutes=9),
        status="running",
        ended_at=None,
    )
    prepared = SimpleNamespace(
        run_id=run.id,
        ticket_id=ticket.id,
        worker_instance_id="worker-test",
        context=SimpleNamespace(),
    )
    fake_db = _PersistentQueueRecoveryDb(
        stale_runs=[run],
        steps_by_run_id={run.id: [step]},
        tickets_by_id={ticket.id: ticket},
        turns_by_run_id={run.id: [turn]},
        sessions_by_id={session.id: session},
        conversations_by_id={conversation.id: conversation},
    )
    observed = {
        "triage_logs": [],
        "failure_notes": [],
        "status_changes": [],
        "manifest_run_ids": [],
    }

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    def raise_nonquiescent(*args, **kwargs):
        raise symbols["PersistentCodexNonQuiescentCleanupError"](
            "Codex output stream persistence did not quiesce after cleanup"
        )

    monkeypatch.setattr("worker.triage._prepare_run", lambda *args, **kwargs: prepared)
    monkeypatch.setattr("worker.triage.execute_triage_pipeline", raise_nonquiescent)
    monkeypatch.setattr("worker.triage._apply_success_result", lambda *args, **kwargs: pytest.fail("unexpected success"))
    monkeypatch.setattr("worker.triage._mark_failed", lambda *args, **kwargs: pytest.fail("premature failure handling"))
    monkeypatch.setattr("worker.triage.publish_ai_failure_note", lambda *args, **kwargs: pytest.fail("premature failure note"))
    monkeypatch.setattr("worker.triage.publish_ai_internal_note", lambda *args, **kwargs: pytest.fail("premature internal note"))
    monkeypatch.setattr("worker.triage.record_status_change", lambda *args, **kwargs: pytest.fail("premature status route"))
    monkeypatch.setattr("worker.triage.route_ticket_after_ai", lambda *args, **kwargs: pytest.fail("premature route"))
    monkeypatch.setattr("worker.triage.process_deferred_requeue", lambda *args, **kwargs: pytest.fail("premature deferred requeue"))
    monkeypatch.setattr("worker.triage.write_run_manifest_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "worker.triage.log_worker_event",
        lambda event, **kwargs: observed["triage_logs"].append((event, kwargs)),
    )
    monkeypatch.setattr("worker.queue.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.queue.log_worker_event", lambda *args, **kwargs: None)
    monkeypatch.setattr("worker.queue.process_deferred_requeue", lambda *args, **kwargs: pytest.fail("unexpected deferred requeue"))
    monkeypatch.setattr("worker.queue.create_pending_ai_run", lambda *args, **kwargs: pytest.fail("unexpected stale-prompt retry"))
    monkeypatch.setattr(
        "worker.queue.publish_ai_failure_note",
        lambda db, ticket, ai_run_id, body_markdown, created_at=None: observed["failure_notes"].append(
            {"ai_run_id": ai_run_id, "body": body_markdown}
        ),
    )
    monkeypatch.setattr(
        "worker.queue.record_status_change",
        lambda db, ticket, to_status, changed_by_type, changed_at, **kwargs: observed["status_changes"].append(
            (ticket.status, to_status, changed_by_type)
        )
        or setattr(ticket, "status", to_status),
    )
    monkeypatch.setattr(
        "worker.queue.write_run_manifest_snapshot",
        lambda _settings, run_id: observed["manifest_run_ids"].append(run_id),
    )

    symbols["process_ai_run"](settings, run_id=run.id, worker_instance_id="worker-test")

    assert run.status == "running"
    assert step.status == "running"
    assert turn.status == "running"
    assert session.status == "active"
    assert conversation.status == "active"
    assert observed["triage_logs"][0][0] == "persistent_codex_cleanup_retained_for_stale_recovery"

    recovered_count = symbols["recover_stale_runs"](settings)

    assert recovered_count == 1
    assert run.status == "failed"
    assert step.status == "failed"
    assert turn.status == "ambiguous"
    assert turn.ended_at is not None
    outcomes = [item for item in fake_db.added if item.__class__.__name__ == "CodexTurnOutcome"]
    assert len(outcomes) == 1
    assert outcomes[0].outcome_kind == "ambiguous"
    assert outcomes[0].payload_json == {
        "reason": "stale_run_recovery",
        "stale_timeout_seconds": settings.ai_run_stale_timeout_seconds,
        "accepted": True,
    }
    assert session.status == "replaced"
    assert session.ended_at is not None
    assert session.lease_owner_run_id is None
    assert session.lease_worker_instance_id is None
    assert session.lease_acquired_at is None
    assert session.lease_heartbeat_at is None
    assert session.lease_expires_at is None
    assert conversation.status == "recovery_required"
    assert persistent_codex._replacement_session_required(
        conversation=conversation,
        session=None,
        prior_turn_count=1,
    )
    assert len(observed["failure_notes"]) == 1
    assert observed["status_changes"] == [("ai_triage", "waiting_on_dev_ti", "system")]
    assert observed["manifest_run_ids"] == [run.id]


def test_process_ai_run_retains_deferred_nonquiescent_cleanup_without_requeue(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    requested_by_user_id = uuid.uuid4()
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        status="ai_triage",
        requeue_requested=True,
        requeue_trigger="requester_reply",
        requeue_requested_by_user_id=requested_by_user_id,
        requeue_forced_route_target_id="support",
        requeue_forced_specialist_id="bug",
        updated_at=None,
    )
    run = SimpleNamespace(
        id=uuid.uuid4(),
        ticket_id=ticket.id,
        status="running",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=11),
        started_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        last_heartbeat_at=None,
        ended_at=None,
        error_text=None,
        triggered_by="new_ticket",
        requested_by_user_id=None,
        forced_route_target_id=None,
        forced_specialist_id=None,
        recovery_attempt_count=0,
    )
    step = SimpleNamespace(
        ai_run_id=run.id,
        step_index=2,
        status="running",
        error_text=None,
        ended_at=None,
    )
    conversation = SimpleNamespace(id=uuid.uuid4(), status="active")
    session = SimpleNamespace(
        id=uuid.uuid4(),
        conversation_id=conversation.id,
        thread_id="thread-from-prior-turn",
        status="active",
        ended_at=None,
        lease_owner_run_id=run.id,
        lease_worker_instance_id="worker-test",
        lease_acquired_at=datetime.now(timezone.utc),
        lease_heartbeat_at=datetime.now(timezone.utc),
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    turn = SimpleNamespace(
        id=uuid.uuid4(),
        ai_run_id=run.id,
        conversation_id=conversation.id,
        session_id=session.id,
        accepted_at=None,
        status="running",
        ended_at=None,
    )
    prepared = SimpleNamespace(
        run_id=run.id,
        ticket_id=ticket.id,
        worker_instance_id="worker-test",
        context=SimpleNamespace(ticket=ticket),
    )
    replacement_run = SimpleNamespace(
        id=uuid.uuid4(),
        triggered_by=None,
        requested_by_user_id=None,
        forced_route_target_id=None,
        forced_specialist_id=None,
    )
    fake_db = _PersistentQueueRecoveryDb(
        stale_runs=[run],
        steps_by_run_id={run.id: [step]},
        tickets_by_id={ticket.id: ticket},
        turns_by_run_id={run.id: [turn]},
        sessions_by_id={session.id: session},
        conversations_by_id={conversation.id: conversation},
    )
    observed = {"triage_logs": [], "manifest_run_ids": [], "deferred_calls": 0, "deferred_metadata": None}

    def raise_nonquiescent(*args, **kwargs):
        raise symbols["PersistentCodexNonQuiescentCleanupError"](
            "Codex output stream persistence did not quiesce after cleanup"
        )

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    def fake_process_deferred_requeue(db, *, ticket):
        observed["deferred_calls"] += 1
        observed["deferred_metadata"] = {
            "triggered_by": ticket.requeue_trigger,
            "requested_by_user_id": ticket.requeue_requested_by_user_id,
            "forced_route_target_id": ticket.requeue_forced_route_target_id,
            "forced_specialist_id": ticket.requeue_forced_specialist_id,
        }
        replacement_run.triggered_by = ticket.requeue_trigger
        replacement_run.requested_by_user_id = ticket.requeue_requested_by_user_id
        replacement_run.forced_route_target_id = ticket.requeue_forced_route_target_id
        replacement_run.forced_specialist_id = ticket.requeue_forced_specialist_id
        ticket.requeue_requested = False
        ticket.requeue_trigger = None
        ticket.requeue_requested_by_user_id = None
        ticket.requeue_forced_route_target_id = None
        ticket.requeue_forced_specialist_id = None
        return replacement_run

    monkeypatch.setattr("worker.triage._prepare_run", lambda *args, **kwargs: prepared)
    monkeypatch.setattr("worker.triage.execute_triage_pipeline", raise_nonquiescent)
    monkeypatch.setattr("worker.triage._apply_success_result", lambda *args, **kwargs: pytest.fail("unexpected success"))
    monkeypatch.setattr("worker.triage._mark_failed", lambda *args, **kwargs: pytest.fail("premature failure handling"))
    monkeypatch.setattr("worker.triage.publish_ai_failure_note", lambda *args, **kwargs: pytest.fail("premature failure note"))
    monkeypatch.setattr("worker.triage.publish_ai_internal_note", lambda *args, **kwargs: pytest.fail("premature internal note"))
    monkeypatch.setattr("worker.triage.record_status_change", lambda *args, **kwargs: pytest.fail("premature status route"))
    monkeypatch.setattr("worker.triage.route_ticket_after_ai", lambda *args, **kwargs: pytest.fail("premature route"))
    monkeypatch.setattr("worker.triage.process_deferred_requeue", lambda *args, **kwargs: pytest.fail("premature deferred requeue"))
    monkeypatch.setattr("worker.triage.write_run_manifest_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "worker.triage.log_worker_event",
        lambda event, **kwargs: observed["triage_logs"].append((event, kwargs)),
    )
    monkeypatch.setattr("worker.queue.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.queue.log_worker_event", lambda *args, **kwargs: None)
    monkeypatch.setattr("worker.queue.create_pending_ai_run", lambda *args, **kwargs: pytest.fail("unexpected stale-prompt retry"))
    monkeypatch.setattr("worker.queue.process_deferred_requeue", fake_process_deferred_requeue)
    monkeypatch.setattr("worker.queue.publish_ai_failure_note", lambda *args, **kwargs: pytest.fail("unexpected failure note"))
    monkeypatch.setattr("worker.queue.record_status_change", lambda *args, **kwargs: pytest.fail("unexpected status change"))
    monkeypatch.setattr(
        "worker.queue.write_run_manifest_snapshot",
        lambda _settings, run_id: observed["manifest_run_ids"].append(run_id),
    )

    symbols["process_ai_run"](settings, run_id=run.id, worker_instance_id="worker-test")

    assert run.status == "running"
    assert run.ended_at is None
    assert run.error_text is None
    stale_before = datetime.now(timezone.utc) - timedelta(seconds=settings.ai_run_stale_timeout_seconds)
    assert run.last_heartbeat_at is None
    assert run.started_at < stale_before
    assert ticket.requeue_requested is True
    assert ticket.requeue_trigger == "requester_reply"
    assert ticket.requeue_requested_by_user_id == requested_by_user_id
    assert ticket.requeue_forced_route_target_id == "support"
    assert ticket.requeue_forced_specialist_id == "bug"
    assert observed["triage_logs"][0][0] == "persistent_codex_cleanup_retained_for_stale_recovery"

    recovered_count = symbols["recover_stale_runs"](settings)

    assert recovered_count == 1
    assert run.status == "failed"
    assert step.status == "failed"
    assert turn.status == "interrupted"
    assert session.status == "replaced"
    assert session.lease_owner_run_id is None
    assert conversation.status == "recovery_required"
    assert observed["deferred_calls"] == 1
    assert observed["deferred_metadata"] == {
        "triggered_by": "requester_reply",
        "requested_by_user_id": requested_by_user_id,
        "forced_route_target_id": "support",
        "forced_specialist_id": "bug",
    }
    assert replacement_run.triggered_by == "requester_reply"
    assert replacement_run.requested_by_user_id == requested_by_user_id
    assert ticket.requeue_requested is False
    assert ticket.requeue_trigger is None
    assert observed["manifest_run_ids"] == [run.id, replacement_run.id]


def test_process_ai_run_marks_failed_for_ordinary_step_run_error(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    prepared = SimpleNamespace(
        run_id=uuid.uuid4(),
        ticket_id=uuid.uuid4(),
        worker_instance_id="worker-test",
        context=SimpleNamespace(),
    )
    observed = {}

    monkeypatch.setattr("worker.triage._prepare_run", lambda *args, **kwargs: prepared)
    monkeypatch.setattr(
        "worker.triage.execute_triage_pipeline",
        lambda *args, **kwargs: (_ for _ in ()).throw(symbols["StepRunError"]("ordinary supervision failure")),
    )
    monkeypatch.setattr("worker.triage.write_run_manifest_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr("worker.triage.log_worker_event", lambda *args, **kwargs: pytest.fail("unexpected stale handoff"))
    monkeypatch.setattr(
        "worker.triage._mark_failed",
        lambda settings, run_id, worker_instance_id, error_text: observed.update(
            {"run_id": run_id, "worker_instance_id": worker_instance_id, "error_text": error_text}
        ),
    )

    symbols["process_ai_run"](settings, run_id=prepared.run_id, worker_instance_id="worker-test")

    assert observed == {
        "run_id": prepared.run_id,
        "worker_instance_id": "worker-test",
        "error_text": "ordinary supervision failure",
    }


def test_process_ai_run_marks_failed_for_quiesced_persistent_cleanup_error_after_finalize(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    prepared = SimpleNamespace(
        run_id=uuid.uuid4(),
        ticket_id=uuid.uuid4(),
        worker_instance_id="worker-test",
        context=SimpleNamespace(),
    )
    finalized = {"called": False}
    observed = {}
    error_text = (
        "Persistent specialist process supervision failed: "
        "Codex output streams did not close after the process exited"
    )

    def raise_after_persistent_finalize(*args, **kwargs):
        finalized["called"] = True
        raise symbols["StepRunError"](error_text)

    monkeypatch.setattr("worker.triage._prepare_run", lambda *args, **kwargs: prepared)
    monkeypatch.setattr("worker.triage.execute_triage_pipeline", raise_after_persistent_finalize)
    monkeypatch.setattr("worker.triage.write_run_manifest_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr("worker.triage._apply_success_result", lambda *args, **kwargs: pytest.fail("unexpected success"))
    monkeypatch.setattr("worker.triage.log_worker_event", lambda *args, **kwargs: pytest.fail("unexpected stale handoff"))
    monkeypatch.setattr(
        "worker.triage._mark_failed",
        lambda settings, run_id, worker_instance_id, error_text: observed.update(
            {"run_id": run_id, "worker_instance_id": worker_instance_id, "error_text": error_text}
        ),
    )

    symbols["process_ai_run"](settings, run_id=prepared.run_id, worker_instance_id="worker-test")

    assert finalized["called"] is True
    assert observed == {
        "run_id": prepared.run_id,
        "worker_instance_id": "worker-test",
        "error_text": error_text,
    }


def test_process_ai_run_marks_failed_for_publication_policy_error(monkeypatch, tmp_path):
    from worker.publication_policy import PublicationPolicyError

    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    prepared = SimpleNamespace(
        run_id=uuid.uuid4(),
        ticket_id=uuid.uuid4(),
        worker_instance_id="worker-test",
        context=SimpleNamespace(),
    )
    observed = {}

    monkeypatch.setattr("worker.triage._prepare_run", lambda *args, **kwargs: prepared)
    monkeypatch.setattr("worker.triage.execute_triage_pipeline", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr("worker.triage.write_run_manifest_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "worker.triage._apply_success_result",
        lambda *args, **kwargs: (_ for _ in ()).throw(PublicationPolicyError("invalid publication policy")),
    )
    monkeypatch.setattr("worker.triage.log_worker_event", lambda *args, **kwargs: pytest.fail("unexpected stale handoff"))
    monkeypatch.setattr(
        "worker.triage._mark_failed",
        lambda settings, run_id, worker_instance_id, error_text: observed.update(
            {"run_id": run_id, "worker_instance_id": worker_instance_id, "error_text": error_text}
        ),
    )

    symbols["process_ai_run"](settings, run_id=prepared.run_id, worker_instance_id="worker-test")

    assert observed == {
        "run_id": prepared.run_id,
        "worker_instance_id": "worker-test",
        "error_text": "invalid publication policy",
    }


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

    symbols["_mark_failed"](settings, run_id=run.id, worker_instance_id="worker-test", error_text="boom")

    assert run.status == "failed"
    assert run.pipeline_version == "agent-pipeline-v1"
    assert run.error_text == "boom"
    assert run.ended_at is not None
    assert ticket.status == "waiting_on_dev_ti"
    assert observed == {"failure_note": 1, "status_changes": 1, "requeue": 1}


def test_mark_failed_raises_when_run_is_no_longer_owned(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        reference="T-000010A",
        title="Recovered failure",
        status="ai_triage",
        urgent=False,
    )
    run = SimpleNamespace(
        id=uuid.uuid4(),
        ticket_id=ticket.id,
        status="failed",
        worker_instance_id="worker-test",
        ended_at=datetime.now(timezone.utc),
        error_text="recovered",
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
        lambda *args, **kwargs: observed.__setitem__("status_changes", observed["status_changes"] + 1),
    )
    monkeypatch.setattr(
        "worker.triage.process_deferred_requeue",
        lambda *args, **kwargs: observed.__setitem__("requeue", observed["requeue"] + 1),
    )

    with pytest.raises(symbols["RunOwnershipLost"], match="failure handling"):
        symbols["_mark_failed"](settings, run_id=run.id, worker_instance_id="worker-test", error_text="boom")

    assert observed == {"failure_note": 0, "status_changes": 0, "requeue": 0}
    assert ticket.status == "ai_triage"


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


def test_write_run_manifest_snapshot_uses_fixed_specialist_registration_id(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    run = SimpleNamespace(
        id=uuid.uuid4(),
        ticket_id=uuid.uuid4(),
        pipeline_version="agent-pipeline-v1",
        status="succeeded",
        final_step_id=uuid.uuid4(),
        final_agent_spec_id="support-spec",
        final_output_contract="specialist_result",
        final_output_json=_specialist_payload(),
        forced_route_target_id="support",
        forced_specialist_id="support-primary",
        worker_pid=9876,
        worker_instance_id="worker-test",
        started_at=SimpleNamespace(isoformat=lambda: "2026-04-06T01:00:00+00:00"),
        last_heartbeat_at=SimpleNamespace(isoformat=lambda: "2026-04-06T01:05:00+00:00"),
        recovered_from_run_id=uuid.uuid4(),
        recovery_attempt_count=2,
        error_text=None,
        ended_at=SimpleNamespace(isoformat=lambda: "2026-04-06T01:10:00+00:00"),
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
            output_json=_route_payload(route_target_id="support", routing_rationale="Standard support request."),
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            step_index=2,
            step_kind="specialist",
            agent_spec_id="support-spec",
            agent_spec_version="2",
            output_contract="specialist_result",
            status="succeeded",
            model_name="gpt-support",
            prompt_path="/tmp/support-prompt.txt",
            schema_path="/tmp/support-schema.json",
            final_output_path="/tmp/support-final.json",
            stdout_jsonl_path="/tmp/support-stdout.jsonl",
            stderr_path="/tmp/support-stderr.txt",
            output_json=_specialist_payload(),
        ),
    ]
    observed = {}
    route_target = SimpleNamespace(
        id="support",
        label="Support",
        kind="direct_ai",
        handler=SimpleNamespace(
            specialist_selection=SimpleNamespace(
                mode="fixed",
                specialist_id="support-primary",
            )
        ),
    )
    registry = SimpleNamespace(require_route_target=lambda route_target_id: route_target)

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
    monkeypatch.setattr("worker.step_runner.load_routing_registry", lambda: registry)
    monkeypatch.setattr("worker.step_runner.build_run_dir", lambda settings, ticket_id, run_id: tmp_path / "run")
    monkeypatch.setattr(
        "worker.step_runner.write_run_manifest",
        lambda run_dir, **kwargs: observed.update({"run_dir": run_dir, **kwargs}),
    )

    symbols["write_run_manifest_snapshot"](settings, run_id=run.id)

    metadata = observed["metadata"]
    assert metadata["selected_specialist_id"] == "support-primary"
    assert metadata["forced_route_target_id"] == "support"
    assert metadata["forced_specialist_id"] == "support-primary"
    assert metadata["worker_pid"] == 9876
    assert metadata["worker_instance_id"] == "worker-test"
    assert metadata["started_at"] == "2026-04-06T01:00:00+00:00"
    assert metadata["last_heartbeat_at"] == "2026-04-06T01:05:00+00:00"
    assert metadata["recovered_from_run_id"] == str(run.recovered_from_run_id)
    assert metadata["recovery_attempt_count"] == 2


def test_claim_oldest_pending_run_sets_worker_ownership(tmp_path):
    symbols = _load_worker_symbols()
    run = SimpleNamespace(
        id=uuid.uuid4(),
        status="pending",
        worker_pid=None,
        worker_instance_id=None,
        started_at=None,
        last_heartbeat_at=None,
        ended_at=SimpleNamespace(),
        error_text="boom",
    )
    db = _ClaimRunDb(run)

    claimed = symbols["claim_oldest_pending_run"](
        db,
        worker_pid=4321,
        worker_instance_id="worker-test",
    )

    assert claimed is run
    assert run.status == "running"
    assert run.worker_pid == 4321
    assert run.worker_instance_id == "worker-test"
    assert run.started_at is not None
    assert run.last_heartbeat_at == run.started_at
    assert run.ended_at is None
    assert run.error_text is None


def test_heartbeat_loop_emits_while_stop_event_controls_exit(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    stop_event = threading.Event()
    observed = {"heartbeats": 0}

    def fake_emit_worker_heartbeat(_settings, **_kwargs):
        observed["heartbeats"] += 1
        stop_event.set()

    monkeypatch.setattr("worker.main.emit_worker_heartbeat", fake_emit_worker_heartbeat)

    symbols["heartbeat_loop"](settings, stop_event=stop_event, interval_seconds=0)

    assert observed["heartbeats"] == 1


def test_emit_worker_heartbeat_initializes_system_state_defaults(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    fake_db = _FakeWorkerStateDb()
    tracker = symbols["ActiveRunTracker"]()
    worker_identity = symbols["WorkerIdentity"](worker_pid=2222, worker_instance_id="worker-test")

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.main.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.main.log_worker_event", lambda *args, **kwargs: None)

    symbols["emit_worker_heartbeat"](
        settings,
        worker_identity=worker_identity,
        active_run_tracker=tracker,
    )

    bootstrap_state = fake_db.objects[("SystemState", "bootstrap_version")]
    heartbeat_state = fake_db.objects[("SystemState", "worker_heartbeat")]
    slack_health_state = fake_db.objects[("SystemState", "slack_dm_delivery_health")]
    slack_user_sync_state = fake_db.objects[("SystemState", "slack_dm_user_sync")]
    assert fake_db.flush_calls == 1
    assert bootstrap_state.value_json == {"version": WORKSPACE_BOOTSTRAP_VERSION}
    assert heartbeat_state.value_json["status"] == "alive"
    assert heartbeat_state.value_json["worker_pid"] == 2222
    assert heartbeat_state.value_json["worker_instance_id"] == "worker-test"
    assert slack_health_state.value_json == {"status": "unknown"}
    assert slack_user_sync_state.value_json == {"status": "unknown", "request_pending": False}


def test_emit_worker_heartbeat_updates_stale_bootstrap_version(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    fake_db = _FakeWorkerStateDb()
    tracker = symbols["ActiveRunTracker"]()
    worker_identity = symbols["WorkerIdentity"](worker_pid=3333, worker_instance_id="worker-test")
    fake_db.objects[("SystemState", "bootstrap_version")] = SimpleNamespace(
        key="bootstrap_version",
        value_json={"version": "stage1-v1"},
        updated_at=None,
    )

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.main.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.main.log_worker_event", lambda *args, **kwargs: None)

    symbols["emit_worker_heartbeat"](
        settings,
        worker_identity=worker_identity,
        active_run_tracker=tracker,
    )

    bootstrap_state = fake_db.objects[("SystemState", "bootstrap_version")]
    assert bootstrap_state.value_json == {"version": WORKSPACE_BOOTSTRAP_VERSION}


def test_emit_worker_heartbeat_updates_active_run_last_heartbeat(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    run = SimpleNamespace(
        id=uuid.uuid4(),
        status="running",
        worker_instance_id="worker-owned",
        last_heartbeat_at=None,
    )
    fake_db = _FakeWorkerStateDb()
    fake_db.objects[("AIRun", run.id)] = run
    tracker = symbols["ActiveRunTracker"]()
    tracker.set_run_id(run.id)
    worker_identity = symbols["WorkerIdentity"](worker_pid=4444, worker_instance_id="worker-owned")

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.main.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.main.log_worker_event", lambda *args, **kwargs: None)

    symbols["emit_worker_heartbeat"](
        settings,
        worker_identity=worker_identity,
        active_run_tracker=tracker,
    )

    assert run.last_heartbeat_at is not None


def test_recover_stale_runs_creates_replacement_run_and_fails_running_steps(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        status="ai_triage",
        requeue_requested=False,
        requeue_trigger=None,
        requeue_requested_by_user_id=None,
        requeue_forced_route_target_id=None,
        requeue_forced_specialist_id=None,
        updated_at=None,
    )
    run = SimpleNamespace(
        id=uuid.uuid4(),
        ticket_id=ticket.id,
        status="running",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=11),
        started_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        last_heartbeat_at=None,
        ended_at=None,
        error_text=None,
        triggered_by="new_ticket",
        requested_by_user_id=None,
        forced_route_target_id=None,
        forced_specialist_id=None,
        recovery_attempt_count=0,
    )
    step = SimpleNamespace(
        ai_run_id=run.id,
        step_index=1,
        status="running",
        error_text=None,
        ended_at=None,
    )
    replacement_run = SimpleNamespace(id=uuid.uuid4())
    fake_db = _QueueRecoveryDb(
        stale_runs=[run],
        steps_by_run_id={run.id: [step]},
        tickets_by_id={ticket.id: ticket},
    )
    observed = {"create_pending_kwargs": None, "manifest_run_ids": []}

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.queue.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.queue.log_worker_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "worker.queue.create_pending_ai_run",
        lambda *args, **kwargs: observed.__setitem__("create_pending_kwargs", kwargs) or replacement_run,
    )
    monkeypatch.setattr("worker.queue.process_deferred_requeue", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "worker.queue.write_run_manifest_snapshot",
        lambda _settings, run_id: observed["manifest_run_ids"].append(run_id),
    )

    recovered_count = symbols["recover_stale_runs"](settings)

    assert recovered_count == 1
    assert run.status == "failed"
    assert run.ended_at is not None
    assert "stale" in run.error_text.lower()
    assert step.status == "failed"
    assert step.ended_at is not None
    assert observed["create_pending_kwargs"]["recovered_from_run_id"] == run.id
    assert observed["create_pending_kwargs"]["recovery_attempt_count"] == 1
    assert observed["manifest_run_ids"] == [run.id, replacement_run.id]


def test_recover_stale_runs_honors_deferred_requeue(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        status="new",
        requeue_requested=True,
        requeue_trigger="manual_rerun",
        requeue_requested_by_user_id=uuid.uuid4(),
        requeue_forced_route_target_id="support",
        requeue_forced_specialist_id="support",
        updated_at=None,
    )
    run = SimpleNamespace(
        id=uuid.uuid4(),
        ticket_id=ticket.id,
        status="running",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=11),
        started_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        last_heartbeat_at=None,
        ended_at=None,
        error_text=None,
        triggered_by="new_ticket",
        requested_by_user_id=None,
        forced_route_target_id=None,
        forced_specialist_id=None,
        recovery_attempt_count=0,
    )
    replacement_run = SimpleNamespace(id=uuid.uuid4())
    fake_db = _QueueRecoveryDb(
        stale_runs=[run],
        steps_by_run_id={run.id: []},
        tickets_by_id={ticket.id: ticket},
    )
    observed = {"deferred_requeue_calls": 0}

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.queue.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.queue.log_worker_event", lambda *args, **kwargs: None)
    monkeypatch.setattr("worker.queue.create_pending_ai_run", lambda *args, **kwargs: pytest.fail("unexpected direct requeue"))
    monkeypatch.setattr(
        "worker.queue.process_deferred_requeue",
        lambda db, ticket: observed.__setitem__("deferred_requeue_calls", observed["deferred_requeue_calls"] + 1) or replacement_run,
    )
    monkeypatch.setattr("worker.queue.write_run_manifest_snapshot", lambda *args, **kwargs: None)

    recovered_count = symbols["recover_stale_runs"](settings)

    assert recovered_count == 1
    assert run.status == "failed"
    assert observed["deferred_requeue_calls"] == 1


def test_recover_stale_runs_processes_deferred_requeue_after_persistent_recovery(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    requested_by_user_id = uuid.uuid4()
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        status="ai_triage",
        requeue_requested=True,
        requeue_trigger="manual_rerun",
        requeue_requested_by_user_id=requested_by_user_id,
        requeue_forced_route_target_id="support",
        requeue_forced_specialist_id="bug",
        updated_at=None,
    )
    run = SimpleNamespace(
        id=uuid.uuid4(),
        ticket_id=ticket.id,
        status="running",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=11),
        started_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        last_heartbeat_at=None,
        ended_at=None,
        error_text=None,
        triggered_by="new_ticket",
        requested_by_user_id=None,
        forced_route_target_id=None,
        forced_specialist_id=None,
        recovery_attempt_count=0,
    )
    fake_db = _QueueRecoveryDb(
        stale_runs=[run],
        steps_by_run_id={run.id: []},
        tickets_by_id={ticket.id: ticket},
    )
    replacement_run = SimpleNamespace(
        id=uuid.uuid4(),
        triggered_by=None,
        requested_by_user_id=None,
        forced_route_target_id=None,
        forced_specialist_id=None,
    )
    observed = {"manifest_run_ids": [], "deferred_calls": 0, "deferred_metadata": None}

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.queue.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.queue.log_worker_event", lambda *args, **kwargs: None)
    monkeypatch.setattr("worker.queue.handle_stale_persistent_run", lambda *args, **kwargs: True)
    monkeypatch.setattr("worker.queue.create_pending_ai_run", lambda *args, **kwargs: pytest.fail("unexpected replacement"))

    def fake_process_deferred_requeue(db, *, ticket):
        observed["deferred_calls"] += 1
        observed["deferred_metadata"] = {
            "triggered_by": ticket.requeue_trigger,
            "requested_by_user_id": ticket.requeue_requested_by_user_id,
            "forced_route_target_id": ticket.requeue_forced_route_target_id,
            "forced_specialist_id": ticket.requeue_forced_specialist_id,
        }
        replacement_run.triggered_by = ticket.requeue_trigger
        replacement_run.requested_by_user_id = ticket.requeue_requested_by_user_id
        replacement_run.forced_route_target_id = ticket.requeue_forced_route_target_id
        replacement_run.forced_specialist_id = ticket.requeue_forced_specialist_id
        ticket.requeue_requested = False
        ticket.requeue_trigger = None
        ticket.requeue_requested_by_user_id = None
        ticket.requeue_forced_route_target_id = None
        ticket.requeue_forced_specialist_id = None
        return replacement_run

    monkeypatch.setattr("worker.queue.process_deferred_requeue", fake_process_deferred_requeue)
    monkeypatch.setattr("worker.queue.publish_ai_failure_note", lambda *args, **kwargs: pytest.fail("unexpected failure note"))
    monkeypatch.setattr("worker.queue.record_status_change", lambda *args, **kwargs: pytest.fail("unexpected status change"))
    monkeypatch.setattr(
        "worker.queue.write_run_manifest_snapshot",
        lambda _settings, run_id: observed["manifest_run_ids"].append(run_id),
    )

    recovered_count = symbols["recover_stale_runs"](settings)

    assert recovered_count == 1
    assert run.status == "failed"
    assert observed["deferred_calls"] == 1
    assert observed["deferred_metadata"] == {
        "triggered_by": "manual_rerun",
        "requested_by_user_id": requested_by_user_id,
        "forced_route_target_id": "support",
        "forced_specialist_id": "bug",
    }
    assert ticket.requeue_requested is False
    assert ticket.requeue_trigger is None
    assert replacement_run.triggered_by == "manual_rerun"
    assert replacement_run.requested_by_user_id == requested_by_user_id
    assert replacement_run.forced_route_target_id == "support"
    assert replacement_run.forced_specialist_id == "bug"
    assert observed["manifest_run_ids"] == [run.id, replacement_run.id]


def test_recover_stale_persistent_deferred_requeue_retained_when_creation_loses_active_run_race(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    requested_by_user_id = uuid.uuid4()
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        status="ai_triage",
        requeue_requested=True,
        requeue_trigger="requester_reply",
        requeue_requested_by_user_id=requested_by_user_id,
        requeue_forced_route_target_id="support",
        requeue_forced_specialist_id="bug",
        updated_at=None,
    )
    run = SimpleNamespace(
        id=uuid.uuid4(),
        ticket_id=ticket.id,
        status="running",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=11),
        started_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        last_heartbeat_at=None,
        ended_at=None,
        error_text=None,
        triggered_by="new_ticket",
        requested_by_user_id=None,
        forced_route_target_id=None,
        forced_specialist_id=None,
        recovery_attempt_count=0,
    )
    fake_db = _QueueRecoveryDb(
        stale_runs=[run],
        steps_by_run_id={run.id: []},
        tickets_by_id={ticket.id: ticket},
    )
    observed = {"deferred_calls": 0, "manifest_run_ids": []}

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    def fake_process_deferred_requeue(db, *, ticket):
        observed["deferred_calls"] += 1
        return None

    monkeypatch.setattr("worker.queue.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.queue.log_worker_event", lambda *args, **kwargs: None)
    monkeypatch.setattr("worker.queue.handle_stale_persistent_run", lambda *args, **kwargs: True)
    monkeypatch.setattr("worker.queue.create_pending_ai_run", lambda *args, **kwargs: pytest.fail("unexpected stale-prompt retry"))
    monkeypatch.setattr("worker.queue.process_deferred_requeue", fake_process_deferred_requeue)
    monkeypatch.setattr("worker.queue.publish_ai_failure_note", lambda *args, **kwargs: pytest.fail("unexpected failure note"))
    monkeypatch.setattr("worker.queue.record_status_change", lambda *args, **kwargs: pytest.fail("unexpected status change"))
    monkeypatch.setattr(
        "worker.queue.write_run_manifest_snapshot",
        lambda _settings, run_id: observed["manifest_run_ids"].append(run_id),
    )

    recovered_count = symbols["recover_stale_runs"](settings)

    assert recovered_count == 1
    assert run.status == "failed"
    assert observed["deferred_calls"] == 1
    assert ticket.requeue_requested is True
    assert ticket.requeue_trigger == "requester_reply"
    assert ticket.requeue_requested_by_user_id == requested_by_user_id
    assert ticket.requeue_forced_route_target_id == "support"
    assert ticket.requeue_forced_specialist_id == "bug"
    assert observed["manifest_run_ids"] == [run.id]


def test_recover_stale_persistent_run_without_deferred_work_routes_internally(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        status="ai_triage",
        requeue_requested=False,
        requeue_trigger=None,
        requeue_requested_by_user_id=None,
        requeue_forced_route_target_id=None,
        requeue_forced_specialist_id=None,
        updated_at=None,
    )
    run = SimpleNamespace(
        id=uuid.uuid4(),
        ticket_id=ticket.id,
        status="running",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=11),
        started_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        last_heartbeat_at=None,
        ended_at=None,
        error_text=None,
        triggered_by="new_ticket",
        requested_by_user_id=None,
        forced_route_target_id=None,
        forced_specialist_id=None,
        recovery_attempt_count=0,
    )
    fake_db = _QueueRecoveryDb(
        stale_runs=[run],
        steps_by_run_id={run.id: []},
        tickets_by_id={ticket.id: ticket},
    )
    observed = {"failure_notes": [], "status_changes": [], "manifest_run_ids": []}

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.queue.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.queue.log_worker_event", lambda *args, **kwargs: None)
    monkeypatch.setattr("worker.queue.handle_stale_persistent_run", lambda *args, **kwargs: True)
    monkeypatch.setattr("worker.queue.process_deferred_requeue", lambda *args, **kwargs: pytest.fail("unexpected deferred requeue"))
    monkeypatch.setattr("worker.queue.create_pending_ai_run", lambda *args, **kwargs: pytest.fail("unexpected replacement run"))
    monkeypatch.setattr(
        "worker.queue.publish_ai_failure_note",
        lambda db, ticket, ai_run_id, body_markdown, created_at=None: observed["failure_notes"].append(
            {
                "visibility_source": "publish_ai_failure_note",
                "ai_run_id": ai_run_id,
                "body": body_markdown,
            }
        ),
    )
    monkeypatch.setattr(
        "worker.queue.record_status_change",
        lambda db, ticket, to_status, changed_by_type, changed_at, **kwargs: observed["status_changes"].append(
            (ticket.status, to_status, changed_by_type)
        )
        or setattr(ticket, "status", to_status),
    )
    monkeypatch.setattr(
        "worker.queue.write_run_manifest_snapshot",
        lambda _settings, run_id: observed["manifest_run_ids"].append(run_id),
    )

    recovered_count = symbols["recover_stale_runs"](settings)

    assert recovered_count == 1
    assert run.status == "failed"
    assert ticket.status == "waiting_on_dev_ti"
    assert len(observed["failure_notes"]) == 1
    assert observed["failure_notes"][0]["ai_run_id"] == run.id
    assert "not retried automatically" in observed["failure_notes"][0]["body"]
    assert observed["status_changes"] == [("ai_triage", "waiting_on_dev_ti", "system")]
    assert observed["manifest_run_ids"] == [run.id]


def test_handle_stale_persistent_run_retires_unaccepted_session_for_recovery():
    pytest.importorskip("sqlalchemy")

    from worker import persistent_codex

    run = SimpleNamespace(id=uuid.uuid4())
    conversation = SimpleNamespace(id=uuid.uuid4(), status="active")
    session = SimpleNamespace(
        id=uuid.uuid4(),
        conversation_id=conversation.id,
        thread_id="thread-from-prior-turn",
        status="active",
        ended_at=None,
        lease_owner_run_id=run.id,
        lease_worker_instance_id="worker-test",
        lease_acquired_at=datetime.now(timezone.utc),
        lease_heartbeat_at=datetime.now(timezone.utc),
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    turn = SimpleNamespace(
        id=uuid.uuid4(),
        ai_run_id=run.id,
        conversation_id=conversation.id,
        session_id=session.id,
        accepted_at=None,
        status="running",
        ended_at=None,
    )

    class StalePersistentDb:
        def __init__(self):
            self.added = []

        def execute(self, statement):
            descriptions = statement.column_descriptions
            first_name = descriptions[0]["name"]
            entity = descriptions[0].get("entity")
            entity_name = getattr(entity, "__name__", "")
            if first_name == "coalesce":
                return _FakePersistentScalarResult(0)
            if entity_name == "CodexTurn":
                return _FakeWorkerStateResult([turn])
            if entity_name == "CodexSession":
                return _FakeWorkerStateResult([session])
            return _FakeWorkerStateResult([])

        def add(self, item):
            self.added.append(item)

        def get(self, model, key):
            if getattr(model, "__name__", "") == "CodexConversation" and key == conversation.id:
                return conversation
            return None

    fake_db = StalePersistentDb()

    handled = persistent_codex.handle_stale_persistent_run(
        fake_db,
        run=run,
        stale_timeout_seconds=600,
    )

    assert handled is True
    assert turn.status == "interrupted"
    assert turn.ended_at is not None
    assert session.status == "replaced"
    assert session.ended_at is not None
    assert session.lease_owner_run_id is None
    assert session.lease_worker_instance_id is None
    assert session.lease_expires_at is None
    assert conversation.status == "recovery_required"
    outcomes = [item for item in fake_db.added if item.__class__.__name__ == "CodexTurnOutcome"]
    assert len(outcomes) == 1
    assert outcomes[0].outcome_kind == "interrupted"
    assert outcomes[0].payload_json["accepted"] is False


def test_recover_stale_runs_routes_ticket_when_retry_budget_is_exhausted(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        status="ai_triage",
        requeue_requested=True,
        requeue_trigger="manual_rerun",
        requeue_requested_by_user_id=uuid.uuid4(),
        requeue_forced_route_target_id="support",
        requeue_forced_specialist_id="support",
        updated_at=None,
    )
    run = SimpleNamespace(
        id=uuid.uuid4(),
        ticket_id=ticket.id,
        status="running",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=11),
        started_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        last_heartbeat_at=None,
        ended_at=None,
        error_text=None,
        triggered_by="new_ticket",
        requested_by_user_id=None,
        forced_route_target_id=None,
        forced_specialist_id=None,
        recovery_attempt_count=settings.ai_run_max_recovery_attempts,
    )
    fake_db = _QueueRecoveryDb(
        stale_runs=[run],
        steps_by_run_id={run.id: []},
        tickets_by_id={ticket.id: ticket},
    )
    observed = {"failure_notes": [], "status_changes": []}

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.queue.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.queue.log_worker_event", lambda *args, **kwargs: None)
    monkeypatch.setattr("worker.queue.process_deferred_requeue", lambda *args, **kwargs: pytest.fail("unexpected deferred requeue"))
    monkeypatch.setattr("worker.queue.create_pending_ai_run", lambda *args, **kwargs: pytest.fail("unexpected replacement run"))
    monkeypatch.setattr(
        "worker.queue.publish_ai_failure_note",
        lambda db, ticket, ai_run_id, body_markdown, created_at=None: observed["failure_notes"].append(body_markdown),
    )
    monkeypatch.setattr(
        "worker.queue.record_status_change",
        lambda db, ticket, to_status, changed_by_type, changed_at, **kwargs: observed["status_changes"].append((ticket.status, to_status, changed_by_type)) or setattr(ticket, "status", to_status),
    )
    monkeypatch.setattr("worker.queue.write_run_manifest_snapshot", lambda *args, **kwargs: None)

    recovered_count = symbols["recover_stale_runs"](settings)

    assert recovered_count == 1
    assert run.status == "failed"
    assert ticket.status == "waiting_on_dev_ti"
    assert ticket.requeue_requested is False
    assert ticket.requeue_trigger is None
    assert observed["failure_notes"]
    assert "exhausted" in observed["failure_notes"][0].lower()
    assert observed["status_changes"] == [("ai_triage", "waiting_on_dev_ti", "system")]
