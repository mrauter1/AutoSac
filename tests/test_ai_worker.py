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


class _SteeringResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)

    def scalar_one(self):
        if not self._rows:
            raise AssertionError("expected one row")
        return self._rows[0]

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self

    def __iter__(self):
        return iter(self._rows)


class _SteeringDb:
    def __init__(self, *, run, session, turn, step, ticket):
        self.run = run
        self.session = session
        self.turn = turn
        self.step = step
        self.ticket = ticket
        self.receipts = []
        self.inputs = []
        self.outcomes = []
        self.added = []
        self.flush_calls = 0

    def add(self, item):
        self.added.append(item)
        name = item.__class__.__name__
        if name == "CodexTurnSteer":
            self.receipts.append(item)
        elif name == "CodexTurnInput":
            self.inputs.append(item)
        elif name == "CodexTurnOutcome":
            self.outcomes.append(item)

    def _criterion_value(self, statement, column_name: str):
        for criterion in getattr(statement, "_where_criteria", ()):
            left = getattr(criterion, "left", None)
            right = getattr(criterion, "right", None)
            if getattr(left, "name", None) == column_name and hasattr(right, "value"):
                return right.value
        return None

    def flush(self):
        self.flush_calls += 1

    def get(self, model, key):
        name = getattr(model, "__name__", "")
        if name == "AIRun" and key == self.run.id:
            return self.run
        if name == "Ticket" and key == self.ticket.id:
            return self.ticket
        if name == "CodexConversation":
            return SimpleNamespace(id=self.turn.conversation_id, status="active")
        return None

    def execute(self, statement):
        descriptions = statement.column_descriptions if getattr(statement, "column_descriptions", None) else []
        first = descriptions[0] if descriptions else {}
        name = first.get("name")
        entity = first.get("entity")
        entity_name = getattr(entity, "__name__", "")
        query_text = str(statement)
        if entity_name == "AIRun":
            return _SteeringResult([self.run])
        if entity_name == "Ticket":
            return _SteeringResult([self.ticket])
        if entity_name == "CodexTurn":
            return _SteeringResult([self.turn])
        if entity_name == "CodexTurnSteer":
            if "codex_turn_steers.status IN" in query_text:
                return _SteeringResult([receipt for receipt in self.receipts if receipt.status in {"prepared", "sending"}])
            receipt_id = self._criterion_value(statement, "id")
            dedupe_key = self._criterion_value(statement, "dedupe_key")
            rows = self.receipts
            if receipt_id is not None:
                rows = [receipt for receipt in rows if receipt.id == receipt_id]
            if dedupe_key is not None:
                rows = [receipt for receipt in rows if receipt.dedupe_key == dedupe_key]
            return _SteeringResult(rows[:1])
        if name == "dedupe_key":
            return _SteeringResult([(receipt.dedupe_key,) for receipt in self.receipts])
        if name == "id":
            dedupe_key = self._criterion_value(statement, "dedupe_key")
            rows = self.inputs
            if dedupe_key is not None:
                rows = [item for item in rows if item.dedupe_key == dedupe_key]
            return _SteeringResult([rows[0].id] if rows else [])
        if name == "coalesce":
            return _FakePersistentScalarResult(max((item.input_index for item in self.inputs), default=0))
        if name == "event_kind":
            return _SteeringResult(
                [
                    (item.event_kind, item.source_kind, item.source_id, item.dedupe_key, item.payload_json)
                    for item in sorted(self.inputs, key=lambda item: item.input_index)
                ]
            )
        return _SteeringResult([])


def _steering_attachment(path: Path, *, is_image: bool, size_bytes: int | None = None, mime_type: str | None = None):
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


def _steering_event(
    body: str = "Need this in the active turn.",
    *,
    source_id=None,
    supported: bool = True,
    attachments=(),
    author_type: str = "dev_ti",
    visibility: str = "internal",
    source: str = "human_internal_note",
):
    source_id = source_id or uuid.uuid4()
    return SimpleNamespace(
        event_kind="ticket_message",
        source_kind="ticket_message",
        source_id=source_id,
        dedupe_key=f"ticket-message:{source_id}",
        payload_json={
            "message_id": str(source_id),
            "dedupe_key": f"ticket-message:{source_id}",
            "ticket_id": str(uuid.uuid4()),
            "author_type": author_type,
            "visibility": visibility,
            "source": source,
            "body_text": body,
            "body": {"text": body, "markdown": body},
            "attachments": tuple(attachments),
            "bundle": {
                "logical_input": "ticket_message_with_attachments",
                "attachment_count": len(tuple(attachments)),
                "representation_status": "supported" if supported else "unsupported",
                "representation_errors": () if supported else ("unsupported_attachment",),
            },
            "causal": {"ai_run_id": None, "codex_turn_outcome_id": None},
        },
        order_key=(2, str(source_id)),
    )


def _steering_runtime(persistent_codex, prepared, *, ticket_status: str = "ai_triage"):
    persistent = persistent_codex.PreparedPersistentSpecialistStep(
        step_id=uuid.uuid4(),
        turn_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        command_spec=persistent_codex.PersistentCommandSpec(
            command=["codex", "app-server", "--stdio"],
            env={},
            runtime_codex_home=Path("/tmp/codex-home"),
            resumed=False,
        ),
        transport_kind="app_server",
        stored_thread_id="thread-1",
        pending_events=(),
        effective_input_hash="initial-hash",
    )
    run = SimpleNamespace(
        id=prepared.run_id,
        ticket_id=prepared.ticket_id,
        status="running",
        forced_route_target_id=None,
        forced_specialist_id=None,
        last_heartbeat_at=None,
    )
    session = SimpleNamespace(
        id=persistent.session_id,
        thread_id="thread-1",
        lease_owner_run_id=prepared.run_id,
        lease_worker_instance_id=prepared.worker_instance_id,
        lease_heartbeat_at=None,
        lease_expires_at=None,
    )
    turn = SimpleNamespace(
        id=persistent.turn_id,
        conversation_id=persistent.conversation_id,
        status="running",
        native_turn_id="turn-1",
        steering_closed_at=None,
        effective_input_hash="initial-hash",
    )
    step = SimpleNamespace(id=persistent.step_id)
    ticket = SimpleNamespace(
        id=prepared.ticket_id,
        status=ticket_status,
        requeue_requested=True,
        requeue_trigger="ticket_content",
        requeue_source_message_id=None,
        requeue_requested_by_user_id=uuid.uuid4(),
        requeue_forced_route_target_id=None,
        requeue_forced_specialist_id=None,
        updated_at=None,
    )
    return persistent, run, session, turn, step, ticket


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
    event = SimpleNamespace(
        event_kind="ticket_message",
        source_kind="ticket_message",
        source_id=uuid.uuid4(),
        dedupe_key=f"ticket-message:{uuid.uuid4()}",
        payload_json={"body_text": "accepted"},
    )
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
        transport_kind="exec",
        stored_thread_id="thread-1" if resumed else None,
        pending_events=(event,),
        effective_input_hash="test-input-hash",
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
    persistent = _persistent_step_for_test(persistent_codex, tmp_path)
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
    monkeypatch.setattr("worker.codex_app_server.session_scope", fake_session_scope)
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
    assert captured == [
        (
            "accepted",
            {
                "event_type": "turn.started",
                "thread_id": "thread-1",
                "native_turn_id": None,
                "effective_input_hash": "test-input-hash",
            },
        )
    ]


def test_app_server_persistent_specialist_starts_accepts_inputs_once_and_completes(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")

    from worker import persistent_codex
    from worker.codex_inputs import hash_input_events

    symbols, base_settings, prepared = _prepare_persistent_test_step(tmp_path)
    settings = Settings(
        **{
            **base_settings.__dict__,
            "codex_app_server_specialist_transport_enabled": True,
        }
    )
    events = (
        SimpleNamespace(
            event_kind="ticket_state_snapshot",
            source_kind="ticket",
            source_id=prepared.ticket_id,
            dedupe_key="ticket-state:1",
            payload_json={"status": "ai_triage"},
        ),
        SimpleNamespace(
            event_kind="ticket_message",
            source_kind="ticket_message",
            source_id=uuid.uuid4(),
            dedupe_key="ticket-message:1",
            payload_json={"body_text": "Please continue."},
        ),
    )
    effective_input_hash = hash_input_events(events)
    persistent = persistent_codex.PreparedPersistentSpecialistStep(
        step_id=uuid.uuid4(),
        turn_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        command_spec=persistent_codex.PersistentCommandSpec(
            command=["codex", "app-server", "--stdio"],
            env={},
            runtime_codex_home=settings.resolved_codex_home,
            resumed=False,
        ),
        transport_kind="app_server",
        stored_thread_id=None,
        pending_events=events,
        effective_input_hash=effective_input_hash,
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
        lease_acquired_at=None,
    )
    turn = SimpleNamespace(
        id=persistent.turn_id,
        accepted_at=None,
        native_turn_id=None,
        effective_input_hash=None,
        transport_kind="app_server",
        steering_closed_at=None,
        status="running",
        ended_at=None,
    )
    step = SimpleNamespace(id=persistent.step_id, ended_at=None, status="running", output_json=None, error_text=None)

    class AppServerDb:
        def __init__(self):
            self.inputs = []
            self.items = []
            self.added = []
            self.accepted_outcomes = 0

        def add(self, item):
            self.added.append(item)
            if item.__class__.__name__ == "CodexTurnInput":
                self.inputs.append(item)
            elif item.__class__.__name__ == "CodexTurnItem":
                self.items.append(item)

        def execute(self, statement):
            descriptions = statement.column_descriptions
            first_name = descriptions[0]["name"]
            entity = descriptions[0].get("entity")
            entity_name = getattr(entity, "__name__", "")
            if first_name == "dedupe_key":
                return _FakeWorkerStateResult([(item.dedupe_key,) for item in self.inputs])
            if first_name == "event_kind":
                return _FakeWorkerStateResult(
                    [
                        (item.event_kind, item.source_kind, item.source_id, item.dedupe_key, item.payload_json)
                        for item in self.inputs
                    ]
                )
            if first_name == "count" or "count" in first_name:
                return _FakePersistentScalarResult(self.accepted_outcomes)
            if entity_name == "Ticket":
                return _FakeWorkerStateResult([SimpleNamespace(id=prepared.ticket_id, status="ai_triage")])
            if entity_name == "CodexTurnSteer":
                return _FakeWorkerStateResult([])
            return _FakeWorkerStateResult([])

        def get(self, model, key):
            return SimpleNamespace(id=persistent.conversation_id, status="active")

    fake_db = AppServerDb()
    outcomes = []
    calls = []

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    class FakeAppServerClient:
        def __init__(self, *args, on_protocol_item=None, on_thread_id=None, on_turn_id=None, **kwargs):
            self.on_protocol_item = on_protocol_item
            self.on_thread_id = on_thread_id
            self.on_turn_id = on_turn_id
            self.stderr_text = ""
            self.process = SimpleNamespace(poll=lambda: 0)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def initialize(self, *, timeout_seconds=None):
            calls.append(("initialize", timeout_seconds))
            return {}

        def start_or_resume_thread(self, *, stored_thread_id, prepared, timeout_seconds=None):
            calls.append(("thread/start", stored_thread_id))
            assert stored_thread_id is None
            self.on_thread_id("thread-new")
            return SimpleNamespace(thread_id="thread-new", resumed=False)

        def start_turn(self, *, thread_id, input_payload, prepared, timeout_seconds=None):
            calls.append(("turn/start", thread_id, input_payload))
            self.on_protocol_item(
                {"method": "turn/started", "params": {"threadId": thread_id, "turn": {"id": "native-turn-1"}}}
            )
            self.on_turn_id("native-turn-1")
            return SimpleNamespace(thread_id=thread_id, turn_id="native-turn-1", response={})

        def supervise_until_completed(self, *, thread_id, turn_id, deadline, on_poll=None, poll_interval_seconds=0.05):
            calls.append(("supervise", thread_id, turn_id))
            completed = {
                "method": "turn/completed",
                "params": {
                    "threadId": thread_id,
                    "turn": {
                        "id": turn_id,
                        "status": "completed",
                        "items": [
                            {
                                "type": "agentMessage",
                                "id": "agent-final",
                                "phase": "final_answer",
                                "text": json.dumps(_specialist_payload()),
                            }
                        ],
                    },
                },
            }
            self.on_protocol_item(completed)
            return completed

    monkeypatch.setattr(persistent_codex, "session_scope", fake_session_scope)
    monkeypatch.setattr("worker.codex_app_server.session_scope", fake_session_scope)
    monkeypatch.setattr(
        persistent_codex,
        "_load_locked_owned_runtime_records",
        lambda db, *, prepared, persistent: (run, session, turn, step),
    )

    def append_outcome(db, *, turn_id, outcome_kind, payload_json):
        outcomes.append((outcome_kind, payload_json))
        if outcome_kind == "accepted":
            fake_db.accepted_outcomes += 1

    monkeypatch.setattr(persistent_codex, "_append_turn_outcome", append_outcome)
    monkeypatch.setattr(
        persistent_codex,
        "prepare_persistent_specialist_step",
        lambda settings, prepared, prompt_state: persistent,
    )
    monkeypatch.setattr(persistent_codex, "CodexAppServerClient", FakeAppServerClient)

    result = persistent_codex.execute_persistent_specialist_step(
        settings,
        prepared=prepared,
        prompt_state=SimpleNamespace(),
    )

    assert result.output_payload["summary_internal"] == "Requester needs access guidance."
    assert session.thread_id == "thread-new"
    assert session.lease_owner_run_id is None
    assert turn.native_turn_id == "native-turn-1"
    assert turn.transport_kind == "app_server"
    assert turn.effective_input_hash == effective_input_hash
    assert [item.dedupe_key for item in fake_db.inputs] == ["ticket-state:1", "ticket-message:1"]
    assert len(fake_db.inputs) == 2
    assert [item.item_kind for item in fake_db.items] == ["turn/started", "turn/completed"]
    assert [call[0] for call in calls] == ["initialize", "thread/start", "turn/start", "supervise"]
    assert "turn/steer" not in [call[0] for call in calls]
    assert [outcome[0] for outcome in outcomes].count("accepted") == 1
    assert outcomes[-1][0] == "completed"


def test_active_turn_steering_accepts_internal_note_clears_matching_escrow(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")

    from worker import persistent_codex

    symbols, base_settings, prepared = _prepare_persistent_test_step(tmp_path)
    settings = Settings(
        **{
            **base_settings.__dict__,
            "codex_app_server_specialist_transport_enabled": True,
            "codex_active_turn_steering_enabled": True,
        }
    )
    persistent, run, session, turn, step, ticket = _steering_runtime(persistent_codex, prepared)
    event = _steering_event()
    ticket.requeue_source_message_id = event.source_id
    db = _SteeringDb(run=run, session=session, turn=turn, step=step, ticket=ticket)

    @contextmanager
    def fake_session_scope(_settings):
        yield db

    class SteeringClient:
        def __init__(self):
            self.calls = []

        def steer_turn(self, *, thread_id, expected_turn_id, input_payload, timeout_seconds=None):
            self.calls.append((thread_id, expected_turn_id, input_payload))
            return SimpleNamespace(rpc_request_id="rpc-steer-1")

    client = SteeringClient()
    monkeypatch.setattr(persistent_codex, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        persistent_codex,
        "_load_locked_owned_runtime_records",
        lambda db, *, prepared, persistent: (run, session, turn, step),
    )
    monkeypatch.setattr(
        persistent_codex,
        "_load_active_steering_candidates",
        lambda *args, **kwargs: (event,),
    )

    sent = persistent_codex._attempt_active_steering_once(
        settings,
        prepared=prepared,
        persistent=persistent,
        client=client,
        thread_id="thread-1",
        native_turn_id="turn-1",
        deadline=time.monotonic() + 30,
    )

    assert sent == 1
    assert len(client.calls) == 1
    assert [receipt.status for receipt in db.receipts] == ["accepted"]
    assert db.receipts[0].rpc_request_id == "rpc-steer-1"
    assert [item.dedupe_key for item in db.inputs] == [event.dedupe_key]
    assert ticket.requeue_requested is False
    assert ticket.requeue_trigger is None
    assert any(outcome.outcome_kind == "accepted" and outcome.payload_json["event_type"] == "turn/steer" for outcome in db.outcomes)


def test_active_turn_steering_accepts_text_plus_image_bundle(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")

    from worker import persistent_codex

    symbols, base_settings, prepared = _prepare_persistent_test_step(tmp_path)
    settings = Settings(
        **{
            **base_settings.__dict__,
            "codex_app_server_specialist_transport_enabled": True,
            "codex_active_turn_steering_enabled": True,
        }
    )
    ticket_dir = settings.uploads_dir / "ticket-steer-1"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    image_path = ticket_dir / "screenshot.png"
    image_path.write_bytes(b"png-data")
    document_path = ticket_dir / "notes.pdf"
    document_path.write_bytes(b"pdf-data")

    persistent, run, session, turn, step, ticket = _steering_runtime(persistent_codex, prepared)
    event = _steering_event(
        "Bundle with evidence.",
        attachments=(
            _steering_attachment(image_path, is_image=True),
            _steering_attachment(document_path, is_image=False, mime_type="application/pdf"),
        ),
        visibility="public",
        source="requester_reply",
    )
    db = _SteeringDb(run=run, session=session, turn=turn, step=step, ticket=ticket)

    @contextmanager
    def fake_session_scope(_settings):
        yield db

    class SteeringClient:
        def __init__(self):
            self.calls = []

        def steer_turn(self, *, thread_id, expected_turn_id, input_payload, timeout_seconds=None):
            self.calls.append(input_payload)
            return SimpleNamespace(rpc_request_id="rpc-steer-bundle")

    client = SteeringClient()
    monkeypatch.setattr(persistent_codex, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        persistent_codex,
        "_load_locked_owned_runtime_records",
        lambda db, *, prepared, persistent: (run, session, turn, step),
    )
    monkeypatch.setattr(
        persistent_codex,
        "_load_active_steering_candidates",
        lambda *args, **kwargs: (event,),
    )

    sent = persistent_codex._attempt_active_steering_once(
        settings,
        prepared=prepared,
        persistent=persistent,
        client=client,
        thread_id="thread-1",
        native_turn_id="turn-1",
        deadline=time.monotonic() + 30,
    )

    assert sent == 1
    assert [receipt.status for receipt in db.receipts] == ["accepted"]
    assert [item.dedupe_key for item in db.inputs] == [event.dedupe_key]
    assert [item["type"] for item in client.calls[0]] == ["text", "localImage"]
    assert client.calls[0][1]["path"] == str(image_path.resolve())
    payload = json.loads(client.calls[0][0]["text"])
    assert payload["events"][0]["attachments"][0]["safe_input"]["stored_path"] == str(image_path)
    assert payload["events"][0]["attachments"][1]["safe_input"]["stored_path"] == str(document_path)


def test_active_turn_steering_accepts_non_image_document_bundle_without_local_image(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")

    from worker import persistent_codex

    symbols, base_settings, prepared = _prepare_persistent_test_step(tmp_path)
    settings = Settings(
        **{
            **base_settings.__dict__,
            "codex_app_server_specialist_transport_enabled": True,
            "codex_active_turn_steering_enabled": True,
        }
    )
    ticket_dir = settings.uploads_dir / "ticket-steer-2"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    document_path = ticket_dir / "notes.pdf"
    document_path.write_bytes(b"pdf-data")

    persistent, run, session, turn, step, ticket = _steering_runtime(persistent_codex, prepared)
    event = _steering_event(
        "Document only.",
        attachments=(_steering_attachment(document_path, is_image=False, mime_type="application/pdf"),),
        visibility="public",
        source="requester_reply",
    )
    db = _SteeringDb(run=run, session=session, turn=turn, step=step, ticket=ticket)

    @contextmanager
    def fake_session_scope(_settings):
        yield db

    class SteeringClient:
        def __init__(self):
            self.calls = []

        def steer_turn(self, *, thread_id, expected_turn_id, input_payload, timeout_seconds=None):
            self.calls.append(input_payload)
            return SimpleNamespace(rpc_request_id="rpc-steer-doc")

    client = SteeringClient()
    monkeypatch.setattr(persistent_codex, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        persistent_codex,
        "_load_locked_owned_runtime_records",
        lambda db, *, prepared, persistent: (run, session, turn, step),
    )
    monkeypatch.setattr(
        persistent_codex,
        "_load_active_steering_candidates",
        lambda *args, **kwargs: (event,),
    )

    sent = persistent_codex._attempt_active_steering_once(
        settings,
        prepared=prepared,
        persistent=persistent,
        client=client,
        thread_id="thread-1",
        native_turn_id="turn-1",
        deadline=time.monotonic() + 30,
    )

    assert sent == 1
    assert [receipt.status for receipt in db.receipts] == ["accepted"]
    assert [item["type"] for item in client.calls[0]] == ["text"]
    payload = json.loads(client.calls[0][0]["text"])
    assert payload["events"][0]["attachments"][0]["safe_input"]["stored_path"] == str(document_path)


def test_active_turn_accepted_steering_finalizes_without_successor_run(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")

    from worker import persistent_codex

    symbols, base_settings, prepared = _prepare_persistent_test_step(tmp_path)
    settings = Settings(
        **{
            **base_settings.__dict__,
            "codex_conversations_enabled": True,
            "codex_app_server_specialist_transport_enabled": True,
            "codex_active_turn_steering_enabled": True,
        }
    )
    persistent, run, session, turn, step, ticket = _steering_runtime(persistent_codex, prepared)
    run.input_hash = "initial-input-hash"
    run.pipeline_version = None
    run.final_step_id = None
    run.final_agent_spec_id = None
    run.final_output_contract = None
    run.final_output_json = None
    run.model_name = None
    run.ended_at = None
    run.error_text = None
    turn.transport_kind = "app_server"
    event = _steering_event()
    ticket.requeue_source_message_id = event.source_id
    ticket.reference = "T-STEER"
    ticket.title = "Steered finalization"
    ticket.urgent = False
    ticket.requester_language = None
    ticket.last_processed_hash = None
    ticket.last_ai_action = None
    ticket.clarification_rounds = 0
    db = _SteeringDb(run=run, session=session, turn=turn, step=step, ticket=ticket)
    observed: list[str] = []

    @contextmanager
    def fake_session_scope(_settings):
        yield db

    class SteeringClient:
        def steer_turn(self, *, thread_id, expected_turn_id, input_payload, timeout_seconds=None):
            return SimpleNamespace(rpc_request_id="rpc-steer-finalize")

    monkeypatch.setattr(persistent_codex, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        persistent_codex,
        "_load_locked_owned_runtime_records",
        lambda db, *, prepared, persistent: (run, session, turn, step),
    )
    monkeypatch.setattr(
        persistent_codex,
        "_load_active_steering_candidates",
        lambda *args, **kwargs: (event,),
    )

    sent = persistent_codex._attempt_active_steering_once(
        settings,
        prepared=prepared,
        persistent=persistent,
        client=SteeringClient(),
        thread_id="thread-1",
        native_turn_id="turn-1",
        deadline=time.monotonic() + 30,
    )

    assert sent == 1
    assert [receipt.status for receipt in db.receipts] == ["accepted"]
    assert [item.dedupe_key for item in db.inputs] == [event.dedupe_key]
    assert ticket.requeue_requested is False
    turn.status = "completed"
    turn.steering_closed_at = datetime.now(timezone.utc)

    context = _make_context(ticket=ticket, internal_body="Need this in the active turn.")
    monkeypatch.setattr("worker.triage.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.triage.load_ticket_context", lambda db, ticket_id: context)
    monkeypatch.setattr(
        "worker.triage._freeze_run_input",
        lambda db, settings, context, run: (turn.effective_input_hash, SimpleNamespace(conversation_id=persistent.conversation_id, input_hash=turn.effective_input_hash)),
    )
    monkeypatch.setattr(
        "worker.triage.build_prompt_conversation_state",
        lambda *args, **kwargs: SimpleNamespace(conversation_id=persistent.conversation_id, input_hash=turn.effective_input_hash),
    )
    monkeypatch.setattr("worker.triage.load_strictly_unseen_input_events", lambda *args, **kwargs: ())
    monkeypatch.setattr("worker.triage._mark_superseded_due_to_stale_input", lambda *args, **kwargs: pytest.fail("accepted steer created a successor run"))
    monkeypatch.setattr("worker.triage.publish_ai_internal_note", lambda *args, **kwargs: observed.append("internal"))
    monkeypatch.setattr(
        "worker.triage.publish_ai_public_reply",
        lambda *args, **kwargs: observed.append(f"public:{kwargs['last_ai_action']}"),
    )
    monkeypatch.setattr("worker.triage.create_ai_draft", lambda *args, **kwargs: observed.append("draft"))
    monkeypatch.setattr("worker.triage.route_ticket_after_ai", lambda *args, **kwargs: observed.append("route"))
    monkeypatch.setattr(
        "worker.triage.process_deferred_requeue",
        lambda db, ticket: observed.append("requeue-check") if not ticket.requeue_requested else pytest.fail("successor run requested"),
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

    assert observed == ["internal", "public:auto_public_reply", "requeue-check"]
    assert run.status == "succeeded"
    assert ticket.last_processed_hash == turn.effective_input_hash


def test_active_turn_accepted_requester_steering_finalizes_without_successor_run(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")

    from worker import persistent_codex

    symbols, base_settings, prepared = _prepare_persistent_test_step(tmp_path)
    settings = Settings(
        **{
            **base_settings.__dict__,
            "codex_conversations_enabled": True,
            "codex_app_server_specialist_transport_enabled": True,
            "codex_active_turn_steering_enabled": True,
        }
    )
    persistent, run, session, turn, step, ticket = _steering_runtime(persistent_codex, prepared)
    run.input_hash = "initial-input-hash"
    run.pipeline_version = None
    run.final_step_id = None
    run.final_agent_spec_id = None
    run.final_output_contract = None
    run.final_output_json = None
    run.model_name = None
    run.ended_at = None
    run.error_text = None
    turn.transport_kind = "app_server"
    requester_id = uuid.uuid4()
    event = _steering_event(
        "Please include this requester follow-up.",
        visibility="public",
        source="requester_reply",
        author_type="requester",
    )
    ticket.requeue_source_message_id = event.source_id
    ticket.requeue_requested_by_user_id = requester_id
    ticket.reference = "T-STEER-REQ"
    ticket.title = "Requester steering finalization"
    ticket.urgent = False
    ticket.requester_language = None
    ticket.last_processed_hash = None
    ticket.last_ai_action = None
    ticket.clarification_rounds = 0
    db = _SteeringDb(run=run, session=session, turn=turn, step=step, ticket=ticket)
    observed: list[str] = []

    @contextmanager
    def fake_session_scope(_settings):
        yield db

    class SteeringClient:
        def steer_turn(self, *, thread_id, expected_turn_id, input_payload, timeout_seconds=None):
            return SimpleNamespace(rpc_request_id="rpc-steer-requester")

    monkeypatch.setattr(persistent_codex, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        persistent_codex,
        "_load_locked_owned_runtime_records",
        lambda db, *, prepared, persistent: (run, session, turn, step),
    )
    monkeypatch.setattr(
        persistent_codex,
        "_load_active_steering_candidates",
        lambda *args, **kwargs: (event,),
    )

    sent = persistent_codex._attempt_active_steering_once(
        settings,
        prepared=prepared,
        persistent=persistent,
        client=SteeringClient(),
        thread_id="thread-1",
        native_turn_id="turn-1",
        deadline=time.monotonic() + 30,
    )

    assert sent == 1
    assert [receipt.status for receipt in db.receipts] == ["accepted"]
    assert [item.dedupe_key for item in db.inputs] == [event.dedupe_key]
    assert ticket.requeue_requested is False
    turn.status = "completed"
    turn.steering_closed_at = datetime.now(timezone.utc)

    context = _make_context(ticket=ticket, public_body="Please include this requester follow-up.")
    monkeypatch.setattr("worker.triage.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.triage.load_ticket_context", lambda db, ticket_id: context)
    monkeypatch.setattr(
        "worker.triage._freeze_run_input",
        lambda db, settings, context, run: (turn.effective_input_hash, SimpleNamespace(conversation_id=persistent.conversation_id, input_hash=turn.effective_input_hash)),
    )
    monkeypatch.setattr(
        "worker.triage.build_prompt_conversation_state",
        lambda *args, **kwargs: SimpleNamespace(conversation_id=persistent.conversation_id, input_hash=turn.effective_input_hash),
    )
    monkeypatch.setattr("worker.triage.load_strictly_unseen_input_events", lambda *args, **kwargs: ())
    monkeypatch.setattr("worker.triage._mark_superseded_due_to_stale_input", lambda *args, **kwargs: pytest.fail("accepted requester steer created a successor run"))
    monkeypatch.setattr("worker.triage.publish_ai_internal_note", lambda *args, **kwargs: observed.append("internal"))
    monkeypatch.setattr(
        "worker.triage.publish_ai_public_reply",
        lambda *args, **kwargs: observed.append(f"public:{kwargs['last_ai_action']}"),
    )
    monkeypatch.setattr("worker.triage.create_ai_draft", lambda *args, **kwargs: observed.append("draft"))
    monkeypatch.setattr("worker.triage.route_ticket_after_ai", lambda *args, **kwargs: observed.append("route"))
    monkeypatch.setattr(
        "worker.triage.process_deferred_requeue",
        lambda db, ticket: observed.append("requeue-check") if not ticket.requeue_requested else pytest.fail("successor run requested"),
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

    assert observed == ["internal", "public:auto_public_reply", "requeue-check"]
    assert run.status == "succeeded"
    assert ticket.last_processed_hash == turn.effective_input_hash


def test_active_turn_steering_sends_multiple_bundles_sequentially(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")

    from worker import persistent_codex

    symbols, base_settings, prepared = _prepare_persistent_test_step(tmp_path)
    settings = Settings(
        **{
            **base_settings.__dict__,
            "codex_app_server_specialist_transport_enabled": True,
            "codex_active_turn_steering_enabled": True,
        }
    )
    persistent, run, session, turn, step, ticket = _steering_runtime(persistent_codex, prepared)
    first = _steering_event("First")
    second = _steering_event("Second")
    db = _SteeringDb(run=run, session=session, turn=turn, step=step, ticket=ticket)

    @contextmanager
    def fake_session_scope(_settings):
        yield db

    class SteeringClient:
        def __init__(self):
            self.calls = []

        def steer_turn(self, *, thread_id, expected_turn_id, input_payload, timeout_seconds=None):
            self.calls.append(input_payload)
            return SimpleNamespace(rpc_request_id=f"rpc-{len(self.calls)}")

    monkeypatch.setattr(persistent_codex, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        persistent_codex,
        "_load_locked_owned_runtime_records",
        lambda db, *, prepared, persistent: (run, session, turn, step),
    )
    monkeypatch.setattr(
        persistent_codex,
        "_load_active_steering_candidates",
        lambda *args, **kwargs: (first, second),
    )

    sent = persistent_codex._attempt_active_steering_once(
        settings,
        prepared=prepared,
        persistent=persistent,
        client=SteeringClient(),
        thread_id="thread-1",
        native_turn_id="turn-1",
        deadline=time.monotonic() + 30,
    )

    assert sent == 2
    assert [receipt.status for receipt in db.receipts] == ["accepted", "accepted"]
    assert [item.input_index for item in db.inputs] == [1, 2]
    assert [item.dedupe_key for item in db.inputs] == [first.dedupe_key, second.dedupe_key]


def test_active_turn_steering_rejects_unsupported_bundle_without_consuming(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")

    from worker import persistent_codex

    symbols, base_settings, prepared = _prepare_persistent_test_step(tmp_path)
    settings = Settings(
        **{
            **base_settings.__dict__,
            "codex_app_server_specialist_transport_enabled": True,
            "codex_active_turn_steering_enabled": True,
        }
    )
    persistent, run, session, turn, step, ticket = _steering_runtime(persistent_codex, prepared)
    event = _steering_event(supported=False)
    db = _SteeringDb(run=run, session=session, turn=turn, step=step, ticket=ticket)

    @contextmanager
    def fake_session_scope(_settings):
        yield db

    monkeypatch.setattr(persistent_codex, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        persistent_codex,
        "_load_locked_owned_runtime_records",
        lambda db, *, prepared, persistent: (run, session, turn, step),
    )
    monkeypatch.setattr(
        persistent_codex,
        "_load_active_steering_candidates",
        lambda *args, **kwargs: (event,),
    )

    sent = persistent_codex._attempt_active_steering_once(
        settings,
        prepared=prepared,
        persistent=persistent,
        client=SimpleNamespace(steer_turn=lambda **kwargs: pytest.fail("unsupported bundle was sent")),
        thread_id="thread-1",
        native_turn_id="turn-1",
        deadline=time.monotonic() + 30,
    )

    assert sent == 0
    assert [receipt.status for receipt in db.receipts] == ["rejected"]
    assert db.receipts[0].error_code == "unsupported_bundle"
    assert db.inputs == []
    assert ticket.requeue_requested is True


def test_active_turn_steering_rejects_mixed_invalid_bundle_before_receipt(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")

    from worker import persistent_codex

    symbols, base_settings, prepared = _prepare_persistent_test_step(tmp_path)
    settings = Settings(
        **{
            **base_settings.__dict__,
            "codex_app_server_specialist_transport_enabled": True,
            "codex_active_turn_steering_enabled": True,
        }
    )
    ticket_dir = settings.uploads_dir / "ticket-steer-3"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    good_image = ticket_dir / "good.png"
    good_image.write_bytes(b"good-image")
    missing_document = ticket_dir / "missing.pdf"

    persistent, run, session, turn, step, ticket = _steering_runtime(persistent_codex, prepared)
    event = _steering_event(
        "Bundle should fail closed.",
        attachments=(
            _steering_attachment(good_image, is_image=True),
            _steering_attachment(missing_document, is_image=False, size_bytes=7, mime_type="application/pdf"),
        ),
        visibility="public",
        source="requester_reply",
    )
    db = _SteeringDb(run=run, session=session, turn=turn, step=step, ticket=ticket)

    @contextmanager
    def fake_session_scope(_settings):
        yield db

    monkeypatch.setattr(persistent_codex, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        persistent_codex,
        "_load_locked_owned_runtime_records",
        lambda db, *, prepared, persistent: (run, session, turn, step),
    )
    monkeypatch.setattr(
        persistent_codex,
        "_load_active_steering_candidates",
        lambda *args, **kwargs: (event,),
    )

    sent = persistent_codex._attempt_active_steering_once(
        settings,
        prepared=prepared,
        persistent=persistent,
        client=SimpleNamespace(steer_turn=lambda **kwargs: pytest.fail("invalid mixed bundle was sent")),
        thread_id="thread-1",
        native_turn_id="turn-1",
        deadline=time.monotonic() + 30,
    )

    assert sent == 0
    assert [receipt.status for receipt in db.receipts] == ["rejected"]
    assert db.receipts[0].error_code == "unsupported_bundle"
    assert db.inputs == []
    assert ticket.requeue_requested is True


def test_active_turn_steering_preserves_escrow_when_other_authorized_content_remains(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")

    from worker import persistent_codex

    symbols, base_settings, prepared = _prepare_persistent_test_step(tmp_path)
    settings = Settings(
        **{
            **base_settings.__dict__,
            "codex_app_server_specialist_transport_enabled": True,
            "codex_active_turn_steering_enabled": True,
        }
    )
    persistent, run, session, turn, step, ticket = _steering_runtime(persistent_codex, prepared)
    retained = _steering_event("Retained unsupported content", supported=False)
    accepted = _steering_event("Accepted later content")
    ticket.requeue_source_message_id = accepted.source_id
    db = _SteeringDb(run=run, session=session, turn=turn, step=step, ticket=ticket)

    @contextmanager
    def fake_session_scope(_settings):
        yield db

    class SteeringClient:
        def steer_turn(self, *, thread_id, expected_turn_id, input_payload, timeout_seconds=None):
            return SimpleNamespace(rpc_request_id="rpc-accepted")

    monkeypatch.setattr(persistent_codex, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        persistent_codex,
        "_load_locked_owned_runtime_records",
        lambda db, *, prepared, persistent: (run, session, turn, step),
    )
    monkeypatch.setattr(
        persistent_codex,
        "_load_active_steering_candidates",
        lambda *args, **kwargs: (retained, accepted),
    )
    monkeypatch.setattr(
        persistent_codex,
        "_has_unconsumed_authorized_ticket_content",
        lambda *args, **kwargs: True,
    )

    sent = persistent_codex._attempt_active_steering_once(
        settings,
        prepared=prepared,
        persistent=persistent,
        client=SteeringClient(),
        thread_id="thread-1",
        native_turn_id="turn-1",
        deadline=time.monotonic() + 30,
    )

    assert sent == 1
    assert [receipt.status for receipt in db.receipts] == ["rejected", "accepted"]
    assert [item.dedupe_key for item in db.inputs] == [accepted.dedupe_key]
    assert ticket.requeue_requested is True
    assert ticket.requeue_trigger == "ticket_content"
    assert ticket.requeue_source_message_id == accepted.source_id


def test_active_turn_steering_status_change_retains_content_without_receipt(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")

    from worker import persistent_codex

    symbols, base_settings, prepared = _prepare_persistent_test_step(tmp_path)
    settings = Settings(
        **{
            **base_settings.__dict__,
            "codex_app_server_specialist_transport_enabled": True,
            "codex_active_turn_steering_enabled": True,
        }
    )
    persistent, run, session, turn, step, ticket = _steering_runtime(
        persistent_codex,
        prepared,
        ticket_status="waiting_on_user",
    )
    event = _steering_event()
    db = _SteeringDb(run=run, session=session, turn=turn, step=step, ticket=ticket)

    @contextmanager
    def fake_session_scope(_settings):
        yield db

    monkeypatch.setattr(persistent_codex, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        persistent_codex,
        "_load_locked_owned_runtime_records",
        lambda db, *, prepared, persistent: (run, session, turn, step),
    )
    monkeypatch.setattr(
        persistent_codex,
        "_load_active_steering_candidates",
        lambda *args, **kwargs: (event,),
    )

    sent = persistent_codex._attempt_active_steering_once(
        settings,
        prepared=prepared,
        persistent=persistent,
        client=SimpleNamespace(steer_turn=lambda **kwargs: pytest.fail("waiting-state content was sent")),
        thread_id="thread-1",
        native_turn_id="turn-1",
        deadline=time.monotonic() + 30,
    )

    assert sent == 0
    assert db.receipts == []
    assert db.inputs == []
    assert ticket.requeue_requested is True


def test_active_turn_steering_no_active_turn_retains_content_without_receipt(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")

    from worker import persistent_codex

    symbols, base_settings, prepared = _prepare_persistent_test_step(tmp_path)
    settings = Settings(
        **{
            **base_settings.__dict__,
            "codex_app_server_specialist_transport_enabled": True,
            "codex_active_turn_steering_enabled": True,
        }
    )
    persistent, run, session, turn, step, ticket = _steering_runtime(persistent_codex, prepared)
    event = _steering_event()
    db = _SteeringDb(run=run, session=session, turn=turn, step=step, ticket=ticket)

    @contextmanager
    def fake_session_scope(_settings):
        yield db

    monkeypatch.setattr(persistent_codex, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        persistent_codex,
        "_load_locked_owned_runtime_records",
        lambda db, *, prepared, persistent: (run, session, None, step),
    )
    monkeypatch.setattr(
        persistent_codex,
        "_load_active_steering_candidates",
        lambda *args, **kwargs: (event,),
    )

    sent = persistent_codex._attempt_active_steering_once(
        settings,
        prepared=prepared,
        persistent=persistent,
        client=SimpleNamespace(steer_turn=lambda **kwargs: pytest.fail("missing active turn was sent")),
        thread_id="thread-1",
        native_turn_id="turn-1",
        deadline=time.monotonic() + 30,
    )

    assert sent == 0
    assert db.receipts == []
    assert db.inputs == []
    assert ticket.requeue_requested is True


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        (lambda turn: setattr(turn, "native_turn_id", "other-turn"), "expected_turn_mismatch"),
        (lambda turn: setattr(turn, "steering_closed_at", datetime.now(timezone.utc)), "steering_closed"),
        (lambda turn: setattr(turn, "status", "completed"), "steering_closed"),
    ],
)
def test_active_turn_steering_revalidation_retains_content_without_publication(
    monkeypatch,
    tmp_path,
    mutation,
    expected_error,
):
    pytest.importorskip("sqlalchemy")

    from worker import persistent_codex

    symbols, base_settings, prepared = _prepare_persistent_test_step(tmp_path)
    settings = Settings(
        **{
            **base_settings.__dict__,
            "codex_app_server_specialist_transport_enabled": True,
            "codex_active_turn_steering_enabled": True,
        }
    )
    persistent, run, session, turn, step, ticket = _steering_runtime(persistent_codex, prepared)
    mutation(turn)
    event = _steering_event()
    db = _SteeringDb(run=run, session=session, turn=turn, step=step, ticket=ticket)

    @contextmanager
    def fake_session_scope(_settings):
        yield db

    monkeypatch.setattr(persistent_codex, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        persistent_codex,
        "_load_locked_owned_runtime_records",
        lambda db, *, prepared, persistent: (run, session, turn, step),
    )
    monkeypatch.setattr(
        persistent_codex,
        "_load_active_steering_candidates",
        lambda *args, **kwargs: (event,),
    )

    sent = persistent_codex._attempt_active_steering_once(
        settings,
        prepared=prepared,
        persistent=persistent,
        client=SimpleNamespace(steer_turn=lambda **kwargs: pytest.fail("invalid active turn was sent")),
        thread_id="thread-1",
        native_turn_id="turn-1",
        deadline=time.monotonic() + 30,
    )

    assert sent == 0
    assert db.receipts == []
    assert db.inputs == []
    assert ticket.requeue_requested is True
    assert expected_error in {"expected_turn_mismatch", "steering_closed"}


def test_active_turn_steering_ambiguous_send_keeps_content_unconsumed(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")

    from worker import persistent_codex
    from worker.codex_app_server import CodexAppServerAmbiguousError

    symbols, base_settings, prepared = _prepare_persistent_test_step(tmp_path)
    settings = Settings(
        **{
            **base_settings.__dict__,
            "codex_app_server_specialist_transport_enabled": True,
            "codex_active_turn_steering_enabled": True,
        }
    )
    persistent, run, session, turn, step, ticket = _steering_runtime(persistent_codex, prepared)
    event = _steering_event()
    db = _SteeringDb(run=run, session=session, turn=turn, step=step, ticket=ticket)

    @contextmanager
    def fake_session_scope(_settings):
        yield db

    class AmbiguousClient:
        def steer_turn(self, **kwargs):
            raise CodexAppServerAmbiguousError("process exited after possible send", error_code="process_exited")

    monkeypatch.setattr(persistent_codex, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        persistent_codex,
        "_load_locked_owned_runtime_records",
        lambda db, *, prepared, persistent: (run, session, turn, step),
    )
    monkeypatch.setattr(
        persistent_codex,
        "_load_active_steering_candidates",
        lambda *args, **kwargs: (event,),
    )

    with pytest.raises(CodexAppServerAmbiguousError):
        persistent_codex._attempt_active_steering_once(
            settings,
            prepared=prepared,
            persistent=persistent,
            client=AmbiguousClient(),
            thread_id="thread-1",
            native_turn_id="turn-1",
            deadline=time.monotonic() + 30,
        )

    assert [receipt.status for receipt in db.receipts] == ["ambiguous"]
    assert db.receipts[0].error_code == "process_exited"
    assert db.receipts[0].acknowledged_at is None
    assert db.inputs == []
    assert ticket.requeue_requested is True


def test_active_turn_steering_lease_loss_before_ack_commit_is_ambiguous(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")

    from worker import persistent_codex
    from worker.run_ownership import RunOwnershipLost

    symbols, base_settings, prepared = _prepare_persistent_test_step(tmp_path)
    settings = Settings(
        **{
            **base_settings.__dict__,
            "codex_app_server_specialist_transport_enabled": True,
            "codex_active_turn_steering_enabled": True,
        }
    )
    persistent, run, session, turn, step, ticket = _steering_runtime(persistent_codex, prepared)
    event = _steering_event()
    db = _SteeringDb(run=run, session=session, turn=turn, step=step, ticket=ticket)

    @contextmanager
    def fake_session_scope(_settings):
        yield db

    class AckClient:
        def steer_turn(self, **kwargs):
            return SimpleNamespace(rpc_request_id="rpc-before-lease-loss")

    monkeypatch.setattr(persistent_codex, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        persistent_codex,
        "_load_locked_owned_runtime_records",
        lambda db, *, prepared, persistent: (run, session, turn, step),
    )
    monkeypatch.setattr(
        persistent_codex,
        "_load_active_steering_candidates",
        lambda *args, **kwargs: (event,),
    )
    monkeypatch.setattr(
        persistent_codex,
        "_accept_steering_receipt",
        lambda *args, **kwargs: (_ for _ in ()).throw(RunOwnershipLost("lease lost")),
    )

    with pytest.raises(RunOwnershipLost):
        persistent_codex._attempt_active_steering_once(
            settings,
            prepared=prepared,
            persistent=persistent,
            client=AckClient(),
            thread_id="thread-1",
            native_turn_id="turn-1",
            deadline=time.monotonic() + 30,
        )

    assert [receipt.status for receipt in db.receipts] == ["ambiguous"]
    assert db.receipts[0].error_code == "lease_lost_before_ack_commit"
    assert db.inputs == []
    assert ticket.requeue_requested is True


def test_active_turn_steering_completion_marks_unresolved_receipts_ambiguous(tmp_path):
    pytest.importorskip("sqlalchemy")

    from shared.models import CodexTurnSteer
    from worker import persistent_codex

    symbols, _settings, prepared = _prepare_persistent_test_step(tmp_path)
    persistent, run, session, turn, step, ticket = _steering_runtime(persistent_codex, prepared)
    db = _SteeringDb(run=run, session=session, turn=turn, step=step, ticket=ticket)
    receipt = CodexTurnSteer(
        id=uuid.uuid4(),
        turn_id=turn.id,
        event_kind="ticket_message",
        source_kind="ticket_message",
        source_id=uuid.uuid4(),
        dedupe_key="ticket-message:ambiguous",
        expected_native_turn_id="turn-1",
        payload_json={"input": []},
        payload_hash="hash",
        status="sending",
    )
    db.receipts.append(receipt)

    @contextmanager
    def fake_session_scope(_settings):
        yield db

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(persistent_codex, "session_scope", fake_session_scope)
    try:
        count = persistent_codex._mark_unresolved_steering_receipts_ambiguous(
            _settings,
            turn_id=turn.id,
            reason="turn_completed_with_unresolved_steer",
        )
    finally:
        monkeypatch.undo()

    assert count == 1
    assert receipt.status == "ambiguous"
    assert receipt.error_code == "turn_completed_with_unresolved_steer"
    assert db.inputs == []


def test_persist_accepted_initial_inputs_is_idempotent_and_hash_bound(tmp_path):
    pytest.importorskip("sqlalchemy")

    from worker import persistent_codex
    from worker.codex_inputs import hash_input_events

    turn = SimpleNamespace(id=uuid.uuid4(), effective_input_hash=None)
    events = (
        SimpleNamespace(
            event_kind="ticket_state_snapshot",
            source_kind="ticket",
            source_id=uuid.uuid4(),
            dedupe_key="ticket-state:1",
            payload_json={"status": "ai_triage"},
        ),
        SimpleNamespace(
            event_kind="ticket_message",
            source_kind="ticket_message",
            source_id=uuid.uuid4(),
            dedupe_key="ticket-message:1",
            payload_json={"body_text": "Please continue."},
        ),
    )
    effective_input_hash = hash_input_events(events)

    class Db:
        def __init__(self):
            self.inputs = []

        def add(self, item):
            self.inputs.append(item)

        def execute(self, statement):
            descriptions = statement.column_descriptions
            if descriptions[0]["name"] == "dedupe_key":
                return _FakeWorkerStateResult([(item.dedupe_key,) for item in self.inputs])
            raise AssertionError(f"unexpected execute call: {descriptions}")

    db = Db()
    persistent_codex._persist_accepted_initial_inputs(
        db,
        turn=turn,
        events=events,
        effective_input_hash=effective_input_hash,
    )

    assert [item.input_index for item in db.inputs] == [1, 2]
    assert [item.dedupe_key for item in db.inputs] == ["ticket-state:1", "ticket-message:1"]
    assert turn.effective_input_hash == effective_input_hash

    persistent_codex._persist_accepted_initial_inputs(
        db,
        turn=turn,
        events=events,
        effective_input_hash=effective_input_hash,
    )

    assert len(db.inputs) == 2
    with pytest.raises(persistent_codex.StepRunError, match="different effective input hash"):
        persistent_codex._persist_accepted_initial_inputs(
            db,
            turn=turn,
            events=events,
            effective_input_hash="different-input-hash",
        )


def test_app_server_initial_effective_hash_uses_full_snapshot_not_resumed_turn_delta():
    from worker import persistent_codex
    from worker.codex_inputs import hash_input_events

    pending_event = SimpleNamespace(
        event_kind="ticket_message",
        source_kind="ticket_message",
        source_id=uuid.uuid4(),
        dedupe_key="ticket-message:new",
        payload_json={"body_text": "Only the resumed-turn delta."},
    )
    full_snapshot_hash = "full-current-ticket-snapshot-hash"
    prompt_state = SimpleNamespace(
        input_hash=full_snapshot_hash,
        pending_events=(pending_event,),
    )

    assert hash_input_events(prompt_state.pending_events) != full_snapshot_hash
    assert persistent_codex._initial_effective_input_hash(prompt_state) == full_snapshot_hash


def test_app_server_steering_refreshes_effective_hash_from_full_ticket_snapshot(monkeypatch):
    from sqlalchemy.orm import Session as SASession

    from worker import persistent_codex

    db = SASession()
    run = SimpleNamespace(id=uuid.uuid4())
    turn = SimpleNamespace(id=uuid.uuid4())
    ticket_id = uuid.uuid4()
    context = SimpleNamespace(ticket=SimpleNamespace(id=ticket_id))
    observed = {}

    monkeypatch.setattr(persistent_codex, "load_ticket_context", lambda actual_db, actual_ticket_id: context)

    def fake_build_prompt_state(actual_db, *, context, run, feature_enabled):
        observed.update(
            db=actual_db,
            context=context,
            run=run,
            feature_enabled=feature_enabled,
        )
        return SimpleNamespace(input_hash="full-snapshot-after-steer")

    monkeypatch.setattr(persistent_codex, "build_prompt_conversation_state", fake_build_prompt_state)
    settings = SimpleNamespace(codex_conversations_enabled=True)
    try:
        result = persistent_codex._current_effective_input_hash(
            db,
            settings=settings,
            run=run,
            turn=turn,
            ticket_id=ticket_id,
        )
    finally:
        db.close()

    assert result == "full-snapshot-after-steer"
    assert observed == {
        "db": db,
        "context": context,
        "run": run,
        "feature_enabled": True,
    }


def test_app_server_accepted_steering_advances_frontier_hash_only_when_frontier_is_complete(monkeypatch):
    from sqlalchemy.orm import Session as SASession

    from worker import persistent_codex

    db = SASession()
    run = SimpleNamespace(id=uuid.uuid4())
    turn = SimpleNamespace(id=uuid.uuid4(), effective_input_hash="accepted-frontier-hash")
    ticket_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    context = SimpleNamespace(ticket=SimpleNamespace(id=ticket_id))

    monkeypatch.setattr(persistent_codex, "load_ticket_context", lambda actual_db, actual_ticket_id: context)
    monkeypatch.setattr(persistent_codex, "load_strictly_unseen_input_events", lambda *args, **kwargs: ())
    monkeypatch.setattr(
        persistent_codex,
        "build_prompt_conversation_state",
        lambda *args, **kwargs: SimpleNamespace(input_hash="frontier-after-steer"),
    )
    settings = SimpleNamespace(codex_conversations_enabled=True, max_image_bytes=1_000_000)
    try:
        advanced = persistent_codex._advance_effective_input_hash_if_frontier_complete(
            db,
            settings=settings,
            run=run,
            turn=turn,
            conversation_id=conversation_id,
            ticket_id=ticket_id,
        )
    finally:
        db.close()

    assert advanced is True
    assert turn.effective_input_hash == "frontier-after-steer"


def test_app_server_accepted_steering_preserves_prior_frontier_hash_when_relevant_unseen_state_remains(monkeypatch):
    from sqlalchemy.orm import Session as SASession

    from worker import persistent_codex

    db = SASession()
    run = SimpleNamespace(id=uuid.uuid4())
    turn = SimpleNamespace(id=uuid.uuid4(), effective_input_hash="accepted-frontier-hash")
    ticket_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    context = SimpleNamespace(ticket=SimpleNamespace(id=ticket_id))

    monkeypatch.setattr(persistent_codex, "load_ticket_context", lambda actual_db, actual_ticket_id: context)
    monkeypatch.setattr(
        persistent_codex,
        "load_strictly_unseen_input_events",
        lambda *args, **kwargs: (SimpleNamespace(source_kind="ticket_status_history", dedupe_key="ticket-status:late"),),
    )
    monkeypatch.setattr(
        persistent_codex,
        "build_prompt_conversation_state",
        lambda *args, **kwargs: pytest.fail("frontier hash should not advance while relevant unseen state remains"),
    )
    settings = SimpleNamespace(codex_conversations_enabled=True, max_image_bytes=1_000_000)
    try:
        advanced = persistent_codex._advance_effective_input_hash_if_frontier_complete(
            db,
            settings=settings,
            run=run,
            turn=turn,
            conversation_id=conversation_id,
            ticket_id=ticket_id,
        )
    finally:
        db.close()

    assert advanced is False
    assert turn.effective_input_hash == "accepted-frontier-hash"


def test_active_steering_change_token_loader_reads_only_polling_signal(monkeypatch, tmp_path):
    from worker import persistent_codex

    settings = _make_settings(tmp_path)
    ticket_id = uuid.uuid4()
    source_message_id = uuid.uuid4()
    updated_at = datetime.now(timezone.utc)
    executed = []

    class Result:
        def one_or_none(self):
            return (updated_at, "ai_triage", True, "ticket_content", source_message_id)

    class Db:
        def execute(self, statement):
            executed.append(statement)
            return Result()

    @contextmanager
    def fake_session_scope(_settings):
        yield Db()

    monkeypatch.setattr(persistent_codex, "session_scope", fake_session_scope)

    token = persistent_codex._load_active_steering_change_token(settings, ticket_id=ticket_id)

    assert token == persistent_codex.ActiveSteeringChangeToken(
        updated_at=updated_at,
        status="ai_triage",
        requeue_requested=True,
        requeue_trigger="ticket_content",
        requeue_source_message_id=source_message_id,
    )
    assert len(executed) == 1
    assert [description["name"] for description in executed[0].column_descriptions] == [
        "updated_at",
        "status",
        "requeue_requested",
        "requeue_trigger",
        "requeue_source_message_id",
    ]


def test_active_steering_poll_scans_first_change_and_concurrent_change_but_skips_unchanged(monkeypatch):
    from worker import persistent_codex

    first_token = persistent_codex.ActiveSteeringChangeToken(
        updated_at=datetime(2026, 8, 28, 10, tzinfo=timezone.utc),
        status="ai_triage",
        requeue_requested=False,
        requeue_trigger=None,
        requeue_source_message_id=None,
    )
    changed_token = replace(
        first_token,
        updated_at=datetime(2026, 8, 28, 10, 0, 1, tzinfo=timezone.utc),
        requeue_requested=True,
        requeue_trigger="ticket_content",
        requeue_source_message_id=uuid.uuid4(),
    )
    current_token = {"value": first_token}
    scan_tokens = []

    monkeypatch.setattr(
        persistent_codex,
        "_load_active_steering_change_token",
        lambda *args, **kwargs: current_token["value"],
    )

    def fake_attempt(*args, **kwargs):
        scan_tokens.append(current_token["value"])
        if len(scan_tokens) == 1:
            # Simulate content committing while the expensive scan is active.
            current_token["value"] = changed_token
        return 1

    monkeypatch.setattr(persistent_codex, "_attempt_active_steering_once", fake_attempt)
    state = persistent_codex.ActiveSteeringPollState()
    common = {
        "settings": SimpleNamespace(),
        "prepared": SimpleNamespace(ticket_id=uuid.uuid4()),
        "persistent": SimpleNamespace(),
        "client": SimpleNamespace(),
        "thread_id": "thread-1",
        "native_turn_id": "turn-1",
        "deadline": time.monotonic() + 60,
        "state": state,
    }

    assert persistent_codex._poll_active_steering_if_changed(**common) == 1
    assert state.change_token == first_token
    assert persistent_codex._poll_active_steering_if_changed(**common) == 1
    assert state.change_token == changed_token
    assert persistent_codex._poll_active_steering_if_changed(**common) == 0
    assert scan_tokens == [first_token, changed_token]


def test_app_server_persistent_specialist_resumes_stored_thread(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")

    from worker import persistent_codex

    symbols, base_settings, prepared = _prepare_persistent_test_step(tmp_path)
    settings = Settings(
        **{
            **base_settings.__dict__,
            "codex_app_server_specialist_transport_enabled": True,
        }
    )
    persistent = _persistent_step_for_test(persistent_codex, tmp_path, resumed=True)
    persistent = replace(
        persistent,
        transport_kind="app_server",
        stored_thread_id="thread-existing",
        command_spec=replace(persistent.command_spec, command=["codex", "app-server", "--stdio"]),
    )
    run = SimpleNamespace(id=prepared.run_id, last_heartbeat_at=None)
    session = SimpleNamespace(
        id=persistent.session_id,
        lease_owner_run_id=prepared.run_id,
        lease_worker_instance_id=prepared.worker_instance_id,
        thread_id="thread-existing",
        status="active",
        started_at=datetime.now(timezone.utc),
        lease_heartbeat_at=None,
        lease_expires_at=None,
    )
    turn = SimpleNamespace(
        id=persistent.turn_id,
        accepted_at=None,
        native_turn_id=None,
        effective_input_hash=None,
        transport_kind="app_server",
        steering_closed_at=None,
        status="running",
        ended_at=None,
    )
    step = SimpleNamespace(id=persistent.step_id, ended_at=None, status="running", output_json=None, error_text=None)
    resumed_with = []

    @contextmanager
    def fake_session_scope(_settings):
        class Db:
            def add(self, _item):
                return None

            def execute(self, statement):
                descriptions = statement.column_descriptions
                first_name = descriptions[0]["name"]
                entity = descriptions[0].get("entity")
                entity_name = getattr(entity, "__name__", "")
                if first_name == "dedupe_key":
                    return _FakeWorkerStateResult([])
                if first_name == "event_kind":
                    return _FakeWorkerStateResult([])
                if first_name == "count" or "count" in first_name:
                    return _FakePersistentScalarResult(0)
                if entity_name == "Ticket":
                    return _FakeWorkerStateResult([SimpleNamespace(id=prepared.ticket_id, status="ai_triage")])
                if entity_name == "CodexTurnSteer":
                    return _FakeWorkerStateResult([])
                return _FakeWorkerStateResult([])

            def get(self, model, key):
                return SimpleNamespace(id=persistent.conversation_id, status="active")

        yield Db()

    class ResumeClient:
        stderr_text = ""
        process = SimpleNamespace(poll=lambda: 0)

        def __init__(self, *args, on_thread_id=None, on_turn_id=None, on_protocol_item=None, **kwargs):
            self.on_thread_id = on_thread_id
            self.on_turn_id = on_turn_id
            self.on_protocol_item = on_protocol_item

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def initialize(self, *, timeout_seconds=None):
            return {}

        def start_or_resume_thread(self, *, stored_thread_id, prepared, timeout_seconds=None):
            resumed_with.append(stored_thread_id)
            self.on_thread_id(stored_thread_id)
            return SimpleNamespace(thread_id=stored_thread_id, resumed=True)

        def start_turn(self, *, thread_id, input_payload, prepared, timeout_seconds=None):
            self.on_turn_id("native-turn-resume")
            return SimpleNamespace(thread_id=thread_id, turn_id="native-turn-resume", response={})

        def supervise_until_completed(self, *, thread_id, turn_id, deadline, on_poll=None, poll_interval_seconds=0.05):
            return {
                "method": "turn/completed",
                "params": {
                    "threadId": thread_id,
                    "turn": {
                        "id": turn_id,
                        "status": "completed",
                        "items": [
                            {
                                "type": "agentMessage",
                                "id": "agent-final",
                                "phase": "final_answer",
                                "text": json.dumps(_specialist_payload(summary_internal="Resumed.")),
                            }
                        ],
                    },
                },
            }

    monkeypatch.setattr(persistent_codex, "session_scope", fake_session_scope)
    monkeypatch.setattr("worker.codex_app_server.session_scope", fake_session_scope)
    monkeypatch.setattr(
        persistent_codex,
        "_load_locked_owned_runtime_records",
        lambda db, *, prepared, persistent: (run, session, turn, step),
    )
    monkeypatch.setattr(persistent_codex, "_append_turn_outcome", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        persistent_codex,
        "prepare_persistent_specialist_step",
        lambda settings, prepared, prompt_state: persistent,
    )
    monkeypatch.setattr(persistent_codex, "CodexAppServerClient", ResumeClient)

    result = persistent_codex.execute_persistent_specialist_step(
        settings,
        prepared=prepared,
        prompt_state=SimpleNamespace(),
    )

    assert resumed_with == ["thread-existing"]
    assert turn.native_turn_id == "native-turn-resume"
    assert result.output_payload["summary_internal"] == "Resumed."


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
    persistent = _persistent_step_for_test(persistent_codex, tmp_path)
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
    persistent = _persistent_step_for_test(persistent_codex, tmp_path)
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


def test_app_server_rollout_keeps_router_and_selector_on_execute_step(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = Settings(
        **{
            **_make_settings(tmp_path).__dict__,
            "codex_conversations_enabled": True,
            "codex_app_server_specialist_transport_enabled": True,
            "codex_active_turn_steering_enabled": False,
        }
    )
    context = _make_context()
    route_target = _build_route_target(
        route_target_id="manual_review",
        kind="human_assist",
        mode="auto",
        candidate_specialist_ids=("bug", "feature"),
        human_queue_status="waiting_on_dev_ti",
    )
    registry = _build_registry(route_target)
    execute_step_kinds = []
    persistent_step_kinds = []

    def fake_prepare_step_run(*args, **kwargs):
        return SimpleNamespace(
            run_id=kwargs["run_id"],
            ticket_id=kwargs["ticket_id"],
            step_index=kwargs["step_index"],
            step_kind=kwargs["step_kind"],
            spec=kwargs["spec"],
            model_name=None,
            candidate_specialist_ids=kwargs.get("candidate_specialist_ids"),
            route_target_id=kwargs.get("target_route_target_id"),
            selected_specialist_id=kwargs.get("selected_specialist_id"),
            requester_role=context.requester_role,
            paths=SimpleNamespace(run_dir=tmp_path / "run", as_payload=lambda: {}),
        )

    def fake_execute_step(_settings, *, prepared):
        execute_step_kinds.append(prepared.step_kind)
        payload = (
            _route_payload(route_target_id="manual_review")
            if prepared.step_kind == "router"
            else _selector_payload(specialist_id="feature")
        )
        return SimpleNamespace(step_id=uuid.uuid4(), prepared=prepared, output_payload=payload)

    def fake_execute_persistent_specialist_step(_settings, *, prepared, prompt_state):
        persistent_step_kinds.append(prepared.step_kind)
        return SimpleNamespace(
            step_id=uuid.uuid4(),
            prepared=prepared,
            output_payload=_specialist_payload(summary_internal="Specialist used persistent transport."),
        )

    monkeypatch.setattr("worker.pipeline.load_routing_registry", lambda: registry)
    monkeypatch.setattr("worker.pipeline.prepare_step_run", fake_prepare_step_run)
    monkeypatch.setattr("worker.pipeline.execute_step", fake_execute_step)
    monkeypatch.setattr(
        "worker.pipeline.execute_persistent_specialist_step",
        fake_execute_persistent_specialist_step,
    )
    monkeypatch.setattr("worker.pipeline.write_run_manifest_snapshot", lambda settings, run_id: None)

    result = symbols["execute_triage_pipeline"](
        settings,
        run_id=uuid.uuid4(),
        ticket_id=context.ticket.id,
        worker_instance_id="worker-test",
        context=context,
        prompt_state=SimpleNamespace(prompt_context=context, prompt_appendix=""),
    )

    assert execute_step_kinds == ["router", "selector"]
    assert persistent_step_kinds == ["specialist"]
    assert result.selector_result.specialist_id == "feature"
    assert result.specialist_result.summary_internal == "Specialist used persistent transport."


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


def test_apply_success_result_app_server_uses_effective_input_hash_after_accepted_steering(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    base_settings = _make_settings(tmp_path)
    settings = Settings(
        **{
            **base_settings.__dict__,
            "codex_conversations_enabled": True,
            "codex_app_server_specialist_transport_enabled": True,
            "codex_active_turn_steering_enabled": True,
        }
    )
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        reference="T-000008A",
        title="Steered content",
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
    run = SimpleNamespace(
        id=uuid.uuid4(),
        ticket_id=ticket.id,
        status="running",
        input_hash="initial-input-hash",
        ended_at=None,
        error_text=None,
        pipeline_version=None,
        final_step_id=None,
        final_agent_spec_id=None,
        final_output_contract=None,
        final_output_json=None,
        model_name=None,
    )
    turn = SimpleNamespace(
        id=uuid.uuid4(),
        ai_run_id=run.id,
        conversation_id=uuid.uuid4(),
        transport_kind="app_server",
        status="completed",
        steering_closed_at=datetime.now(timezone.utc),
        effective_input_hash="effective-after-steer",
    )

    class Db(_FakeDb):
        def execute(self, statement):
            entity = statement.column_descriptions[0].get("entity") if getattr(statement, "column_descriptions", None) else None
            if getattr(entity, "__name__", "") == "CodexTurn":
                return _FakeWorkerStateResult([turn])
            if getattr(entity, "__name__", "") == "CodexTurnSteer":
                return _FakeWorkerStateResult([])
            return super().execute(statement)

    fake_db = Db(run=run)
    events: list[str] = []

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.triage.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.triage.load_ticket_context", lambda db, ticket_id: context)
    monkeypatch.setattr(
        "worker.triage._freeze_run_input",
        lambda db, settings, context, run: ("effective-after-steer", SimpleNamespace(conversation_id=uuid.uuid4(), input_hash="effective-after-steer")),
    )
    monkeypatch.setattr(
        "worker.triage.build_prompt_conversation_state",
        lambda *args, **kwargs: SimpleNamespace(conversation_id=uuid.uuid4(), input_hash="effective-after-steer"),
    )
    monkeypatch.setattr("worker.triage.load_strictly_unseen_input_events", lambda *args, **kwargs: ())
    monkeypatch.setattr("worker.triage._mark_superseded_due_to_stale_input", lambda *args, **kwargs: pytest.fail("accepted steering was treated as stale"))
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
    assert run.status == "succeeded"
    assert ticket.last_processed_hash == "effective-after-steer"


def test_apply_success_result_uses_durable_app_server_frontier_even_if_transport_flag_is_now_disabled(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    base_settings = _make_settings(tmp_path)
    settings = Settings(
        **{
            **base_settings.__dict__,
            "codex_conversations_enabled": True,
            "codex_app_server_specialist_transport_enabled": False,
            "codex_active_turn_steering_enabled": False,
        }
    )
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        reference="T-000008AA",
        title="Durable app-server frontier",
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
    run = SimpleNamespace(
        id=uuid.uuid4(),
        ticket_id=ticket.id,
        status="running",
        input_hash="stale-original-hash",
        ended_at=None,
        error_text=None,
        pipeline_version=None,
        final_step_id=None,
        final_agent_spec_id=None,
        final_output_contract=None,
        final_output_json=None,
        model_name=None,
    )
    turn = SimpleNamespace(
        id=uuid.uuid4(),
        ai_run_id=run.id,
        conversation_id=uuid.uuid4(),
        transport_kind="app_server",
        status="completed",
        steering_closed_at=datetime.now(timezone.utc),
        effective_input_hash="accepted-frontier-hash",
    )

    class Db(_FakeDb):
        def execute(self, statement):
            entity = statement.column_descriptions[0].get("entity") if getattr(statement, "column_descriptions", None) else None
            if getattr(entity, "__name__", "") == "CodexTurn":
                return _FakeWorkerStateResult([turn])
            if getattr(entity, "__name__", "") == "CodexTurnSteer":
                return _FakeWorkerStateResult([])
            return super().execute(statement)

    fake_db = Db(run=run)
    events: list[str] = []

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.triage.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.triage.load_ticket_context", lambda db, ticket_id: context)
    monkeypatch.setattr(
        "worker.triage._freeze_run_input",
        lambda db, settings, context, run: ("accepted-frontier-hash", SimpleNamespace(conversation_id=uuid.uuid4(), input_hash="accepted-frontier-hash")),
    )
    monkeypatch.setattr(
        "worker.triage.build_prompt_conversation_state",
        lambda *args, **kwargs: SimpleNamespace(conversation_id=uuid.uuid4(), input_hash="accepted-frontier-hash"),
    )
    monkeypatch.setattr("worker.triage.load_strictly_unseen_input_events", lambda *args, **kwargs: ())
    monkeypatch.setattr("worker.triage._mark_superseded_due_to_stale_input", lambda *args, **kwargs: pytest.fail("durable app-server frontier was ignored"))
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
    assert run.status == "succeeded"
    assert ticket.last_processed_hash == "accepted-frontier-hash"


def test_apply_success_result_stale_without_authorized_requeue_does_not_synthesize_requester_reply(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        reference="T-000008B",
        title="Dormant stale input",
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
    run = SimpleNamespace(
        id=uuid.uuid4(),
        ticket_id=ticket.id,
        status="running",
        input_hash="old-hash",
        ended_at=None,
        error_text=None,
    )
    fake_db = _FakeDb(run=run)
    observed = {"requeue": 0}

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.triage.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.triage.load_ticket_context", lambda db, ticket_id: context)
    monkeypatch.setattr("worker.triage.process_deferred_requeue", lambda *args, **kwargs: observed.__setitem__("requeue", observed["requeue"] + 1))
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
    assert observed["requeue"] == 0
    assert ticket.requeue_requested is False
    assert ticket.requeue_trigger is None


def test_apply_success_result_app_server_blocks_publication_when_completion_fence_incomplete(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    base_settings = _make_settings(tmp_path)
    settings = Settings(
        **{
            **base_settings.__dict__,
            "codex_conversations_enabled": True,
            "codex_app_server_specialist_transport_enabled": True,
            "codex_active_turn_steering_enabled": True,
        }
    )
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        reference="T-000008C",
        title="Early output",
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
    run = SimpleNamespace(
        id=uuid.uuid4(),
        ticket_id=ticket.id,
        status="running",
        input_hash="effective-input",
        ended_at=None,
        error_text=None,
    )
    turn = SimpleNamespace(
        id=uuid.uuid4(),
        ai_run_id=run.id,
        conversation_id=uuid.uuid4(),
        transport_kind="app_server",
        status="running",
        steering_closed_at=None,
        effective_input_hash="effective-input",
    )

    class Db(_FakeDb):
        def __init__(self, *, run):
            super().__init__(run=run)
            self.added = []

        def add(self, item):
            self.added.append(item)

        def execute(self, statement):
            descriptions = statement.column_descriptions if getattr(statement, "column_descriptions", None) else []
            first_name = descriptions[0].get("name") if descriptions else None
            entity = descriptions[0].get("entity") if descriptions else None
            entity_name = getattr(entity, "__name__", "")
            if first_name == "coalesce":
                return _FakePersistentScalarResult(0)
            if entity_name == "CodexTurn":
                return _FakeWorkerStateResult([turn])
            if entity_name == "CodexTurnSteer":
                return _FakeWorkerStateResult([])
            return super().execute(statement)

    fake_db = Db(run=run)
    observed = {"published": 0, "draft": 0, "route": 0, "requeue": 0}

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.triage.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.triage.load_ticket_context", lambda db, ticket_id: context)
    monkeypatch.setattr("worker.triage._freeze_run_input", lambda *args, **kwargs: ("effective-input", SimpleNamespace(input_hash="effective-input")))
    monkeypatch.setattr("worker.triage.publish_ai_internal_note", lambda *args, **kwargs: pytest.fail("should not publish internal note"))
    monkeypatch.setattr("worker.triage.publish_ai_public_reply", lambda *args, **kwargs: observed.__setitem__("published", observed["published"] + 1))
    monkeypatch.setattr("worker.triage.create_ai_draft", lambda *args, **kwargs: observed.__setitem__("draft", observed["draft"] + 1))
    monkeypatch.setattr("worker.triage.route_ticket_after_ai", lambda *args, **kwargs: observed.__setitem__("route", observed["route"] + 1))
    monkeypatch.setattr("worker.triage.process_deferred_requeue", lambda *args, **kwargs: observed.__setitem__("requeue", observed["requeue"] + 1))
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
    assert observed == {"published": 0, "draft": 0, "route": 0, "requeue": 0}
    outcomes = [item for item in fake_db.added if item.__class__.__name__ == "CodexTurnOutcome"]
    assert outcomes[0].payload_json["reason"] == "completion_fence_incomplete"


def test_apply_success_result_app_server_blocks_receipts_unseen_content_and_status_override(monkeypatch, tmp_path):
    from shared.models import CodexTurnSteer

    symbols = _load_worker_symbols()
    base_settings = _make_settings(tmp_path)
    settings = Settings(
        **{
            **base_settings.__dict__,
            "codex_conversations_enabled": True,
            "codex_app_server_specialist_transport_enabled": True,
            "codex_active_turn_steering_enabled": True,
        }
    )
    cases = [
        ("blocking_steering_receipts", "ai_triage", True, None),
        ("workflow_status_changed", "waiting_on_user", False, None),
        ("unseen_authorized_content", "ai_triage", False, "ticket_message"),
        ("unseen_authorized_content", "ai_triage", False, "ticket_status_history"),
    ]
    for expected_reason, ticket_status, has_receipt, unseen_source_kind in cases:
        ticket = SimpleNamespace(
            id=uuid.uuid4(),
            reference="T-000008D",
            title=expected_reason,
            status=ticket_status,
            urgent=False,
            requester_language=None,
            last_processed_hash=None,
            last_ai_action=None,
            clarification_rounds=0,
            requeue_requested=False,
            requeue_trigger=None,
        )
        context = _make_context(ticket=ticket)
        run = SimpleNamespace(id=uuid.uuid4(), ticket_id=ticket.id, status="running", input_hash="effective-input", ended_at=None, error_text=None)
        turn = SimpleNamespace(
            id=uuid.uuid4(),
            ai_run_id=run.id,
            conversation_id=uuid.uuid4(),
            transport_kind="app_server",
            status="completed",
            steering_closed_at=datetime.now(timezone.utc),
            effective_input_hash="effective-input",
        )
        receipt = CodexTurnSteer(
            id=uuid.uuid4(),
            turn_id=turn.id,
            event_kind="ticket_message",
            source_kind="ticket_message",
            source_id=uuid.uuid4(),
            dedupe_key="ticket-message:blocking",
            expected_native_turn_id="turn-1",
            payload_json={},
            payload_hash="payload-hash",
            status="ambiguous",
        )

        class Db(_FakeDb):
            def __init__(self, *, run):
                super().__init__(run=run)
                self.added = []

            def add(self, item):
                self.added.append(item)

            def execute(self, statement):
                descriptions = statement.column_descriptions if getattr(statement, "column_descriptions", None) else []
                first_name = descriptions[0].get("name") if descriptions else None
                entity = descriptions[0].get("entity") if descriptions else None
                entity_name = getattr(entity, "__name__", "")
                if first_name == "coalesce":
                    return _FakePersistentScalarResult(0)
                if entity_name == "CodexTurn":
                    return _FakeWorkerStateResult([turn])
                if entity_name == "CodexTurnSteer":
                    return _FakeWorkerStateResult([receipt] if has_receipt else [])
                return super().execute(statement)

        fake_db = Db(run=run)
        observed = {"visible": 0, "requeue": 0}

        @contextmanager
        def fake_session_scope(_settings):
            yield fake_db

        unseen_event = SimpleNamespace(source_kind=unseen_source_kind) if unseen_source_kind is not None else None
        monkeypatch.setattr("worker.triage.session_scope", fake_session_scope)
        monkeypatch.setattr("worker.triage.load_ticket_context", lambda db, ticket_id, context=context: context)
        monkeypatch.setattr("worker.triage._freeze_run_input", lambda *args, **kwargs: ("effective-input", SimpleNamespace(input_hash="effective-input")))
        monkeypatch.setattr("worker.triage.load_strictly_unseen_input_events", lambda *args, **kwargs: (unseen_event,) if unseen_event else ())
        monkeypatch.setattr("worker.triage.publish_ai_internal_note", lambda *args, **kwargs: pytest.fail("should not publish internal note"))
        monkeypatch.setattr("worker.triage.publish_ai_public_reply", lambda *args, **kwargs: observed.__setitem__("visible", observed["visible"] + 1))
        monkeypatch.setattr("worker.triage.create_ai_draft", lambda *args, **kwargs: observed.__setitem__("visible", observed["visible"] + 1))
        monkeypatch.setattr("worker.triage.route_ticket_after_ai", lambda *args, **kwargs: observed.__setitem__("visible", observed["visible"] + 1))
        monkeypatch.setattr("worker.triage.process_deferred_requeue", lambda *args, **kwargs: observed.__setitem__("requeue", observed["requeue"] + 1))
        monkeypatch.setattr("worker.triage.write_run_manifest_snapshot", lambda *args, **kwargs: None)

        symbols["_apply_success_result"](
            settings,
            run_id=run.id,
            worker_instance_id="worker-test",
            pipeline_result=_pipeline_result(
                route_target=_build_route_target(route_target_id="support", kind="direct_ai", mode="fixed", specialist_id="support"),
                specialist_payload=_specialist_payload(),
            ),
        )

        assert run.status == "superseded"
        assert observed == {"visible": 0, "requeue": 0}
        outcomes = [item for item in fake_db.added if item.__class__.__name__ == "CodexTurnOutcome"]
        assert outcomes[0].payload_json["reason"] == expected_reason


@pytest.mark.parametrize(
    ("trigger", "forced_route_target_id", "forced_specialist_id"),
    [
        ("manual_rerun", None, None),
        ("reopen", None, None),
        ("requester_reply", "support", "support"),
    ],
)
def test_apply_success_result_app_server_blocks_stronger_control_requests(
    monkeypatch,
    tmp_path,
    trigger,
    forced_route_target_id,
    forced_specialist_id,
):
    symbols = _load_worker_symbols()
    base_settings = _make_settings(tmp_path)
    settings = Settings(
        **{
            **base_settings.__dict__,
            "codex_conversations_enabled": True,
            "codex_app_server_specialist_transport_enabled": True,
            "codex_active_turn_steering_enabled": True,
        }
    )
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        reference="T-000008F",
        title=f"Blocked {trigger}",
        status="ai_triage",
        urgent=False,
        requester_language=None,
        last_processed_hash=None,
        last_ai_action=None,
        clarification_rounds=0,
        requeue_requested=True,
        requeue_trigger=trigger,
        requeue_requested_by_user_id=uuid.uuid4(),
        requeue_source_message_id=uuid.uuid4(),
        requeue_forced_route_target_id=forced_route_target_id,
        requeue_forced_specialist_id=forced_specialist_id,
    )
    context = _make_context(ticket=ticket)
    run = SimpleNamespace(id=uuid.uuid4(), ticket_id=ticket.id, status="running", input_hash="effective-input", ended_at=None, error_text=None)
    turn = SimpleNamespace(
        id=uuid.uuid4(),
        ai_run_id=run.id,
        conversation_id=uuid.uuid4(),
        transport_kind="app_server",
        status="completed",
        steering_closed_at=datetime.now(timezone.utc),
        effective_input_hash="effective-input",
    )

    class Db(_FakeDb):
        def __init__(self, *, run):
            super().__init__(run=run)
            self.added = []

        def add(self, item):
            self.added.append(item)

        def execute(self, statement):
            descriptions = statement.column_descriptions if getattr(statement, "column_descriptions", None) else []
            first_name = descriptions[0].get("name") if descriptions else None
            entity = descriptions[0].get("entity") if descriptions else None
            entity_name = getattr(entity, "__name__", "")
            if first_name == "coalesce":
                return _FakePersistentScalarResult(0)
            if entity_name == "CodexTurn":
                return _FakeWorkerStateResult([turn])
            if entity_name == "CodexTurnSteer":
                return _FakeWorkerStateResult([])
            return super().execute(statement)

    fake_db = Db(run=run)
    observed = {"public": 0, "draft": 0, "route": 0, "requeue": 0}

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.triage.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.triage.load_ticket_context", lambda db, ticket_id: context)
    monkeypatch.setattr("worker.triage._freeze_run_input", lambda *args, **kwargs: ("effective-input", SimpleNamespace(input_hash="effective-input")))
    monkeypatch.setattr("worker.triage.publish_ai_internal_note", lambda *args, **kwargs: pytest.fail("should not publish internal note"))
    monkeypatch.setattr("worker.triage.publish_ai_public_reply", lambda *args, **kwargs: observed.__setitem__("public", observed["public"] + 1))
    monkeypatch.setattr("worker.triage.create_ai_draft", lambda *args, **kwargs: observed.__setitem__("draft", observed["draft"] + 1))
    monkeypatch.setattr("worker.triage.route_ticket_after_ai", lambda *args, **kwargs: observed.__setitem__("route", observed["route"] + 1))
    monkeypatch.setattr("worker.triage.process_deferred_requeue", lambda *args, **kwargs: observed.__setitem__("requeue", observed["requeue"] + 1))
    monkeypatch.setattr("worker.triage.write_run_manifest_snapshot", lambda *args, **kwargs: None)

    symbols["_apply_success_result"](
        settings,
        run_id=run.id,
        worker_instance_id="worker-test",
        pipeline_result=_pipeline_result(
            route_target=_build_route_target(route_target_id="support", kind="direct_ai", mode="fixed", specialist_id="support"),
            specialist_payload=_specialist_payload(),
        ),
    )

    assert run.status == "superseded"
    assert observed == {"public": 0, "draft": 0, "route": 0, "requeue": 1}
    outcomes = [item for item in fake_db.added if item.__class__.__name__ == "CodexTurnOutcome"]
    assert outcomes[0].payload_json["reason"] == "stronger_control_request"


def test_apply_success_result_stale_ticket_content_requeue_creates_successor(monkeypatch, tmp_path):
    symbols = _load_worker_symbols()
    settings = _make_settings(tmp_path)
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        reference="T-000008E",
        title="Stale content request",
        status="ai_triage",
        urgent=False,
        requester_language=None,
        last_processed_hash=None,
        last_ai_action=None,
        clarification_rounds=0,
        requeue_requested=True,
        requeue_trigger="ticket_content",
        requeue_source_message_id=uuid.uuid4(),
    )
    context = _make_context(ticket=ticket)
    run = SimpleNamespace(id=uuid.uuid4(), ticket_id=ticket.id, status="running", input_hash="old-hash", ended_at=None, error_text=None)
    fake_db = _FakeDb(run=run)
    observed = {"requeue": 0, "visible": 0}

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.triage.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.triage.load_ticket_context", lambda db, ticket_id: context)
    monkeypatch.setattr("worker.triage.process_deferred_requeue", lambda *args, **kwargs: observed.__setitem__("requeue", observed["requeue"] + 1))
    monkeypatch.setattr("worker.triage.publish_ai_public_reply", lambda *args, **kwargs: observed.__setitem__("visible", observed["visible"] + 1))
    monkeypatch.setattr("worker.triage.create_ai_draft", lambda *args, **kwargs: observed.__setitem__("visible", observed["visible"] + 1))
    monkeypatch.setattr("worker.triage.route_ticket_after_ai", lambda *args, **kwargs: observed.__setitem__("visible", observed["visible"] + 1))
    monkeypatch.setattr("worker.triage.write_run_manifest_snapshot", lambda *args, **kwargs: None)

    symbols["_apply_success_result"](
        settings,
        run_id=run.id,
        worker_instance_id="worker-test",
        pipeline_result=_pipeline_result(
            route_target=_build_route_target(route_target_id="support", kind="direct_ai", mode="fixed", specialist_id="support"),
            specialist_payload=_specialist_payload(),
        ),
    )

    assert run.status == "superseded"
    assert observed == {"requeue": 1, "visible": 0}


@pytest.mark.parametrize("ticket_status", ["waiting_on_user", "waiting_on_dev_ti", "resolved"])
def test_apply_success_result_retires_stale_ticket_content_outside_ai_triage_without_successor(
    monkeypatch,
    tmp_path,
    ticket_status,
):
    symbols = _load_worker_symbols()
    base_settings = _make_settings(tmp_path)
    settings = Settings(
        **{
            **base_settings.__dict__,
            "codex_conversations_enabled": True,
            "codex_app_server_specialist_transport_enabled": True,
            "codex_active_turn_steering_enabled": True,
        }
    )
    source_message_id = uuid.uuid4()
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        reference="T-000008G",
        title=f"Dormant stale content in {ticket_status}",
        status=ticket_status,
        urgent=False,
        requester_language=None,
        last_processed_hash=None,
        last_ai_action=None,
        clarification_rounds=0,
        requeue_requested=True,
        requeue_trigger="ticket_content",
        requeue_requested_by_user_id=uuid.uuid4(),
        requeue_source_message_id=source_message_id,
        requeue_forced_route_target_id=None,
        requeue_forced_specialist_id=None,
    )
    context = _make_context(ticket=ticket)
    run = SimpleNamespace(id=uuid.uuid4(), ticket_id=ticket.id, status="running", input_hash="effective-input", ended_at=None, error_text=None)
    turn = SimpleNamespace(
        id=uuid.uuid4(),
        ai_run_id=run.id,
        conversation_id=uuid.uuid4(),
        transport_kind="app_server",
        status="completed",
        steering_closed_at=datetime.now(timezone.utc),
        effective_input_hash="effective-input",
    )

    class Db(_FakeDb):
        def __init__(self, *, run):
            super().__init__(run=run)
            self.added = []

        def add(self, item):
            self.added.append(item)

        def execute(self, statement):
            descriptions = statement.column_descriptions if getattr(statement, "column_descriptions", None) else []
            first_name = descriptions[0].get("name") if descriptions else None
            entity = descriptions[0].get("entity") if descriptions else None
            entity_name = getattr(entity, "__name__", "")
            if first_name == "coalesce":
                return _FakePersistentScalarResult(0)
            if entity_name == "CodexTurn":
                return _FakeWorkerStateResult([turn])
            if entity_name == "CodexTurnSteer":
                return _FakeWorkerStateResult([])
            return super().execute(statement)

    fake_db = Db(run=run)
    observed = {"public": 0, "draft": 0, "route": 0}

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.triage.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.triage.load_ticket_context", lambda db, ticket_id: context)
    monkeypatch.setattr("worker.triage._freeze_run_input", lambda *args, **kwargs: ("effective-input", SimpleNamespace(input_hash="effective-input")))
    monkeypatch.setattr("worker.triage.publish_ai_internal_note", lambda *args, **kwargs: pytest.fail("should not publish internal note"))
    monkeypatch.setattr("worker.triage.publish_ai_public_reply", lambda *args, **kwargs: observed.__setitem__("public", observed["public"] + 1))
    monkeypatch.setattr("worker.triage.create_ai_draft", lambda *args, **kwargs: observed.__setitem__("draft", observed["draft"] + 1))
    monkeypatch.setattr("worker.triage.route_ticket_after_ai", lambda *args, **kwargs: observed.__setitem__("route", observed["route"] + 1))
    monkeypatch.setattr("worker.triage.write_run_manifest_snapshot", lambda *args, **kwargs: None)

    symbols["_apply_success_result"](
        settings,
        run_id=run.id,
        worker_instance_id="worker-test",
        pipeline_result=_pipeline_result(
            route_target=_build_route_target(route_target_id="support", kind="direct_ai", mode="fixed", specialist_id="support"),
            specialist_payload=_specialist_payload(),
        ),
    )

    assert run.status == "superseded"
    assert observed == {"public": 0, "draft": 0, "route": 0}
    assert ticket.status == ticket_status
    assert ticket.requeue_requested is False
    assert ticket.requeue_trigger is None
    assert ticket.requeue_requested_by_user_id is None
    assert ticket.requeue_source_message_id is None
    outcomes = [item for item in fake_db.added if item.__class__.__name__ == "CodexTurnOutcome"]
    assert outcomes[0].payload_json["reason"] == "workflow_status_changed"


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
    assert outcomes[0].payload_json["reason"] == "stale_run_recovery"
    assert outcomes[0].payload_json["stale_timeout_seconds"] == settings.ai_run_stale_timeout_seconds
    assert outcomes[0].payload_json["accepted"] is True
    assert outcomes[0].payload_json["ambiguous_steering_receipts"] == 0
    assert outcomes[0].payload_json["accepted_inputs_remain_consumed"] is True
    assert outcomes[0].payload_json["rejected_and_dormant_inputs_remain_discoverable"] is True
    assert outcomes[0].payload_json["late_retired_session_output_publishable"] is False
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


def test_handle_stale_persistent_run_marks_sending_steer_receipt_ambiguous_without_input():
    pytest.importorskip("sqlalchemy")

    from shared.models import CodexTurnSteer
    from worker import persistent_codex

    run = SimpleNamespace(id=uuid.uuid4())
    conversation = SimpleNamespace(id=uuid.uuid4(), status="active")
    session = SimpleNamespace(
        id=uuid.uuid4(),
        conversation_id=conversation.id,
        thread_id="thread-1",
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
        accepted_at=datetime.now(timezone.utc),
        status="running",
        ended_at=None,
    )
    receipt = CodexTurnSteer(
        id=uuid.uuid4(),
        turn_id=turn.id,
        event_kind="ticket_message",
        source_kind="ticket_message",
        source_id=uuid.uuid4(),
        dedupe_key="ticket-message:after-send",
        expected_native_turn_id="turn-1",
        rpc_request_id="rpc-1",
        payload_json={"body_text": "after possible send"},
        payload_hash="payload-hash",
        status="sending",
    )

    class StalePersistentDb:
        def __init__(self):
            self.added = []
            self.inputs = []

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
            if entity_name == "CodexTurnSteer":
                return _FakeWorkerStateResult([receipt])
            return _FakeWorkerStateResult([])

        def add(self, item):
            self.added.append(item)
            if item.__class__.__name__ == "CodexTurnInput":
                self.inputs.append(item)

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
    assert turn.status == "ambiguous"
    assert session.status == "replaced"
    assert conversation.status == "recovery_required"
    assert receipt.status == "ambiguous"
    assert receipt.resolved_at is not None
    assert receipt.error_code == "stale_run_recovery"
    assert fake_db.inputs == []
    outcomes = [item for item in fake_db.added if item.__class__.__name__ == "CodexTurnOutcome"]
    assert [item.outcome_kind for item in outcomes] == ["ambiguous", "ambiguous"]
    assert outcomes[0].payload_json["event_type"] == "turn/steer"
    assert outcomes[1].payload_json["ambiguous_steering_receipts"] == 1


def test_app_server_completion_fence_closes_steering_reconciles_receipts_without_advancing_frontier_hash(tmp_path):
    from shared.models import CodexTurnSteer
    from worker import persistent_codex

    settings = _make_settings(tmp_path)
    run = SimpleNamespace(id=uuid.uuid4(), ticket_id=uuid.uuid4(), status="running", last_heartbeat_at=None)
    session = SimpleNamespace(
        id=uuid.uuid4(),
        thread_id="thread-1",
        lease_owner_run_id=run.id,
        lease_worker_instance_id="worker-test",
        lease_heartbeat_at=None,
        lease_expires_at=None,
    )
    turn = SimpleNamespace(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        session_id=session.id,
        ai_run_id=run.id,
        transport_kind="app_server",
        native_turn_id="turn-1",
        steering_closed_at=None,
        effective_input_hash="accepted-frontier-hash",
    )
    step = SimpleNamespace(id=uuid.uuid4(), ended_at="not-none")
    ticket = SimpleNamespace(id=run.ticket_id, status="ai_triage")
    source_id = uuid.uuid4()
    accepted_input = SimpleNamespace(
        event_kind="ticket_message",
        source_kind="ticket_message",
        source_id=source_id,
        dedupe_key=f"ticket-message:{source_id}",
        payload_json={"message_id": str(source_id), "body_text": "accepted"},
        input_index=1,
    )
    receipt = CodexTurnSteer(
        id=uuid.uuid4(),
        turn_id=turn.id,
        event_kind="ticket_message",
        source_kind="ticket_message",
        source_id=uuid.uuid4(),
        dedupe_key="ticket-message:possibly-sent",
        expected_native_turn_id="turn-1",
        payload_json={"body_text": "possibly sent"},
        payload_hash="payload-hash",
        status="sending",
    )

    class FenceDb:
        def __init__(self):
            self.added = []

        def add(self, item):
            self.added.append(item)

        def execute(self, statement):
            descriptions = statement.column_descriptions if getattr(statement, "column_descriptions", None) else []
            first_name = descriptions[0].get("name") if descriptions else None
            entity = descriptions[0].get("entity") if descriptions else None
            entity_name = getattr(entity, "__name__", "")
            if first_name == "coalesce":
                return _FakePersistentScalarResult(0)
            if first_name == "event_kind":
                return _SteeringResult(
                    [
                        (
                            accepted_input.event_kind,
                            accepted_input.source_kind,
                            accepted_input.source_id,
                            accepted_input.dedupe_key,
                            accepted_input.payload_json,
                        )
                    ]
                )
            if entity_name == "AIRun":
                return _SteeringResult([run])
            if entity_name == "CodexSession":
                return _SteeringResult([session])
            if entity_name == "CodexTurn":
                return _SteeringResult([turn])
            if entity_name == "Ticket":
                return _SteeringResult([ticket])
            if entity_name == "CodexTurnSteer":
                return _SteeringResult([receipt] if receipt.status in {"prepared", "sending"} else [])
            return _SteeringResult([])

        def get(self, model, key):
            if getattr(model, "__name__", "") == "CodexTurn" and key == turn.id:
                return turn
            if getattr(model, "__name__", "") == "AIRunStep" and key == step.id:
                return step
            return None

    fake_db = FenceDb()

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    prepared = SimpleNamespace(run_id=run.id, ticket_id=ticket.id, worker_instance_id="worker-test")
    persistent = SimpleNamespace(turn_id=turn.id, session_id=session.id, conversation_id=turn.conversation_id, step_id=step.id)
    completed_message = {
        "method": "turn/completed",
        "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "items": [], "status": "completed"}},
    }

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr("worker.persistent_codex.session_scope", fake_session_scope)
        ambiguous_receipts = persistent_codex._commit_app_server_completion_fence(
            settings,
            prepared=prepared,
            persistent=persistent,
            completed_message=completed_message,
            expected_thread_id="thread-1",
            expected_native_turn_id="turn-1",
        )
    finally:
        monkeypatch.undo()

    assert ambiguous_receipts == 1
    assert turn.steering_closed_at is not None
    assert receipt.status == "ambiguous"
    assert receipt.error_code == "turn_completed_with_unresolved_steer"
    assert turn.effective_input_hash == "accepted-frontier-hash"
    assert step.ended_at is None
    outcomes = [item for item in fake_db.added if item.__class__.__name__ == "CodexTurnOutcome"]
    assert [item.outcome_kind for item in outcomes] == ["ambiguous", "completed"]
    assert outcomes[-1].payload_json["event_type"] == "completion_fence"
    assert outcomes[-1].payload_json["ambiguous_steering_receipts"] == 1


def test_completed_turn_payload_uses_only_final_answer_agent_message():
    from worker import persistent_codex

    early_payload = json.dumps(_specialist_payload(public_reply_markdown="Too early"))
    final_payload = json.dumps(_specialist_payload(public_reply_markdown="After completion"))
    completed_message = {
        "method": "turn/completed",
        "params": {
            "threadId": "thread-1",
            "turn": {
                "id": "turn-1",
                "status": "completed",
                "items": [
                    {"type": "agentMessage", "text": early_payload},
                    {"type": "agentMessage", "phase": "final_answer", "text": final_payload},
                ],
            },
        },
    }
    assert persistent_codex._extract_completed_turn_payload(completed_message)["public_reply_markdown"] == "After completion"

    early_only_message = {
        "method": "turn/completed",
        "params": {
            "threadId": "thread-1",
            "turn": {
                "id": "turn-1",
                "status": "completed",
                "items": [{"type": "agentMessage", "text": early_payload}],
            },
        },
    }
    assert persistent_codex._extract_completed_turn_payload(early_only_message) is None


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
