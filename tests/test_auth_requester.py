from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
import re
import uuid

import pytest

from shared.config import Settings


def _load_ticketing_symbols():
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("argon2")
    from shared.models import AIRun, SessionRecord, Ticket, TicketAttachment, TicketMessage, TicketStatusHistory, TicketView, User
    from shared.ticketing import add_requester_reply, create_requester_ticket, resolve_ticket_for_requester

    return {
        "AIRun": AIRun,
        "SessionRecord": SessionRecord,
        "Ticket": Ticket,
        "TicketAttachment": TicketAttachment,
        "TicketMessage": TicketMessage,
        "TicketStatusHistory": TicketStatusHistory,
        "TicketView": TicketView,
        "User": User,
        "add_requester_reply": add_requester_reply,
        "create_requester_ticket": create_requester_ticket,
        "resolve_ticket_for_requester": resolve_ticket_for_requester,
    }


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value


class _FakeSession:
    def __init__(self, *, next_reference_num: int = 1):
        self.added = []
        self.commits = 0
        self.existing = {}
        self.next_reference_num = next_reference_num
        self.operations = []

    def add(self, item):
        self.added.append(item)
        self.operations.append(("add", item))
        key = getattr(item, "user_id", None), getattr(item, "ticket_id", None)
        if key != (None, None):
            self.existing[key] = item

    def flush(self):
        self.operations.append(("flush", None))

    def get(self, model, key):
        return self.existing.get(key)

    def execute(self, statement):
        return _FakeScalarResult(self.next_reference_num)


@dataclass
class _FakeImage:
    original_filename: str = "shot.png"
    mime_type: str = "image/png"
    sha256: str = "abc123"
    size_bytes: int = 128
    width: int = 40
    height: int = 20


@dataclass
class _FakeAttachment:
    original_filename: str = "notes.pdf"
    mime_type: str = "application/pdf"
    sha256: str = "pdf123"
    size_bytes: int = 256
    width: int | None = None
    height: int | None = None


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


def _make_settings(tmp_path: Path) -> Settings:
    workspace_dir = tmp_path / "workspace"
    return Settings(
        app_base_url="https://triage.example.test",
        app_secret_key="secret",
        database_url="postgresql+psycopg://triage:triage@localhost:5432/triage",
        uploads_dir=tmp_path / "uploads",
        triage_workspace_dir=workspace_dir,
        repo_mount_dir=workspace_dir / "app",
        manuals_mount_dir=workspace_dir / "manuals",
        codex_bin="codex",
        codex_api_key="key",
        codex_model="",
        codex_timeout_seconds=75,
        worker_poll_seconds=10,
        auto_support_reply_min_confidence=0.85,
        auto_confirm_intent_min_confidence=0.90,
        max_images_per_message=3,
        max_image_bytes=5 * 1024 * 1024,
        session_default_hours=12,
        session_remember_days=30,
    )


def _load_web_stack():
    pytest.importorskip("fastapi")
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("argon2")
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app import routes_auth, routes_ops, routes_requester
    from shared.db import db_session_dependency

    return {
        "TestClient": TestClient,
        "create_app": create_app,
        "routes_auth": routes_auth,
        "routes_ops": routes_ops,
        "routes_requester": routes_requester,
        "db_session_dependency": db_session_dependency,
    }


class _RouteDb:
    def __init__(self):
        self.commit_calls = 0
        self.rollback_calls = 0
        self.objects = {}

    def commit(self):
        self.commit_calls += 1

    def rollback(self):
        self.rollback_calls += 1

    def get(self, model, key):
        return self.objects.get((model, key))


class _FakePreauthStore:
    def __init__(self):
        self.records = {}
        self.counter = 0

    def create(self, db, *, next_path, user_agent, ip_address):
        self.counter += 1
        raw_token = f"preauth-{self.counter}"
        record = SimpleNamespace(
            csrf_token=f"csrf-{self.counter}",
            next_path=next_path,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        self.records[raw_token] = record
        return record, raw_token

    def get_valid(self, db, raw_token):
        return self.records.get(raw_token)

    def invalidate(self, db, raw_token):
        if raw_token:
            self.records.pop(raw_token, None)


def test_create_requester_ticket_creates_initial_records(monkeypatch, tmp_path):
    symbols = _load_ticketing_symbols()
    fake_db = _FakeSession(next_reference_num=17)
    requester = symbols["User"](
        id=uuid.uuid4(),
        email="requester@example.com",
        display_name="Requester",
        password_hash="hash",
        role="requester",
        is_active=True,
    )
    pending_run = symbols["AIRun"](
        ticket_id=uuid.uuid4(),
        status="pending",
        triggered_by="new_ticket",
        requested_by_user_id=requester.id,
    )
    monkeypatch.setattr("shared.ticketing.create_pending_ai_run", lambda *args, **kwargs: pending_run)

    ticket, message, attachments, run = symbols["create_requester_ticket"](
        fake_db,
        settings=_make_settings(tmp_path),
        requester=requester,
        title="",
        description_markdown="Email reports are missing after noon. Please check.",
        urgent=True,
        attachments=[_FakeImage()],
    )

    history = [item for item in fake_db.added if isinstance(item, symbols["TicketStatusHistory"])]
    views = [item for item in fake_db.added if isinstance(item, symbols["TicketView"])]

    assert ticket.reference == "T-000017"
    assert ticket.title == "Email reports are missing after noon."
    assert ticket.status == "new"
    assert ticket.urgent is True
    assert message.source == "ticket_create"
    assert message.visibility == "public"
    assert len(attachments) == 1
    assert str(ticket.id) in attachments[0].stored_path
    assert attachments[0].stored_path.endswith(".png")
    assert history[0].from_status is None
    assert history[0].to_status == "new"
    assert views[0].user_id == requester.id
    assert views[0].ticket_id == ticket.id
    assert run is pending_run


def test_add_requester_reply_reopens_and_requeues(monkeypatch, tmp_path):
    symbols = _load_ticketing_symbols()
    from shared.security import utc_now

    fake_db = _FakeSession()
    requester = symbols["User"](
        id=uuid.uuid4(),
        email="requester@example.com",
        display_name="Requester",
        password_hash="hash",
        role="requester",
        is_active=True,
    )
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=1,
        reference="T-000001",
        title="Closed ticket",
        created_by_user_id=requester.id,
        status="resolved",
        urgent=False,
        resolved_at=utc_now(),
    )
    existing_view = symbols["TicketView"](user_id=requester.id, ticket_id=ticket.id, last_viewed_at=utc_now())
    fake_db.existing[(requester.id, ticket.id)] = existing_view
    monkeypatch.setattr("shared.ticketing.create_pending_ai_run", lambda *args, **kwargs: None)

    message, attachments, run = symbols["add_requester_reply"](
        fake_db,
        settings=_make_settings(tmp_path),
        ticket=ticket,
        requester=requester,
        body_markdown="The issue came back after I retried it.",
        attachments=[],
    )

    history = [item for item in fake_db.added if isinstance(item, symbols["TicketStatusHistory"])]

    assert message.source == "requester_reply"
    assert attachments == []
    assert run is None
    assert ticket.status == "ai_triage"
    assert ticket.resolved_at is None
    assert ticket.requeue_requested is True
    assert ticket.requeue_trigger == "reopen"
    assert history[0].from_status == "resolved"
    assert history[0].to_status == "ai_triage"
    assert fake_db.existing[(requester.id, ticket.id)].last_viewed_at >= existing_view.last_viewed_at


def test_create_requester_ticket_persists_non_image_attachment_on_created_message(monkeypatch, tmp_path):
    symbols = _load_ticketing_symbols()
    fake_db = _FakeSession(next_reference_num=21)
    requester = symbols["User"](
        id=uuid.uuid4(),
        email="requester@example.com",
        display_name="Requester",
        password_hash="hash",
        role="requester",
        is_active=True,
    )
    pending_run = symbols["AIRun"](
        ticket_id=uuid.uuid4(),
        status="pending",
        triggered_by="new_ticket",
        requested_by_user_id=requester.id,
    )
    monkeypatch.setattr("shared.ticketing.create_pending_ai_run", lambda *args, **kwargs: pending_run)

    ticket, message, attachments, run = symbols["create_requester_ticket"](
        fake_db,
        settings=_make_settings(tmp_path),
        requester=requester,
        title="Attach a report",
        description_markdown="Please review the attached report export.",
        urgent=False,
        attachments=[_FakeAttachment()],
    )

    assert run is pending_run
    assert len(attachments) == 1
    _assert_flush_before_attachments(fake_db, attachments)
    assert attachments[0].message_id == message.id
    assert attachments[0].mime_type == "application/pdf"
    assert attachments[0].original_filename == "notes.pdf"
    assert attachments[0].width is None
    assert attachments[0].height is None
    assert attachments[0].stored_path.endswith(".pdf")
    assert str(ticket.id) in attachments[0].stored_path


def test_add_requester_reply_accepts_mixed_attachments(monkeypatch, tmp_path):
    symbols = _load_ticketing_symbols()
    fake_db = _FakeSession()
    requester = symbols["User"](
        id=uuid.uuid4(),
        email="requester@example.com",
        display_name="Requester",
        password_hash="hash",
        role="requester",
        is_active=True,
    )
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=1,
        reference="T-000001",
        title="Mixed uploads",
        created_by_user_id=requester.id,
        status="waiting_on_user",
        urgent=False,
    )
    monkeypatch.setattr("shared.ticketing.create_pending_ai_run", lambda *args, **kwargs: None)

    message, attachments, _ = symbols["add_requester_reply"](
        fake_db,
        settings=_make_settings(tmp_path),
        ticket=ticket,
        requester=requester,
        body_markdown="Attaching both a screenshot and a report.",
        attachments=[_FakeImage(), _FakeAttachment()],
    )

    assert [attachment.original_filename for attachment in attachments] == ["shot.png", "notes.pdf"]
    assert [attachment.mime_type for attachment in attachments] == ["image/png", "application/pdf"]
    _assert_flush_before_attachments(fake_db, attachments)
    assert all(attachment.message_id == message.id for attachment in attachments)


def test_resolve_ticket_for_requester_updates_status_and_view():
    symbols = _load_ticketing_symbols()
    fake_db = _FakeSession()
    requester = symbols["User"](
        id=uuid.uuid4(),
        email="requester@example.com",
        display_name="Requester",
        password_hash="hash",
        role="requester",
        is_active=True,
    )
    ticket = symbols["Ticket"](
        id=uuid.uuid4(),
        reference_num=2,
        reference="T-000002",
        title="Need help",
        created_by_user_id=requester.id,
        status="waiting_on_user",
        urgent=False,
    )

    symbols["resolve_ticket_for_requester"](fake_db, ticket=ticket, requester=requester)

    history = [item for item in fake_db.added if isinstance(item, symbols["TicketStatusHistory"])]
    views = [item for item in fake_db.added if isinstance(item, symbols["TicketView"])]

    assert ticket.status == "resolved"
    assert ticket.resolved_at is not None
    assert history[0].to_status == "resolved"
    assert views[0].user_id == requester.id
    assert views[0].ticket_id == ticket.id


def test_validate_csrf_token_rejects_mismatch():
    pytest.importorskip("fastapi")
    pytest.importorskip("argon2")
    from fastapi import HTTPException
    from app.auth import validate_csrf_token
    from shared.security import utc_now

    symbols = _load_ticketing_symbols()
    auth_session = symbols["SessionRecord"](
        user_id=uuid.uuid4(),
        token_hash="token",
        csrf_token="expected-token",
        remember_me=False,
        expires_at=utc_now(),
    )

    with pytest.raises(HTTPException) as excinfo:
        validate_csrf_token(auth_session, "wrong-token")
    assert excinfo.value.status_code == 403


def test_session_expiry_and_requester_status_mapping(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("argon2")
    from app.ui import REQUESTER_AUTHOR_LABELS, REQUESTER_STATUS_LABELS
    from shared.security import calculate_session_expiry, utc_now

    settings = _make_settings(tmp_path)
    now = utc_now()

    remember_expiry = calculate_session_expiry(settings, True, now=now)
    default_expiry = calculate_session_expiry(settings, False, now=now)

    assert int((remember_expiry - now).total_seconds()) == 30 * 24 * 60 * 60
    assert int((default_expiry - now).total_seconds()) == 12 * 60 * 60
    assert REQUESTER_STATUS_LABELS["new"] == "Reviewing"
    assert REQUESTER_STATUS_LABELS["waiting_on_user"] == "Waiting for your reply"
    assert REQUESTER_AUTHOR_LABELS["dev_ti"] == "Team"


def test_requester_routes_source_uses_custom_auth_and_explicit_multipart_limits():
    source = Path("app/routes_requester.py").read_text(encoding="utf-8")
    upload_source = Path("app/uploads.py").read_text(encoding="utf-8")
    ui_source = Path("app/ui.py").read_text(encoding="utf-8")
    template_source = Path("app/templates/requester_ticket_detail.html").read_text(encoding="utf-8")

    assert '"/app/tickets/{reference}/reply"' in source
    assert "parse_multipart_form(request, settings)" in source
    assert "require_requester_user" in source
    assert "MULTIPART_PART_SIZE_SLACK_BYTES" in upload_source
    assert "max_part_size=settings.max_image_bytes + MULTIPART_PART_SIZE_SLACK_BYTES" in upload_source
    assert '"dev_ti": "Team"' in ui_source
    assert "requester_author_label(message.author_type)" in source
    assert "message.author_label" in template_source


def test_ticket_access_guard_allows_requester_and_ops_roles():
    pytest.importorskip("fastapi")
    from app.auth import require_requester_user

    requester = SimpleNamespace(role="requester")
    dev_ti = SimpleNamespace(role="dev_ti")
    admin = SimpleNamespace(role="admin")

    assert require_requester_user(requester) is requester
    assert require_requester_user(dev_ti) is dev_ti
    assert require_requester_user(admin) is admin


def test_ops_user_management_routes_and_role_limits_are_present():
    source = Path("app/routes_ops.py").read_text(encoding="utf-8")
    template_source = Path("app/templates/ops_users.html").read_text(encoding="utf-8")
    base_template_source = Path("app/templates/base.html").read_text(encoding="utf-8")

    assert '"/ops/users"' in source
    assert '"/ops/users/create"' in source
    assert "if actor.role == \"admin\"" in source
    assert "return (\"requester\",)" in source
    assert "Create user" in template_source
    assert "/ops/users" in base_template_source


def test_login_route_sets_remember_me_cookie(monkeypatch, tmp_path):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    preauth = _FakePreauthStore()
    requester = SimpleNamespace(
        id=uuid.uuid4(),
        email="requester@example.com",
        display_name="Requester",
        password_hash="hash",
        role="requester",
        is_active=True,
    )
    settings = _make_settings(tmp_path)
    observed = {}

    def fake_begin_user_session(*, request, response, db, user, remember_me, settings):
        observed["remember_me"] = remember_me
        response.set_cookie(
            "triage_session",
            "opaque-token",
            max_age=settings.session_remember_days * 24 * 60 * 60 if remember_me else None,
            expires=settings.session_remember_days * 24 * 60 * 60 if remember_me else None,
            httponly=True,
            samesite="lax",
            secure=settings.secure_cookies,
            path="/",
        )

    monkeypatch.setattr(stack["routes_auth"], "get_user_by_email", lambda db, email: requester)
    monkeypatch.setattr(stack["routes_auth"], "verify_password", lambda password, password_hash: True)
    monkeypatch.setattr(stack["routes_auth"], "begin_user_session", fake_begin_user_session)
    monkeypatch.setattr(stack["routes_auth"], "create_preauth_login_session", preauth.create)
    monkeypatch.setattr(stack["routes_auth"], "get_valid_preauth_login_session", preauth.get_valid)
    monkeypatch.setattr(stack["routes_auth"], "invalidate_preauth_login_session", preauth.invalidate)

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_auth"].get_current_user_optional] = lambda: None
    app.dependency_overrides[stack["routes_auth"].get_settings_dependency] = lambda: settings

    with stack["TestClient"](app, base_url="https://testserver") as client:
        login_page = client.get("/login?next=/app")
        csrf_token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
        response = client.post(
            "/login",
            data={"email": requester.email, "password": "secret", "remember_me": "on", "csrf_token": csrf_token},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/app"
    assert any("Max-Age=2592000" in header for header in response.headers.get_list("set-cookie"))
    assert any("triage_preauth_login=\"\"" in header for header in response.headers.get_list("set-cookie"))
    assert observed["remember_me"] is True
    assert db.commit_calls == 2


def test_root_route_redirects_to_app():
    stack = _load_web_stack()
    app = stack["create_app"]()

    with stack["TestClient"](app, base_url="https://testserver") as client:
        response = client.get("/", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/app"


def test_login_get_issues_preauth_challenge_with_sanitized_next(monkeypatch, tmp_path):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    preauth = _FakePreauthStore()
    settings = _make_settings(tmp_path)

    monkeypatch.setattr(stack["routes_auth"], "create_preauth_login_session", preauth.create)
    monkeypatch.setattr(stack["routes_auth"], "invalidate_preauth_login_session", preauth.invalidate)

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_auth"].get_current_user_optional] = lambda: None
    app.dependency_overrides[stack["routes_auth"].get_settings_dependency] = lambda: settings

    with stack["TestClient"](app, base_url="https://testserver") as client:
        response = client.get("/login?next=https://evil.example/ops", follow_redirects=False)

    assert response.status_code == 200
    assert re.search(r'name="csrf_token" value="csrf-1"', response.text)
    assert "triage_preauth_login=preauth-1" in response.headers["set-cookie"]
    assert preauth.records["preauth-1"].next_path is None
    assert db.commit_calls == 1


def test_login_post_rejects_missing_or_invalid_preauth_csrf_before_auth(monkeypatch, tmp_path):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    preauth = _FakePreauthStore()
    settings = _make_settings(tmp_path)

    monkeypatch.setattr(stack["routes_auth"], "create_preauth_login_session", preauth.create)
    monkeypatch.setattr(stack["routes_auth"], "get_valid_preauth_login_session", preauth.get_valid)
    monkeypatch.setattr(stack["routes_auth"], "invalidate_preauth_login_session", preauth.invalidate)
    monkeypatch.setattr(
        stack["routes_auth"],
        "get_user_by_email",
        lambda db, email: (_ for _ in ()).throw(AssertionError("authentication should not run")),
    )

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_auth"].get_current_user_optional] = lambda: None
    app.dependency_overrides[stack["routes_auth"].get_settings_dependency] = lambda: settings

    with stack["TestClient"](app, base_url="https://testserver") as client:
        first = client.get("/login?next=/ops")
        initial_csrf = re.search(r'name="csrf_token" value="([^"]+)"', first.text).group(1)

        invalid_csrf = client.post(
            "/login",
            data={"email": "ops@example.com", "password": "secret", "csrf_token": "wrong-token"},
            follow_redirects=False,
        )
        assert invalid_csrf.status_code == 403
        assert "Invalid login form token." in invalid_csrf.text
        assert initial_csrf not in invalid_csrf.text
        assert "triage_preauth_login=preauth-2" in invalid_csrf.headers["set-cookie"]

        client.cookies.clear()
        missing_state = client.post(
            "/login",
            data={"email": "ops@example.com", "password": "secret", "csrf_token": "anything"},
            follow_redirects=False,
        )

    assert missing_state.status_code == 403
    assert "Your login form expired." in missing_state.text
    assert "triage_preauth_login=preauth-3" in missing_state.headers["set-cookie"]
    assert db.commit_calls == 3


def test_login_post_failed_credentials_rotates_preauth_challenge(monkeypatch, tmp_path):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    preauth = _FakePreauthStore()
    requester = SimpleNamespace(
        id=uuid.uuid4(),
        email="requester@example.com",
        display_name="Requester",
        password_hash="hash",
        role="requester",
        is_active=True,
    )
    settings = _make_settings(tmp_path)

    monkeypatch.setattr(stack["routes_auth"], "create_preauth_login_session", preauth.create)
    monkeypatch.setattr(stack["routes_auth"], "get_valid_preauth_login_session", preauth.get_valid)
    monkeypatch.setattr(stack["routes_auth"], "invalidate_preauth_login_session", preauth.invalidate)
    monkeypatch.setattr(stack["routes_auth"], "get_user_by_email", lambda db, email: requester)
    monkeypatch.setattr(stack["routes_auth"], "verify_password", lambda password, password_hash: False)
    monkeypatch.setattr(
        stack["routes_auth"],
        "begin_user_session",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("session should not start on invalid credentials")),
    )

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_auth"].get_current_user_optional] = lambda: None
    app.dependency_overrides[stack["routes_auth"].get_settings_dependency] = lambda: settings

    with stack["TestClient"](app, base_url="https://testserver") as client:
        first = client.get("/login?next=/app")
        initial_csrf = re.search(r'name="csrf_token" value="([^"]+)"', first.text).group(1)

        failed = client.post(
            "/login",
            data={"email": requester.email, "password": "wrong-secret", "csrf_token": initial_csrf},
            follow_redirects=False,
        )

    assert failed.status_code == 400
    assert "Invalid email or password." in failed.text
    assert initial_csrf not in failed.text
    assert re.search(r'name="csrf_token" value="csrf-2"', failed.text)
    assert "triage_preauth_login=preauth-2" in failed.headers["set-cookie"]
    assert "preauth-1" not in preauth.records
    assert preauth.records["preauth-2"].next_path == "/app"
    assert db.commit_calls == 2


def test_protected_html_get_redirects_to_login_with_safe_next(monkeypatch):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_auth"].get_optional_auth_session] = lambda: None

    with stack["TestClient"](app) as client:
        response = client.get("/app/tickets/new?draft=1", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fapp%2Ftickets%2Fnew%3Fdraft%3D1"


@pytest.mark.parametrize("role", ["dev_ti", "admin"])
def test_ops_roles_can_open_new_ticket_page(role):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    current_user = SimpleNamespace(id=uuid.uuid4(), display_name=role.upper(), role=role, is_active=True)
    auth_session = SimpleNamespace(csrf_token="csrf-token")

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_requester"].get_current_user] = lambda: current_user
    app.dependency_overrides[stack["routes_requester"].get_required_auth_session] = lambda: auth_session

    with stack["TestClient"](app) as client:
        response = client.get("/app/tickets/new")

    assert response.status_code == 200
    assert '<form method="post" action="/app/tickets"' in response.text
    assert db.commit_calls == 1


@pytest.mark.parametrize("role", ["dev_ti", "admin"])
def test_ops_roles_can_submit_new_ticket_through_requester_flow(monkeypatch, tmp_path, role):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    settings = _make_settings(tmp_path)
    current_user = SimpleNamespace(id=uuid.uuid4(), display_name=role.upper(), role=role, is_active=True)
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    captured = {}

    async def fake_parse_ticket_create_form(request, *, settings):
        return "Printer issue", "Queue is stuck after login.", True, "csrf-token", []

    def fake_create_requester_ticket(db, *, settings, requester, title, description_markdown, urgent, attachments):
        captured.update(
            {
                "settings": settings,
                "requester": requester,
                "title": title,
                "description_markdown": description_markdown,
                "urgent": urgent,
                "attachments": attachments,
            }
        )
        return SimpleNamespace(reference="T-000321"), None, [], None

    monkeypatch.setattr(stack["routes_requester"], "_parse_ticket_create_form", fake_parse_ticket_create_form)
    monkeypatch.setattr(stack["routes_requester"], "create_requester_ticket", fake_create_requester_ticket)

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_requester"].get_current_user] = lambda: current_user
    app.dependency_overrides[stack["routes_requester"].get_required_auth_session] = lambda: auth_session
    app.dependency_overrides[stack["routes_requester"].get_settings] = lambda: settings

    with stack["TestClient"](app) as client:
        response = client.post("/app/tickets", data={"csrf_token": "csrf-token"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/app/tickets/T-000321"
    assert captured["settings"] is settings
    assert captured["requester"] is current_user
    assert captured["title"] == "Printer issue"
    assert captured["description_markdown"] == "Queue is stuck after login."
    assert captured["urgent"] is True
    assert captured["attachments"] == []
    assert db.commit_calls == 1


def test_protected_htmx_get_keeps_401_instead_of_redirect():
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_auth"].get_optional_auth_session] = lambda: None

    with stack["TestClient"](app) as client:
        response = client.get("/app", headers={"HX-Request": "true"}, follow_redirects=False)

    assert response.status_code == 401
    assert "location" not in response.headers


def test_wrong_role_still_returns_403_instead_of_redirect():
    stack = _load_web_stack()
    app = stack["create_app"]()

    def deny():
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="Requester access required")

    app.dependency_overrides[stack["routes_requester"].require_requester_user] = deny

    with stack["TestClient"](app) as client:
        response = client.get("/app", follow_redirects=False)

    assert response.status_code == 403
    assert "location" not in response.headers


def test_safe_next_path_rejects_external_empty_and_recursive_targets():
    pytest.importorskip("fastapi")
    from app.ui import sanitize_next_path

    assert sanitize_next_path("/ops?status=new") == "/ops?status=new"
    assert sanitize_next_path("https://evil.example/ops") is None
    assert sanitize_next_path("//evil.example/ops") is None
    assert sanitize_next_path("") is None
    assert sanitize_next_path("/login?next=/ops") is None


def test_module_relative_paths_support_rendering_and_static_assets(monkeypatch, tmp_path):
    stack = _load_web_stack()
    from app import ui

    monkeypatch.chdir(tmp_path)
    app = stack["create_app"]()
    settings = _make_settings(tmp_path)

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: _RouteDb()
    app.dependency_overrides[stack["routes_auth"].get_current_user_optional] = lambda: None
    app.dependency_overrides[stack["routes_auth"].get_settings_dependency] = lambda: settings
    monkeypatch.setattr(
        stack["routes_auth"],
        "_issue_login_challenge",
        lambda **kwargs: ui.templates.TemplateResponse(
            kwargs["request"],
            "login.html",
            ui.build_template_context(
                request=kwargs["request"],
                current_user=None,
                extra={"login_csrf_token": "csrf-token"},
            ),
        ),
    )

    with stack["TestClient"](app, base_url="https://testserver") as client:
        login_response = client.get("/login")
        static_response = client.get("/static/app.css")

    assert login_response.status_code == 200
    assert static_response.status_code == 200


def test_logout_route_rejects_invalid_csrf_without_committing():
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_auth"].get_required_auth_session] = lambda: SimpleNamespace(csrf_token="expected")

    with stack["TestClient"](app) as client:
        response = client.post("/logout", data={"csrf_token": "wrong"}, follow_redirects=False)

    assert response.status_code == 403
    assert db.commit_calls == 0


def test_requester_list_route_does_not_mark_ticket_as_read(monkeypatch):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    requester = SimpleNamespace(id=uuid.uuid4(), display_name="Requester", role="requester")
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    observed = {"view_updates": 0}

    monkeypatch.setattr(stack["routes_requester"], "_ticket_list_rows", lambda db, requester_id: [])
    monkeypatch.setattr(
        stack["routes_requester"],
        "upsert_ticket_view",
        lambda *args, **kwargs: observed.__setitem__("view_updates", observed["view_updates"] + 1),
    )

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_requester"].require_requester_user] = lambda: requester
    app.dependency_overrides[stack["routes_requester"].get_required_auth_session] = lambda: auth_session

    with stack["TestClient"](app) as client:
        response = client.get("/app")

    assert response.status_code == 200
    assert observed["view_updates"] == 0


def test_requester_detail_route_marks_ticket_as_read(monkeypatch):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    requester = SimpleNamespace(id=uuid.uuid4(), display_name="Requester", role="requester")
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    ticket = SimpleNamespace(reference="T-000001", id=uuid.uuid4(), title="Ticket", status="new", urgent=False)
    observed = {"view_updates": 0}

    monkeypatch.setattr(stack["routes_requester"], "_load_requester_ticket_or_404", lambda *args, **kwargs: ticket)
    monkeypatch.setattr(stack["routes_requester"], "_serialize_public_thread", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        stack["routes_requester"],
        "upsert_ticket_view",
        lambda *args, **kwargs: observed.__setitem__("view_updates", observed["view_updates"] + 1),
    )

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_requester"].require_requester_user] = lambda: requester
    app.dependency_overrides[stack["routes_requester"].get_required_auth_session] = lambda: auth_session

    with stack["TestClient"](app) as client:
        response = client.get(f"/app/tickets/{ticket.reference}")

    assert response.status_code == 200
    assert observed["view_updates"] == 1
    assert db.commit_calls == 1


def test_serialize_public_thread_includes_message_attachments(monkeypatch):
    stack = _load_web_stack()
    first_message_id = uuid.uuid4()
    second_message_id = uuid.uuid4()
    messages = [
        SimpleNamespace(
            id=first_message_id,
            created_at=SimpleNamespace(),
            author_type="requester",
            source="ticket_create",
            body_markdown="Attached a report",
        ),
        SimpleNamespace(
            id=second_message_id,
            created_at=SimpleNamespace(),
            author_type="requester",
            source="requester_reply",
            body_markdown="No attachment here",
        ),
    ]
    linked_attachment = SimpleNamespace(
        id=uuid.uuid4(),
        message_id=first_message_id,
        original_filename="notes.pdf",
        mime_type="application/pdf",
        size_bytes=256,
    )

    monkeypatch.setattr(stack["routes_requester"], "_load_public_ticket_messages", lambda db, ticket_id: messages)
    monkeypatch.setattr(
        stack["routes_requester"],
        "_load_attachments_by_message",
        lambda db, ticket_id, visibility="public": {first_message_id: [linked_attachment]},
    )
    monkeypatch.setattr(stack["routes_requester"], "render_markdown_to_html", lambda body: f"<p>{body}</p>")

    thread = stack["routes_requester"]._serialize_public_thread(object(), ticket_id=uuid.uuid4())

    assert thread[0]["attachments"] == [linked_attachment]
    assert thread[1]["attachments"] == []


def test_requester_detail_renders_attachment_links(monkeypatch):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    requester = SimpleNamespace(id=uuid.uuid4(), display_name="Requester", role="requester")
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    ticket = SimpleNamespace(reference="T-000001", id=uuid.uuid4(), title="Ticket", status="new", urgent=False)
    attachment_id = uuid.uuid4()
    thread = [
        {
            "id": str(uuid.uuid4()),
            "created_at": SimpleNamespace(strftime=lambda fmt: "2026-03-26 19:00 UTC"),
            "author_label": "You",
            "body_html": "<p>See the attached report.</p>",
            "attachments": [
                SimpleNamespace(
                    id=attachment_id,
                    original_filename="notes.pdf",
                    mime_type="application/pdf",
                    size_bytes=256,
                )
            ],
        }
    ]

    monkeypatch.setattr(stack["routes_requester"], "_load_requester_ticket_or_404", lambda *args, **kwargs: ticket)
    monkeypatch.setattr(stack["routes_requester"], "_serialize_public_thread", lambda *args, **kwargs: thread)

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_requester"].require_requester_user] = lambda: requester
    app.dependency_overrides[stack["routes_requester"].get_required_auth_session] = lambda: auth_session

    with stack["TestClient"](app) as client:
        response = client.get(f"/app/tickets/{ticket.reference}")

    assert response.status_code == 200
    assert f'/attachments/{attachment_id}' in response.text
    assert "notes.pdf" in response.text
    assert "application/pdf" in response.text


def test_attachment_download_forbids_non_owner_requester():
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    requester = SimpleNamespace(id=uuid.uuid4(), display_name="Requester", role="requester")
    attachment_id = uuid.uuid4()
    attachment = SimpleNamespace(
        id=attachment_id,
        ticket_id=uuid.uuid4(),
        visibility="public",
        mime_type="image/png",
        stored_path="/tmp/test.png",
        original_filename="shot.png",
    )
    other_ticket = SimpleNamespace(id=attachment.ticket_id, created_by_user_id=uuid.uuid4())
    from shared.models import Ticket, TicketAttachment

    db.objects[(TicketAttachment, attachment_id)] = attachment
    db.objects[(Ticket, attachment.ticket_id)] = other_ticket

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_requester"].get_current_user] = lambda: requester

    with stack["TestClient"](app) as client:
        response = client.get(f"/attachments/{attachment_id}")

    assert response.status_code == 403
    assert db.commit_calls == 0


def test_attachment_download_uses_stored_mime_type_and_filename(monkeypatch):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    requester = SimpleNamespace(id=uuid.uuid4(), display_name="Requester", role="requester")
    attachment_id = uuid.uuid4()
    attachment = SimpleNamespace(
        id=attachment_id,
        ticket_id=uuid.uuid4(),
        visibility="public",
        mime_type="application/pdf",
        stored_path="/tmp/report.pdf",
        original_filename="report.pdf",
    )
    owned_ticket = SimpleNamespace(id=attachment.ticket_id, created_by_user_id=requester.id)
    captured = {}
    from fastapi import Response
    from shared.models import Ticket, TicketAttachment

    def fake_file_response(*, path, media_type, filename):
        captured.update({"path": path, "media_type": media_type, "filename": filename})
        return Response(content="ok", media_type=media_type, headers={"X-Filename": filename})

    db.objects[(TicketAttachment, attachment_id)] = attachment
    db.objects[(Ticket, attachment.ticket_id)] = owned_ticket

    monkeypatch.setattr(stack["routes_requester"], "FileResponse", fake_file_response)

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_requester"].get_current_user] = lambda: requester

    with stack["TestClient"](app) as client:
        response = client.get(f"/attachments/{attachment_id}")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["x-filename"] == "report.pdf"
    assert captured == {
        "path": "/tmp/report.pdf",
        "media_type": "application/pdf",
        "filename": "report.pdf",
    }
    assert db.commit_calls == 1
