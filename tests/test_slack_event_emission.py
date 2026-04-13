from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import uuid

import pytest

from shared.config import Settings, SlackSettings


class _FakeNestedTransaction:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value
        self.rowcount = 0

    def scalar_one(self):
        return self._value


class _FakeSession:
    def __init__(self, *, next_reference_num: int = 1):
        pytest.importorskip("sqlalchemy")
        from shared.models import IntegrationEvent, IntegrationEventTarget, User

        self._integration_event_type = IntegrationEvent
        self._integration_event_target_type = IntegrationEventTarget
        self._user_type = User
        self.added = []
        self.operations = []
        self.existing = {}
        self.next_reference_num = next_reference_num
        self.events_by_dedupe_key: dict[str, object] = {}
        self.targets_by_event_id: dict[uuid.UUID, list[object]] = {}

    def add(self, item):
        self.added.append(item)
        self.operations.append(("add", item))
        if isinstance(item, self._integration_event_type):
            self.events_by_dedupe_key[item.dedupe_key] = item
        elif isinstance(item, self._integration_event_target_type):
            self.targets_by_event_id.setdefault(item.event_id, []).append(item)
        elif isinstance(item, self._user_type):
            self.existing[(self._user_type, item.id)] = item
        key = getattr(item, "user_id", None), getattr(item, "ticket_id", None)
        if key != (None, None):
            self.existing[key] = item

    def flush(self):
        self.operations.append(("flush", None))

    def begin_nested(self):
        self.operations.append(("begin_nested", None))
        return _FakeNestedTransaction()

    def get(self, model, key):
        return self.existing.get((model, key)) or self.existing.get(key)

    def execute(self, statement):
        return _FakeScalarResult(self.next_reference_num)


@dataclass
class _FakeAttachment:
    original_filename: str = "evidence.txt"
    mime_type: str = "text/plain"
    sha256: str = "sha-1"
    size_bytes: int = 128
    width: int | None = None
    height: int | None = None


@pytest.fixture(autouse=True)
def _fake_integration_queries(monkeypatch):
    def fake_load_event(db, *, dedupe_key: str):
        return db.events_by_dedupe_key.get(dedupe_key)

    def fake_load_targets(db, *, event_id):
        return list(db.targets_by_event_id.get(event_id, ()))

    monkeypatch.setattr("shared.integrations.load_integration_event_by_dedupe_key", fake_load_event)
    monkeypatch.setattr("shared.integrations.load_integration_event_targets", fake_load_targets)


def _make_slack_settings(
    *,
    enabled: bool = True,
    notify_ticket_created: bool = True,
    notify_public_message_added: bool = True,
    notify_status_changed: bool = True,
    is_valid: bool = True,
    config_error_code: str | None = None,
    config_error_summary: str | None = None,
    message_preview_max_chars: int = 24,
) -> SlackSettings:
    return SlackSettings(
        enabled=enabled,
        notify_ticket_created=notify_ticket_created,
        notify_public_message_added=notify_public_message_added,
        notify_status_changed=notify_status_changed,
        message_preview_max_chars=message_preview_max_chars,
        is_valid=is_valid,
        config_error_code=config_error_code,
        config_error_summary=config_error_summary,
        routing_mode="dm",
    )


def _make_settings(tmp_path: Path, *, app_base_url: str = "https://autosac.example.local", slack: SlackSettings | None = None) -> Settings:
    workspace_dir = tmp_path / "workspace"
    return Settings(
        app_base_url=app_base_url,
        app_secret_key="secret",
        database_url="postgresql+psycopg://triage:triage@localhost:5432/triage",
        uploads_dir=tmp_path / "uploads",
        triage_workspace_dir=workspace_dir,
        repo_mount_dir=workspace_dir / "app",
        manuals_mount_dir=workspace_dir / "manuals",
        codex_bin="codex",
        codex_api_key="key",
        codex_model="",
        codex_timeout_seconds=3600,
        worker_poll_seconds=10,
        auto_support_reply_min_confidence=0.85,
        auto_confirm_intent_min_confidence=0.90,
        max_images_per_message=3,
        max_image_bytes=5 * 1024 * 1024,
        session_default_hours=12,
        session_remember_days=30,
        slack=slack or SlackSettings(),
    )


def _make_slack_runtime(
    settings: Settings,
    *,
    event_logger=None,
    clock=None,
):
    from shared.integrations import build_slack_runtime_context
    from shared.logging import log_event
    from shared.security import utc_now

    return build_slack_runtime_context(
        settings,
        event_logger=event_logger or log_event,
        clock=clock or utc_now,
    )


def _load_symbols():
    pytest.importorskip("sqlalchemy")
    from shared.integrations import (
        build_message_preview,
        build_ticket_created_payload,
        record_ticket_created_event,
        record_ticket_public_message_added_event,
    )
    from shared.models import (
        AIDraft,
        IntegrationEvent,
        IntegrationEventLink,
        IntegrationEventTarget,
        Ticket,
        TicketMessage,
        TicketStatusHistory,
        User,
    )
    from shared.ticketing import (
        add_ops_public_reply,
        add_ops_internal_note,
        add_requester_reply,
        create_ai_draft,
        create_requester_ticket,
        publish_ai_draft_for_ops,
        publish_ai_failure_note,
        record_status_change,
        route_ticket_after_ai,
    )

    return {
        "AIDraft": AIDraft,
        "IntegrationEvent": IntegrationEvent,
        "IntegrationEventLink": IntegrationEventLink,
        "IntegrationEventTarget": IntegrationEventTarget,
        "Ticket": Ticket,
        "TicketMessage": TicketMessage,
        "TicketStatusHistory": TicketStatusHistory,
        "User": User,
        "add_ops_public_reply": add_ops_public_reply,
        "add_ops_internal_note": add_ops_internal_note,
        "add_requester_reply": add_requester_reply,
        "build_message_preview": build_message_preview,
        "build_ticket_created_payload": build_ticket_created_payload,
        "create_ai_draft": create_ai_draft,
        "create_requester_ticket": create_requester_ticket,
        "publish_ai_draft_for_ops": publish_ai_draft_for_ops,
        "publish_ai_failure_note": publish_ai_failure_note,
        "record_status_change": record_status_change,
        "record_ticket_created_event": record_ticket_created_event,
        "record_ticket_public_message_added_event": record_ticket_public_message_added_event,
        "route_ticket_after_ai": route_ticket_after_ai,
    }


def _make_user(symbols, *, role: str = "requester", slack_user_id: str | None = None, is_active: bool = True):
    return symbols["User"](
        id=uuid.uuid4(),
        email=f"{role}@example.com",
        display_name=role.upper(),
        password_hash="hash",
        role=role,
        slack_user_id=slack_user_id,
        is_active=is_active,
    )


def _make_ticket(
    symbols,
    *,
    status: str = "new",
    title: str = "Ticket title",
    reference: str = "T-000001",
    created_by_user_id: uuid.UUID | None = None,
    assigned_to_user_id: uuid.UUID | None = None,
):
    return symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=1,
        reference=reference,
        title=title,
        created_by_user_id=created_by_user_id or uuid.uuid4(),
        assigned_to_user_id=assigned_to_user_id,
        status=status,
        urgent=False,
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        updated_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
    )


def _make_public_message(symbols, *, ticket_id, source: str = "requester_reply", author_type: str = "requester", body_text: str = "Body text"):
    return symbols["TicketMessage"](
        id=uuid.uuid4(),
        ticket_id=ticket_id,
        author_user_id=uuid.uuid4(),
        author_type=author_type,
        visibility="public",
        source=source,
        body_markdown=body_text,
        body_text=body_text,
        created_at=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
    )


def _integration_rows(fake_db, symbols, row_name: str):
    row_type = symbols[row_name]
    return [item for item in fake_db.added if isinstance(item, row_type)]


def _register_users(fake_db, *users):
    for user in users:
        fake_db.add(user)


def test_build_message_preview_normalizes_unicode_whitespace_and_truncates(tmp_path):
    symbols = _load_symbols()

    assert symbols["build_message_preview"]("  Oi\tmundo\n\u00a0legal  ", max_chars=64) == "Oi mundo legal"
    assert symbols["build_message_preview"]("um\t dois\n tres", max_chars=10) == "um dois..."


def test_build_ticket_created_payload_normalizes_trailing_slash_in_ticket_url(tmp_path):
    symbols = _load_symbols()
    ticket = _make_ticket(symbols, reference="T-000123")
    occurred_at = datetime(2026, 4, 10, 13, 0, tzinfo=timezone.utc)

    payload_without_slash = symbols["build_ticket_created_payload"](
        _make_settings(tmp_path, app_base_url="https://autosac.example.local"),
        ticket=ticket,
        occurred_at=occurred_at,
    )
    payload_with_slash = symbols["build_ticket_created_payload"](
        _make_settings(tmp_path, app_base_url="https://autosac.example.local/"),
        ticket=ticket,
        occurred_at=occurred_at,
    )

    assert payload_without_slash["ticket_url"] == "https://autosac.example.local/ops/tickets/T-000123"
    assert payload_with_slash["ticket_url"] == payload_without_slash["ticket_url"]


def test_create_requester_ticket_emits_ticket_created_only(monkeypatch, tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(
        tmp_path,
        slack=_make_slack_settings(
            notify_ticket_created=True,
            notify_public_message_added=True,
            notify_status_changed=True,
        ),
    )
    fake_db = _FakeSession(next_reference_num=17)
    slack_runtime = _make_slack_runtime(settings)
    requester = _make_user(symbols, slack_user_id="UREQUESTER")
    _register_users(fake_db, requester)

    monkeypatch.setattr("shared.ticketing.create_pending_ai_run", lambda *args, **kwargs: None)

    ticket, initial_message, _attachments, _run = symbols["create_requester_ticket"](
        fake_db,
        settings=settings,
        slack_runtime=slack_runtime,
        requester=requester,
        title="",
        description_markdown="  Falha\tna abertura\n do sistema  ",
        urgent=True,
        attachments=[_FakeAttachment()],
    )

    events = _integration_rows(fake_db, symbols, "IntegrationEvent")
    links = _integration_rows(fake_db, symbols, "IntegrationEventLink")
    targets = _integration_rows(fake_db, symbols, "IntegrationEventTarget")

    assert [event.event_type for event in events] == ["ticket.created"]
    assert events[0].dedupe_key == f"ticket.created:{ticket.id}"
    assert events[0].payload_json["ticket_id"] == str(ticket.id)
    assert events[0].payload_json["ticket_reference"] == ticket.reference
    assert events[0].payload_json["ticket_status"] == "new"
    assert "_integration_routing" not in events[0].payload_json
    assert events[0].routing_result == "created"
    assert events[0].routing_target_name is None
    assert events[0].routing_config_error_code is None
    assert events[0].routing_config_error_summary is None
    assert {(link.entity_type, link.entity_id, link.relation_kind) for link in links} == {
        ("ticket", ticket.id, "primary"),
        ("ticket_message", initial_message.id, "message"),
    }
    assert len(targets) == 1
    assert targets[0].target_name == f"user:{requester.id}"
    assert targets[0].target_kind == "slack_dm"
    assert targets[0].recipient_user_id == requester.id
    assert targets[0].recipient_reason == "requester"
    assert targets[0].delivery_status == "pending"


@pytest.mark.parametrize(
    ("slack_enabled", "notify_ticket_created", "requester_slack_user_id", "expected_routing_result"),
    (
        (False, True, "UREQUESTER", "suppressed_slack_disabled"),
        (True, False, "UREQUESTER", "suppressed_notify_disabled"),
        (True, True, None, "suppressed_no_recipients"),
    ),
)
def test_ticket_created_suppression_paths_record_event_and_links_without_target_row(
    tmp_path,
    slack_enabled,
    notify_ticket_created,
    requester_slack_user_id,
    expected_routing_result,
):
    symbols = _load_symbols()
    settings = _make_settings(
        tmp_path,
        slack=_make_slack_settings(
            enabled=slack_enabled,
            notify_ticket_created=notify_ticket_created,
        ),
    )
    fake_db = _FakeSession()
    observed = []
    slack_runtime = _make_slack_runtime(
        settings,
        event_logger=lambda service, event, **payload: observed.append((service, event, payload)),
    )
    requester = _make_user(symbols, slack_user_id=requester_slack_user_id)
    _register_users(fake_db, requester)
    ticket = _make_ticket(symbols, created_by_user_id=requester.id)
    message = _make_public_message(symbols, ticket_id=ticket.id, source="ticket_create")

    result = symbols["record_ticket_created_event"](
        fake_db,
        slack_runtime=slack_runtime,
        ticket=ticket,
        initial_message=message,
    )

    events = _integration_rows(fake_db, symbols, "IntegrationEvent")
    links = _integration_rows(fake_db, symbols, "IntegrationEventLink")
    targets = _integration_rows(fake_db, symbols, "IntegrationEventTarget")

    assert len(events) == 1
    assert {(link.entity_type, link.entity_id, link.relation_kind) for link in links} == {
        ("ticket", ticket.id, "primary"),
        ("ticket_message", message.id, "message"),
    }
    assert targets == []
    assert result.routing_result == expected_routing_result
    assert events[0].routing_result == expected_routing_result
    assert "_integration_routing" not in events[0].payload_json
    assert observed[0][2]["routing_result"] == expected_routing_result
    assert result.target_name is None
    assert result.recipient_target_count == 0
    assert events[0].routing_target_name is None
    assert observed[0][2]["recipient_target_count"] == 0


def test_add_requester_reply_emits_public_message_and_status_changed(monkeypatch, tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path, slack=_make_slack_settings())
    fake_db = _FakeSession()
    slack_runtime = _make_slack_runtime(settings)
    requester = _make_user(symbols, slack_user_id="UREQUESTER")
    _register_users(fake_db, requester)
    ticket = _make_ticket(symbols, status="resolved", created_by_user_id=requester.id)

    monkeypatch.setattr("shared.ticketing.enqueue_or_requeue_ai_run", lambda *args, **kwargs: None)

    message, _attachments, _run = symbols["add_requester_reply"](
        fake_db,
        settings=settings,
        slack_runtime=slack_runtime,
        ticket=ticket,
        requester=requester,
        body_markdown=" Ainda\tacontece\n o erro ao salvar. ",
        attachments=[],
    )

    events = _integration_rows(fake_db, symbols, "IntegrationEvent")
    targets = _integration_rows(fake_db, symbols, "IntegrationEventTarget")

    assert [event.event_type for event in events] == [
        "ticket.status_changed",
        "ticket.public_message_added",
    ]
    status_event = next(event for event in events if event.event_type == "ticket.status_changed")
    message_event = next(event for event in events if event.event_type == "ticket.public_message_added")
    assert status_event.payload_json["status_from"] == "resolved"
    assert status_event.payload_json["status_to"] == "ai_triage"
    assert message_event.payload_json["message_id"] == str(message.id)
    assert message_event.payload_json["message_author_type"] == "requester"
    assert message_event.payload_json["message_source"] == "requester_reply"
    assert message_event.payload_json["message_preview"] == "Ainda acontece o erro..."
    assert message_event.payload_json["ticket_status"] == "ai_triage"
    assert len(targets) == 2
    assert {
        (target.event_id, target.target_name, target.recipient_user_id, target.recipient_reason)
        for target in targets
    } == {
        (status_event.id, f"user:{requester.id}", requester.id, "requester"),
        (message_event.id, f"user:{requester.id}", requester.id, "requester"),
    }


def test_record_ticket_created_event_creates_requester_and_assignee_dm_targets_when_distinct_and_eligible(tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path, slack=_make_slack_settings())
    fake_db = _FakeSession()
    requester = _make_user(symbols, slack_user_id="UREQUESTER")
    assignee = _make_user(symbols, role="dev_ti", slack_user_id="UASSIGNEE")
    _register_users(fake_db, requester, assignee)
    ticket = _make_ticket(
        symbols,
        created_by_user_id=requester.id,
        assigned_to_user_id=assignee.id,
    )
    message = _make_public_message(symbols, ticket_id=ticket.id, source="ticket_create")

    result = symbols["record_ticket_created_event"](
        fake_db,
        slack_runtime=_make_slack_runtime(settings),
        ticket=ticket,
        initial_message=message,
    )

    targets = _integration_rows(fake_db, symbols, "IntegrationEventTarget")

    assert result.routing_result == "created"
    assert result.recipient_target_count == 2
    assert {target.target_name for target in targets} == {
        f"user:{requester.id}",
        f"user:{assignee.id}",
    }
    assert {(target.recipient_user_id, target.recipient_reason) for target in targets} == {
        (requester.id, "requester"),
        (assignee.id, "assignee"),
    }


def test_record_ticket_created_event_logs_created_recipient_count_without_target_name(tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path, slack=_make_slack_settings())
    fake_db = _FakeSession()
    requester = _make_user(symbols, slack_user_id="UREQUESTER")
    assignee = _make_user(symbols, role="dev_ti", slack_user_id="UASSIGNEE")
    _register_users(fake_db, requester, assignee)
    ticket = _make_ticket(
        symbols,
        created_by_user_id=requester.id,
        assigned_to_user_id=assignee.id,
    )
    message = _make_public_message(symbols, ticket_id=ticket.id, source="ticket_create")
    observed = []

    symbols["record_ticket_created_event"](
        fake_db,
        slack_runtime=_make_slack_runtime(
            settings,
            event_logger=lambda service, event, **payload: observed.append((service, event, payload)),
        ),
        ticket=ticket,
        initial_message=message,
    )

    assert observed == [
        (
            "integration",
            "integration_event_recorded",
            {
                "event_id": str(_integration_rows(fake_db, symbols, "IntegrationEvent")[0].id),
                "event_type": "ticket.created",
                "aggregate_type": "ticket",
                "aggregate_id": str(ticket.id),
                "dedupe_key": f"ticket.created:{ticket.id}",
                "routing_result": "created",
                "recipient_target_count": 2,
            },
        )
    ]


def test_record_ticket_created_event_creates_assignee_only_target_when_requester_is_ineligible(tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path, slack=_make_slack_settings())
    fake_db = _FakeSession()
    requester = _make_user(symbols)
    assignee = _make_user(symbols, role="dev_ti", slack_user_id="UASSIGNEE")
    _register_users(fake_db, requester, assignee)
    ticket = _make_ticket(
        symbols,
        created_by_user_id=requester.id,
        assigned_to_user_id=assignee.id,
    )
    message = _make_public_message(symbols, ticket_id=ticket.id, source="ticket_create")

    result = symbols["record_ticket_created_event"](
        fake_db,
        slack_runtime=_make_slack_runtime(settings),
        ticket=ticket,
        initial_message=message,
    )

    targets = _integration_rows(fake_db, symbols, "IntegrationEventTarget")

    assert result.routing_result == "created"
    assert result.recipient_target_count == 1
    assert len(targets) == 1
    assert targets[0].target_name == f"user:{assignee.id}"
    assert targets[0].recipient_user_id == assignee.id
    assert targets[0].recipient_reason == "assignee"


def test_record_ticket_created_event_ignores_inactive_requester_and_routes_to_active_assignee(tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path, slack=_make_slack_settings())
    fake_db = _FakeSession()
    requester = _make_user(symbols, slack_user_id="UREQUESTER", is_active=False)
    assignee = _make_user(symbols, role="dev_ti", slack_user_id="UASSIGNEE")
    _register_users(fake_db, requester, assignee)
    ticket = _make_ticket(
        symbols,
        created_by_user_id=requester.id,
        assigned_to_user_id=assignee.id,
    )
    message = _make_public_message(symbols, ticket_id=ticket.id, source="ticket_create")

    result = symbols["record_ticket_created_event"](
        fake_db,
        slack_runtime=_make_slack_runtime(settings),
        ticket=ticket,
        initial_message=message,
    )

    targets = _integration_rows(fake_db, symbols, "IntegrationEventTarget")

    assert result.routing_result == "created"
    assert result.recipient_target_count == 1
    assert len(targets) == 1
    assert targets[0].target_name == f"user:{assignee.id}"
    assert targets[0].recipient_user_id == assignee.id
    assert targets[0].recipient_reason == "assignee"


def test_record_ticket_created_event_collapses_requester_and_assignee_when_same_user(tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path, slack=_make_slack_settings())
    fake_db = _FakeSession()
    operator = _make_user(symbols, role="dev_ti", slack_user_id="UOPERATOR")
    _register_users(fake_db, operator)
    ticket = _make_ticket(
        symbols,
        created_by_user_id=operator.id,
        assigned_to_user_id=operator.id,
    )
    message = _make_public_message(symbols, ticket_id=ticket.id, source="ticket_create")

    result = symbols["record_ticket_created_event"](
        fake_db,
        slack_runtime=_make_slack_runtime(settings),
        ticket=ticket,
        initial_message=message,
    )

    targets = _integration_rows(fake_db, symbols, "IntegrationEventTarget")

    assert result.routing_result == "created"
    assert result.recipient_target_count == 1
    assert len(targets) == 1
    assert targets[0].target_name == f"user:{operator.id}"
    assert targets[0].recipient_user_id == operator.id
    assert targets[0].recipient_reason == "requester_assignee"


def test_add_ops_public_reply_keeps_assignee_self_notification_when_actor_is_recipient(tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path, slack=_make_slack_settings())
    fake_db = _FakeSession()
    actor = _make_user(symbols, role="dev_ti", slack_user_id="UASSIGNEE")
    _register_users(fake_db, actor)
    ticket = _make_ticket(
        symbols,
        status="waiting_on_user",
        created_by_user_id=uuid.uuid4(),
        assigned_to_user_id=actor.id,
    )

    symbols["add_ops_public_reply"](
        fake_db,
        slack_runtime=_make_slack_runtime(settings),
        ticket=ticket,
        actor=actor,
        body_markdown="Segue atualização.",
        next_status="waiting_on_user",
    )

    events = _integration_rows(fake_db, symbols, "IntegrationEvent")
    targets = _integration_rows(fake_db, symbols, "IntegrationEventTarget")

    assert [event.event_type for event in events] == ["ticket.public_message_added"]
    assert len(targets) == 1
    assert targets[0].target_name == f"user:{actor.id}"
    assert targets[0].recipient_user_id == actor.id
    assert targets[0].recipient_reason == "assignee"


def test_add_ops_internal_note_creates_no_integration_rows(tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path, slack=_make_slack_settings())
    fake_db = _FakeSession()
    actor = _make_user(symbols, role="dev_ti")
    ticket = _make_ticket(symbols, status="waiting_on_dev_ti")

    symbols["add_ops_internal_note"](
        fake_db,
        ticket=ticket,
        actor=actor,
        body_markdown="Internal only note.",
    )

    assert _integration_rows(fake_db, symbols, "IntegrationEvent") == []
    assert _integration_rows(fake_db, symbols, "IntegrationEventLink") == []
    assert _integration_rows(fake_db, symbols, "IntegrationEventTarget") == []


def test_create_ai_draft_emits_only_status_changed_for_worker_draft_creation(tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path, slack=_make_slack_settings())
    fake_db = _FakeSession()
    slack_runtime = _make_slack_runtime(settings)
    ticket = _make_ticket(symbols, status="ai_triage")

    draft = symbols["create_ai_draft"](
        fake_db,
        slack_runtime=slack_runtime,
        ticket=ticket,
        ai_run_id=uuid.uuid4(),
        body_markdown="Need a human to review this answer.",
        next_status="waiting_on_dev_ti",
    )

    events = _integration_rows(fake_db, symbols, "IntegrationEvent")

    assert isinstance(draft, symbols["AIDraft"])
    assert [event.event_type for event in events] == ["ticket.status_changed"]
    assert events[0].payload_json["status_from"] == "ai_triage"
    assert events[0].payload_json["status_to"] == "waiting_on_dev_ti"


def test_route_ticket_after_ai_emits_status_changed_without_public_message(tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path, slack=_make_slack_settings())
    fake_db = _FakeSession()
    slack_runtime = _make_slack_runtime(settings)
    ticket = _make_ticket(symbols, status="ai_triage")

    symbols["route_ticket_after_ai"](
        fake_db,
        slack_runtime=slack_runtime,
        ticket=ticket,
        next_status="waiting_on_dev_ti",
        last_ai_action="manual_only",
    )

    events = _integration_rows(fake_db, symbols, "IntegrationEvent")

    assert [event.event_type for event in events] == ["ticket.status_changed"]
    assert events[0].payload_json["status_from"] == "ai_triage"
    assert events[0].payload_json["status_to"] == "waiting_on_dev_ti"


def test_ai_failure_note_flow_emits_status_changed_but_no_public_message(tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path, slack=_make_slack_settings())
    fake_db = _FakeSession()
    slack_runtime = _make_slack_runtime(settings)
    ticket = _make_ticket(symbols, status="ai_triage")

    symbols["publish_ai_failure_note"](
        fake_db,
        ticket=ticket,
        ai_run_id=uuid.uuid4(),
        body_markdown="Internal failure details",
    )
    symbols["record_status_change"](
        fake_db,
        slack_runtime=slack_runtime,
        ticket=ticket,
        to_status="waiting_on_dev_ti",
        changed_by_type="system",
    )

    events = _integration_rows(fake_db, symbols, "IntegrationEvent")

    assert [event.event_type for event in events] == ["ticket.status_changed"]
    assert events[0].payload_json["status_to"] == "waiting_on_dev_ti"


def test_publish_ai_draft_for_ops_uses_ai_public_message_author_in_payload(tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path, slack=_make_slack_settings())
    fake_db = _FakeSession()
    slack_runtime = _make_slack_runtime(settings)
    actor = _make_user(symbols, role="dev_ti")
    ticket = _make_ticket(symbols, status="waiting_on_dev_ti")
    draft = symbols["AIDraft"](
        id=uuid.uuid4(),
        ticket_id=ticket.id,
        ai_run_id=uuid.uuid4(),
        kind="public_reply",
        body_markdown="Resposta revisada",
        body_text="Resposta revisada",
        status="pending_approval",
        created_at=datetime(2026, 4, 10, 11, 0, tzinfo=timezone.utc),
    )

    symbols["publish_ai_draft_for_ops"](
        fake_db,
        slack_runtime=slack_runtime,
        ticket=ticket,
        draft=draft,
        actor=actor,
        next_status="waiting_on_user",
    )

    events = _integration_rows(fake_db, symbols, "IntegrationEvent")
    message_event = next(event for event in events if event.event_type == "ticket.public_message_added")

    assert message_event.payload_json["message_author_type"] == "ai"
    assert message_event.payload_json["message_source"] == "ai_draft_published"
    assert message_event.payload_json["ticket_status"] == "waiting_on_user"


def test_duplicate_reuse_preserves_zero_target_state_after_later_slack_enablement(tmp_path):
    symbols = _load_symbols()
    disabled_settings = _make_settings(tmp_path, slack=_make_slack_settings(enabled=False))
    enabled_settings = _make_settings(tmp_path, slack=_make_slack_settings())
    fake_db = _FakeSession()
    requester = _make_user(symbols, slack_user_id="UREQUESTER")
    _register_users(fake_db, requester)
    ticket = _make_ticket(symbols, created_by_user_id=requester.id)
    message = _make_public_message(symbols, ticket_id=ticket.id, source="ticket_create")
    observed = []

    first = symbols["record_ticket_created_event"](
        fake_db,
        slack_runtime=_make_slack_runtime(
            disabled_settings,
            event_logger=lambda service, event, **payload: observed.append((service, event, payload)),
        ),
        ticket=ticket,
        initial_message=message,
    )
    second = symbols["record_ticket_created_event"](
        fake_db,
        slack_runtime=_make_slack_runtime(
            enabled_settings,
            event_logger=lambda service, event, **payload: observed.append((service, event, payload)),
        ),
        ticket=ticket,
        initial_message=message,
    )

    events = _integration_rows(fake_db, symbols, "IntegrationEvent")
    targets = _integration_rows(fake_db, symbols, "IntegrationEventTarget")

    assert len(events) == 1
    assert targets == []
    assert first.event.id == second.event.id
    assert second.event_reused is True
    assert first.event.routing_result == "suppressed_slack_disabled"
    assert second.routing_result == "suppressed_slack_disabled"
    assert second.recipient_target_count == 0
    assert observed[1] == (
        "integration",
        "integration_event_recorded",
        {
            "event_id": str(first.event.id),
            "event_type": "ticket.created",
            "aggregate_type": "ticket",
            "aggregate_id": str(ticket.id),
            "dedupe_key": f"ticket.created:{ticket.id}",
            "routing_result": "suppressed_slack_disabled",
            "recipient_target_count": 0,
            "event_reused": True,
        },
    )


def test_duplicate_reuse_preserves_zero_target_state_after_later_slack_id_change(tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path, slack=_make_slack_settings())
    fake_db = _FakeSession()
    requester = _make_user(symbols)
    _register_users(fake_db, requester)
    ticket = _make_ticket(symbols, created_by_user_id=requester.id)
    message = _make_public_message(symbols, ticket_id=ticket.id, source="ticket_create")
    observed = []

    first = symbols["record_ticket_created_event"](
        fake_db,
        slack_runtime=_make_slack_runtime(
            settings,
            event_logger=lambda service, event, **payload: observed.append((service, event, payload)),
        ),
        ticket=ticket,
        initial_message=message,
    )
    requester.slack_user_id = "UREQUESTER"
    second = symbols["record_ticket_created_event"](
        fake_db,
        slack_runtime=_make_slack_runtime(
            settings,
            event_logger=lambda service, event, **payload: observed.append((service, event, payload)),
        ),
        ticket=ticket,
        initial_message=message,
    )

    events = _integration_rows(fake_db, symbols, "IntegrationEvent")
    targets = _integration_rows(fake_db, symbols, "IntegrationEventTarget")

    assert len(events) == 1
    assert targets == []
    assert first.event.id == second.event.id
    assert second.event_reused is True
    assert "_integration_routing" not in first.event.payload_json
    assert first.event.routing_result == "suppressed_no_recipients"
    assert second.routing_result == "suppressed_no_recipients"
    assert second.recipient_target_count == 0
    assert observed[1] == (
        "integration",
        "integration_event_recorded",
        {
            "event_id": str(first.event.id),
            "event_type": "ticket.created",
            "aggregate_type": "ticket",
            "aggregate_id": str(ticket.id),
            "dedupe_key": f"ticket.created:{ticket.id}",
            "routing_result": "suppressed_no_recipients",
            "recipient_target_count": 0,
            "event_reused": True,
        },
    )


def test_duplicate_reuse_does_not_add_missing_assignee_row_after_later_assignment_change(tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path, slack=_make_slack_settings())
    fake_db = _FakeSession()
    requester = _make_user(symbols, slack_user_id="UREQUESTER")
    assignee = _make_user(symbols, role="dev_ti", slack_user_id="UASSIGNEE")
    _register_users(fake_db, requester, assignee)
    ticket = _make_ticket(symbols, created_by_user_id=requester.id)
    message = _make_public_message(symbols, ticket_id=ticket.id, source="ticket_create")
    observed = []

    first = symbols["record_ticket_created_event"](
        fake_db,
        slack_runtime=_make_slack_runtime(
            settings,
            event_logger=lambda service, event, **payload: observed.append((service, event, payload)),
        ),
        ticket=ticket,
        initial_message=message,
    )

    targets = _integration_rows(fake_db, symbols, "IntegrationEventTarget")
    assert len(targets) == 1
    original_target = targets[0]
    original_target.delivery_status = "failed"
    original_target.attempt_count = 3
    original_target.last_error = "timeout"
    original_target.locked_by = "host:123"
    original_target.locked_at = datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc)
    ticket.assigned_to_user_id = assignee.id

    second = symbols["record_ticket_created_event"](
        fake_db,
        slack_runtime=_make_slack_runtime(
            settings,
            event_logger=lambda service, event, **payload: observed.append((service, event, payload)),
        ),
        ticket=ticket,
        initial_message=message,
    )

    events = _integration_rows(fake_db, symbols, "IntegrationEvent")
    targets = _integration_rows(fake_db, symbols, "IntegrationEventTarget")

    assert len(events) == 1
    assert len(targets) == 1
    assert second.event_reused is True
    assert second.event.id == first.event.id
    assert second.routing_result == "created"
    assert second.recipient_target_count == 1
    assert targets[0] is original_target
    assert targets[0].delivery_status == "failed"
    assert targets[0].attempt_count == 3
    assert targets[0].last_error == "timeout"
    assert targets[0].locked_by == "host:123"
    assert targets[0].locked_at == datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc)
    assert targets[0].target_name == f"user:{requester.id}"
    assert targets[0].recipient_user_id == requester.id
    assert targets[0].recipient_reason == "requester"
    assert observed[1] == (
        "integration",
        "integration_event_recorded",
        {
            "event_id": str(first.event.id),
            "event_type": "ticket.created",
            "aggregate_type": "ticket",
            "aggregate_id": str(ticket.id),
            "dedupe_key": f"ticket.created:{ticket.id}",
            "routing_result": "created",
            "recipient_target_count": 1,
            "event_reused": True,
        },
    )


@pytest.mark.parametrize(
    ("routing_result", "config_error_code", "config_error_summary", "expected_log_fields"),
    [
        (
            "suppressed_no_recipients",
            None,
            None,
            {},
        ),
        (
            "suppressed_invalid_config",
            "slack_targets_json_parse_error",
            "SLACK_TARGETS_JSON must be a valid JSON object",
            {
                "config_error_code": "slack_targets_json_parse_error",
                "config_error_summary": "SLACK_TARGETS_JSON must be a valid JSON object",
            },
        ),
    ],
)
def test_duplicate_reuse_zero_target_preserves_stored_non_created_routing_snapshot(
    tmp_path,
    routing_result,
    config_error_code,
    config_error_summary,
    expected_log_fields,
):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path, slack=_make_slack_settings())
    fake_db = _FakeSession()
    ticket = _make_ticket(symbols)
    message = _make_public_message(symbols, ticket_id=ticket.id, source="ticket_create")
    observed = []
    event = symbols["IntegrationEvent"](
        id=uuid.uuid4(),
        source_system="autosac",
        event_type="ticket.created",
        aggregate_type="ticket",
        aggregate_id=ticket.id,
        dedupe_key=f"ticket.created:{ticket.id}",
        payload_json=symbols["build_ticket_created_payload"](
            settings,
            ticket=ticket,
            occurred_at=ticket.created_at,
        ),
        routing_result=routing_result,
        routing_target_name=None,
        routing_config_error_code=config_error_code,
        routing_config_error_summary=config_error_summary,
        created_at=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
    )
    fake_db.add(event)

    result = symbols["record_ticket_created_event"](
        fake_db,
        slack_runtime=_make_slack_runtime(
            settings,
            event_logger=lambda service, event_name, **payload: observed.append((service, event_name, payload)),
        ),
        ticket=ticket,
        initial_message=message,
    )

    assert result.event.id == event.id
    assert result.event_reused is True
    assert result.routing_result == routing_result
    assert result.recipient_target_count == 0
    assert result.target_name is None
    assert result.config_error_code == config_error_code
    assert result.config_error_summary == config_error_summary
    assert _integration_rows(fake_db, symbols, "IntegrationEventTarget") == []
    assert observed == [
        (
            "integration",
            "integration_event_recorded",
            {
                "event_id": str(event.id),
                "event_type": "ticket.created",
                "aggregate_type": "ticket",
                "aggregate_id": str(ticket.id),
                "dedupe_key": f"ticket.created:{ticket.id}",
                "routing_result": routing_result,
                "recipient_target_count": 0,
                "event_reused": True,
                **expected_log_fields,
            },
        )
    ]


@pytest.mark.parametrize("routing_result", ["created", None])
def test_duplicate_reuse_zero_target_falls_back_to_suppressed_notify_disabled_for_stale_or_missing_snapshot(tmp_path, routing_result):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path, slack=_make_slack_settings())
    fake_db = _FakeSession()
    ticket = _make_ticket(symbols)
    message = _make_public_message(symbols, ticket_id=ticket.id, source="ticket_create")
    event = symbols["IntegrationEvent"](
        id=uuid.uuid4(),
        source_system="autosac",
        event_type="ticket.created",
        aggregate_type="ticket",
        aggregate_id=ticket.id,
        dedupe_key=f"ticket.created:{ticket.id}",
        payload_json=symbols["build_ticket_created_payload"](
            settings,
            ticket=ticket,
            occurred_at=ticket.created_at,
        ),
        routing_result=routing_result,
        routing_target_name=None,
        routing_config_error_code=None,
        routing_config_error_summary=None,
        created_at=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
    )
    fake_db.add(event)

    result = symbols["record_ticket_created_event"](
        fake_db,
        slack_runtime=_make_slack_runtime(settings),
        ticket=ticket,
        initial_message=message,
    )

    assert result.event.id == event.id
    assert result.event_reused is True
    assert result.routing_result == "suppressed_notify_disabled"
    assert result.recipient_target_count == 0
    assert result.target_name is None
    assert _integration_rows(fake_db, symbols, "IntegrationEventTarget") == []


def test_invalid_config_emission_logs_suppression_without_row_state_fields(tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(
        tmp_path,
        slack=_make_slack_settings(
            is_valid=False,
            config_error_code="slack_targets_json_parse_error",
            config_error_summary="SLACK_TARGETS_JSON must be a valid JSON object",
        ),
    )
    fake_db = _FakeSession()
    ticket = _make_ticket(symbols)
    message = _make_public_message(symbols, ticket_id=ticket.id, source="ticket_create")
    observed = []

    symbols["record_ticket_created_event"](
        fake_db,
        slack_runtime=_make_slack_runtime(
            settings,
            event_logger=lambda service, event, **payload: observed.append((service, event, payload)),
        ),
        ticket=ticket,
        initial_message=message,
    )

    events = _integration_rows(fake_db, symbols, "IntegrationEvent")
    targets = _integration_rows(fake_db, symbols, "IntegrationEventTarget")

    assert len(events) == 1
    assert targets == []
    assert observed == [
        (
            "integration",
            "integration_event_recorded",
            {
                "event_id": str(events[0].id),
                "event_type": "ticket.created",
                "aggregate_type": "ticket",
                "aggregate_id": str(ticket.id),
                "dedupe_key": f"ticket.created:{ticket.id}",
                "routing_result": "suppressed_invalid_config",
                "recipient_target_count": 0,
                "config_error_code": "slack_targets_json_parse_error",
                "config_error_summary": "SLACK_TARGETS_JSON must be a valid JSON object",
            },
        )
    ]


def test_record_ticket_created_event_requires_explicit_runtime_context_even_with_session_settings(tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path, slack=_make_slack_settings())
    fake_db = _FakeSession()
    ticket = _make_ticket(symbols)
    message = _make_public_message(symbols, ticket_id=ticket.id, source="ticket_create")

    with pytest.raises(TypeError):
        symbols["record_ticket_created_event"](
            fake_db,
            ticket=ticket,
            initial_message=message,
        )
