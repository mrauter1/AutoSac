from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
import uuid

import pytest


def _load_symbols():
    pytest.importorskip("sqlalchemy")
    from shared.models import AIDraft, Ticket, TicketMessage, TicketStatusHistory, TicketView, User
    from shared.ticketing import (
        add_ops_internal_note,
        add_ops_public_reply,
        assign_ticket_for_ops,
        process_deferred_requeue,
        publish_ai_draft_for_ops,
        reject_ai_draft_for_ops,
        request_manual_rerun,
        set_ticket_status_for_ops,
    )

    return {
        "AIDraft": AIDraft,
        "Ticket": Ticket,
        "TicketMessage": TicketMessage,
        "TicketStatusHistory": TicketStatusHistory,
        "TicketView": TicketView,
        "User": User,
        "add_ops_internal_note": add_ops_internal_note,
        "add_ops_public_reply": add_ops_public_reply,
        "assign_ticket_for_ops": assign_ticket_for_ops,
        "process_deferred_requeue": process_deferred_requeue,
        "publish_ai_draft_for_ops": publish_ai_draft_for_ops,
        "reject_ai_draft_for_ops": reject_ai_draft_for_ops,
        "request_manual_rerun": request_manual_rerun,
        "set_ticket_status_for_ops": set_ticket_status_for_ops,
    }


class _FakeSession:
    def __init__(self):
        self.added = []
        self.objects = {}
        self.commit_calls = 0
        self.flush_calls = 0

    def add(self, item):
        self.added.append(item)
        key = getattr(item, "user_id", None), getattr(item, "ticket_id", None)
        if key != (None, None):
            self.objects[key] = item

    def get(self, model, key):
        return self.objects.get((model, key)) or self.objects.get(key)

    def flush(self):
        self.flush_calls += 1

    def commit(self):
        self.commit_calls += 1


def _make_ops_user(symbols, *, role: str = "dev_ti"):
    return symbols["User"](
        id=uuid.uuid4(),
        email=f"{role}@example.com",
        display_name=role.upper(),
        password_hash="hash",
        role=role,
        is_active=True,
    )


def test_add_ops_public_reply_records_status_history_and_view():
    symbols = _load_symbols()
    fake_db = _FakeSession()
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

    message = symbols["add_ops_public_reply"](
        fake_db,
        ticket=ticket,
        actor=actor,
        body_markdown="Please confirm the affected user account.",
        next_status="waiting_on_user",
    )

    history = [item for item in fake_db.added if isinstance(item, symbols["TicketStatusHistory"])]

    assert message.author_type == "dev_ti"
    assert message.visibility == "public"
    assert message.source == "human_public_reply"
    assert ticket.status == "waiting_on_user"
    assert history[0].from_status == "waiting_on_dev_ti"
    assert history[0].to_status == "waiting_on_user"
    assert fake_db.objects[(actor.id, ticket.id)].last_viewed_at >= existing_view.last_viewed_at


def test_add_ops_public_reply_rejects_invalid_next_status():
    symbols = _load_symbols()
    fake_db = _FakeSession()
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
            ticket=ticket,
            actor=actor,
            body_markdown="This should fail.",
            next_status="new",
        )


def test_add_ops_public_reply_ai_triage_delegates_to_manual_rerun(monkeypatch):
    symbols = _load_symbols()
    fake_db = _FakeSession()
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
    observed = {"manual_rerun": 0}

    monkeypatch.setattr(
        "shared.ticketing.request_manual_rerun",
        lambda db, ticket, actor: observed.__setitem__("manual_rerun", observed["manual_rerun"] + 1),
    )

    message = symbols["add_ops_public_reply"](
        fake_db,
        ticket=ticket,
        actor=actor,
        body_markdown="Please continue and answer directly.",
        next_status="ai_triage",
    )

    history = [item for item in fake_db.added if isinstance(item, symbols["TicketStatusHistory"])]

    assert message.author_type == "dev_ti"
    assert message.visibility == "public"
    assert message.source == "human_public_reply"
    assert observed["manual_rerun"] == 1
    assert history == []


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

    symbols["set_ticket_status_for_ops"](fake_db, ticket=ticket, actor=actor, next_status="resolved")
    history = [item for item in fake_db.added if isinstance(item, symbols["TicketStatusHistory"])]

    assert ticket.status == "resolved"
    assert ticket.resolved_at is not None
    assert history[0].to_status == "resolved"

    with pytest.raises(ValueError):
        symbols["set_ticket_status_for_ops"](fake_db, ticket=ticket, actor=actor, next_status="not-a-status")


def test_set_ticket_status_for_ops_ai_triage_delegates_to_manual_rerun(monkeypatch):
    symbols = _load_symbols()
    fake_db = _FakeSession()
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
        lambda db, ticket, actor: observed.__setitem__("manual_rerun", observed["manual_rerun"] + 1),
    )

    symbols["set_ticket_status_for_ops"](fake_db, ticket=ticket, actor=actor, next_status="ai_triage")

    history = [item for item in fake_db.added if isinstance(item, symbols["TicketStatusHistory"])]

    assert observed["manual_rerun"] == 1
    assert history == []


def test_request_manual_rerun_requeues_when_run_is_active(monkeypatch):
    symbols = _load_symbols()
    fake_db = _FakeSession()
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

    run = symbols["request_manual_rerun"](fake_db, ticket=ticket, actor=actor)

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

    run = symbols["request_manual_rerun"](fake_db, ticket=ticket, actor=actor)
    history = [item for item in fake_db.added if isinstance(item, symbols["TicketStatusHistory"])]

    assert run is expected_run
    assert ticket.status == "ai_triage"
    assert history[0].from_status == "waiting_on_dev_ti"
    assert history[0].to_status == "ai_triage"


def test_request_manual_rerun_passes_forced_specialist_override_to_new_run(monkeypatch):
    symbols = _load_symbols()
    fake_db = _FakeSession()
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


def test_publish_ai_draft_for_ops_creates_ai_message_and_status_change():
    symbols = _load_symbols()
    fake_db = _FakeSession()
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
    assert fake_db.flush_calls == 1


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


def test_publish_ai_draft_for_ops_rejects_non_pending_draft():
    symbols = _load_symbols()
    fake_db = _FakeSession()
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
            ticket=ticket,
            draft=draft,
            actor=actor,
            next_status="waiting_on_user",
        )


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
    ("role", "expected_options"),
    [
        ("dev_ti", ['<option value="requester">requester</option>']),
        (
            "admin",
            [
                '<option value="requester">requester</option>',
                '<option value="dev_ti">dev_ti</option>',
            ],
        ),
    ],
)
def test_ops_users_page_allows_dev_ti_and_admin_with_role_scoped_options(monkeypatch, role, expected_options):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    current_user = SimpleNamespace(id=uuid.uuid4(), display_name=role.upper(), role=role, is_active=True)
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    users = [SimpleNamespace(email="existing@example.com", display_name="Existing User", role="requester", is_active=True)]

    monkeypatch.setattr(stack["routes_ops"], "_load_users_for_admin", lambda db: users)

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["auth"].get_current_user] = lambda: current_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session

    with stack["TestClient"](app) as client:
        response = client.get("/ops/users")

    assert response.status_code == 200
    assert "Existing User" in response.text
    for option in expected_options:
        assert option in response.text
    assert '<option value="admin">admin</option>' not in response.text
    if role == "dev_ti":
        assert '<option value="dev_ti">dev_ti</option>' not in response.text
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

    def fake_create_user(db, *, email, display_name, password, role):
        created.append(
            {
                "email": email,
                "display_name": display_name,
                "password": password,
                "role": role,
            }
        )
        return SimpleNamespace(email=email, display_name=display_name, role=role)

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


def test_ops_user_creation_validation_error_keeps_users_page_context(monkeypatch):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    current_user = SimpleNamespace(id=uuid.uuid4(), display_name="ADMIN", role="admin", is_active=True)
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    users = [SimpleNamespace(email="existing@example.com", display_name="Existing User", role="dev_ti", is_active=True)]

    def fail_create_user(db, *, email, display_name, password, role):
        raise ValueError("User already exists: existing@example.com")

    monkeypatch.setattr(stack["routes_ops"], "create_user", fail_create_user)
    monkeypatch.setattr(stack["routes_ops"], "_load_users_for_admin", lambda db: users)

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
    assert "User already exists: existing@example.com" in response.text
    assert "Existing User" in response.text
    assert "Create user" in response.text
    assert '<option value="dev_ti">dev_ti</option>' in response.text
    assert db.commit_calls == 0
    assert db.rollback_calls == 1


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

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_ops"].require_ops_user] = lambda: ops_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session

    with stack["TestClient"](app) as client:
        response = client.get(f"/ops/tickets/{ticket.reference}")

    assert response.status_code == 200
    assert observed["view_updates"] == 1
    assert db.commit_calls == 1
    assert 'name="forced_route_target_id"' in response.text
    assert "Use normal routing" in response.text
    assert "Software Architect" in response.text


def test_ops_set_ticket_status_ai_triage_triggers_manual_rerun(monkeypatch):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    ops_user = SimpleNamespace(id=uuid.uuid4(), display_name="Ops", role="dev_ti")
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    ticket = SimpleNamespace(reference="T-000012", id=uuid.uuid4())
    observed = {"next_status": None}

    monkeypatch.setattr(stack["routes_ops"], "_load_ops_ticket_or_404", lambda *args, **kwargs: ticket)
    monkeypatch.setattr(
        stack["routes_ops"],
        "set_ticket_status_for_ops",
        lambda db, ticket, actor, next_status, note=None: observed.__setitem__("next_status", next_status),
    )

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_ops"].require_ops_user] = lambda: ops_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session

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
    ops_user = SimpleNamespace(id=uuid.uuid4(), display_name="Ops", role="dev_ti")
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    ticket = SimpleNamespace(reference="T-000012A", id=uuid.uuid4())
    observed = {}

    monkeypatch.setattr(stack["routes_ops"], "_load_ops_ticket_or_404", lambda *args, **kwargs: ticket)
    monkeypatch.setattr(
        stack["routes_ops"],
        "request_manual_rerun",
        lambda db, ticket, actor, forced_route_target_id=None, forced_specialist_id=None: observed.update(
            {
                "forced_route_target_id": forced_route_target_id,
                "forced_specialist_id": forced_specialist_id,
            }
        ),
    )

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_ops"].require_ops_user] = lambda: ops_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session

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
    ops_user = SimpleNamespace(id=uuid.uuid4(), display_name="Ops", role="dev_ti")
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    ticket = SimpleNamespace(reference="T-000012B", id=uuid.uuid4())

    monkeypatch.setattr(stack["routes_ops"], "_load_ops_ticket_or_404", lambda *args, **kwargs: ticket)

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_ops"].require_ops_user] = lambda: ops_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session

    with stack["TestClient"](app) as client:
        response = client.post(
            f"/ops/tickets/{ticket.reference}/rerun-ai",
            data={"csrf_token": "csrf-token", "forced_route_target_id": "manual_review"},
            follow_redirects=False,
        )

    assert response.status_code == 400


def test_ops_detail_route_separates_analysis_artifacts_from_latest_run(monkeypatch):
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

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_ops"].require_ops_user] = lambda: ops_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session

    with stack["TestClient"](app) as client:
        response = client.get(f"/ops/tickets/{ticket.reference}")

    assert response.status_code == 200
    assert "Analysis steps" in response.text
    assert "/tmp/analysis-support-final.json" in response.text
    assert "Latest run steps" in response.text
    assert "/tmp/latest-router-final.json" in response.text


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
    board_template = Path("app/templates/ops_board_columns.html").read_text(encoding="utf-8")
    list_template = Path("app/templates/ops_ticket_list.html").read_text(encoding="utf-8")
    rows_template = Path("app/templates/ops_ticket_rows.html").read_text(encoding="utf-8")

    assert '"/ops/tickets/{reference}/reply-public"' in source
    assert '"/ops/tickets/{reference}/note-internal"' in source
    assert '"/ops/tickets/{reference}/assign"' in source
    assert '"/ops/tickets/{reference}/set-status"' in source
    assert '"/ops/tickets/{reference}/rerun-ai"' in source
    assert "Route AI to specialist" in detail_template
    assert 'name="forced_route_target_id"' in detail_template
    assert "add_ops_public_reply" in source
    assert "add_ops_internal_note" in source
    assert "assign_ticket_for_ops" in source
    assert "set_ticket_status_for_ops" in source
    assert "Status" in filters_template
    assert "Route target" in filters_template
    assert "Assigned to" in filters_template
    assert 'hx-get="{{ filters_action }}"' in filters_template
    assert 'hx-target="#{{ filters_target_id }}"' in filters_template
    assert 'hx-swap="outerHTML"' in filters_template
    assert "Urgent only" in filters_template
    assert "Unassigned only" in filters_template
    assert "Created by me" in filters_template
    assert "Needs approval" in filters_template
    assert "Updated since my last view" in filters_template
    assert "Activity" in detail_template
    assert "AI analysis" in detail_template
    assert "lane-pill" in detail_template
    assert "timeline-status" in detail_template
    assert "Summary" in detail_template
    assert "Internal summary" in detail_template
    assert '<details class="analysis-disclosure">' in detail_template
    assert "More analysis" in detail_template
    assert "Relevant repo/docs paths" in detail_template
    assert "Pending AI draft" in detail_template
    assert "Ticket queue" in list_template
    assert 'id="ops-results"' in rows_template
    assert 'id="ops-results"' in board_template
    assert '/static/htmx.min.js' in base_template
    assert "Pending draft approval" in board_template
    assert ".analysis-disclosure" in app_css
