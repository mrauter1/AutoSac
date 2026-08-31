from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
import uuid

import pytest

from shared.config import Settings


@pytest.fixture(autouse=True)
def _configure_codex_deployment_defaults(monkeypatch):
    """Keep route-level settings loads independent from a developer's local .env."""
    from shared.config import get_settings

    monkeypatch.delenv("CODEX_MODEL", raising=False)
    monkeypatch.setenv("DEFAULT_CODEX_MODEL", "gpt-test")
    monkeypatch.setenv("DEFAULT_CODEX_EFFORT", "medium")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _load_symbols():
    pytest.importorskip("sqlalchemy")
    from shared.models import AIRun, AIDraft, CodexTurn, Ticket, TicketAttachment, TicketMessage, TicketStatusHistory, TicketView, User
    from shared.ticketing import (
        add_ops_internal_note,
        add_ops_public_reply,
        assign_ticket_for_ops,
        clear_matching_ticket_content_requeue,
        process_deferred_requeue,
        publish_ai_draft_for_ops,
        reject_ai_draft_for_ops,
        request_manual_rerun,
        set_ticket_status_for_ops,
    )

    return {
        "AIRun": AIRun,
        "AIDraft": AIDraft,
        "CodexTurn": CodexTurn,
        "Ticket": Ticket,
        "TicketAttachment": TicketAttachment,
        "TicketMessage": TicketMessage,
        "TicketStatusHistory": TicketStatusHistory,
        "TicketView": TicketView,
        "User": User,
        "add_ops_internal_note": add_ops_internal_note,
        "add_ops_public_reply": add_ops_public_reply,
        "assign_ticket_for_ops": assign_ticket_for_ops,
        "clear_matching_ticket_content_requeue": clear_matching_ticket_content_requeue,
        "process_deferred_requeue": process_deferred_requeue,
        "publish_ai_draft_for_ops": publish_ai_draft_for_ops,
        "reject_ai_draft_for_ops": reject_ai_draft_for_ops,
        "request_manual_rerun": request_manual_rerun,
        "set_ticket_status_for_ops": set_ticket_status_for_ops,
    }


def _make_settings() -> Settings:
    workspace_dir = Path("/tmp/autosac-ops-workflow/workspace")
    return Settings(
        app_base_url="https://autosac.example.local",
        app_secret_key="secret",
        database_url="postgresql+psycopg://triage:triage@localhost:5432/triage",
        uploads_dir=workspace_dir / "attachments_store",
        triage_workspace_dir=workspace_dir,
        repo_mount_dir=workspace_dir / "app",
        manuals_mount_dir=workspace_dir / "manuals",
        codex_bin="codex",
        codex_api_key="key",
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
    )


def _make_slack_runtime(settings: Settings | None = None):
    from shared.integrations import build_slack_runtime_context

    return build_slack_runtime_context(settings or _make_settings())


class _FakeSession:
    def __init__(self):
        pytest.importorskip("sqlalchemy")
        from shared.models import IntegrationEvent, IntegrationEventTarget

        self._integration_event_type = IntegrationEvent
        self._integration_event_target_type = IntegrationEventTarget
        self.added = []
        self.objects = {}
        self.commit_calls = 0
        self.flush_calls = 0
        self.operations = []
        self.events_by_dedupe_key: dict[str, object] = {}
        self.targets_by_event_id: dict[uuid.UUID, list[object]] = {}

    def add(self, item):
        self.added.append(item)
        self.operations.append(("add", item))
        if isinstance(item, self._integration_event_type):
            self.events_by_dedupe_key[item.dedupe_key] = item
        elif isinstance(item, self._integration_event_target_type):
            self.targets_by_event_id.setdefault(item.event_id, []).append(item)
        key = getattr(item, "user_id", None), getattr(item, "ticket_id", None)
        if key != (None, None):
            self.objects[key] = item

    def get(self, model, key):
        return self.objects.get((model, key)) or self.objects.get(key)

    def flush(self):
        self.flush_calls += 1
        self.operations.append(("flush", None))

    def commit(self):
        self.commit_calls += 1


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value


def _make_active_turn_session(symbols):
    from sqlalchemy.orm import Session

    class _ActiveTurnSession(Session):
        def __init__(self):
            self._ai_run_type = pytest.importorskip("shared.models").AIRun
            self._codex_turn_type = pytest.importorskip("shared.models").CodexTurn
            self._ticket_view_type = pytest.importorskip("shared.models").TicketView
            self.added = []
            self.objects = {}
            self.operations = []
            self.ai_runs = []
            self.turns = []

        def add(self, item):
            self.added.append(item)
            self.operations.append(("add", item))
            if isinstance(item, self._ai_run_type):
                self.ai_runs.append(item)
            elif isinstance(item, self._codex_turn_type):
                self.turns.append(item)
            key = getattr(item, "user_id", None), getattr(item, "ticket_id", None)
            if key != (None, None):
                self.objects[(type(item), key)] = item

        def get(self, model, key):
            return self.objects.get((model, key))

        def flush(self):
            self.operations.append(("flush", None))

        def execute(self, statement, *args, **kwargs):
            compiled = statement.compile()
            sql = " ".join(str(compiled).split())
            params = compiled.params
            if "FROM ai_runs JOIN codex_turns" in sql:
                count = 0
                for run in self.ai_runs:
                    if run.ticket_id != params["ticket_id_1"] or run.status not in params["status_1"]:
                        continue
                    if run.forced_route_target_id is not None or run.forced_specialist_id is not None:
                        continue
                    for turn in self.turns:
                        if turn.ai_run_id != run.id:
                            continue
                        if turn.status not in params["status_2"]:
                            continue
                        if turn.steering_closed_at is not None:
                            continue
                        if turn.transport_kind != params["transport_kind_1"]:
                            continue
                        count += 1
                return _FakeScalarResult(count)
            if "FROM ai_runs" in sql and "JOIN codex_turns" not in sql:
                count = sum(
                    1
                    for run in self.ai_runs
                    if run.ticket_id == params["ticket_id_1"] and run.status in params["status_1"]
                )
                return _FakeScalarResult(count)
            raise AssertionError(f"Unexpected execute: {sql}")

    return _ActiveTurnSession()


def _make_ops_user(symbols, *, role: str = "dev_ti"):
    return symbols["User"](
        id=uuid.uuid4(),
        email=f"{role}@example.com",
        display_name=role.upper(),
        password_hash="hash",
        role=role,
        is_active=True,
    )


def _assert_flush_before_attachments(fake_db, attachments):
    flush_index = next(
        (index for index, (operation, _) in enumerate(fake_db.operations) if operation == "flush"),
        None,
    )
    assert flush_index is not None

    attachment_indexes = [
        index
        for index, (operation, item) in enumerate(fake_db.operations)
        if operation == "add" and item in attachments
    ]
    assert attachment_indexes
    assert flush_index < min(attachment_indexes)


def test_add_ops_public_reply_records_status_history_and_view():
    symbols = _load_symbols()
    fake_db = _FakeSession()
    slack_runtime = _make_slack_runtime()
    actor = _make_ops_user(symbols)
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=5,
        reference="T-000005",
        title="Needs follow-up",
        created_by_user_id=uuid.uuid4(),
        status="waiting_on_dev_ti",
        urgent=False,
    )
    existing_view = symbols["TicketView"](user_id=actor.id, ticket_id=ticket.id)
    fake_db.objects[(actor.id, ticket.id)] = existing_view

    message, attachments = symbols["add_ops_public_reply"](
        fake_db,
        slack_runtime=slack_runtime,
        ticket=ticket,
        actor=actor,
        body_markdown="Please confirm the affected user account.",
        next_status="waiting_on_user",
    )

    history = [item for item in fake_db.added if isinstance(item, symbols["TicketStatusHistory"])]

    assert message.author_type == "dev_ti"
    assert message.visibility == "public"
    assert message.source == "human_public_reply"
    assert attachments == []
    assert ticket.status == "waiting_on_user"
    assert history[0].from_status == "waiting_on_dev_ti"
    assert history[0].to_status == "waiting_on_user"
    assert fake_db.objects[(actor.id, ticket.id)].last_viewed_at >= existing_view.last_viewed_at


def test_add_ops_public_reply_accepts_mixed_attachments():
    symbols = _load_symbols()
    fake_db = _FakeSession()
    settings = _make_settings()
    slack_runtime = _make_slack_runtime(settings)
    actor = _make_ops_user(symbols)
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=52,
        reference="T-000052",
        title="Ops upload",
        created_by_user_id=uuid.uuid4(),
        status="waiting_on_dev_ti",
        urgent=False,
    )
    uploads = [
        SimpleNamespace(
            original_filename="shot.png",
            mime_type="image/png",
            sha256="abc123",
            size_bytes=128,
            width=40,
            height=20,
        ),
        SimpleNamespace(
            original_filename="notes.pdf",
            mime_type="application/pdf",
            sha256="pdf123",
            size_bytes=256,
            width=None,
            height=None,
        ),
    ]

    message, attachments = symbols["add_ops_public_reply"](
        fake_db,
        slack_runtime=slack_runtime,
        ticket=ticket,
        actor=actor,
        body_markdown="Attaching the requested files.",
        next_status="waiting_on_user",
        settings=settings,
        attachments=uploads,
    )

    assert [attachment.original_filename for attachment in attachments] == ["shot.png", "notes.pdf"]
    assert [attachment.mime_type for attachment in attachments] == ["image/png", "application/pdf"]
    assert all(attachment.visibility == "public" for attachment in attachments)
    assert all(attachment.message_id == message.id for attachment in attachments)
    assert all(Path(attachment.stored_path).resolve().is_relative_to(settings.uploads_dir.resolve()) for attachment in attachments)
    _assert_flush_before_attachments(fake_db, attachments)


def test_add_ops_public_reply_rejects_invalid_next_status():
    symbols = _load_symbols()
    fake_db = _FakeSession()
    slack_runtime = _make_slack_runtime()
    actor = _make_ops_user(symbols)
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=50,
        reference="T-000050",
        title="Invalid transition",
        created_by_user_id=uuid.uuid4(),
        status="waiting_on_dev_ti",
        urgent=False,
    )

    with pytest.raises(ValueError):
        symbols["add_ops_public_reply"](
            fake_db,
            slack_runtime=slack_runtime,
            ticket=ticket,
            actor=actor,
            body_markdown="This should fail.",
            next_status="new",
        )


def test_add_ops_public_reply_ai_triage_delegates_to_manual_rerun(monkeypatch):
    symbols = _load_symbols()
    fake_db = _FakeSession()
    slack_runtime = _make_slack_runtime()
    actor = _make_ops_user(symbols, role="admin")
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=51,
        reference="T-000051",
        title="Reply back to AI",
        created_by_user_id=actor.id,
        status="waiting_on_dev_ti",
        urgent=False,
    )
    observed = {"manual_rerun": 0, "forced_route_target_id": None, "forced_specialist_id": None}

    monkeypatch.setattr(
        "shared.ticketing.request_manual_rerun",
        lambda db, slack_runtime, ticket, actor, forced_route_target_id=None, forced_specialist_id=None: observed.update(
            {
                "manual_rerun": observed["manual_rerun"] + 1,
                "forced_route_target_id": forced_route_target_id,
                "forced_specialist_id": forced_specialist_id,
            }
        ),
    )

    message, attachments = symbols["add_ops_public_reply"](
        fake_db,
        slack_runtime=slack_runtime,
        ticket=ticket,
        actor=actor,
        body_markdown="Please continue and answer directly.",
        next_status="ai_triage",
        forced_route_target_id="software_architect",
        forced_specialist_id="software-architect",
    )

    history = [item for item in fake_db.added if isinstance(item, symbols["TicketStatusHistory"])]

    assert message.author_type == "dev_ti"
    assert message.visibility == "public"
    assert message.source == "human_public_reply"
    assert attachments == []
    assert observed["manual_rerun"] == 1
    assert observed["forced_route_target_id"] == "software_architect"
    assert observed["forced_specialist_id"] == "software-architect"
    assert history == []


@pytest.mark.parametrize("next_status", ["waiting_on_user", "waiting_on_dev_ti", "resolved"])
def test_add_ops_public_reply_non_ai_status_persists_without_scheduling(monkeypatch, next_status):
    symbols = _load_symbols()
    fake_db = _FakeSession()
    slack_runtime = _make_slack_runtime()
    actor = _make_ops_user(symbols)
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=52,
        reference="T-000052",
        title="Operator reply without AI",
        created_by_user_id=uuid.uuid4(),
        status="waiting_on_dev_ti" if next_status != "waiting_on_dev_ti" else "ai_triage",
        urgent=False,
    )

    monkeypatch.setattr("shared.ticketing.request_manual_rerun", lambda *args, **kwargs: pytest.fail("unexpected manual rerun"))
    monkeypatch.setattr("shared.ticketing.create_pending_ai_run", lambda *args, **kwargs: pytest.fail("unexpected AI run"))
    monkeypatch.setattr("shared.ticketing.request_requeue", lambda *args, **kwargs: pytest.fail("unexpected requeue"))
    monkeypatch.setattr(
        "shared.ticketing.has_compatible_active_ai_triage_specialist_turn",
        lambda *args, **kwargs: pytest.fail("unexpected steering compatibility check"),
    )

    message, attachments = symbols["add_ops_public_reply"](
        fake_db,
        slack_runtime=slack_runtime,
        ticket=ticket,
        actor=actor,
        body_markdown="Public operator update.",
        next_status=next_status,
    )
    history = [item for item in fake_db.added if isinstance(item, symbols["TicketStatusHistory"])]

    assert message.source == "human_public_reply"
    assert message.visibility == "public"
    assert attachments == []
    assert ticket.status == next_status
    assert ticket.requeue_requested is None or ticket.requeue_requested is False
    assert ticket.requeue_trigger is None
    assert len(history) == 1
    assert history[0].to_status == next_status


def test_add_ops_public_reply_ai_triage_uses_content_escrow_for_compatible_active_turn(monkeypatch):
    symbols = _load_symbols()
    fake_db = _FakeSession()
    slack_runtime = _make_slack_runtime()
    actor = _make_ops_user(symbols)
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=53,
        reference="T-000053",
        title="Active AI follow-up",
        created_by_user_id=uuid.uuid4(),
        status="ai_triage",
        urgent=False,
    )

    monkeypatch.setattr(
        "shared.ticketing.has_compatible_active_ai_triage_specialist_turn",
        lambda db, *, settings=None, ticket, forced_route_target_id=None, forced_specialist_id=None: True,
    )
    monkeypatch.setattr("shared.ticketing.request_manual_rerun", lambda *args, **kwargs: pytest.fail("unexpected manual rerun"))

    message, attachments = symbols["add_ops_public_reply"](
        fake_db,
        slack_runtime=slack_runtime,
        ticket=ticket,
        actor=actor,
        body_markdown="Please include this in the active answer.",
        next_status="ai_triage",
    )
    history = [item for item in fake_db.added if isinstance(item, symbols["TicketStatusHistory"])]

    assert attachments == []
    assert ticket.status == "ai_triage"
    assert ticket.requeue_requested is True
    assert ticket.requeue_trigger == "ticket_content"
    assert ticket.requeue_source_message_id == message.id
    assert ticket.requeue_requested_by_user_id == actor.id
    assert history == []


def test_add_ops_public_reply_ai_triage_forced_override_preserves_manual_rerun(monkeypatch):
    symbols = _load_symbols()
    fake_db = _FakeSession()
    slack_runtime = _make_slack_runtime()
    actor = _make_ops_user(symbols)
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=54,
        reference="T-000054",
        title="Forced AI follow-up",
        created_by_user_id=uuid.uuid4(),
        status="ai_triage",
        urgent=False,
    )
    observed = {"manual_rerun": 0}

    monkeypatch.setattr(
        "shared.ticketing.has_compatible_active_ai_triage_specialist_turn",
        lambda db, *, settings=None, ticket, forced_route_target_id=None, forced_specialist_id=None: False,
    )
    monkeypatch.setattr(
        "shared.ticketing.request_manual_rerun",
        lambda *args, **kwargs: observed.__setitem__("manual_rerun", observed["manual_rerun"] + 1),
    )

    symbols["add_ops_public_reply"](
        fake_db,
        slack_runtime=slack_runtime,
        ticket=ticket,
        actor=actor,
        body_markdown="Use the architect specialist.",
        next_status="ai_triage",
        forced_route_target_id="software_architect",
        forced_specialist_id="software-architect",
    )

    assert observed["manual_rerun"] == 1


def test_add_ops_internal_note_keeps_status_and_adds_internal_message():
    symbols = _load_symbols()
    fake_db = _FakeSession()
    actor = _make_ops_user(symbols)
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=6,
        reference="T-000006",
        title="Investigate mismatch",
        created_by_user_id=uuid.uuid4(),
        status="waiting_on_dev_ti",
        urgent=False,
    )

    message = symbols["add_ops_internal_note"](
        fake_db,
        settings=_make_settings(),
        ticket=ticket,
        actor=actor,
        body_markdown="Internal note for Dev/TI only.",
    )

    history = [item for item in fake_db.added if isinstance(item, symbols["TicketStatusHistory"])]

    assert message.author_type == "dev_ti"
    assert message.visibility == "internal"
    assert message.source == "human_internal_note"
    assert ticket.status == "waiting_on_dev_ti"
    assert history == []


def test_add_ops_internal_note_creates_ticket_content_escrow_for_compatible_active_turn(monkeypatch):
    symbols = _load_symbols()
    fake_db = _FakeSession()
    actor = _make_ops_user(symbols)
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=55,
        reference="T-000055",
        title="Active internal context",
        created_by_user_id=uuid.uuid4(),
        status="ai_triage",
        urgent=False,
    )

    monkeypatch.setattr(
        "shared.ticketing.has_compatible_active_ai_triage_specialist_turn",
        lambda db, *, settings=None, ticket: True,
    )

    message = symbols["add_ops_internal_note"](
        fake_db,
        settings=_make_settings(),
        ticket=ticket,
        actor=actor,
        body_markdown="Internal context for the active specialist.",
    )

    assert message.visibility == "internal"
    assert ticket.requeue_requested is True
    assert ticket.requeue_trigger == "ticket_content"
    assert ticket.requeue_source_message_id == message.id
    assert ticket.requeue_requested_by_user_id == actor.id


def test_add_ops_internal_note_without_compatible_turn_remains_dormant(monkeypatch):
    symbols = _load_symbols()
    fake_db = _FakeSession()
    actor = _make_ops_user(symbols)
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=56,
        reference="T-000056",
        title="Dormant internal context",
        created_by_user_id=uuid.uuid4(),
        status="waiting_on_dev_ti",
        urgent=False,
    )

    monkeypatch.setattr(
        "shared.ticketing.has_compatible_active_ai_triage_specialist_turn",
        lambda db, *, settings=None, ticket: False,
    )

    message = symbols["add_ops_internal_note"](
        fake_db,
        settings=_make_settings(),
        ticket=ticket,
        actor=actor,
        body_markdown="Internal context for a later AI run.",
    )

    assert message.visibility == "internal"
    assert ticket.status == "waiting_on_dev_ti"
    assert ticket.requeue_requested is None or ticket.requeue_requested is False
    assert ticket.requeue_trigger is None
    assert ticket.requeue_source_message_id is None


def test_add_ops_internal_note_does_not_overwrite_stronger_requeue(monkeypatch):
    symbols = _load_symbols()
    fake_db = _FakeSession()
    actor = _make_ops_user(symbols)
    source_message_id = uuid.uuid4()
    previous_updated_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=57,
        reference="T-000057",
        title="Manual rerun already queued",
        created_by_user_id=uuid.uuid4(),
        status="ai_triage",
        urgent=False,
        requeue_requested=True,
        requeue_trigger="manual_rerun",
        requeue_requested_by_user_id=uuid.uuid4(),
        requeue_source_message_id=source_message_id,
        requeue_forced_route_target_id="software_architect",
        requeue_forced_specialist_id="software-architect",
        updated_at=previous_updated_at,
    )

    monkeypatch.setattr(
        "shared.ticketing.has_compatible_active_ai_triage_specialist_turn",
        lambda db, *, settings=None, ticket: True,
    )

    message = symbols["add_ops_internal_note"](
        fake_db,
        settings=_make_settings(),
        ticket=ticket,
        actor=actor,
        body_markdown="Do not replace the forced rerun.",
    )

    assert message.source == "human_internal_note"
    assert ticket.requeue_trigger == "manual_rerun"
    assert ticket.requeue_source_message_id == source_message_id
    assert ticket.requeue_forced_route_target_id == "software_architect"
    assert ticket.requeue_forced_specialist_id == "software-architect"
    assert ticket.updated_at > previous_updated_at


def test_add_ops_public_reply_touches_ticket_when_stronger_requeue_prevents_content_escrow(monkeypatch):
    symbols = _load_symbols()
    fake_db = _FakeSession()
    slack_runtime = _make_slack_runtime()
    actor = _make_ops_user(symbols)
    source_message_id = uuid.uuid4()
    previous_updated_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=58,
        reference="T-000058",
        title="Manual rerun already queued",
        created_by_user_id=uuid.uuid4(),
        status="ai_triage",
        urgent=False,
        requeue_requested=True,
        requeue_trigger="manual_rerun",
        requeue_requested_by_user_id=uuid.uuid4(),
        requeue_source_message_id=source_message_id,
        requeue_forced_route_target_id="software_architect",
        requeue_forced_specialist_id="software-architect",
        updated_at=previous_updated_at,
    )
    monkeypatch.setattr(
        "shared.ticketing.has_compatible_active_ai_triage_specialist_turn",
        lambda db, *, settings=None, ticket, forced_route_target_id=None, forced_specialist_id=None: True,
    )
    monkeypatch.setattr("shared.ticketing.request_manual_rerun", lambda *args, **kwargs: pytest.fail("unexpected rerun"))

    symbols["add_ops_public_reply"](
        fake_db,
        slack_runtime=slack_runtime,
        ticket=ticket,
        actor=actor,
        body_markdown="Additional context without replacing the forced rerun.",
        next_status="ai_triage",
    )

    assert ticket.requeue_trigger == "manual_rerun"
    assert ticket.requeue_source_message_id == source_message_id
    assert ticket.requeue_forced_route_target_id == "software_architect"
    assert ticket.requeue_forced_specialist_id == "software-architect"
    assert ticket.updated_at > previous_updated_at


def test_publish_ai_internal_note_links_exact_publication_outcome(monkeypatch):
    from shared.models import Ticket
    from shared.ticketing import publish_ai_internal_note

    fake_db = _FakeSession()
    ticket = Ticket(
        id=uuid.uuid4(),
        reference_num=59,
        reference="T-000059",
        title="Internal AI note",
        created_by_user_id=uuid.uuid4(),
        status="ai_triage",
        urgent=False,
    )
    ai_run_id = uuid.uuid4()
    outcome_id = uuid.uuid4()
    observed = {}

    def append_outcome(db, *, ai_run_id, outcome_kind, payload_json):
        observed.update(ai_run_id=ai_run_id, outcome_kind=outcome_kind, payload_json=payload_json)
        return outcome_id

    monkeypatch.setattr("shared.ticketing._append_turn_outcome_for_message", append_outcome)

    message = publish_ai_internal_note(
        fake_db,
        ticket=ticket,
        ai_run_id=ai_run_id,
        body_markdown="Exact internal note.",
    )

    assert observed["ai_run_id"] == ai_run_id
    assert observed["outcome_kind"] == "internal_note_published"
    assert observed["payload_json"]["internal_note_markdown"] == "Exact internal note."
    assert message.codex_turn_outcome_id == outcome_id
    assert message.ai_run_id == ai_run_id


def test_active_turn_compatibility_requires_ai_triage_unforced_open_specialist_turn():
    symbols = _load_symbols()
    from sqlalchemy.orm import Session
    from shared.ticketing import has_compatible_active_ai_triage_specialist_turn

    class _ScalarResult:
        def scalar_one(self):
            return 1

    class _InspectingSession(Session):
        def __init__(self):
            self.statements = []

        def execute(self, statement, *args, **kwargs):
            self.statements.append(statement)
            return _ScalarResult()

    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=58,
        reference="T-000058",
        title="Compatibility",
        created_by_user_id=uuid.uuid4(),
        status="waiting_on_user",
        urgent=False,
    )
    db = _InspectingSession()
    settings = Settings(
        app_base_url="https://autosac.example.local",
        app_secret_key="secret",
        database_url="postgresql+psycopg://triage:triage@localhost:5432/triage",
        uploads_dir=Path("/tmp/autosac-ops-workflow/workspace/attachments_store"),
        triage_workspace_dir=Path("/tmp/autosac-ops-workflow/workspace"),
        repo_mount_dir=Path("/tmp/autosac-ops-workflow/workspace/app"),
        manuals_mount_dir=Path("/tmp/autosac-ops-workflow/workspace/manuals"),
        codex_bin="codex",
        codex_api_key="key",
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
        codex_active_turn_steering_enabled=True,
    )

    assert has_compatible_active_ai_triage_specialist_turn(db, settings=settings, ticket=ticket) is False
    assert db.statements == []

    ticket.status = "ai_triage"
    disabled_settings = replace(settings, codex_active_turn_steering_enabled=False)
    disabled_transport_settings = replace(settings, codex_app_server_specialist_transport_enabled=False)
    assert has_compatible_active_ai_triage_specialist_turn(db, settings=disabled_settings, ticket=ticket) is False
    assert has_compatible_active_ai_triage_specialist_turn(db, settings=disabled_transport_settings, ticket=ticket) is False
    assert has_compatible_active_ai_triage_specialist_turn(
        db,
        settings=settings,
        ticket=ticket,
        forced_route_target_id="support",
    ) is False
    assert has_compatible_active_ai_triage_specialist_turn(
        db,
        settings=settings,
        ticket=ticket,
        forced_specialist_id="support",
    ) is False
    assert db.statements == []

    assert has_compatible_active_ai_triage_specialist_turn(db, settings=settings, ticket=ticket) is True
    compiled = str(db.statements[0].compile(compile_kwargs={"literal_binds": True}))

    assert "ai_runs" in compiled
    assert "codex_turns" in compiled
    assert "ai_runs.status IN ('pending', 'running')" in compiled
    assert "ai_runs.forced_route_target_id IS NULL" in compiled
    assert "ai_runs.forced_specialist_id IS NULL" in compiled
    assert "codex_turns.status IN ('prepared', 'running')" in compiled
    assert "codex_turns.steering_closed_at IS NULL" in compiled
    assert "codex_turns.transport_kind = 'app_server'" in compiled


@pytest.mark.parametrize(
    (
        "steering_enabled",
        "app_server_transport_enabled",
        "transport_kind",
        "ticket_status",
        "forced_route_target_id",
        "forced_specialist_id",
        "expected_trigger",
    ),
    [
        (True, True, "app_server", "ai_triage", None, None, "ticket_content"),
        (False, True, "app_server", "ai_triage", None, None, "manual_rerun"),
        (True, False, "app_server", "ai_triage", None, None, "manual_rerun"),
        (True, True, "exec", "ai_triage", None, None, "manual_rerun"),
        (True, True, "app_server", "waiting_on_user", None, None, "manual_rerun"),
        (True, True, "app_server", "ai_triage", "support", None, "manual_rerun"),
        (True, True, "app_server", "ai_triage", None, "support-specialist", "manual_rerun"),
    ],
)
def test_add_ops_public_reply_real_compatibility_matrix(
    monkeypatch,
    steering_enabled,
    app_server_transport_enabled,
    transport_kind,
    ticket_status,
    forced_route_target_id,
    forced_specialist_id,
    expected_trigger,
):
    symbols = _load_symbols()
    db = _make_active_turn_session(symbols)
    monkeypatch.setattr("shared.ticketing.record_ticket_public_message_added_event", lambda *args, **kwargs: None)
    settings = _make_settings()
    settings = replace(
        settings,
        codex_conversations_enabled=True,
        codex_app_server_specialist_transport_enabled=app_server_transport_enabled,
        codex_active_turn_steering_enabled=steering_enabled,
    )
    slack_runtime = _make_slack_runtime(settings)
    actor = _make_ops_user(symbols)
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=59,
        reference="T-000059",
        title="Compatibility matrix",
        created_by_user_id=uuid.uuid4(),
        status=ticket_status,
        urgent=False,
    )
    active_run = symbols["AIRun"](
        id=uuid.uuid4(),
        ticket_id=ticket.id,
        status="running",
        triggered_by="requester_reply",
        forced_route_target_id=None,
        forced_specialist_id=None,
    )
    active_turn = symbols["CodexTurn"](
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        ai_run_id=active_run.id,
        turn_index=1,
        status="running",
        transport_kind=transport_kind,
        specialist_id="support",
        agent_spec_version="v1",
        output_contract="specialist_result",
        steering_closed_at=None,
    )
    db.add(active_run)
    db.add(active_turn)
    observed = {"manual_rerun": 0}

    monkeypatch.setattr(
        "shared.ticketing.request_manual_rerun",
        lambda *args, **kwargs: observed.__setitem__("manual_rerun", observed["manual_rerun"] + 1),
    )

    message, attachments = symbols["add_ops_public_reply"](
        db,
        slack_runtime=slack_runtime,
        ticket=ticket,
        actor=actor,
        body_markdown="Use this in the answer.",
        next_status="ai_triage",
        settings=settings,
        forced_route_target_id=forced_route_target_id,
        forced_specialist_id=forced_specialist_id,
    )

    assert message.source == "human_public_reply"
    assert attachments == []
    if expected_trigger == "ticket_content":
        assert observed["manual_rerun"] == 0
        assert ticket.requeue_trigger == "ticket_content"
        assert ticket.requeue_source_message_id == message.id
    else:
        assert observed["manual_rerun"] == 1
        assert ticket.requeue_trigger is None
        assert ticket.requeue_source_message_id is None


@pytest.mark.parametrize(
    ("steering_enabled", "app_server_transport_enabled", "transport_kind", "ticket_status", "expected_requeue"),
    [
        (True, True, "app_server", "ai_triage", "ticket_content"),
        (False, True, "app_server", "ai_triage", None),
        (True, False, "app_server", "ai_triage", None),
        (True, True, "exec", "ai_triage", None),
        (True, True, "app_server", "waiting_on_dev_ti", None),
    ],
)
def test_add_ops_internal_note_real_compatibility_matrix(
    monkeypatch,
    steering_enabled,
    app_server_transport_enabled,
    transport_kind,
    ticket_status,
    expected_requeue,
):
    symbols = _load_symbols()
    db = _make_active_turn_session(symbols)
    monkeypatch.setattr("shared.ticketing.record_ticket_public_message_added_event", lambda *args, **kwargs: None)
    settings = replace(
        _make_settings(),
        codex_conversations_enabled=True,
        codex_app_server_specialist_transport_enabled=app_server_transport_enabled,
        codex_active_turn_steering_enabled=steering_enabled,
    )
    actor = _make_ops_user(symbols)
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=60,
        reference="T-000060",
        title="Internal compatibility matrix",
        created_by_user_id=uuid.uuid4(),
        status=ticket_status,
        urgent=False,
    )
    active_run = symbols["AIRun"](
        id=uuid.uuid4(),
        ticket_id=ticket.id,
        status="running",
        triggered_by="requester_reply",
        forced_route_target_id=None,
        forced_specialist_id=None,
    )
    active_turn = symbols["CodexTurn"](
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        ai_run_id=active_run.id,
        turn_index=1,
        status="running",
        transport_kind=transport_kind,
        specialist_id="support",
        agent_spec_version="v1",
        output_contract="specialist_result",
        steering_closed_at=None,
    )
    db.add(active_run)
    db.add(active_turn)

    message = symbols["add_ops_internal_note"](
        db,
        settings=settings,
        ticket=ticket,
        actor=actor,
        body_markdown="Internal follow-up.",
    )

    assert message.source == "human_internal_note"
    assert message.visibility == "internal"
    assert ticket.requeue_trigger == expected_requeue
    if expected_requeue == "ticket_content":
        assert ticket.requeue_source_message_id == message.id
    else:
        assert ticket.requeue_source_message_id is None


def test_assign_ticket_for_ops_touches_ticket_and_view_only_when_assignment_changes():
    symbols = _load_symbols()
    fake_db = _FakeSession()
    actor = _make_ops_user(symbols)
    assignee = _make_ops_user(symbols, role="admin")
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=60,
        reference="T-000060",
        title="Assignment",
        created_by_user_id=uuid.uuid4(),
        status="waiting_on_dev_ti",
        urgent=False,
    )
    before_update = ticket.updated_at

    symbols["assign_ticket_for_ops"](fake_db, ticket=ticket, actor=actor, assignee=assignee)

    assert ticket.assigned_to_user_id == assignee.id
    assert ticket.updated_at != before_update
    assert fake_db.objects[(actor.id, ticket.id)].ticket_id == ticket.id


def test_set_ticket_status_for_ops_records_resolve_history_and_rejects_invalid_status():
    symbols = _load_symbols()
    fake_db = _FakeSession()
    slack_runtime = _make_slack_runtime()
    actor = _make_ops_user(symbols)
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=61,
        reference="T-000061",
        title="Status",
        created_by_user_id=uuid.uuid4(),
        status="waiting_on_dev_ti",
        urgent=False,
    )

    symbols["set_ticket_status_for_ops"](
        fake_db,
        slack_runtime=slack_runtime,
        ticket=ticket,
        actor=actor,
        next_status="resolved",
    )
    history = [item for item in fake_db.added if isinstance(item, symbols["TicketStatusHistory"])]

    assert ticket.status == "resolved"
    assert ticket.resolved_at is not None
    assert history[0].to_status == "resolved"

    with pytest.raises(ValueError):
        symbols["set_ticket_status_for_ops"](
            fake_db,
            slack_runtime=slack_runtime,
            ticket=ticket,
            actor=actor,
            next_status="not-a-status",
        )


def test_set_ticket_status_for_ops_ai_triage_delegates_to_manual_rerun(monkeypatch):
    symbols = _load_symbols()
    fake_db = _FakeSession()
    slack_runtime = _make_slack_runtime()
    actor = _make_ops_user(symbols)
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=62,
        reference="T-000062",
        title="Requeue from status change",
        created_by_user_id=uuid.uuid4(),
        status="waiting_on_dev_ti",
        urgent=False,
    )
    observed = {"manual_rerun": 0}

    monkeypatch.setattr(
        "shared.ticketing.request_manual_rerun",
        lambda db, slack_runtime, ticket, actor: observed.__setitem__("manual_rerun", observed["manual_rerun"] + 1),
    )

    symbols["set_ticket_status_for_ops"](
        fake_db,
        slack_runtime=slack_runtime,
        ticket=ticket,
        actor=actor,
        next_status="ai_triage",
    )

    history = [item for item in fake_db.added if isinstance(item, symbols["TicketStatusHistory"])]

    assert observed["manual_rerun"] == 1
    assert history == []


def test_request_manual_rerun_requeues_when_run_is_active(monkeypatch):
    symbols = _load_symbols()
    fake_db = _FakeSession()
    slack_runtime = _make_slack_runtime()
    actor = _make_ops_user(symbols)
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=7,
        reference="T-000007",
        title="Rerun requested",
        created_by_user_id=uuid.uuid4(),
        status="waiting_on_dev_ti",
        urgent=False,
    )
    monkeypatch.setattr("shared.ticketing.has_active_ai_run", lambda db, ticket_id: True)

    run = symbols["request_manual_rerun"](
        fake_db,
        slack_runtime=slack_runtime,
        ticket=ticket,
        actor=actor,
    )

    history = [item for item in fake_db.added if isinstance(item, symbols["TicketStatusHistory"])]

    assert run is None
    assert ticket.requeue_requested is True
    assert ticket.requeue_trigger == "manual_rerun"
    assert ticket.requeue_requested_by_user_id == actor.id
    assert ticket.status == "waiting_on_dev_ti"
    assert history == []


def test_request_manual_rerun_requeues_with_forced_specialist_override(monkeypatch):
    symbols = _load_symbols()
    fake_db = _FakeSession()
    slack_runtime = _make_slack_runtime()
    actor = _make_ops_user(symbols)
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=70,
        reference="T-000070",
        title="Architect review",
        created_by_user_id=uuid.uuid4(),
        status="waiting_on_dev_ti",
        urgent=False,
    )
    monkeypatch.setattr("shared.ticketing.has_active_ai_run", lambda db, ticket_id: True)

    run = symbols["request_manual_rerun"](
        fake_db,
        slack_runtime=slack_runtime,
        ticket=ticket,
        actor=actor,
        forced_route_target_id="software_architect",
        forced_specialist_id="software-architect",
    )

    assert run is None
    assert ticket.requeue_requested is True
    assert ticket.requeue_trigger == "manual_rerun"
    assert ticket.requeue_requested_by_user_id == actor.id
    assert ticket.requeue_forced_route_target_id == "software_architect"
    assert ticket.requeue_forced_specialist_id == "software-architect"


def test_request_manual_rerun_creates_pending_run_and_moves_ticket_to_ai_triage(monkeypatch):
    symbols = _load_symbols()
    fake_db = _FakeSession()
    slack_runtime = _make_slack_runtime()
    actor = _make_ops_user(symbols)
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=71,
        reference="T-000071",
        title="Fresh rerun",
        created_by_user_id=uuid.uuid4(),
        status="waiting_on_dev_ti",
        urgent=False,
    )
    expected_run = object()
    monkeypatch.setattr("shared.ticketing.has_active_ai_run", lambda db, ticket_id: False)
    monkeypatch.setattr("shared.ticketing.create_pending_ai_run", lambda *args, **kwargs: expected_run)

    run = symbols["request_manual_rerun"](
        fake_db,
        slack_runtime=slack_runtime,
        ticket=ticket,
        actor=actor,
    )
    history = [item for item in fake_db.added if isinstance(item, symbols["TicketStatusHistory"])]

    assert run is expected_run
    assert ticket.status == "ai_triage"
    assert history[0].from_status == "waiting_on_dev_ti"
    assert history[0].to_status == "ai_triage"


def test_request_manual_rerun_passes_forced_specialist_override_to_new_run(monkeypatch):
    symbols = _load_symbols()
    fake_db = _FakeSession()
    slack_runtime = _make_slack_runtime()
    actor = _make_ops_user(symbols)
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=72,
        reference="T-000072",
        title="Forced rerun",
        created_by_user_id=uuid.uuid4(),
        status="waiting_on_dev_ti",
        urgent=False,
        requeue_forced_route_target_id="support",
        requeue_forced_specialist_id="support",
    )
    observed = {}
    expected_run = object()
    monkeypatch.setattr("shared.ticketing.has_active_ai_run", lambda db, ticket_id: False)

    def fake_create_pending_ai_run(*args, **kwargs):
        observed.update(kwargs)
        return expected_run

    monkeypatch.setattr("shared.ticketing.create_pending_ai_run", fake_create_pending_ai_run)

    run = symbols["request_manual_rerun"](
        fake_db,
        slack_runtime=slack_runtime,
        ticket=ticket,
        actor=actor,
        forced_route_target_id="software_architect",
        forced_specialist_id="software-architect",
    )

    assert run is expected_run
    assert observed["requested_by_user_id"] == actor.id
    assert observed["forced_route_target_id"] == "software_architect"
    assert observed["forced_specialist_id"] == "software-architect"
    assert ticket.requeue_requested_by_user_id is None
    assert ticket.requeue_forced_route_target_id is None
    assert ticket.requeue_forced_specialist_id is None


def test_process_deferred_requeue_transfers_forced_specialist_override(monkeypatch):
    symbols = _load_symbols()
    fake_db = _FakeSession()
    requester_id = uuid.uuid4()
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=73,
        reference="T-000073",
        title="Deferred forced rerun",
        created_by_user_id=uuid.uuid4(),
        status="ai_triage",
        urgent=False,
        requeue_requested=True,
        requeue_trigger="manual_rerun",
        requeue_requested_by_user_id=requester_id,
        requeue_forced_route_target_id="software_architect",
        requeue_forced_specialist_id="software-architect",
    )
    observed = {}
    expected_run = object()

    monkeypatch.setattr("shared.ticketing.has_active_ai_run", lambda db, ticket_id: False)
    monkeypatch.setattr("shared.ticketing.create_pending_ai_run", lambda *args, **kwargs: observed.update(kwargs) or expected_run)

    run = symbols["process_deferred_requeue"](fake_db, ticket=ticket)

    assert run is expected_run
    assert observed["requested_by_user_id"] == requester_id
    assert observed["forced_route_target_id"] == "software_architect"
    assert observed["forced_specialist_id"] == "software-architect"
    assert ticket.requeue_requested is False
    assert ticket.requeue_trigger is None
    assert ticket.requeue_requested_by_user_id is None
    assert ticket.requeue_forced_route_target_id is None
    assert ticket.requeue_forced_specialist_id is None


@pytest.mark.parametrize("trigger", ["requester_reply", "ticket_content"])
def test_process_deferred_requeue_retires_content_driven_requests_outside_ai_triage(monkeypatch, trigger):
    symbols = _load_symbols()
    fake_db = _FakeSession()
    source_message_id = uuid.uuid4()
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=74,
        reference="T-000074",
        title="Dormant content",
        created_by_user_id=uuid.uuid4(),
        status="waiting_on_user",
        urgent=False,
        requeue_requested=True,
        requeue_trigger=trigger,
        requeue_requested_by_user_id=uuid.uuid4(),
        requeue_source_message_id=source_message_id,
    )

    monkeypatch.setattr("shared.ticketing.has_active_ai_run", lambda *args, **kwargs: pytest.fail("unexpected active-run check"))
    monkeypatch.setattr("shared.ticketing.create_pending_ai_run", lambda *args, **kwargs: pytest.fail("unexpected AI run"))

    run = symbols["process_deferred_requeue"](fake_db, ticket=ticket)

    assert run is None
    assert ticket.requeue_requested is False
    assert ticket.requeue_trigger is None
    assert ticket.requeue_requested_by_user_id is None
    assert ticket.requeue_source_message_id is None


@pytest.mark.parametrize("trigger", ["manual_rerun", "reopen"])
def test_process_deferred_requeue_honors_stronger_control_requests_outside_ai_triage(monkeypatch, trigger):
    symbols = _load_symbols()
    fake_db = _FakeSession()
    requested_by_user_id = uuid.uuid4()
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=75,
        reference="T-000075",
        title="Strong control",
        created_by_user_id=uuid.uuid4(),
        status="waiting_on_user",
        urgent=False,
        requeue_requested=True,
        requeue_trigger=trigger,
        requeue_requested_by_user_id=requested_by_user_id,
        requeue_source_message_id=uuid.uuid4(),
        requeue_forced_route_target_id="support",
        requeue_forced_specialist_id="support",
    )
    observed = {}
    expected_run = object()

    monkeypatch.setattr("shared.ticketing.has_active_ai_run", lambda db, ticket_id: False)
    monkeypatch.setattr("shared.ticketing.create_pending_ai_run", lambda *args, **kwargs: observed.update(kwargs) or expected_run)

    run = symbols["process_deferred_requeue"](fake_db, ticket=ticket)

    assert run is expected_run
    assert observed["triggered_by"] == trigger
    assert observed["requested_by_user_id"] == requested_by_user_id
    assert observed["forced_route_target_id"] == "support"
    assert observed["forced_specialist_id"] == "support"
    assert ticket.requeue_requested is False
    assert ticket.requeue_trigger is None
    assert ticket.requeue_source_message_id is None


def test_clear_matching_ticket_content_requeue_only_clears_exact_content_source():
    symbols = _load_symbols()
    source_message_id = uuid.uuid4()
    newer_source_message_id = uuid.uuid4()
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=76,
        reference="T-000076",
        title="Clear matching content",
        created_by_user_id=uuid.uuid4(),
        status="ai_triage",
        urgent=False,
        requeue_requested=True,
        requeue_trigger="ticket_content",
        requeue_requested_by_user_id=uuid.uuid4(),
        requeue_source_message_id=newer_source_message_id,
    )

    assert symbols["clear_matching_ticket_content_requeue"](ticket, source_message_id=source_message_id) is False
    assert ticket.requeue_requested is True
    assert ticket.requeue_source_message_id == newer_source_message_id

    assert symbols["clear_matching_ticket_content_requeue"](ticket, source_message_id=newer_source_message_id) is True
    assert ticket.requeue_requested is False
    assert ticket.requeue_trigger is None
    assert ticket.requeue_source_message_id is None


def test_clear_matching_ticket_content_requeue_preserves_stronger_control_request():
    symbols = _load_symbols()
    source_message_id = uuid.uuid4()
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=77,
        reference="T-000077",
        title="Preserve manual control",
        created_by_user_id=uuid.uuid4(),
        status="ai_triage",
        urgent=False,
        requeue_requested=True,
        requeue_trigger="manual_rerun",
        requeue_requested_by_user_id=uuid.uuid4(),
        requeue_source_message_id=source_message_id,
        requeue_forced_route_target_id="software_architect",
        requeue_forced_specialist_id="software-architect",
    )

    assert symbols["clear_matching_ticket_content_requeue"](ticket, source_message_id=source_message_id) is False
    assert ticket.requeue_requested is True
    assert ticket.requeue_trigger == "manual_rerun"
    assert ticket.requeue_source_message_id == source_message_id
    assert ticket.requeue_forced_route_target_id == "software_architect"


def test_publish_ai_draft_for_ops_creates_ai_message_and_status_change():
    symbols = _load_symbols()
    fake_db = _FakeSession()
    slack_runtime = _make_slack_runtime()
    actor = _make_ops_user(symbols)
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=8,
        reference="T-000008",
        title="Approval needed",
        created_by_user_id=uuid.uuid4(),
        status="waiting_on_dev_ti",
        urgent=False,
    )
    draft = symbols["AIDraft"](
        id=uuid.uuid4(),
        ticket_id=ticket.id,
        ai_run_id=uuid.uuid4(),
        kind="public_reply",
        body_markdown="This is a safe draft reply.",
        body_text="This is a safe draft reply.",
        status="pending_approval",
    )

    message = symbols["publish_ai_draft_for_ops"](
        fake_db,
        slack_runtime=slack_runtime,
        ticket=ticket,
        draft=draft,
        actor=actor,
        next_status="waiting_on_user",
    )

    history = [item for item in fake_db.added if isinstance(item, symbols["TicketStatusHistory"])]

    assert message.author_type == "ai"
    assert message.source == "ai_draft_published"
    assert draft.status == "published"
    assert draft.reviewed_by_user_id == actor.id
    assert draft.published_message_id == message.id
    assert ticket.status == "waiting_on_user"
    assert history[0].to_status == "waiting_on_user"
    assert fake_db.flush_calls == 3


def test_publish_ai_draft_for_ops_links_publication_outcome_and_supports_edited_body(monkeypatch):
    symbols = _load_symbols()
    fake_db = _FakeSession()
    slack_runtime = _make_slack_runtime()
    actor = _make_ops_user(symbols)
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=808,
        reference="T-000808",
        title="Approval with edit",
        created_by_user_id=uuid.uuid4(),
        status="waiting_on_dev_ti",
        urgent=False,
    )
    draft = symbols["AIDraft"](
        id=uuid.uuid4(),
        ticket_id=ticket.id,
        ai_run_id=uuid.uuid4(),
        kind="public_reply",
        body_markdown="Original draft body.",
        body_text="Original draft body.",
        status="pending_approval",
    )
    outcome_id = uuid.uuid4()
    captured = {}

    monkeypatch.setattr(
        "shared.ticketing._append_turn_outcome_for_message",
        lambda db, *, ai_run_id, outcome_kind, payload_json: captured.update(
            {
                "ai_run_id": ai_run_id,
                "outcome_kind": outcome_kind,
                "payload_json": payload_json,
            }
        )
        or outcome_id,
    )

    message = symbols["publish_ai_draft_for_ops"](
        fake_db,
        slack_runtime=slack_runtime,
        ticket=ticket,
        draft=draft,
        actor=actor,
        next_status="waiting_on_user",
        body_markdown="Edited published body.",
    )

    assert message.body_markdown == "Edited published body."
    assert message.codex_turn_outcome_id == outcome_id
    assert captured["outcome_kind"] == "published_with_edits"
    assert captured["payload_json"]["edited"] is True
    assert captured["payload_json"]["original_draft_body_markdown"] == "Original draft body."


def test_reject_ai_draft_for_ops_marks_review_metadata_without_status_change():
    symbols = _load_symbols()
    fake_db = _FakeSession()
    actor = _make_ops_user(symbols)
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=81,
        reference="T-000081",
        title="Reject draft",
        created_by_user_id=uuid.uuid4(),
        status="waiting_on_dev_ti",
        urgent=False,
    )
    draft = symbols["AIDraft"](
        id=uuid.uuid4(),
        ticket_id=ticket.id,
        ai_run_id=uuid.uuid4(),
        kind="public_reply",
        body_markdown="Hold for review.",
        body_text="Hold for review.",
        status="pending_approval",
    )

    symbols["reject_ai_draft_for_ops"](fake_db, ticket=ticket, draft=draft, actor=actor)
    history = [item for item in fake_db.added if isinstance(item, symbols["TicketStatusHistory"])]

    assert draft.status == "rejected"
    assert draft.reviewed_by_user_id == actor.id
    assert draft.reviewed_at is not None
    assert ticket.status == "waiting_on_dev_ti"
    assert history == []


def test_reject_ai_draft_for_ops_records_rejection_outcome(monkeypatch):
    symbols = _load_symbols()
    fake_db = _FakeSession()
    actor = _make_ops_user(symbols)
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=811,
        reference="T-000811",
        title="Reject draft outcome",
        created_by_user_id=uuid.uuid4(),
        status="waiting_on_dev_ti",
        urgent=False,
    )
    draft = symbols["AIDraft"](
        id=uuid.uuid4(),
        ticket_id=ticket.id,
        ai_run_id=uuid.uuid4(),
        kind="public_reply",
        body_markdown="Needs review.",
        body_text="Needs review.",
        status="pending_approval",
    )
    captured = {}

    monkeypatch.setattr(
        "shared.ticketing._append_turn_outcome_for_message",
        lambda db, *, ai_run_id, outcome_kind, payload_json: captured.update(
            {
                "ai_run_id": ai_run_id,
                "outcome_kind": outcome_kind,
                "payload_json": payload_json,
            }
        ),
    )

    symbols["reject_ai_draft_for_ops"](fake_db, ticket=ticket, draft=draft, actor=actor)

    assert captured["ai_run_id"] == draft.ai_run_id
    assert captured["outcome_kind"] == "draft_rejected"
    assert captured["payload_json"]["draft_id"] == str(draft.id)
    assert captured["payload_json"]["draft_body_markdown"] == draft.body_markdown


def test_publish_ai_draft_for_ops_rejects_non_pending_draft():
    symbols = _load_symbols()
    fake_db = _FakeSession()
    slack_runtime = _make_slack_runtime()
    actor = _make_ops_user(symbols)
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=82,
        reference="T-000082",
        title="Already handled",
        created_by_user_id=uuid.uuid4(),
        status="waiting_on_dev_ti",
        urgent=False,
    )
    draft = symbols["AIDraft"](
        id=uuid.uuid4(),
        ticket_id=ticket.id,
        ai_run_id=uuid.uuid4(),
        kind="public_reply",
        body_markdown="Already published.",
        body_text="Already published.",
        status="published",
    )

    with pytest.raises(ValueError):
        symbols["publish_ai_draft_for_ops"](
            fake_db,
            slack_runtime=slack_runtime,
            ticket=ticket,
            draft=draft,
            actor=actor,
            next_status="waiting_on_user",
        )


def test_publish_ai_public_reply_is_idempotent_when_message_already_exists(monkeypatch):
    pytest.importorskip("sqlalchemy")

    from shared.ticketing import publish_ai_public_reply

    symbols = _load_symbols()
    fake_db = _FakeSession()
    slack_runtime = _make_slack_runtime()
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=812,
        reference="T-000812",
        title="Auto publish idempotent",
        created_by_user_id=uuid.uuid4(),
        status="waiting_on_dev_ti",
        urgent=False,
    )
    existing_message = symbols["TicketMessage"](
        id=uuid.uuid4(),
        ticket_id=ticket.id,
        author_user_id=None,
        author_type="ai",
        visibility="public",
        source="ai_auto_public",
        body_markdown="Already published.",
        body_text="Already published.",
        ai_run_id=uuid.uuid4(),
    )

    monkeypatch.setattr("shared.ticketing._load_existing_ai_public_message", lambda db, *, ai_run_id: existing_message)
    monkeypatch.setattr("shared.ticketing._append_turn_outcome_for_message", lambda *args, **kwargs: pytest.fail("unexpected outcome append"))

    returned = publish_ai_public_reply(
        fake_db,
        slack_runtime=slack_runtime,
        ticket=ticket,
        ai_run_id=existing_message.ai_run_id,
        body_markdown="Should not duplicate.",
        next_status="waiting_on_user",
        last_ai_action="auto_public_reply",
    )

    assert returned is existing_message
    assert fake_db.added == []


def _load_web_stack():
    pytest.importorskip("fastapi")
    pytest.importorskip("sqlalchemy")
    from fastapi.testclient import TestClient
    from app import auth, routes_ops
    from app.main import create_app
    from shared.db import db_session_dependency

    return {
        "TestClient": TestClient,
        "auth": auth,
        "routes_ops": routes_ops,
        "create_app": create_app,
        "db_session_dependency": db_session_dependency,
    }


def test_present_ticket_route_target_falls_back_to_legacy_ticket_class():
    pytest.importorskip("fastapi")
    from app.ai_run_presenters import present_ticket_route_target

    presentation = present_ticket_route_target(
        SimpleNamespace(
            route_target_id=None,
            ticket_class="support",
        )
    )

    assert presentation == {
        "id": "support",
        "label": "Support",
        "kind": "direct_ai",
        "known": True,
    }


def test_present_ai_run_output_exposes_legacy_triage_fields():
    pytest.importorskip("fastapi")
    from app.ai_run_presenters import present_ai_run_output

    presentation = present_ai_run_output(
        SimpleNamespace(
            final_output_contract="triage_result",
            final_output_json={
                "ticket_class": "support",
                "confidence": 0.95,
                "impact_level": "medium",
                "requester_language": "en",
                "summary_short": "Accepted analysis",
                "summary_internal": "Internal accepted analysis",
                "development_needed": False,
                "needs_clarification": False,
                "clarifying_questions": [],
                "incorrect_or_conflicting_details": [],
                "evidence_found": True,
                "relevant_paths": [{"path": "manuals/access.md", "reason": "Relevant doc"}],
                "answer_scope": "document_scoped",
                "evidence_status": "verified",
                "misuse_or_safety_risk": False,
                "human_review_reason": "",
                "recommended_next_action": "auto_public_reply",
                "auto_public_reply_allowed": True,
                "public_reply_markdown": "Reply",
                "internal_note_markdown": "Note",
            },
        )
    )

    assert presentation["contract_id"] == "triage_result"
    assert presentation["summary_short"] == "Accepted analysis"
    assert presentation["legacy_confidence"] == 0.95
    assert presentation["legacy_impact_level"] == "medium"
    assert presentation["legacy_development_needed"] is False


def test_present_codex_turn_summary_tracks_publication_and_recovery_state():
    pytest.importorskip("fastapi")
    from app.codex_turn_presenters import present_codex_turn_summary

    started_at = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
    ended_at = started_at + timedelta(minutes=4)
    turn = SimpleNamespace(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        ai_run_id=uuid.uuid4(),
        turn_index=2,
        status="completed",
        specialist_id="support_specialist",
        route_target_id="support",
        output_contract="specialist_result",
        prompt_path="/tmp/prompt.txt",
        schema_path="/tmp/schema.json",
        final_output_path="/tmp/final.json",
        stdout_jsonl_path="/tmp/stdout.jsonl",
        stderr_path="/tmp/stderr.txt",
        accepted_at=started_at,
        started_at=started_at,
        ended_at=ended_at,
        transport_kind="app_server",
        native_turn_id="native-turn-2",
        steering_closed_at=ended_at,
        effective_input_hash="effective-hash",
    )
    run = SimpleNamespace(
        triggered_by="manual_rerun",
        final_output_contract="specialist_result",
        final_output_json={
            "summary_internal": "Generated draft needs review.",
            "public_reply_markdown": "Original generated reply.",
            "internal_note_markdown": "Internal follow-up.",
        },
    )
    outcomes = [
        SimpleNamespace(outcome_index=1, outcome_kind="accepted", created_at=started_at, payload_json={}),
        SimpleNamespace(outcome_index=2, outcome_kind="published_with_edits", created_at=ended_at, payload_json={}),
    ]
    session = SimpleNamespace(
        status="active",
        thread_id="thread-123",
        lease_owner_run_id=uuid.uuid4(),
        lease_worker_instance_id="worker-a",
        lease_expires_at=ended_at + timedelta(minutes=1),
    )
    published_message = SimpleNamespace(
        id=uuid.uuid4(),
        body_markdown="Edited published reply.",
        created_at=ended_at,
    )
    receipt = SimpleNamespace(
        id=uuid.uuid4(),
        event_kind="ticket_message",
        source_kind="ticket_message",
        source_id=uuid.uuid4(),
        dedupe_key="ticket-message:steered",
        expected_native_turn_id="native-turn-2",
        rpc_request_id="autosac-4",
        payload_json={"body_text": "Steered message."},
        payload_hash="payload-hash",
        status="accepted",
        attempted_at=started_at,
        acknowledged_at=started_at + timedelta(milliseconds=250),
        resolved_at=started_at + timedelta(milliseconds=250),
        error_code=None,
        error_text=None,
    )

    presented = present_codex_turn_summary(
        turn,
        run=run,
        outcomes=outcomes,
        session=session,
        session_segment_index=2,
        draft=None,
        published_message=published_message,
        raw_item_count=3,
        conversation_status="recovery_required",
        receipts=[receipt],
        delivery_events=[
            {
                "event_kind": "ticket_message",
                "source_kind": "ticket_message",
                "source_id": uuid.uuid4(),
                "dedupe_key": "ticket-message:dormant",
                "delivery_state": "waiting_future_context",
                "payload_excerpt": "Dormant content.",
            },
            {
                "event_kind": "ticket_message",
                "source_kind": "ticket_message",
                "source_id": uuid.uuid4(),
                "dedupe_key": "ticket-message:queued",
                "delivery_state": "queued_another_run",
                "payload_excerpt": "Queued content.",
            },
        ],
    )

    assert presented["publication"]["state"] == "edited_before_publish"
    assert presented["structured_result"]["public_reply_excerpt"] == "Original generated reply."
    assert presented["raw_item_count"] == 3
    assert presented["recovery_boundary"] is True
    assert presented["recovery_marker_keys"] == [
        "ops.detail.recovery_marker.conversation_recovery_required",
        "ops.detail.recovery_marker.replacement_session_segment",
    ]
    assert presented["specialist_display_name"] == "Support Specialist"
    assert presented["transport_kind"] == "app_server"
    assert presented["native_turn_id"] == "native-turn-2"
    assert presented["steering_receipt_count"] == 1
    assert presented["delivery_state_counts"] == {
        "included_active_turn": 1,
        "waiting_future_context": 1,
        "queued_another_run": 1,
    }


def test_present_codex_turn_detail_classifies_rejected_queued_receipt():
    pytest.importorskip("fastapi")
    from app.codex_turn_presenters import present_codex_turn_detail

    queued_source_id = uuid.uuid4()
    turn = SimpleNamespace(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        ai_run_id=uuid.uuid4(),
        turn_index=1,
        status="completed",
        specialist_id="support",
        route_target_id="support",
        output_contract="specialist_result",
        transport_kind="app_server",
        native_turn_id="native-turn-1",
        steering_closed_at=datetime.now(timezone.utc),
        effective_input_hash="effective-input",
    )
    receipt = SimpleNamespace(
        id=uuid.uuid4(),
        event_kind="ticket_message",
        source_kind="ticket_message",
        source_id=queued_source_id,
        dedupe_key=f"ticket-message:{queued_source_id}",
        expected_native_turn_id="native-turn-1",
        rpc_request_id="autosac-9",
        payload_json={"body_text": "Queued after rejected steering."},
        payload_hash="payload-hash",
        status="rejected",
        attempted_at=datetime.now(timezone.utc),
        acknowledged_at=None,
        resolved_at=datetime.now(timezone.utc),
        error_code="steering_closed",
        error_text="Native turn was closed.",
    )

    presented = present_codex_turn_detail(
        turn,
        run=SimpleNamespace(triggered_by="ticket_content", final_output_contract="specialist_result", final_output_json={}),
        outcomes=[],
        items=[],
        session=SimpleNamespace(status="active", thread_id="thread-1"),
        session_segment_index=1,
        draft=None,
        published_message=None,
        conversation_status="active",
        receipts=[receipt],
        inputs=[],
        delivery_events=[],
        queued_source_id=queued_source_id,
    )

    assert presented["steering_receipts"][0]["delivery_state"] == "queued_another_run"
    assert presented["delivery_state_counts"] == {"queued_another_run": 1}


def test_ops_delivery_event_loader_marks_queued_and_dormant_without_represented_duplicates():
    pytest.importorskip("fastapi")
    from app import routes_ops
    from shared.codex_knowledge import KnownConversationInputs

    ticket_id = uuid.uuid4()
    run_id = uuid.uuid4()
    queued_message_id = uuid.uuid4()
    dormant_message_id = uuid.uuid4()
    accepted_message_id = uuid.uuid4()
    receipted_message_id = uuid.uuid4()
    current_run_message_id = uuid.uuid4()
    previously_known_message_id = uuid.uuid4()
    prior_run_id = uuid.uuid4()
    causal_ai_message_id = uuid.uuid4()
    edited_ai_message_id = uuid.uuid4()
    causal_outcome_id = uuid.uuid4()
    edited_outcome_id = uuid.uuid4()
    ticket = SimpleNamespace(
        id=ticket_id,
        requeue_requested=True,
        requeue_trigger="ticket_content",
        requeue_source_message_id=queued_message_id,
    )
    turn = SimpleNamespace(ai_run_id=run_id)
    messages = [
        SimpleNamespace(
            id=accepted_message_id,
            ticket_id=ticket_id,
            body_text="Already accepted.",
            body_markdown=None,
            ai_run_id=None,
        ),
        SimpleNamespace(
            id=receipted_message_id,
            ticket_id=ticket_id,
            body_text="Already represented by a receipt.",
            body_markdown=None,
            ai_run_id=None,
        ),
        SimpleNamespace(
            id=current_run_message_id,
            ticket_id=ticket_id,
            body_text="AI output from this turn.",
            body_markdown=None,
            ai_run_id=run_id,
        ),
        SimpleNamespace(
            id=previously_known_message_id,
            ticket_id=ticket_id,
            body_text="Already included in an earlier turn.",
            body_markdown=None,
            ai_run_id=None,
        ),
        SimpleNamespace(
            id=causal_ai_message_id,
            ticket_id=ticket_id,
            body_text="Exact prior AI note.",
            body_markdown="Exact prior AI note.",
            author_type="ai",
            source="ai_internal_note",
            ai_run_id=prior_run_id,
            codex_turn_outcome_id=causal_outcome_id,
        ),
        SimpleNamespace(
            id=edited_ai_message_id,
            ticket_id=ticket_id,
            body_text="Edited by an operator.",
            body_markdown="Edited by an operator.",
            author_type="ai",
            source="ai_draft_published",
            ai_run_id=prior_run_id,
            codex_turn_outcome_id=edited_outcome_id,
        ),
        SimpleNamespace(
            id=dormant_message_id,
            ticket_id=ticket_id,
            body_text="Dormant future context.",
            body_markdown=None,
            ai_run_id=None,
        ),
        SimpleNamespace(
            id=queued_message_id,
            ticket_id=ticket_id,
            body_text="Queued successor context.",
            body_markdown=None,
            ai_run_id=None,
        ),
    ]
    message_by_id = {message.id: message for message in messages}

    class ScalarResult:
        def __init__(self, items):
            self.items = items

        def scalars(self):
            return iter(self.items)

    class Db:
        def get(self, model, key):
            return message_by_id.get(key)

        def execute(self, statement):
            return ScalarResult(messages)

    events = routes_ops._load_delivery_events_for_turn_detail(
        Db(),
        ticket=ticket,
        turn=turn,
        inputs=[SimpleNamespace(source_kind="ticket_message", source_id=accepted_message_id)],
        receipts=[SimpleNamespace(source_kind="ticket_message", source_id=receipted_message_id)],
        known_inputs=KnownConversationInputs(
            dedupe_keys=frozenset(),
            causal_ai_run_ids=frozenset({prior_run_id}),
            causal_outcome_payloads={
                causal_outcome_id: {"internal_note_markdown": "Exact prior AI note."},
                edited_outcome_id: {
                    "edited": True,
                    "published_body_markdown": "Edited by an operator.",
                    "original_draft_body_markdown": "Original draft.",
                },
            },
            outcome_ai_run_ids={causal_outcome_id: prior_run_id, edited_outcome_id: prior_run_id},
            ticket_message_ids=frozenset({previously_known_message_id}),
        ),
    )

    states_by_source_id = {event["source_id"]: event["delivery_state"] for event in events}
    assert states_by_source_id == {
        queued_message_id: "queued_another_run",
        dormant_message_id: "waiting_future_context",
        edited_ai_message_id: "waiting_future_context",
    }
    assert all(event["event_kind"] == "ticket_message" for event in events)


class _RouteDb:
    def __init__(self):
        self.commit_calls = 0
        self.rollback_calls = 0

    def commit(self):
        self.commit_calls += 1

    def rollback(self):
        self.rollback_calls += 1


def test_requester_cannot_access_ops_routes():
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    requester = SimpleNamespace(id=uuid.uuid4(), display_name="Requester", role="requester", is_active=True)

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["auth"].get_current_user] = lambda: requester
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: SimpleNamespace(csrf_token="csrf")

    with stack["TestClient"](app) as client:
        response = client.get("/ops")

    assert response.status_code == 403


def test_requester_cannot_access_ops_board():
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    requester = SimpleNamespace(id=uuid.uuid4(), display_name="Requester", role="requester", is_active=True)

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["auth"].get_current_user] = lambda: requester
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: SimpleNamespace(csrf_token="csrf")

    with stack["TestClient"](app) as client:
        response = client.get("/ops/board")

    assert response.status_code == 403


def test_requester_cannot_access_ops_persistent_turn_detail():
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    requester = SimpleNamespace(id=uuid.uuid4(), display_name="Requester", role="requester", is_active=True)

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["auth"].get_current_user] = lambda: requester
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: SimpleNamespace(csrf_token="csrf")

    with stack["TestClient"](app) as client:
        response = client.get(f"/ops/tickets/T-000999/persistent-turns/{uuid.uuid4()}")

    assert response.status_code == 403


def test_requester_cannot_access_ops_ticket_detail():
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    requester = SimpleNamespace(id=uuid.uuid4(), display_name="Requester", role="requester", is_active=True)

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["auth"].get_current_user] = lambda: requester
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: SimpleNamespace(csrf_token="csrf")

    with stack["TestClient"](app) as client:
        response = client.get("/ops/tickets/T-000999")

    assert response.status_code == 403


@pytest.mark.parametrize(
    ("role", "admin_edit_visible", "slack_column_visible"),
    [
        ("dev_ti", False, False),
        ("admin", True, True),
    ],
)
def test_ops_users_page_lists_table_rows_with_role_scoped_actions(monkeypatch, role, admin_edit_visible, slack_column_visible):
    from app.i18n import translate
    from shared.config import get_default_ui_locale

    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    current_user = SimpleNamespace(id=uuid.uuid4(), display_name=role.upper(), role=role, is_active=True)
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    manageable_user = SimpleNamespace(
        id=uuid.uuid4(),
        email="existing@example.com",
        display_name="Existing User",
        role="requester",
        slack_user_id="U123456",
        is_active=True,
    )
    admin_user = SimpleNamespace(
        id=uuid.uuid4(),
        email="admin@example.com",
        display_name="Admin User",
        role="admin",
        slack_user_id="UADMIN",
        is_active=True,
    )
    users = [manageable_user, admin_user]
    locale = get_default_ui_locale()

    monkeypatch.setattr(stack["routes_ops"], "_load_users_for_admin", lambda db: users)

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["auth"].get_current_user] = lambda: current_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session

    with stack["TestClient"](app) as client:
        response = client.get("/ops/users")

    assert response.status_code == 200
    assert '<table class="table ops-users__table">' in response.text
    assert "Existing User" in response.text
    assert '/ops/users?create=1' in response.text
    assert f'/ops/users?edit_user={manageable_user.id}' in response.text
    assert f'/ops/users/{manageable_user.id}/set-active' in response.text
    assert (f'/ops/users?edit_user={admin_user.id}' in response.text) is admin_edit_visible
    assert f'/ops/users/{admin_user.id}/set-active' not in response.text
    assert f'/ops/users/{manageable_user.id}/update' not in response.text
    assert translate(locale, "button.edit") in response.text
    assert (translate(locale, "field.slack_user_id") in response.text) is slack_column_visible
    assert ("U123456" in response.text) is slack_column_visible
    assert db.commit_calls == 1


@pytest.mark.parametrize(
    ("role", "expected_options"),
    [
        ("dev_ti", ("requester",)),
        ("admin", ("requester", "dev_ti")),
    ],
)
def test_ops_users_create_panel_uses_role_scoped_options(monkeypatch, role, expected_options):
    from app.i18n import translate, user_role_label
    from shared.config import get_default_ui_locale

    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    current_user = SimpleNamespace(id=uuid.uuid4(), display_name=role.upper(), role=role, is_active=True)
    auth_session = SimpleNamespace(csrf_token="csrf-token")

    monkeypatch.setattr(stack["routes_ops"], "_load_users_for_admin", lambda db: [])
    locale = get_default_ui_locale()

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["auth"].get_current_user] = lambda: current_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session

    with stack["TestClient"](app) as client:
        response = client.get("/ops/users?create=1")

    assert response.status_code == 200
    assert translate(locale, "ops.users.create_heading") in response.text
    for role_value in expected_options:
        assert f'<option value="{role_value}">{user_role_label(role_value, locale)}</option>' in response.text
    assert f'<option value="admin">{user_role_label("admin", locale)}</option>' not in response.text
    if role == "dev_ti":
        assert f'<option value="dev_ti">{user_role_label("dev_ti", locale)}</option>' not in response.text
        assert 'name="slack_user_id"' not in response.text
    else:
        assert 'name="slack_user_id"' in response.text
    assert translate(locale, "button.cancel") in response.text
    assert db.commit_calls == 1


def test_ops_users_page_opens_selected_inline_editor(monkeypatch):
    from app.i18n import translate
    from shared.config import get_default_ui_locale

    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    current_user = SimpleNamespace(id=uuid.uuid4(), display_name="ADMIN", role="admin", is_active=True)
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    editable_user = SimpleNamespace(
        id=uuid.uuid4(),
        email="editable@example.com",
        display_name="Editable User",
        role="dev_ti",
        slack_user_id="UEDITABLE",
        is_active=True,
    )
    other_user = SimpleNamespace(
        id=uuid.uuid4(),
        email="other@example.com",
        display_name="Other User",
        role="requester",
        slack_user_id=None,
        is_active=True,
    )
    locale = get_default_ui_locale()

    monkeypatch.setattr(stack["routes_ops"], "_load_users_for_admin", lambda db: [editable_user, other_user])

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["auth"].get_current_user] = lambda: current_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session

    with stack["TestClient"](app) as client:
        response = client.get(f"/ops/users?edit_user={editable_user.id}")

    assert response.status_code == 200
    assert f'/ops/users/{editable_user.id}/update' in response.text
    assert f'/ops/users/{other_user.id}/update' not in response.text
    assert translate(locale, "field.new_password") in response.text
    assert f'value="{editable_user.slack_user_id}"' in response.text
    assert translate(locale, "button.save_changes") in response.text
    assert db.commit_calls == 1


def test_requester_cannot_access_ops_users_page():
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    requester = SimpleNamespace(id=uuid.uuid4(), display_name="Requester", role="requester", is_active=True)

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["auth"].get_current_user] = lambda: requester
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: SimpleNamespace(csrf_token="csrf")

    with stack["TestClient"](app) as client:
        response = client.get("/ops/users")

    assert response.status_code == 403


def test_dev_ti_cannot_access_admin_slack_integration_page():
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    dev_ti_user = SimpleNamespace(id=uuid.uuid4(), display_name="Dev", role="dev_ti", is_active=True)

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["auth"].get_current_user] = lambda: dev_ti_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: SimpleNamespace(csrf_token="csrf")
    app.dependency_overrides[stack["routes_ops"].get_settings] = _make_settings

    with stack["TestClient"](app) as client:
        response = client.get("/ops/integrations/slack")

    assert response.status_code == 403


def test_admin_slack_integration_page_shows_metadata_and_guidance(monkeypatch):
    from shared.config import SlackSettings
    from shared.slack_dm import SlackDeliveryHealthSnapshot

    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    admin_user = SimpleNamespace(id=uuid.uuid4(), display_name="Admin", role="admin", is_active=True)
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    updater = SimpleNamespace(id=uuid.uuid4(), display_name="Config Admin")
    db.get = lambda model, key: updater if key == updater.id else None

    monkeypatch.setattr(
        stack["routes_ops"],
        "load_slack_dm_settings",
        lambda db, app_settings: SlackSettings(
            enabled=True,
            notify_ticket_created=True,
            notify_public_message_added=False,
            notify_status_changed=True,
            has_stored_token=True,
            team_id="T123",
            team_name="Acme Workspace",
            bot_user_id="UBOT123",
            validated_at=datetime(2026, 4, 10, 20, 5, tzinfo=timezone.utc),
            updated_by_user_id=updater.id,
            updated_at=datetime(2026, 4, 10, 20, 10, tzinfo=timezone.utc),
            is_valid=False,
            config_error_code="slack_bot_token_undecryptable",
            config_error_summary="Stored Slack bot token could not be decrypted with APP_SECRET_KEY",
            routing_mode="dm",
        ),
    )
    monkeypatch.setattr(
        stack["routes_ops"],
        "load_slack_delivery_health",
        lambda db: SlackDeliveryHealthSnapshot(
            status="invalid_config",
            checked_at="2026-04-10T20:15:00Z",
            error_code="invalid_auth",
            summary="Slack auth.test returned invalid_auth",
        ),
    )
    monkeypatch.setattr(
        stack["routes_ops"],
        "load_slack_user_sync_state",
        lambda db: SimpleNamespace(
            status="succeeded",
            checked_at="2026-04-10T20:18:00Z",
            error_code=None,
            summary="Matched 2 user(s) by email, updated 2, left 1 unmatched, and skipped 0 conflict(s).",
            matched_count=2,
            updated_count=2,
            no_match_count=1,
            conflict_count=0,
        ),
    )

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["auth"].get_current_user] = lambda: admin_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session
    app.dependency_overrides[stack["routes_ops"].get_settings] = _make_settings

    with stack["TestClient"](app) as client:
        response = client.get("/ops/integrations/slack", headers={"Accept-Language": "en"})

    assert response.status_code == 200
    assert "Slack integration" in response.text
    assert "Acme Workspace" in response.text
    assert "Config Admin" in response.text
    assert "slack_bot_token_undecryptable" in response.text
    assert "Slack auth.test returned invalid_auth" in response.text
    assert "auth.test" in response.text
    assert "conversations.open" in response.text
    assert "chat.postMessage" in response.text
    assert "users.list" in response.text
    assert "Matched 2 user(s) by email, updated 2, left 1 unmatched, and skipped 0 conflict(s)." in response.text
    assert 'name="bot_token"' in response.text
    assert db.commit_calls == 1


def test_admin_slack_integration_save_with_new_token_calls_auth_test_and_persists_metadata(monkeypatch):
    from shared.config import SlackSettings
    from shared.slack_dm import SlackWebApiResponse

    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    admin_user = SimpleNamespace(id=uuid.uuid4(), display_name="Admin", role="admin", is_active=True)
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    observed = {}

    monkeypatch.setattr(
        stack["routes_ops"],
        "load_slack_dm_settings",
        lambda db, app_settings: SlackSettings(routing_mode="dm"),
    )

    def fake_auth_test(*, bot_token: str, timeout_seconds: int):
        observed["auth_test"] = {"bot_token": bot_token, "timeout_seconds": timeout_seconds}
        return SlackWebApiResponse(
            method="auth.test",
            http_status=200,
            body_json={"ok": True, "team_id": "T123", "team": "Acme Workspace", "user_id": "UBOT123"},
        )

    def fake_upsert(db, *, app_settings, values, updated_by_user_id=None, auth_result=None, updated_at=None):
        observed["upsert"] = {
            "values": values,
            "updated_by_user_id": updated_by_user_id,
            "auth_result": auth_result,
        }

    monkeypatch.setattr(stack["routes_ops"], "slack_api_auth_test", fake_auth_test)
    monkeypatch.setattr(stack["routes_ops"], "upsert_slack_dm_settings", fake_upsert)
    monkeypatch.setattr(
        stack["routes_ops"],
        "request_slack_user_sync",
        lambda db, *, trigger, requested_by_user_id=None, updated_at=None: observed.update(
            {
                "sync_request": {
                    "trigger": trigger,
                    "requested_by_user_id": requested_by_user_id,
                }
            }
        ),
    )

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["auth"].get_current_user] = lambda: admin_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session
    app.dependency_overrides[stack["routes_ops"].get_settings] = _make_settings

    with stack["TestClient"](app) as client:
        response = client.post(
            "/ops/integrations/slack",
            data={
                "csrf_token": "csrf-token",
                "enabled": "on",
                "bot_token": " xoxb-new-token ",
                "notify_ticket_created": "on",
                "notify_public_message_added": "",
                "notify_status_changed": "on",
                "message_preview_max_chars": "240",
                "http_timeout_seconds": "12",
                "delivery_batch_size": "6",
                "delivery_max_attempts": "4",
                "delivery_stale_lock_seconds": "90",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/ops/integrations/slack"
    assert observed["auth_test"] == {"bot_token": "xoxb-new-token", "timeout_seconds": 12}
    assert observed["upsert"]["values"].bot_token == "xoxb-new-token"
    assert observed["upsert"]["values"].enabled is True
    assert observed["upsert"]["auth_result"].team_id == "T123"
    assert observed["upsert"]["updated_by_user_id"] == admin_user.id
    assert observed["sync_request"] == {"trigger": "settings_saved", "requested_by_user_id": admin_user.id}
    assert db.commit_calls == 1
    assert db.rollback_calls == 0


def test_admin_slack_integration_save_preserves_existing_token_on_blank_input(monkeypatch):
    from shared.config import SlackSettings

    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    admin_user = SimpleNamespace(id=uuid.uuid4(), display_name="Admin", role="admin", is_active=True)
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    observed = {"auth_test_calls": 0}

    monkeypatch.setattr(
        stack["routes_ops"],
        "load_slack_dm_settings",
        lambda db, app_settings: SlackSettings(enabled=False, has_stored_token=True, routing_mode="dm"),
    )
    monkeypatch.setattr(
        stack["routes_ops"],
        "slack_api_auth_test",
        lambda **kwargs: observed.__setitem__("auth_test_calls", observed["auth_test_calls"] + 1),
    )
    monkeypatch.setattr(
        stack["routes_ops"],
        "upsert_slack_dm_settings",
        lambda db, *, app_settings, values, updated_by_user_id=None, auth_result=None, updated_at=None: observed.update(
            {
                "values": values,
                "updated_by_user_id": updated_by_user_id,
                "auth_result": auth_result,
            }
        ),
    )
    monkeypatch.setattr(
        stack["routes_ops"],
        "request_slack_user_sync",
        lambda db, *, trigger, requested_by_user_id=None, updated_at=None: observed.update(
            {
                "sync_request": {
                    "trigger": trigger,
                    "requested_by_user_id": requested_by_user_id,
                }
            }
        ),
    )

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["auth"].get_current_user] = lambda: admin_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session
    app.dependency_overrides[stack["routes_ops"].get_settings] = _make_settings

    with stack["TestClient"](app) as client:
        response = client.post(
            "/ops/integrations/slack",
            data={
                "csrf_token": "csrf-token",
                "enabled": "on",
                "bot_token": "",
                "notify_ticket_created": "on",
                "notify_public_message_added": "on",
                "notify_status_changed": "",
                "message_preview_max_chars": "200",
                "http_timeout_seconds": "10",
                "delivery_batch_size": "10",
                "delivery_max_attempts": "5",
                "delivery_stale_lock_seconds": "120",
            },
            headers={"Accept-Language": "en"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert observed["auth_test_calls"] == 0
    assert observed["values"].bot_token is None
    assert observed["auth_result"] is None
    assert observed["updated_by_user_id"] == admin_user.id
    assert observed["sync_request"] == {"trigger": "settings_saved", "requested_by_user_id": admin_user.id}
    assert db.commit_calls == 1
    assert db.rollback_calls == 0


def test_admin_slack_integration_save_error_does_not_echo_token(monkeypatch):
    from shared.config import SlackSettings
    from shared.slack_dm import SlackWebApiResponse

    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    admin_user = SimpleNamespace(id=uuid.uuid4(), display_name="Admin", role="admin", is_active=True)
    auth_session = SimpleNamespace(csrf_token="csrf-token")

    monkeypatch.setattr(
        stack["routes_ops"],
        "load_slack_dm_settings",
        lambda db, app_settings: SlackSettings(routing_mode="dm"),
    )
    monkeypatch.setattr(
        stack["routes_ops"],
        "slack_api_auth_test",
        lambda **kwargs: SlackWebApiResponse(
            method="auth.test",
            http_status=200,
            body_json={"ok": False, "error": "invalid_auth"},
        ),
    )

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["auth"].get_current_user] = lambda: admin_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session
    app.dependency_overrides[stack["routes_ops"].get_settings] = _make_settings

    with stack["TestClient"](app) as client:
        response = client.post(
            "/ops/integrations/slack",
            data={
                "csrf_token": "csrf-token",
                "enabled": "on",
                "bot_token": "xoxb-sensitive-token",
                "notify_ticket_created": "on",
                "notify_public_message_added": "on",
                "notify_status_changed": "on",
                "message_preview_max_chars": "200",
                "http_timeout_seconds": "10",
                "delivery_batch_size": "10",
                "delivery_max_attempts": "5",
                "delivery_stale_lock_seconds": "120",
            },
            headers={"Accept-Language": "en"},
            follow_redirects=False,
        )

    assert response.status_code == 400
    assert "Slack auth.test failed: invalid_auth" in response.text
    assert "xoxb-sensitive-token" not in response.text
    assert 'action="/ops/integrations/slack"' in response.text
    assert db.commit_calls == 0
    assert db.rollback_calls == 1


def test_admin_slack_integration_disconnect_clears_token(monkeypatch):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    admin_user = SimpleNamespace(id=uuid.uuid4(), display_name="Admin", role="admin", is_active=True)
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    observed = {}

    monkeypatch.setattr(
        stack["routes_ops"],
        "clear_slack_dm_token",
        lambda db, *, updated_by_user_id=None, updated_at=None: observed.update({"updated_by_user_id": updated_by_user_id}),
    )

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["auth"].get_current_user] = lambda: admin_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session
    app.dependency_overrides[stack["routes_ops"].get_settings] = _make_settings

    with stack["TestClient"](app) as client:
        response = client.post(
            "/ops/integrations/slack/disconnect",
            data={"csrf_token": "csrf-token"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/ops/integrations/slack"
    assert observed["updated_by_user_id"] == admin_user.id
    assert db.commit_calls == 1
    assert db.rollback_calls == 0


@pytest.mark.parametrize(
    ("actor_role", "target_role", "expected_status"),
    [
        ("admin", "requester", 303),
        ("admin", "dev_ti", 303),
        ("admin", "admin", 403),
        ("dev_ti", "requester", 303),
        ("dev_ti", "dev_ti", 403),
        ("dev_ti", "admin", 403),
    ],
)
def test_ops_user_creation_role_matrix(monkeypatch, actor_role, target_role, expected_status):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    current_user = SimpleNamespace(id=uuid.uuid4(), display_name=actor_role.upper(), role=actor_role, is_active=True)
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    created = []

    def fake_create_user(db, *, email, display_name, password, role, slack_user_id=None):
        created.append(
            {
                "email": email,
                "display_name": display_name,
                "password": password,
                "role": role,
                "slack_user_id": slack_user_id,
            }
        )
        return SimpleNamespace(email=email, display_name=display_name, role=role, slack_user_id=slack_user_id)

    monkeypatch.setattr(stack["routes_ops"], "create_user", fake_create_user)

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["auth"].get_current_user] = lambda: current_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session

    with stack["TestClient"](app) as client:
        response = client.post(
            "/ops/users/create",
            data={
                "csrf_token": "csrf-token",
                "email": "new.user@example.com",
                "display_name": "New User",
                "password": "supersecret",
                "role": target_role,
            },
            follow_redirects=False,
        )

    assert response.status_code == expected_status
    if expected_status == 303:
        assert response.headers["location"] == "/ops/users"
        assert created == [
            {
                "email": "new.user@example.com",
                "display_name": "New User",
                "password": "supersecret",
                "role": target_role,
                "slack_user_id": None,
            }
        ]
        assert db.commit_calls == 1
        assert db.rollback_calls == 0
    else:
        assert created == []
        assert db.commit_calls == 0
        assert db.rollback_calls == 0


def test_requester_cannot_post_ops_user_creation():
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    requester = SimpleNamespace(id=uuid.uuid4(), display_name="Requester", role="requester", is_active=True)

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["auth"].get_current_user] = lambda: requester
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: SimpleNamespace(csrf_token="csrf-token")

    with stack["TestClient"](app) as client:
        response = client.post(
            "/ops/users/create",
            data={
                "csrf_token": "csrf-token",
                "email": "blocked@example.com",
                "display_name": "Blocked User",
                "password": "supersecret",
                "role": "requester",
            },
            follow_redirects=False,
        )

    assert response.status_code == 403
    assert db.commit_calls == 0
    assert db.rollback_calls == 0


def test_dev_ti_cannot_submit_slack_user_id_on_user_creation(monkeypatch):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    current_user = SimpleNamespace(id=uuid.uuid4(), display_name="DEV", role="dev_ti", is_active=True)
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    created = []

    monkeypatch.setattr(
        stack["routes_ops"],
        "create_user",
        lambda *args, **kwargs: created.append(kwargs),
    )

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["auth"].get_current_user] = lambda: current_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session

    with stack["TestClient"](app) as client:
        response = client.post(
            "/ops/users/create",
            data={
                "csrf_token": "csrf-token",
                "email": "new.user@example.com",
                "display_name": "New User",
                "password": "supersecret",
                "role": "requester",
                "slack_user_id": "UFORBIDDEN",
            },
            follow_redirects=False,
        )

    assert response.status_code == 403
    assert created == []
    assert db.commit_calls == 0
    assert db.rollback_calls == 0


def test_ops_user_creation_validation_error_keeps_users_page_context(monkeypatch):
    from app.i18n import translate, translate_error_text, user_role_label
    from shared.config import get_default_ui_locale

    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    current_user = SimpleNamespace(id=uuid.uuid4(), display_name="ADMIN", role="admin", is_active=True)
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    users = [
        SimpleNamespace(
            id=uuid.uuid4(),
            email="existing@example.com",
            display_name="Existing User",
            role="dev_ti",
            is_active=True,
        )
    ]

    def fail_create_user(db, *, email, display_name, password, role, slack_user_id=None):
        raise ValueError("User already exists: existing@example.com")

    monkeypatch.setattr(stack["routes_ops"], "create_user", fail_create_user)
    monkeypatch.setattr(stack["routes_ops"], "_load_users_for_admin", lambda db: users)
    locale = get_default_ui_locale()

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["auth"].get_current_user] = lambda: current_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session

    with stack["TestClient"](app) as client:
        response = client.post(
            "/ops/users/create",
            data={
                "csrf_token": "csrf-token",
                "email": "existing@example.com",
                "display_name": "Existing User",
                "password": "supersecret",
                "role": "dev_ti",
            },
            follow_redirects=False,
        )

    assert response.status_code == 400
    assert translate_error_text("User already exists: existing@example.com", locale) in response.text
    assert "Existing User" in response.text
    assert translate(locale, "ops.users.create_heading") in response.text
    assert 'action="/ops/users/create"' in response.text
    assert f'<option value="dev_ti">{user_role_label("dev_ti", locale)}</option>' in response.text
    assert db.commit_calls == 0
    assert db.rollback_calls == 1


def test_admin_user_creation_without_slack_id_requests_sync_when_slack_is_configured(monkeypatch):
    from shared.config import SlackSettings

    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    db.info = {"settings": _make_settings()}
    current_user = SimpleNamespace(id=uuid.uuid4(), display_name="ADMIN", role="admin", is_active=True)
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    observed = {}

    monkeypatch.setattr(
        stack["routes_ops"],
        "create_user",
        lambda db, **kwargs: SimpleNamespace(**kwargs),
    )
    monkeypatch.setattr(
        stack["routes_ops"],
        "load_slack_dm_settings",
        lambda db, app_settings: SlackSettings(has_stored_token=True, routing_mode="dm"),
    )
    monkeypatch.setattr(
        stack["routes_ops"],
        "request_slack_user_sync",
        lambda db, *, trigger, requested_by_user_id=None, updated_at=None: observed.update(
            {"trigger": trigger, "requested_by_user_id": requested_by_user_id}
        ),
    )

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["auth"].get_current_user] = lambda: current_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session

    with stack["TestClient"](app) as client:
        response = client.post(
            "/ops/users/create",
            data={
                "csrf_token": "csrf-token",
                "email": "new.user@example.com",
                "display_name": "New User",
                "password": "supersecret",
                "role": "requester",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert observed == {"trigger": "user_created", "requested_by_user_id": current_user.id}
    assert db.commit_calls == 1
    assert db.rollback_calls == 0


def test_admin_user_creation_with_blank_slack_id_requests_sync_when_slack_is_configured(monkeypatch):
    from shared.config import SlackSettings

    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    db.info = {"settings": _make_settings()}
    current_user = SimpleNamespace(id=uuid.uuid4(), display_name="ADMIN", role="admin", is_active=True)
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    observed = {}

    monkeypatch.setattr(
        stack["routes_ops"],
        "create_user",
        lambda db, **kwargs: SimpleNamespace(**kwargs),
    )
    monkeypatch.setattr(
        stack["routes_ops"],
        "load_slack_dm_settings",
        lambda db, app_settings: SlackSettings(has_stored_token=True, routing_mode="dm"),
    )
    monkeypatch.setattr(
        stack["routes_ops"],
        "request_slack_user_sync",
        lambda db, *, trigger, requested_by_user_id=None, updated_at=None: observed.update(
            {"trigger": trigger, "requested_by_user_id": requested_by_user_id}
        ),
    )

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["auth"].get_current_user] = lambda: current_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session

    with stack["TestClient"](app) as client:
        response = client.post(
            "/ops/users/create",
            data={
                "csrf_token": "csrf-token",
                "email": "new.user@example.com",
                "display_name": "New User",
                "password": "supersecret",
                "role": "requester",
                "slack_user_id": "",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert observed == {"trigger": "user_created", "requested_by_user_id": current_user.id}
    assert db.commit_calls == 1
    assert db.rollback_calls == 0


@pytest.mark.parametrize(
    ("actor_role", "target_role", "requested_role", "expected_status"),
    [
        ("admin", "requester", "requester", 303),
        ("admin", "requester", "dev_ti", 303),
        ("admin", "dev_ti", "requester", 303),
        ("admin", "dev_ti", "dev_ti", 303),
        ("admin", "admin", "admin", 303),
        ("admin", "admin", "requester", 403),
        ("dev_ti", "requester", "requester", 303),
        ("dev_ti", "dev_ti", "dev_ti", 303),
        ("dev_ti", "requester", "dev_ti", 403),
        ("dev_ti", "dev_ti", "requester", 403),
    ],
)
def test_ops_user_update_role_matrix(monkeypatch, actor_role, target_role, requested_role, expected_status):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    current_user = SimpleNamespace(id=uuid.uuid4(), display_name=actor_role.upper(), role=actor_role, is_active=True)
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    target_user = SimpleNamespace(
        id=uuid.uuid4(),
        email="existing@example.com",
        display_name="Existing User",
        role=target_role,
        slack_user_id="UEXISTING",
        is_active=True,
    )
    updated = []

    monkeypatch.setattr(stack["routes_ops"], "_load_user_or_404", lambda db, user_id: target_user)
    monkeypatch.setattr(
        stack["routes_ops"],
        "update_user",
        lambda db, *, user, display_name, role, slack_user_id, password=None: updated.append(
            {
                "user": user,
                "display_name": display_name,
                "role": role,
                "slack_user_id": slack_user_id,
                "password": password,
            }
        ),
    )

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["auth"].get_current_user] = lambda: current_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session

    with stack["TestClient"](app) as client:
        response = client.post(
            f"/ops/users/{target_user.id}/update",
            data={
                "csrf_token": "csrf-token",
                "display_name": "Updated User",
                "password": "new-password-123",
                "role": requested_role,
            },
            follow_redirects=False,
        )

    assert response.status_code == expected_status
    if expected_status == 303:
        assert response.headers["location"] == "/ops/users"
        assert updated == [
            {
                "user": target_user,
                "display_name": "Updated User",
                "role": requested_role,
                "slack_user_id": target_user.slack_user_id,
                "password": "new-password-123",
            }
        ]
        assert db.commit_calls == 1
        assert db.rollback_calls == 0
    else:
        assert updated == []
        assert db.commit_calls == 0
        assert db.rollback_calls == 0


def test_ops_user_update_validation_error_keeps_users_page_context(monkeypatch):
    from app.i18n import translate, translate_error_text
    from shared.config import get_default_ui_locale

    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    current_user = SimpleNamespace(id=uuid.uuid4(), display_name="ADMIN", role="admin", is_active=True)
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    target_user = SimpleNamespace(
        id=uuid.uuid4(),
        email="existing@example.com",
        display_name="Existing User",
        role="dev_ti",
        slack_user_id="UEXISTING",
        is_active=True,
    )
    locale = get_default_ui_locale()

    monkeypatch.setattr(stack["routes_ops"], "_load_user_or_404", lambda db, user_id: target_user)
    monkeypatch.setattr(stack["routes_ops"], "_load_users_for_admin", lambda db: [target_user])
    monkeypatch.setattr(stack["routes_ops"], "update_user", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("Display name is required.")))

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["auth"].get_current_user] = lambda: current_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session

    with stack["TestClient"](app) as client:
        response = client.post(
            f"/ops/users/{target_user.id}/update",
            data={
                "csrf_token": "csrf-token",
                "display_name": "   ",
                "password": "",
                "role": "dev_ti",
            },
            follow_redirects=False,
        )

    assert response.status_code == 400
    assert translate_error_text("Display name is required.", locale) in response.text
    assert translate(locale, "ops.users.current_heading") in response.text
    assert "Existing User" in response.text
    assert f'action="/ops/users/{target_user.id}/update"' in response.text
    assert db.commit_calls == 0
    assert db.rollback_calls == 1


def test_admin_user_update_without_slack_id_requests_sync_when_slack_is_configured(monkeypatch):
    from shared.config import SlackSettings

    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    db.info = {"settings": _make_settings()}
    current_user = SimpleNamespace(id=uuid.uuid4(), display_name="ADMIN", role="admin", is_active=True)
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    target_user = SimpleNamespace(
        id=uuid.uuid4(),
        email="existing@example.com",
        display_name="Existing User",
        role="requester",
        slack_user_id=None,
        is_active=True,
    )
    observed = {}

    monkeypatch.setattr(stack["routes_ops"], "_load_user_or_404", lambda db, user_id: target_user)
    monkeypatch.setattr(
        stack["routes_ops"],
        "update_user",
        lambda db, *, user, display_name, role, slack_user_id, password=None: SimpleNamespace(
            user=user,
            display_name=display_name,
            role=role,
            slack_user_id=slack_user_id,
            password=password,
        ),
    )
    monkeypatch.setattr(
        stack["routes_ops"],
        "load_slack_dm_settings",
        lambda db, app_settings: SlackSettings(has_stored_token=True, routing_mode="dm"),
    )
    monkeypatch.setattr(
        stack["routes_ops"],
        "request_slack_user_sync",
        lambda db, *, trigger, requested_by_user_id=None, updated_at=None: observed.update(
            {"trigger": trigger, "requested_by_user_id": requested_by_user_id}
        ),
    )

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["auth"].get_current_user] = lambda: current_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session

    with stack["TestClient"](app) as client:
        response = client.post(
            f"/ops/users/{target_user.id}/update",
            data={
                "csrf_token": "csrf-token",
                "display_name": "Updated User",
                "password": "",
                "role": "requester",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert observed == {"trigger": "user_updated", "requested_by_user_id": current_user.id}
    assert db.commit_calls == 1
    assert db.rollback_calls == 0


def test_admin_user_update_with_blank_slack_id_requests_sync_when_slack_is_configured(monkeypatch):
    from shared.config import SlackSettings

    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    db.info = {"settings": _make_settings()}
    current_user = SimpleNamespace(id=uuid.uuid4(), display_name="ADMIN", role="admin", is_active=True)
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    target_user = SimpleNamespace(
        id=uuid.uuid4(),
        email="existing@example.com",
        display_name="Existing User",
        role="requester",
        slack_user_id="UEXISTING",
        is_active=True,
    )
    observed = {}

    monkeypatch.setattr(stack["routes_ops"], "_load_user_or_404", lambda db, user_id: target_user)
    monkeypatch.setattr(
        stack["routes_ops"],
        "update_user",
        lambda db, *, user, display_name, role, slack_user_id, password=None: SimpleNamespace(
            user=user,
            display_name=display_name,
            role=role,
            slack_user_id=slack_user_id,
            password=password,
        ),
    )
    monkeypatch.setattr(
        stack["routes_ops"],
        "load_slack_dm_settings",
        lambda db, app_settings: SlackSettings(has_stored_token=True, routing_mode="dm"),
    )
    monkeypatch.setattr(
        stack["routes_ops"],
        "request_slack_user_sync",
        lambda db, *, trigger, requested_by_user_id=None, updated_at=None: observed.update(
            {"trigger": trigger, "requested_by_user_id": requested_by_user_id}
        ),
    )

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["auth"].get_current_user] = lambda: current_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session

    with stack["TestClient"](app) as client:
        response = client.post(
            f"/ops/users/{target_user.id}/update",
            data={
                "csrf_token": "csrf-token",
                "display_name": "Updated User",
                "password": "",
                "role": "requester",
                "slack_user_id": " ",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert observed == {"trigger": "user_updated", "requested_by_user_id": current_user.id}
    assert db.commit_calls == 1
    assert db.rollback_calls == 0


def test_dev_ti_cannot_submit_slack_user_id_on_user_update(monkeypatch):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    current_user = SimpleNamespace(id=uuid.uuid4(), display_name="DEV", role="dev_ti", is_active=True)
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    target_user = SimpleNamespace(
        id=uuid.uuid4(),
        email="existing@example.com",
        display_name="Existing User",
        role="requester",
        slack_user_id="UCURRENT",
        is_active=True,
    )
    updated = []

    monkeypatch.setattr(stack["routes_ops"], "_load_user_or_404", lambda db, user_id: target_user)
    monkeypatch.setattr(
        stack["routes_ops"],
        "update_user",
        lambda *args, **kwargs: updated.append(kwargs),
    )

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["auth"].get_current_user] = lambda: current_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session

    with stack["TestClient"](app) as client:
        response = client.post(
            f"/ops/users/{target_user.id}/update",
            data={
                "csrf_token": "csrf-token",
                "display_name": "Updated User",
                "password": "",
                "role": "requester",
                "slack_user_id": "UFORBIDDEN",
            },
            follow_redirects=False,
        )

    assert response.status_code == 403
    assert updated == []
    assert db.commit_calls == 0
    assert db.rollback_calls == 0


@pytest.mark.parametrize(
    ("actor_role", "target_role", "is_active_value", "expected_status"),
    [
        ("admin", "requester", "0", 303),
        ("admin", "dev_ti", "1", 303),
        ("admin", "admin", "0", 403),
        ("dev_ti", "requester", "0", 303),
        ("dev_ti", "dev_ti", "0", 303),
    ],
)
def test_ops_set_user_active_role_matrix(monkeypatch, actor_role, target_role, is_active_value, expected_status):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    current_user = SimpleNamespace(id=uuid.uuid4(), display_name=actor_role.upper(), role=actor_role, is_active=True)
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    target_user = SimpleNamespace(
        id=uuid.uuid4(),
        email="existing@example.com",
        display_name="Existing User",
        role=target_role,
        is_active=True,
    )
    observed = []

    monkeypatch.setattr(stack["routes_ops"], "_load_user_or_404", lambda db, user_id: target_user)
    monkeypatch.setattr(
        stack["routes_ops"],
        "set_user_active_state",
        lambda db, *, user, is_active: observed.append({"user": user, "is_active": is_active}),
    )

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["auth"].get_current_user] = lambda: current_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session

    with stack["TestClient"](app) as client:
        response = client.post(
            f"/ops/users/{target_user.id}/set-active",
            data={"csrf_token": "csrf-token", "is_active": is_active_value},
            follow_redirects=False,
        )

    assert response.status_code == expected_status
    if expected_status == 303:
        assert response.headers["location"] == "/ops/users"
        assert observed == [{"user": target_user, "is_active": is_active_value == "1"}]
        assert db.commit_calls == 1
        assert db.rollback_calls == 0
    else:
        assert observed == []
        assert db.commit_calls == 0
        assert db.rollback_calls == 0


def test_ops_set_user_active_rejects_invalid_flag(monkeypatch):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    current_user = SimpleNamespace(id=uuid.uuid4(), display_name="ADMIN", role="admin", is_active=True)
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    target_user = SimpleNamespace(
        id=uuid.uuid4(),
        email="existing@example.com",
        display_name="Existing User",
        role="requester",
        is_active=True,
    )
    observed = []

    monkeypatch.setattr(stack["routes_ops"], "_load_user_or_404", lambda db, user_id: target_user)
    monkeypatch.setattr(
        stack["routes_ops"],
        "set_user_active_state",
        lambda db, *, user, is_active: observed.append({"user": user, "is_active": is_active}),
    )

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["auth"].get_current_user] = lambda: current_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session

    with stack["TestClient"](app) as client:
        response = client.post(
            f"/ops/users/{target_user.id}/set-active",
            data={"csrf_token": "csrf-token", "is_active": "maybe"},
            follow_redirects=False,
        )

    assert response.status_code == 400
    assert observed == []
    assert db.commit_calls == 0
    assert db.rollback_calls == 0


def test_ops_list_route_does_not_mark_ticket_as_read(monkeypatch):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    ops_user = SimpleNamespace(id=uuid.uuid4(), display_name="Ops", role="dev_ti")
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    observed = {"view_updates": 0}

    monkeypatch.setattr(
        stack["routes_ops"],
        "_ops_filter_context",
        lambda *args, **kwargs: {
            "rows": [],
            "grouped_rows": {key: [] for key in ("new", "ai_triage", "waiting_on_user", "waiting_on_dev_ti", "resolved")},
            "filters": {
                "status": "",
                "route_target_id": "",
                "assigned_to": "",
                "urgent": False,
                "unassigned_only": False,
                "created_by_me": False,
                "needs_approval": False,
                "updated_since_viewed": False,
            },
            "ops_users": [],
            "status_options": [],
            "route_target_options": [],
        },
    )
    monkeypatch.setattr(
        stack["routes_ops"],
        "upsert_ticket_view",
        lambda *args, **kwargs: observed.__setitem__("view_updates", observed["view_updates"] + 1),
    )

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_ops"].require_ops_user] = lambda: ops_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session

    with stack["TestClient"](app) as client:
        response = client.get("/ops")
        fragment = client.get("/ops?status=new", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "<html" in response.text
    assert 'id="ops-results"' in response.text
    assert fragment.status_code == 200
    assert "<html" not in fragment.text
    assert 'id="ops-results"' in fragment.text
    assert observed["view_updates"] == 0
    assert db.commit_calls == 2


def test_ops_board_route_does_not_mark_ticket_as_read(monkeypatch):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    ops_user = SimpleNamespace(id=uuid.uuid4(), display_name="Ops", role="dev_ti")
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    observed = {"view_updates": 0}

    monkeypatch.setattr(
        stack["routes_ops"],
        "_ops_filter_context",
        lambda *args, **kwargs: {
            "rows": [],
            "grouped_rows": {key: [] for key in ("new", "ai_triage", "waiting_on_user", "waiting_on_dev_ti", "resolved")},
            "filters": {
                "status": "",
                "route_target_id": "",
                "assigned_to": "",
                "urgent": False,
                "unassigned_only": False,
                "created_by_me": False,
                "needs_approval": False,
                "updated_since_viewed": False,
            },
            "ops_users": [],
            "status_options": [],
            "route_target_options": [],
        },
    )
    monkeypatch.setattr(
        stack["routes_ops"],
        "upsert_ticket_view",
        lambda *args, **kwargs: observed.__setitem__("view_updates", observed["view_updates"] + 1),
    )

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_ops"].require_ops_user] = lambda: ops_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session

    with stack["TestClient"](app) as client:
        response = client.get("/ops/board")
        fragment = client.get("/ops/board?status=new", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "<html" in response.text
    assert 'id="ops-results"' in response.text
    assert fragment.status_code == 200
    assert "<html" not in fragment.text
    assert 'id="ops-results"' in fragment.text
    assert observed["view_updates"] == 0
    assert db.commit_calls == 2


def test_ops_detail_route_marks_ticket_as_read(monkeypatch):
    from app.i18n import translate
    from shared.config import get_default_ui_locale

    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    ops_user = SimpleNamespace(id=uuid.uuid4(), display_name="Ops", role="dev_ti")
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    ticket = SimpleNamespace(
        reference="T-000010",
        id=uuid.uuid4(),
        title="Ops ticket",
        status="new",
        urgent=False,
        updated_at=datetime.now(timezone.utc),
    )
    observed = {"view_updates": 0}

    monkeypatch.setattr(stack["routes_ops"], "_load_ops_ticket_or_404", lambda *args, **kwargs: ticket)
    monkeypatch.setattr(
        stack["routes_ops"],
        "_ticket_detail_context",
        lambda *args, **kwargs: {
            "ticket": ticket,
            "route_target_display": {"id": None, "label": "Unclassified", "kind": None},
            "activity_timeline": [],
            "ops_users": [],
            "status_options": [],
            "draft_reply_status_options": [],
            "public_reply_status_options": [],
            "default_public_reply_status": "waiting_on_user",
            "pending_draft": None,
            "pending_draft_html": "",
            "latest_run": None,
            "latest_analysis_run": None,
            "latest_run_steps": [],
            "latest_analysis_steps": [],
            "latest_ai_note": None,
            "latest_ai_note_html": "",
            "analysis_view": {
                "summary_short": "",
                "summary_internal": "",
                "relevant_paths": [],
                "response_confidence": None,
                "risk_level": None,
                "publish_mode_recommendation": None,
                "risk_reason": "",
                "handoff_reason": "",
                "assistant_used": None,
                "assistant_specialist_id": None,
            },
            "ai_relevant_paths": [],
            "ai_summary_short": "",
            "ai_summary_internal": "",
            "creator": None,
            "assignee": None,
            "rerun_specialist_options": [
                {
                    "route_target_id": "software_architect",
                    "route_target_label": "Software Architect",
                    "specialist_id": "software-architect",
                    "specialist_display_name": "Software Architect",
                }
            ],
        },
    )
    monkeypatch.setattr(
        stack["routes_ops"],
        "upsert_ticket_view",
        lambda *args, **kwargs: observed.__setitem__("view_updates", observed["view_updates"] + 1),
    )
    locale = get_default_ui_locale()

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_ops"].require_ops_user] = lambda: ops_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session

    with stack["TestClient"](app) as client:
        response = client.get(f"/ops/tickets/{ticket.reference}")

    assert response.status_code == 200
    assert observed["view_updates"] == 1
    assert db.commit_calls == 1
    assert response.text.count('name="forced_route_target_id"') == 2
    assert translate(locale, "button.use_normal_routing") in response.text
    assert "Software Architect" in response.text


def test_ops_reply_public_allows_forced_specialist_route_target(monkeypatch):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    settings = _make_settings()
    ops_user = SimpleNamespace(id=uuid.uuid4(), display_name="Ops", role="dev_ti")
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    ticket = SimpleNamespace(reference="T-000012R", id=uuid.uuid4())
    observed = {}

    monkeypatch.setattr(stack["routes_ops"], "_load_ops_ticket_or_404", lambda *args, **kwargs: ticket)

    def fake_add_ops_public_reply(
        db,
        *,
        slack_runtime,
        ticket,
        actor,
        body_markdown,
        next_status,
        settings=None,
        attachments=None,
        forced_route_target_id=None,
        forced_specialist_id=None,
    ):
        observed.update(
            {
                "body_markdown": body_markdown,
                "next_status": next_status,
                "settings": settings,
                "attachments": attachments,
                "forced_route_target_id": forced_route_target_id,
                "forced_specialist_id": forced_specialist_id,
            }
        )
        return SimpleNamespace(), []

    monkeypatch.setattr(stack["routes_ops"], "add_ops_public_reply", fake_add_ops_public_reply)

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_ops"].require_ops_user] = lambda: ops_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session
    app.dependency_overrides[stack["routes_ops"].get_settings] = lambda: settings

    with stack["TestClient"](app) as client:
        response = client.post(
            f"/ops/tickets/{ticket.reference}/reply-public",
            data={
                "csrf_token": "csrf-token",
                "body": "Please retry the AI with architecture context.",
                "next_status": "ai_triage",
                "forced_route_target_id": "software_architect",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == f"/ops/tickets/{ticket.reference}"
    assert observed == {
        "body_markdown": "Please retry the AI with architecture context.",
        "next_status": "ai_triage",
        "settings": settings,
        "attachments": [],
        "forced_route_target_id": "software_architect",
        "forced_specialist_id": "software-architect",
    }
    assert db.commit_calls == 1


def test_ops_reply_public_accepts_attachment_uploads(monkeypatch, tmp_path):
    stack = _load_web_stack()
    db = _RouteDb()
    settings = _make_settings()
    ops_user = SimpleNamespace(id=uuid.uuid4(), display_name="Ops", role="dev_ti")
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    ticket = SimpleNamespace(reference="T-000012U", id=uuid.uuid4())
    persisted_path = tmp_path / "attachments_store" / "ticket" / "reply.pdf"
    upload = SimpleNamespace(
        original_filename="reply.pdf",
        mime_type="application/pdf",
        sha256="pdf123",
        size_bytes=8,
        width=None,
        height=None,
        data=b"fake pdf",
    )
    observed = {}

    monkeypatch.setattr(stack["routes_ops"], "_load_ops_ticket_or_404", lambda *args, **kwargs: ticket)

    async def fake_parse_ops_public_reply_form(request, *, settings):
        return "Please review the attached report.", "csrf-token", "waiting_on_user", "", [upload]

    monkeypatch.setattr(stack["routes_ops"], "_parse_ops_public_reply_form", fake_parse_ops_public_reply_form)

    def fake_add_ops_public_reply(
        db,
        *,
        slack_runtime,
        ticket,
        actor,
        body_markdown,
        next_status,
        settings=None,
        attachments=None,
        forced_route_target_id=None,
        forced_specialist_id=None,
    ):
        observed.update(
            {
                "body_markdown": body_markdown,
                "next_status": next_status,
                "attachments": attachments,
                "forced_route_target_id": forced_route_target_id,
                "forced_specialist_id": forced_specialist_id,
            }
        )
        return SimpleNamespace(), [SimpleNamespace(stored_path=str(persisted_path))]

    monkeypatch.setattr(stack["routes_ops"], "add_ops_public_reply", fake_add_ops_public_reply)

    response = asyncio.run(
        stack["routes_ops"].ops_reply_public(
            ticket.reference,
            SimpleNamespace(),
            current_user=ops_user,
            auth_session=auth_session,
            settings=settings,
            db=db,
        )
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/ops/tickets/{ticket.reference}"
    assert observed["body_markdown"] == "Please review the attached report."
    assert observed["next_status"] == "waiting_on_user"
    assert observed["forced_route_target_id"] is None
    assert observed["forced_specialist_id"] is None
    assert [attachment.original_filename for attachment in observed["attachments"]] == ["reply.pdf"]
    assert [attachment.mime_type for attachment in observed["attachments"]] == ["application/pdf"]
    assert persisted_path.read_bytes() == b"fake pdf"
    assert db.commit_calls == 1


def test_ops_reply_public_rejects_invalid_csrf_before_ticket_lookup(monkeypatch):
    stack = _load_web_stack()
    db = _RouteDb()
    settings = _make_settings()
    ops_user = SimpleNamespace(id=uuid.uuid4(), display_name="Ops", role="dev_ti")
    auth_session = SimpleNamespace(csrf_token="csrf-token")

    async def fake_parse_ops_public_reply_form(request, *, settings):
        return "Please review the attached report.", "wrong-token", "waiting_on_user", "", []

    monkeypatch.setattr(stack["routes_ops"], "_parse_ops_public_reply_form", fake_parse_ops_public_reply_form)
    monkeypatch.setattr(
        stack["routes_ops"],
        "_load_ops_ticket_or_404",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("ticket lookup should not run")),
    )

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            stack["routes_ops"].ops_reply_public(
                "T-000012C",
                SimpleNamespace(),
                current_user=ops_user,
                auth_session=auth_session,
                settings=settings,
                db=db,
            )
        )

    assert exc_info.value.status_code == 403
    assert db.commit_calls == 0


def test_parse_ops_public_reply_form_accepts_multipart_attachment(monkeypatch):
    stack = _load_web_stack()
    settings = _make_settings()
    boundary = "----autosacboundary"

    def form_field(name: str, value: str) -> bytes:
        return (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"{name}\"\r\n"
            "\r\n"
            f"{value}\r\n"
        ).encode()

    def file_field(name: str, filename: str, content_type: str, data: bytes) -> bytes:
        return (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {content_type}\r\n"
            "\r\n"
        ).encode() + data + b"\r\n"

    body = b"".join(
        [
            form_field("csrf_token", "csrf-token"),
            form_field("body", "Please see the attached report."),
            form_field("next_status", "waiting_on_user"),
            form_field("forced_route_target_id", ""),
            file_field("attachments", "reply.txt", "text/plain", b"hello"),
            f"--{boundary}--\r\n".encode(),
        ]
    )
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    from starlette.requests import Request

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/ops/tickets/T-000012P/reply-public",
            "headers": [
                (b"content-type", f"multipart/form-data; boundary={boundary}".encode()),
                (b"content-length", str(len(body)).encode()),
            ],
        },
        receive,
    )
    observed = {}

    async def fake_validate_attachment_upload(upload, settings):
        observed["filename"] = upload.filename
        observed["content_type"] = upload.content_type
        observed["data"] = await upload.read()
        return SimpleNamespace(
            original_filename=upload.filename,
            mime_type=upload.content_type,
            sha256="sha-text",
            size_bytes=len(observed["data"]),
            width=None,
            height=None,
            data=observed["data"],
        )

    monkeypatch.setattr(stack["routes_ops"], "validate_attachment_upload", fake_validate_attachment_upload)

    body_markdown, csrf_token, next_status, forced_route_target_id, attachments = asyncio.run(
        stack["routes_ops"]._parse_ops_public_reply_form(request, settings=settings)
    )

    assert body_markdown == "Please see the attached report."
    assert csrf_token == "csrf-token"
    assert next_status == "waiting_on_user"
    assert forced_route_target_id == ""
    assert observed == {"filename": "reply.txt", "content_type": "text/plain", "data": b"hello"}
    assert [attachment.original_filename for attachment in attachments] == ["reply.txt"]


def test_ops_reply_public_rejects_invalid_forced_route_target(monkeypatch):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    settings = _make_settings()
    ops_user = SimpleNamespace(id=uuid.uuid4(), display_name="Ops", role="dev_ti")
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    ticket = SimpleNamespace(reference="T-000012S", id=uuid.uuid4())

    monkeypatch.setattr(stack["routes_ops"], "_load_ops_ticket_or_404", lambda *args, **kwargs: ticket)

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_ops"].require_ops_user] = lambda: ops_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session
    app.dependency_overrides[stack["routes_ops"].get_settings] = lambda: settings

    with stack["TestClient"](app) as client:
        response = client.post(
            f"/ops/tickets/{ticket.reference}/reply-public",
            data={
                "csrf_token": "csrf-token",
                "body": "Please retry.",
                "next_status": "ai_triage",
                "forced_route_target_id": "manual_review",
            },
            follow_redirects=False,
        )

    assert response.status_code == 400


def test_ops_set_ticket_status_ai_triage_triggers_manual_rerun(monkeypatch):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    settings = _make_settings()
    ops_user = SimpleNamespace(id=uuid.uuid4(), display_name="Ops", role="dev_ti")
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    ticket = SimpleNamespace(reference="T-000012", id=uuid.uuid4())
    observed = {"next_status": None}

    monkeypatch.setattr(stack["routes_ops"], "_load_ops_ticket_or_404", lambda *args, **kwargs: ticket)
    monkeypatch.setattr(
        stack["routes_ops"],
        "set_ticket_status_for_ops",
        lambda db, slack_runtime, ticket, actor, next_status, note=None: observed.__setitem__("next_status", next_status),
    )

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_ops"].require_ops_user] = lambda: ops_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session
    app.dependency_overrides[stack["routes_ops"].get_settings] = lambda: settings

    with stack["TestClient"](app) as client:
        response = client.post(
            f"/ops/tickets/{ticket.reference}/set-status",
            data={"csrf_token": "csrf-token", "next_status": "ai_triage"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == f"/ops/tickets/{ticket.reference}"
    assert observed["next_status"] == "ai_triage"
    assert db.commit_calls == 1


def test_ops_rerun_ai_allows_forced_specialist_route_target(monkeypatch):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    settings = _make_settings()
    ops_user = SimpleNamespace(id=uuid.uuid4(), display_name="Ops", role="dev_ti")
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    ticket = SimpleNamespace(reference="T-000012A", id=uuid.uuid4())
    observed = {}

    monkeypatch.setattr(stack["routes_ops"], "_load_ops_ticket_or_404", lambda *args, **kwargs: ticket)
    monkeypatch.setattr(
        stack["routes_ops"],
        "request_manual_rerun",
        lambda db, slack_runtime, ticket, actor, forced_route_target_id=None, forced_specialist_id=None: observed.update(
            {
                "forced_route_target_id": forced_route_target_id,
                "forced_specialist_id": forced_specialist_id,
            }
        ),
    )

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_ops"].require_ops_user] = lambda: ops_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session
    app.dependency_overrides[stack["routes_ops"].get_settings] = lambda: settings

    with stack["TestClient"](app) as client:
        response = client.post(
            f"/ops/tickets/{ticket.reference}/rerun-ai",
            data={"csrf_token": "csrf-token", "forced_route_target_id": "software_architect"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == f"/ops/tickets/{ticket.reference}"
    assert observed == {
        "forced_route_target_id": "software_architect",
        "forced_specialist_id": "software-architect",
    }
    assert db.commit_calls == 1


def test_ops_rerun_ai_rejects_invalid_forced_route_target(monkeypatch):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    settings = _make_settings()
    ops_user = SimpleNamespace(id=uuid.uuid4(), display_name="Ops", role="dev_ti")
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    ticket = SimpleNamespace(reference="T-000012B", id=uuid.uuid4())

    monkeypatch.setattr(stack["routes_ops"], "_load_ops_ticket_or_404", lambda *args, **kwargs: ticket)

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_ops"].require_ops_user] = lambda: ops_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session
    app.dependency_overrides[stack["routes_ops"].get_settings] = lambda: settings

    with stack["TestClient"](app) as client:
        response = client.post(
            f"/ops/tickets/{ticket.reference}/rerun-ai",
            data={"csrf_token": "csrf-token", "forced_route_target_id": "manual_review"},
            follow_redirects=False,
        )

    assert response.status_code == 400


def test_ops_detail_route_separates_analysis_artifacts_from_latest_run(monkeypatch):
    from app.i18n import translate
    from shared.config import get_default_ui_locale

    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    ops_user = SimpleNamespace(id=uuid.uuid4(), display_name="Ops", role="dev_ti")
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    ticket = SimpleNamespace(
        reference="T-000011",
        id=uuid.uuid4(),
        title="Ops ticket",
        status="waiting_on_user",
        urgent=False,
        updated_at=datetime.now(timezone.utc),
        route_target_id="support",
        ai_confidence=0.9,
        impact_level="low",
        development_needed=False,
        requester_language="en",
        last_ai_action="draft_public_reply",
        requeue_requested=False,
    )
    latest_run = SimpleNamespace(
        id=uuid.uuid4(),
        status="failed",
        error_text="boom",
    )
    latest_analysis_run = SimpleNamespace(
        id=uuid.uuid4(),
        status="human_review",
    )
    latest_run_steps = [
        SimpleNamespace(
            step_index=1,
            step_kind="router",
            agent_spec_id="router",
            status="succeeded",
            prompt_path="/tmp/latest-router-prompt.txt",
            schema_path="/tmp/latest-router-schema.json",
            final_output_path="/tmp/latest-router-final.json",
            stdout_jsonl_path="/tmp/latest-router-stdout.jsonl",
            stderr_path="/tmp/latest-router-stderr.txt",
        )
    ]
    latest_analysis_steps = [
        SimpleNamespace(
            step_index=1,
            step_kind="router",
            agent_spec_id="router",
            status="succeeded",
            prompt_path="/tmp/analysis-router-prompt.txt",
            schema_path="/tmp/analysis-router-schema.json",
            final_output_path="/tmp/analysis-router-final.json",
            stdout_jsonl_path="/tmp/analysis-router-stdout.jsonl",
            stderr_path="/tmp/analysis-router-stderr.txt",
        ),
        SimpleNamespace(
            step_index=2,
            step_kind="specialist",
            agent_spec_id="support",
            status="succeeded",
            prompt_path="/tmp/analysis-support-prompt.txt",
            schema_path="/tmp/analysis-support-schema.json",
            final_output_path="/tmp/analysis-support-final.json",
            stdout_jsonl_path="/tmp/analysis-support-stdout.jsonl",
            stderr_path="/tmp/analysis-support-stderr.txt",
        ),
    ]

    monkeypatch.setattr(stack["routes_ops"], "_load_ops_ticket_or_404", lambda *args, **kwargs: ticket)
    monkeypatch.setattr(
        stack["routes_ops"],
        "_ticket_detail_context",
        lambda *args, **kwargs: {
            "ticket": ticket,
            "route_target_display": {"id": "support", "label": "Support", "kind": "direct_ai"},
            "creator": None,
            "assignee": None,
            "activity_timeline": [],
            "ops_users": [],
            "status_options": [],
            "draft_reply_status_options": [],
            "public_reply_status_options": [],
            "default_public_reply_status": "waiting_on_user",
            "pending_draft": None,
            "pending_draft_html": "",
            "latest_run": latest_run,
            "latest_analysis_run": latest_analysis_run,
            "latest_run_steps": latest_run_steps,
            "latest_analysis_steps": latest_analysis_steps,
            "latest_ai_note": None,
            "latest_ai_note_html": "",
            "analysis_view": {
                "summary_short": "",
                "summary_internal": "Accepted internal summary",
                "relevant_paths": [],
                "response_confidence": "high",
                "risk_level": "low",
                "publish_mode_recommendation": "draft_for_human",
                "risk_reason": "Needs human review before sending.",
                "handoff_reason": "",
                "assistant_used": None,
                "assistant_specialist_id": None,
            },
            "ai_relevant_paths": [],
            "ai_summary_short": "Accepted analysis",
            "ai_summary_internal": "Accepted internal summary",
        },
    )
    monkeypatch.setattr(stack["routes_ops"], "upsert_ticket_view", lambda *args, **kwargs: None)
    locale = get_default_ui_locale()

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_ops"].require_ops_user] = lambda: ops_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session

    with stack["TestClient"](app) as client:
        response = client.get(f"/ops/tickets/{ticket.reference}")

    assert response.status_code == 200
    assert translate(locale, "ops.detail.analysis_steps") in response.text
    assert "/tmp/analysis-support-final.json" in response.text
    assert translate(locale, "ops.detail.latest_run_steps") in response.text
    assert "/tmp/latest-router-final.json" in response.text


def test_ops_detail_route_renders_running_run_worker_metadata(monkeypatch):
    from app.i18n import translate
    from shared.config import get_default_ui_locale

    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    ops_user = SimpleNamespace(id=uuid.uuid4(), display_name="Ops", role="dev_ti")
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    started_at = datetime(2026, 4, 8, 12, 0, tzinfo=timezone.utc)
    last_heartbeat_at = started_at + timedelta(minutes=2)
    ticket = SimpleNamespace(
        reference="T-000012",
        id=uuid.uuid4(),
        title="Running run metadata",
        status="ai_triage",
        urgent=False,
        updated_at=datetime.now(timezone.utc),
        route_target_id="support",
        ai_confidence=None,
        impact_level=None,
        development_needed=None,
        requester_language=None,
        last_ai_action=None,
        requeue_requested=False,
    )
    latest_run = SimpleNamespace(
        id=uuid.uuid4(),
        status="running",
        started_at=started_at,
        worker_pid=9876,
        last_heartbeat_at=last_heartbeat_at,
        recovery_attempt_count=2,
        error_text=None,
    )

    monkeypatch.setattr(stack["routes_ops"], "_load_ops_ticket_or_404", lambda *args, **kwargs: ticket)
    monkeypatch.setattr(
        stack["routes_ops"],
        "_ticket_detail_context",
        lambda *args, **kwargs: {
            "ticket": ticket,
            "route_target_display": {"id": "support", "label": "Support", "kind": "direct_ai"},
            "creator": None,
            "assignee": None,
            "activity_timeline": [],
            "ops_users": [],
            "status_options": [],
            "draft_reply_status_options": [],
            "public_reply_status_options": [],
            "default_public_reply_status": "waiting_on_user",
            "pending_draft": None,
            "pending_draft_html": "",
            "latest_run": latest_run,
            "latest_analysis_run": None,
            "latest_run_steps": [],
            "latest_analysis_steps": [],
            "latest_ai_note": None,
            "latest_ai_note_html": "",
            "analysis_view": {
                "summary_short": "",
                "summary_internal": "",
                "relevant_paths": [],
                "response_confidence": None,
                "risk_level": None,
                "publish_mode_recommendation": None,
                "risk_reason": "",
                "handoff_reason": "",
                "assistant_used": None,
                "assistant_specialist_id": None,
            },
            "ai_relevant_paths": [],
            "ai_summary_short": "",
            "ai_summary_internal": "",
        },
    )
    monkeypatch.setattr(stack["routes_ops"], "upsert_ticket_view", lambda *args, **kwargs: None)
    locale = get_default_ui_locale()

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_ops"].require_ops_user] = lambda: ops_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session

    with stack["TestClient"](app) as client:
        response = client.get(f"/ops/tickets/{ticket.reference}")

    assert response.status_code == 200
    assert translate(locale, "ops.detail.worker_pid") in response.text
    assert "9876" in response.text
    assert translate(locale, "ops.detail.last_heartbeat") in response.text
    assert translate(locale, "ops.detail.recovery_attempts") in response.text


def test_ops_detail_route_renders_persistent_turn_history_when_enabled(monkeypatch):
    from app.i18n import translate
    from shared.config import get_default_ui_locale

    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    settings = Settings(**{**_make_settings().__dict__, "codex_conversations_enabled": True})
    ops_user = SimpleNamespace(id=uuid.uuid4(), display_name="Ops", role="dev_ti")
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    turn_id = uuid.uuid4()
    ticket = SimpleNamespace(
        reference="T-000014",
        id=uuid.uuid4(),
        title="Persistent history",
        status="waiting_on_dev_ti",
        urgent=False,
        updated_at=datetime.now(timezone.utc),
        requester_language="pt-BR",
        last_ai_action=None,
        requeue_requested=False,
    )

    monkeypatch.setattr(stack["routes_ops"], "_load_ops_ticket_or_404", lambda *args, **kwargs: ticket)
    monkeypatch.setattr(
        stack["routes_ops"],
        "_ticket_detail_context",
        lambda *args, **kwargs: {
            "ticket": ticket,
            "route_target_display": {"id": "support", "label": "Support", "kind": "direct_ai"},
            "creator": None,
            "assignee": None,
            "activity_timeline": [],
            "ops_users": [],
            "status_options": [],
            "draft_reply_status_options": [],
            "public_reply_status_options": [],
            "default_public_reply_status": "waiting_on_user",
            "pending_draft": None,
            "pending_draft_html": "",
            "latest_run": None,
            "latest_analysis_run": None,
            "latest_run_steps": [],
            "latest_analysis_steps": [],
            "latest_ai_note": None,
            "latest_ai_note_html": "",
            "analysis_view": {
                "summary_short": "",
                "summary_internal": "",
                "relevant_paths": [],
                "response_confidence": None,
                "risk_level": None,
                "publish_mode_recommendation": None,
                "risk_reason": "",
                "handoff_reason": "",
                "assistant_used": None,
                "assistant_specialist_id": None,
            },
            "ai_relevant_paths": [],
            "ai_summary_short": "",
            "ai_summary_internal": "",
            "rerun_specialist_options": [],
            "persistent_visibility_enabled": True,
            "persistent_conversation": {
                "conversation": {
                    "status": "active",
                    "turn_count": 1,
                    "session_count": 2,
                    "active_session_segment_index": 2,
                    "active_thread_id": "thread-123",
                    "lease_owner_run_id": uuid.uuid4(),
                },
                "turns": [
                    {
                        "turn_id": turn_id,
                        "turn_index": 2,
                        "status": "completed",
                        "specialist_display_name": "Support Specialist",
                        "latest_outcome_kind": "published_with_edits",
                        "triggered_by": "manual_rerun",
                        "route_target_display": {"label": "Support"},
                        "output_contract": "specialist_result",
                        "session_segment_index": 2,
                        "session_thread_id": "thread-123",
                        "structured_result": {
                            "summary_short_excerpt": "",
                            "summary_internal_excerpt": "Draft updated after review.",
                            "public_reply_excerpt": "Original generated reply.",
                        },
                        "publication": {
                            "state": "edited_before_publish",
                            "generated_excerpt": "Original generated reply.",
                            "published_excerpt": "Edited published reply.",
                        },
                        "artifact_paths": ["/tmp/persistent/final.json"],
                        "recovery_marker_keys": ["ops.detail.recovery_marker.replacement_session_segment"],
                        "outcome_count": 3,
                        "raw_item_count": 4,
                        "detail_path": f"/ops/tickets/{ticket.reference}/persistent-turns/{turn_id}",
                    }
                ],
            },
        },
    )
    monkeypatch.setattr(stack["routes_ops"], "upsert_ticket_view", lambda *args, **kwargs: None)
    locale = get_default_ui_locale()

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_ops"].require_ops_user] = lambda: ops_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session
    app.dependency_overrides[stack["routes_ops"].get_settings] = lambda: settings

    with stack["TestClient"](app) as client:
        response = client.get(f"/ops/tickets/{ticket.reference}")

    assert response.status_code == 200
    persistent_history_label = translate(locale, "ops.detail.persistent_turn_history")
    disclosure_marker = 'class="analysis-disclosure analysis-disclosure--persistent" data-persistent-turn-history'
    disclosure_marker_index = response.text.index(disclosure_marker)
    disclosure_start = response.text.rindex("<details", 0, disclosure_marker_index)
    disclosure_tag_end = response.text.index(">", disclosure_start)
    assert response.text.count(persistent_history_label) == 1
    assert response.text.index(translate(locale, "ops.detail.more_analysis")) < disclosure_start
    assert " open" not in response.text[disclosure_start:disclosure_tag_end]
    assert translate(locale, "button.inspect_turn") in response.text
    assert "Support Specialist" in response.text
    assert "/tmp/persistent/final.json" in response.text
    assert f"/ops/tickets/{ticket.reference}/persistent-turns/{turn_id}" in response.text


def test_ops_persistent_turn_detail_route_renders_outcomes_and_raw_items(monkeypatch):
    from app.i18n import translate
    from shared.config import get_default_ui_locale

    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    settings = Settings(**{**_make_settings().__dict__, "codex_conversations_enabled": True})
    ops_user = SimpleNamespace(id=uuid.uuid4(), display_name="Ops", role="dev_ti")
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    turn_id = uuid.uuid4()
    ticket = SimpleNamespace(
        reference="T-000015",
        id=uuid.uuid4(),
        title="Persistent turn detail",
        status="waiting_on_dev_ti",
        urgent=False,
        updated_at=datetime.now(timezone.utc),
    )

    monkeypatch.setattr(stack["routes_ops"], "_load_ops_ticket_or_404", lambda *args, **kwargs: ticket)
    monkeypatch.setattr(
        stack["routes_ops"],
        "_load_ops_persistent_turn_detail_or_404",
        lambda *args, **kwargs: {
            "conversation": {
                "status": "active",
                "turn_count": 2,
                "session_count": 2,
            },
            "turn": {
                "turn_id": turn_id,
                "turn_index": 2,
                "status": "completed",
                "specialist_display_name": "Support Specialist",
                "latest_outcome_kind": "completed",
                "triggered_by": "manual_rerun",
                "route_target_display": {"label": "Support"},
                "output_contract": "specialist_result",
                "session_status": "active",
                "session_segment_index": 2,
                "session_thread_id": "thread-123",
                "transport_kind": "app_server",
                "native_turn_id": "native-turn-2",
                "steering_closed_at": datetime(2026, 8, 24, 12, 4, tzinfo=timezone.utc),
                "effective_input_hash": "effective-input-hash",
                "steering_receipt_count": 2,
                "ambiguous_blocker_count": 1,
                "lease_owner_run_id": uuid.uuid4(),
                "lease_worker_instance_id": "worker-a",
                "lease_expires_at": datetime(2026, 8, 24, 12, 5, tzinfo=timezone.utc),
                "structured_result": {
                    "summary_short_excerpt": "",
                    "summary_internal_excerpt": "Stored internal summary.",
                    "internal_note_excerpt": "Internal note excerpt.",
                },
                "publication": {
                    "state": "unpublished",
                    "generated_excerpt": "Requester-facing draft.",
                    "published_excerpt": "",
                },
                "recovery_marker_keys": ["ops.detail.recovery_marker.replacement_session_segment"],
                "artifact_paths": ["/tmp/persistent/stdout.jsonl"],
                "delivery_events": [
                    {
                        "event_kind": "ticket_message",
                        "source_kind": "ticket_message",
                        "source_id": uuid.uuid4(),
                        "dedupe_key": "ticket-message:included",
                        "delivery_state": "included_active_turn",
                        "delivery_state_label_key": "ops.detail.delivery_state.included_active_turn",
                        "payload_excerpt": "Included content.",
                    },
                    {
                        "event_kind": "ticket_message",
                        "source_kind": "ticket_message",
                        "source_id": uuid.uuid4(),
                        "dedupe_key": "ticket-message:dormant",
                        "delivery_state": "waiting_future_context",
                        "delivery_state_label_key": "ops.detail.delivery_state.waiting_future_context",
                        "payload_excerpt": "Dormant content.",
                    },
                    {
                        "event_kind": "ticket_message",
                        "source_kind": "ticket_message",
                        "source_id": uuid.uuid4(),
                        "dedupe_key": "ticket-message:queued",
                        "delivery_state": "queued_another_run",
                        "delivery_state_label_key": "ops.detail.delivery_state.queued_another_run",
                        "payload_excerpt": "Queued content.",
                    },
                ],
                "steering_receipts": [
                    {
                        "status": "ambiguous",
                        "delivery_state": "delivery_uncertain",
                        "delivery_state_label_key": "ops.detail.delivery_state.delivery_uncertain",
                        "source_kind": "ticket_message",
                        "source_id": uuid.uuid4(),
                        "dedupe_key": "ticket-message:uncertain",
                        "expected_native_turn_id": "native-turn-2",
                        "rpc_request_id": "autosac-5",
                        "payload_hash": "receipt-hash",
                        "attempted_at": datetime(2026, 8, 24, 12, 3, tzinfo=timezone.utc),
                        "acknowledged_at": None,
                        "commit_to_ack_latency_ms": None,
                        "error_code": "stale_run_recovery",
                        "error_text": "Delivery became ambiguous.",
                        "payload_json_pretty": '{\n  "body_text": "uncertain"\n}',
                    }
                ],
                "outcomes": [
                    {
                        "outcome_index": 1,
                        "outcome_kind": "completed",
                        "created_at": datetime(2026, 8, 24, 12, 4, tzinfo=timezone.utc),
                        "payload_json_pretty": '{\n  "output_json": {\n    "summary_internal": "Stored internal summary."\n  }\n}',
                    }
                ],
                "items": [
                    {
                        "item_index": 1,
                        "item_kind": "thread.started",
                        "codex_item_id": "evt-1",
                        "created_at": datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc),
                        "payload_json_pretty": '{\n  "thread_id": "thread-123",\n  "type": "thread.started"\n}',
                    }
                ],
            },
        },
    )
    monkeypatch.setattr(stack["routes_ops"], "upsert_ticket_view", lambda *args, **kwargs: None)
    locale = get_default_ui_locale()

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_ops"].require_ops_user] = lambda: ops_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session
    app.dependency_overrides[stack["routes_ops"].get_settings] = lambda: settings

    with stack["TestClient"](app) as client:
        response = client.get(f"/ops/tickets/{ticket.reference}/persistent-turns/{turn_id}")

    assert response.status_code == 200
    assert translate(locale, "ops.detail.outcome_history") in response.text
    assert translate(locale, "ops.detail.raw_turn_items") in response.text
    assert translate(locale, "ops.detail.steering_receipt_history") in response.text
    assert translate(locale, "ops.detail.delivery_state.included_active_turn") in response.text
    assert translate(locale, "ops.detail.delivery_state.waiting_future_context") in response.text
    assert translate(locale, "ops.detail.delivery_state.queued_another_run") in response.text
    assert translate(locale, "ops.detail.delivery_state.delivery_uncertain") in response.text
    assert "effective-input-hash" in response.text
    assert "native-turn-2" in response.text
    assert "thread.started" in response.text
    assert "Requester-facing draft." in response.text
    assert "/tmp/persistent/stdout.jsonl" in response.text


def test_ticket_detail_context_uses_latest_accepted_analysis_run(tmp_path, monkeypatch):
    stack = _load_web_stack()
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        created_by_user_id=uuid.uuid4(),
        assigned_to_user_id=None,
    )
    latest_run = SimpleNamespace(status="failed", error_text="boom")
    analysis_run = SimpleNamespace(
        status="human_review",
        id=uuid.uuid4(),
        final_output_contract="triage_result",
        final_output_json={
            "summary_short": "Accepted analysis",
            "summary_internal": "Accepted internal summary",
            "confidence": 0.92,
            "impact_level": "low",
            "development_needed": False,
            "relevant_paths": [{"path": "manuals/", "reason": "Checked first."}],
        },
    )

    class _ContextDb:
        def get(self, model, key):
            return None

    monkeypatch.setattr(stack["routes_ops"], "_build_ops_activity_timeline", lambda *args, **kwargs: [])
    monkeypatch.setattr(stack["routes_ops"], "_load_pending_draft", lambda *args, **kwargs: None)
    monkeypatch.setattr(stack["routes_ops"], "_load_latest_run", lambda *args, **kwargs: latest_run)
    monkeypatch.setattr(stack["routes_ops"], "_load_latest_analysis_run", lambda *args, **kwargs: analysis_run)
    monkeypatch.setattr(stack["routes_ops"], "_load_run_steps", lambda *args, **kwargs: [])
    monkeypatch.setattr(stack["routes_ops"], "_load_latest_internal_ai_note", lambda *args, **kwargs: None)
    monkeypatch.setattr(stack["routes_ops"], "_load_ops_users", lambda *args, **kwargs: [])
    current_user = SimpleNamespace(id=ticket.created_by_user_id, role="admin")

    context = stack["routes_ops"]._ticket_detail_context(_ContextDb(), ticket=ticket, current_user=current_user)

    assert context["latest_run"] is latest_run
    assert context["latest_analysis_run"] is analysis_run
    assert context["latest_analysis_steps"] == []
    assert context["ai_summary_short"] == "Accepted analysis"
    assert context["ai_summary_internal"] == "Accepted internal summary"
    assert context["ai_relevant_paths"] == [{"path": "manuals/", "reason": "Checked first."}]
    assert context["route_target_display"]["label"] == "Unclassified"
    assert context["analysis_view"]["contract_id"] == "triage_result"
    assert context["analysis_view"]["legacy_confidence"] == 0.92
    assert context["analysis_view"]["legacy_impact_level"] == "low"
    assert context["analysis_view"]["legacy_development_needed"] is False
    assert context["public_reply_status_options"][0] == "ai_triage"
    assert context["default_public_reply_status"] == "ai_triage"


def test_ticket_detail_context_uses_last_public_message_for_auto_scroll(monkeypatch):
    stack = _load_web_stack()
    last_public_message_id = str(uuid.uuid4())
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        created_by_user_id=uuid.uuid4(),
        assigned_to_user_id=None,
    )
    timeline = [
        {
            "kind": "message",
            "id": str(uuid.uuid4()),
            "lane": "public",
            "created_at": datetime.now(timezone.utc),
            "author_label": "Requester",
            "body_html": "<p>Original request</p>",
            "attachments": [],
        },
        {
            "kind": "message",
            "id": str(uuid.uuid4()),
            "lane": "internal",
            "created_at": datetime.now(timezone.utc),
            "author_label": "Dev/TI",
            "body_html": "<p>Internal note</p>",
            "attachments": [],
        },
        {
            "kind": "message",
            "id": last_public_message_id,
            "lane": "public",
            "created_at": datetime.now(timezone.utc),
            "author_label": "Requester",
            "body_html": "<p>Follow-up</p>",
            "attachments": [],
        },
        {
            "kind": "status_change",
            "id": str(uuid.uuid4()),
            "created_at": datetime.now(timezone.utc),
            "summary": "AI Triage -> Waiting on User",
            "actor_label": "AI",
        },
    ]

    class _ContextDb:
        def get(self, model, key):
            return None

    monkeypatch.setattr(stack["routes_ops"], "_build_ops_activity_timeline", lambda *args, **kwargs: timeline)
    monkeypatch.setattr(stack["routes_ops"], "_load_pending_draft", lambda *args, **kwargs: None)
    monkeypatch.setattr(stack["routes_ops"], "_load_latest_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(stack["routes_ops"], "_load_latest_analysis_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(stack["routes_ops"], "_load_latest_internal_ai_note", lambda *args, **kwargs: None)
    monkeypatch.setattr(stack["routes_ops"], "_load_ops_users", lambda *args, **kwargs: [])

    context = stack["routes_ops"]._ticket_detail_context(
        _ContextDb(),
        ticket=ticket,
        current_user=SimpleNamespace(id=uuid.uuid4(), role="admin"),
    )

    assert context["activity_timeline"] == timeline
    assert context["auto_scroll_message_id"] == last_public_message_id


def test_ticket_detail_context_loads_persistent_projection_when_enabled(monkeypatch):
    stack = _load_web_stack()
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        created_by_user_id=uuid.uuid4(),
        assigned_to_user_id=None,
    )
    persistent_projection = {
        "conversation": {"status": "active", "turn_count": 1, "session_count": 1},
        "turns": [],
    }

    class _ContextDb:
        def get(self, model, key):
            return None

    monkeypatch.setattr(stack["routes_ops"], "_build_ops_activity_timeline", lambda *args, **kwargs: [])
    monkeypatch.setattr(stack["routes_ops"], "_load_pending_draft", lambda *args, **kwargs: None)
    monkeypatch.setattr(stack["routes_ops"], "_load_latest_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(stack["routes_ops"], "_load_latest_analysis_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(stack["routes_ops"], "_load_latest_internal_ai_note", lambda *args, **kwargs: None)
    monkeypatch.setattr(stack["routes_ops"], "_load_ops_users", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        stack["routes_ops"],
        "_load_persistent_conversation_projection",
        lambda *args, **kwargs: persistent_projection,
    )

    context = stack["routes_ops"]._ticket_detail_context(
        _ContextDb(),
        ticket=ticket,
        current_user=SimpleNamespace(id=uuid.uuid4(), role="admin"),
        persistent_visibility_enabled=True,
    )

    assert context["persistent_visibility_enabled"] is True
    assert context["persistent_conversation"] == persistent_projection


def test_ticket_detail_context_skips_persistent_projection_when_disabled(monkeypatch):
    stack = _load_web_stack()
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        created_by_user_id=uuid.uuid4(),
        assigned_to_user_id=None,
    )

    class _ContextDb:
        def get(self, model, key):
            return None

    monkeypatch.setattr(stack["routes_ops"], "_build_ops_activity_timeline", lambda *args, **kwargs: [])
    monkeypatch.setattr(stack["routes_ops"], "_load_pending_draft", lambda *args, **kwargs: None)
    monkeypatch.setattr(stack["routes_ops"], "_load_latest_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(stack["routes_ops"], "_load_latest_analysis_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(stack["routes_ops"], "_load_latest_internal_ai_note", lambda *args, **kwargs: None)
    monkeypatch.setattr(stack["routes_ops"], "_load_ops_users", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        stack["routes_ops"],
        "_load_persistent_conversation_projection",
        lambda *args, **kwargs: pytest.fail("persistent projection should stay hidden when disabled"),
    )

    context = stack["routes_ops"]._ticket_detail_context(
        _ContextDb(),
        ticket=ticket,
        current_user=SimpleNamespace(id=uuid.uuid4(), role="admin"),
        persistent_visibility_enabled=False,
    )

    assert context["persistent_visibility_enabled"] is False
    assert context["persistent_conversation"] is None


def test_build_ops_activity_timeline_merges_status_changes_after_messages(monkeypatch):
    stack = _load_web_stack()
    ticket_id = uuid.uuid4()
    start = datetime(2026, 4, 2, 12, 0, tzinfo=timezone.utc)
    activity_messages = [
        {
            "kind": "message",
            "id": str(uuid.uuid4()),
            "created_at": start,
            "lane": "public",
            "lane_label": "Public",
            "author_label": "Requester",
            "body_html": "<p>Need help</p>",
            "attachments": [],
        },
        {
            "kind": "message",
            "id": str(uuid.uuid4()),
            "created_at": start + timedelta(minutes=2),
            "lane": "internal",
            "lane_label": "Internal",
            "author_label": "Dev/TI",
            "body_html": "<p>Checking the environment.</p>",
            "attachments": [],
        },
    ]
    history = [
        SimpleNamespace(
            id=uuid.uuid4(),
            created_at=start + timedelta(minutes=2),
            from_status="ai_triage",
            to_status="waiting_on_dev_ti",
            changed_by_type="ai",
        )
    ]

    monkeypatch.setattr(stack["routes_ops"], "_serialize_thread", lambda *args, **kwargs: activity_messages)
    monkeypatch.setattr(stack["routes_ops"], "load_ticket_status_history", lambda *args, **kwargs: history)

    timeline = stack["routes_ops"]._build_ops_activity_timeline(object(), ticket_id=ticket_id)

    assert [item["kind"] for item in timeline] == ["message", "message", "status_change"]
    assert timeline[2]["summary"] == "AI Triage -> Waiting on Dev/TI"
    assert timeline[2]["actor_label"] == "AI"


def test_ticket_detail_context_defaults_public_reply_to_waiting_on_user_for_other_ops_tickets(monkeypatch):
    stack = _load_web_stack()
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        created_by_user_id=uuid.uuid4(),
        assigned_to_user_id=None,
    )

    class _ContextDb:
        def get(self, model, key):
            return None

    monkeypatch.setattr(stack["routes_ops"], "_build_ops_activity_timeline", lambda *args, **kwargs: [])
    monkeypatch.setattr(stack["routes_ops"], "_load_pending_draft", lambda *args, **kwargs: None)
    monkeypatch.setattr(stack["routes_ops"], "_load_latest_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(stack["routes_ops"], "_load_latest_analysis_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(stack["routes_ops"], "_load_latest_internal_ai_note", lambda *args, **kwargs: None)
    monkeypatch.setattr(stack["routes_ops"], "_load_ops_users", lambda *args, **kwargs: [])

    context = stack["routes_ops"]._ticket_detail_context(
        _ContextDb(),
        ticket=ticket,
        current_user=SimpleNamespace(id=uuid.uuid4(), role="admin"),
    )

    assert context["default_public_reply_status"] == "waiting_on_user"


def test_ops_detail_route_renders_ai_triage_as_public_reply_option_for_self_owned_ops_tickets(monkeypatch):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    ops_user = SimpleNamespace(id=uuid.uuid4(), display_name="Admin", role="admin")
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    ticket = SimpleNamespace(
        reference="T-000013",
        id=uuid.uuid4(),
        title="Admin-owned ticket",
        status="waiting_on_dev_ti",
        urgent=False,
        updated_at=datetime.now(timezone.utc),
    )

    monkeypatch.setattr(stack["routes_ops"], "_load_ops_ticket_or_404", lambda *args, **kwargs: ticket)
    monkeypatch.setattr(
        stack["routes_ops"],
        "_ticket_detail_context",
        lambda *args, **kwargs: {
            "ticket": ticket,
            "route_target_display": {"id": None, "label": "Unclassified", "kind": None},
            "creator": ops_user,
            "assignee": None,
            "activity_timeline": [],
            "ops_users": [],
            "status_options": [],
            "draft_reply_status_options": ["waiting_on_user", "waiting_on_dev_ti", "resolved"],
            "public_reply_status_options": ["ai_triage", "waiting_on_user", "waiting_on_dev_ti", "resolved"],
            "default_public_reply_status": "ai_triage",
            "pending_draft": None,
            "pending_draft_html": "",
            "latest_run": None,
            "latest_analysis_run": None,
            "latest_run_steps": [],
            "latest_analysis_steps": [],
            "latest_ai_note": None,
            "latest_ai_note_html": "",
            "analysis_view": {
                "summary_short": "",
                "summary_internal": "",
                "relevant_paths": [],
                "response_confidence": None,
                "risk_level": None,
                "publish_mode_recommendation": None,
                "risk_reason": "",
                "handoff_reason": "",
                "assistant_used": None,
                "assistant_specialist_id": None,
            },
            "ai_relevant_paths": [],
            "ai_summary_short": "",
            "ai_summary_internal": "",
        },
    )
    monkeypatch.setattr(stack["routes_ops"], "upsert_ticket_view", lambda *args, **kwargs: None)

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_ops"].require_ops_user] = lambda: ops_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session

    with stack["TestClient"](app) as client:
        response = client.get(f"/ops/tickets/{ticket.reference}")

    assert response.status_code == 200
    assert '<option value="ai_triage" selected>' in response.text


def test_ops_routes_source_and_templates_keep_internal_and_public_lanes_separate():
    source = Path("app/routes_ops.py").read_text(encoding="utf-8")
    app_css = Path("app/static/app.css").read_text(encoding="utf-8")
    base_template = Path("app/templates/base.html").read_text(encoding="utf-8")
    filters_template = Path("app/templates/ops_filters.html").read_text(encoding="utf-8")
    detail_template = Path("app/templates/ops_ticket_detail.html").read_text(encoding="utf-8")
    persistent_history_template = Path("app/templates/ops_persistent_turn_history.html").read_text(encoding="utf-8")
    board_template = Path("app/templates/ops_board_columns.html").read_text(encoding="utf-8")
    list_template = Path("app/templates/ops_ticket_list.html").read_text(encoding="utf-8")
    rows_template = Path("app/templates/ops_ticket_rows.html").read_text(encoding="utf-8")

    assert '"/ops/tickets/{reference}/reply-public"' in source
    assert '"/ops/tickets/{reference}/note-internal"' in source
    assert '"/ops/tickets/{reference}/assign"' in source
    assert '"/ops/tickets/{reference}/set-status"' in source
    assert '"/ops/tickets/{reference}/rerun-ai"' in source
    assert "_parse_ops_public_reply_form" in source
    assert 'enctype="multipart/form-data"' in detail_template
    assert 'name="attachments"' in detail_template
    assert 't("field.route_ai_to_specialist")' in detail_template
    assert detail_template.count('name="forced_route_target_id"') == 2
    assert 'ops-ticket-detail__main-section' in detail_template
    assert "add_ops_public_reply" in source
    assert "add_ops_internal_note" in source
    assert "assign_ticket_for_ops" in source
    assert "set_ticket_status_for_ops" in source
    assert 't("filters.status")' in filters_template
    assert 't("filters.route_target")' in filters_template
    assert 't("filters.assigned_to")' in filters_template
    assert 'hx-get="{{ filters_action }}"' in filters_template
    assert 'hx-target="#{{ filters_target_id }}"' in filters_template
    assert 'hx-swap="outerHTML"' in filters_template
    assert 't("filters.urgent_only")' in filters_template
    assert 't("filters.unassigned_only")' in filters_template
    assert 't("filters.created_by_me")' in filters_template
    assert 't("filters.needs_approval")' in filters_template
    assert 't("filters.updated_since_viewed")' in filters_template
    assert 't("ops.detail.activity")' in detail_template
    assert 't("ops.detail.ai_analysis")' in detail_template
    assert 'page{% block page_class %}{% endblock %}' in base_template
    assert 'data-auto-scroll-target="true"' in detail_template
    assert 'scrollIntoView({ block: "start" })' in base_template
    assert 'page--ops-ticket-detail' in detail_template
    assert "ops-ticket-detail__layout" in detail_template
    assert "ops-ticket-detail__main" in detail_template
    assert "ops-ticket-detail__sidebar" in detail_template
    assert "ops-ticket-detail__sidebar-section" in detail_template
    assert "lane-pill" in detail_template
    assert "timeline-status" in detail_template
    assert 't("ops.detail.summary")' in detail_template
    assert 't("ops.detail.internal_summary")' in detail_template
    assert '<details class="analysis-disclosure">' in detail_template
    assert 't("ops.detail.more_analysis")' in detail_template
    persistent_include = '{% include "ops_persistent_turn_history.html" %}'
    assert detail_template.count(persistent_include) == 1
    assert (
        detail_template.index("analysis-panel")
        < detail_template.index('t("ops.detail.more_analysis")')
        < detail_template.index(persistent_include)
    )
    assert 'class="analysis-disclosure analysis-disclosure--persistent" data-persistent-turn-history' in detail_template
    persistent_marker_index = detail_template.index("data-persistent-turn-history")
    persistent_tag_start = detail_template.rindex("<details", 0, persistent_marker_index)
    persistent_tag_end = detail_template.index(">", persistent_tag_start)
    assert " open" not in detail_template[persistent_tag_start:persistent_tag_end]
    assert "message-card" not in persistent_history_template
    assert "ops-ticket-detail__turn-card" in persistent_history_template
    assert 't("ops.detail.relevant_paths")' in detail_template
    assert 't("ops.detail.pending_ai_draft")' in detail_template
    assert 't("ops.list.heading")' in list_template
    assert 'id="ops-results"' in rows_template
    assert 'id="ops-results"' in board_template
    assert '/static/htmx.min.js' in base_template
    assert 't("ops.board.pending_draft_approval")' in board_template
    assert ".analysis-disclosure" in app_css
    assert ".analysis-disclosure--persistent .ops-ticket-detail__turn-card" in app_css
    assert ".page--ops-ticket-detail" in app_css
    assert ".ops-ticket-detail__layout" in app_css
