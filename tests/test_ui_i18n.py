from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import re
import uuid

import pytest

from shared.config import Settings


class _RouteDb:
    def __init__(self):
        self.commit_calls = 0
        self.rollback_calls = 0

    def commit(self):
        self.commit_calls += 1

    def rollback(self):
        self.rollback_calls += 1


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

    def invalidate(self, db, raw_token):
        if raw_token:
            self.records.pop(raw_token, None)


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
        codex_timeout_seconds=3600,
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
    from fastapi.testclient import TestClient
    from app import auth, routes_auth, routes_ops, routes_requester
    from app.main import create_app
    from shared.db import db_session_dependency

    return {
        "TestClient": TestClient,
        "auth": auth,
        "routes_auth": routes_auth,
        "routes_ops": routes_ops,
        "routes_requester": routes_requester,
        "create_app": create_app,
        "db_session_dependency": db_session_dependency,
    }


def test_translation_catalogs_have_matching_keys():
    pytest.importorskip("fastapi")
    from app.i18n import SUPPORTED_UI_LOCALES, translation_catalog

    key_sets = {locale: set(translation_catalog(locale).keys()) for locale in SUPPORTED_UI_LOCALES}
    assert key_sets["en"] == key_sets["pt-BR"]


def test_locale_resolution_prefers_cookie_then_accept_language():
    pytest.importorskip("fastapi")
    from app.i18n import locale_from_accept_language, resolve_ui_locale

    request = SimpleNamespace(cookies={"triage_ui_locale": "en"}, headers={"accept-language": "pt-BR,pt;q=0.9"})
    assert resolve_ui_locale(request) == "en"

    request = SimpleNamespace(cookies={}, headers={"accept-language": "pt, en-US;q=0.8"})
    assert resolve_ui_locale(request) == "pt-BR"
    assert locale_from_accept_language("en-US,en;q=0.9") == "en"

    request = SimpleNamespace(cookies={}, headers={})
    assert resolve_ui_locale(request, default_locale="pt-BR") == "pt-BR"


def test_ui_language_route_sets_cookie_and_redirects_back_to_login(monkeypatch, tmp_path):
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
        response = client.get("/ui-language?locale=pt-BR&next=%2Flogin%3Fnext%3D%252Fapp", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fapp"
    assert "triage_ui_locale=pt-BR" in response.headers["set-cookie"]


def test_ui_language_route_ignores_invalid_locale_and_unsafe_next(monkeypatch, tmp_path):
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
        response = client.get("/ui-language?locale=es&next=https://evil.example/ops", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/app"
    assert "triage_ui_locale=" not in response.headers.get("set-cookie", "")


def test_login_page_renders_portuguese_from_accept_language(monkeypatch, tmp_path):
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
        response = client.get("/login", headers={"Accept-Language": "pt-BR"}, follow_redirects=False)

    assert response.status_code == 200
    assert '<html lang="pt-BR">' in response.text
    assert "Acesso interno" in response.text
    assert "Entrar" in response.text


def test_requester_page_renders_portuguese_empty_state(monkeypatch):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    requester = SimpleNamespace(id=uuid.uuid4(), display_name="Requester", role="requester")
    auth_session = SimpleNamespace(csrf_token="csrf-token")

    monkeypatch.setattr(stack["routes_requester"], "_ticket_list_rows", lambda db, requester_id: [])

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_requester"].require_requester_user] = lambda: requester
    app.dependency_overrides[stack["routes_requester"].get_required_auth_session] = lambda: auth_session

    with stack["TestClient"](app) as client:
        response = client.get("/app", headers={"Accept-Language": "pt-BR"})

    assert response.status_code == 200
    assert '<html lang="pt-BR">' in response.text
    assert "Meus tickets" in response.text
    assert "Criar seu primeiro ticket" in response.text


def test_ops_fragment_renders_portuguese_filters(monkeypatch):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    ops_user = SimpleNamespace(id=uuid.uuid4(), display_name="Ops", role="dev_ti")
    auth_session = SimpleNamespace(csrf_token="csrf-token")

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

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_ops"].require_ops_user] = lambda: ops_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session

    with stack["TestClient"](app) as client:
        response = client.get("/ops?status=new", headers={"HX-Request": "true", "Accept-Language": "pt-BR"})

    assert response.status_code == 200
    assert "<html" not in response.text
    assert "Nenhum ticket correspondente" in response.text
    assert "Ajuste os filtros para ampliar a fila." in response.text


def test_browser_visible_http_error_translates_to_portuguese():
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    requester = SimpleNamespace(id=uuid.uuid4(), display_name="Requester", role="requester", is_active=True)

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["auth"].get_current_user] = lambda: requester
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: SimpleNamespace(csrf_token="csrf")

    with stack["TestClient"](app) as client:
        response = client.get("/ops", headers={"Accept-Language": "pt-BR"})

    assert response.status_code == 403
    assert response.json() == {"detail": "Acesso de operações é obrigatório"}


def test_multipart_errors_translate_to_portuguese():
    pytest.importorskip("fastapi")
    from app.i18n import translate_error_text

    assert translate_error_text("Too many files. Maximum number of files is 3.", "pt-BR") == "Anexe no máximo 3 arquivos."
    assert (
        translate_error_text("Part exceeded maximum size of 5184KB.", "pt-BR")
        == "O arquivo excede o limite de tamanho para upload."
    )
    assert translate_error_text("Too many fields. Maximum number of fields is 1000.", "pt-BR") == "Há campos demais no formulário."
    assert translate_error_text("Missing boundary in multipart.", "pt-BR") == "Solicitação de upload inválida."


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("message_preview_max_chars must be greater than or equal to 4", "message_preview_max_chars deve ser maior ou igual a 4"),
        ("http_timeout_seconds must be between 1 and 30 inclusive", "http_timeout_seconds deve ficar entre 1 e 30, inclusive"),
        ("delivery_batch_size must be greater than or equal to 1", "delivery_batch_size deve ser maior ou igual a 1"),
        ("delivery_max_attempts must be greater than or equal to 1", "delivery_max_attempts deve ser maior ou igual a 1"),
        (
            "delivery_stale_lock_seconds must be greater than http_timeout_seconds",
            "delivery_stale_lock_seconds deve ser maior que http_timeout_seconds",
        ),
    ],
)
def test_slack_settings_validation_errors_translate_to_portuguese(message, expected):
    pytest.importorskip("fastapi")
    from app.i18n import translate_error_text

    assert translate_error_text(message, "pt-BR") == expected


def test_requester_create_error_uses_get_path_for_language_switch(monkeypatch, tmp_path):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    settings = _make_settings(tmp_path)
    requester = SimpleNamespace(id=uuid.uuid4(), display_name="Requester", role="requester")
    auth_session = SimpleNamespace(csrf_token="csrf-token")

    async def fake_parse_ticket_create_form(request, *, settings):
        return "Printer issue", "", False, "csrf-token", []

    monkeypatch.setattr(stack["routes_requester"], "_parse_ticket_create_form", fake_parse_ticket_create_form)

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_requester"].require_requester_user] = lambda: requester
    app.dependency_overrides[stack["routes_requester"].get_required_auth_session] = lambda: auth_session
    app.dependency_overrides[stack["routes_requester"].get_settings] = lambda: settings

    with stack["TestClient"](app) as client:
        response = client.post("/app/tickets", data={"csrf_token": "csrf-token"}, follow_redirects=False)

    assert response.status_code == 400
    assert "/ui-language?locale=en&amp;next=%2Fapp%2Ftickets%2Fnew" in response.text
    assert "/ui-language?locale=pt-BR&amp;next=%2Fapp%2Ftickets%2Fnew" in response.text


def test_requester_reply_error_uses_ticket_detail_for_language_switch(monkeypatch, tmp_path):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    settings = _make_settings(tmp_path)
    requester = SimpleNamespace(id=uuid.uuid4(), display_name="Requester", role="requester")
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    ticket = SimpleNamespace(reference="T-000123", id=uuid.uuid4(), title="Ticket", status="new", urgent=False)

    async def fake_parse_requester_message_form(request, *, settings):
        return "", "csrf-token", []

    monkeypatch.setattr(stack["routes_requester"], "_load_requester_ticket_or_404", lambda *args, **kwargs: ticket)
    monkeypatch.setattr(stack["routes_requester"], "_parse_requester_message_form", fake_parse_requester_message_form)
    monkeypatch.setattr(stack["routes_requester"], "_build_requester_timeline", lambda *args, **kwargs: [])

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_requester"].require_requester_user] = lambda: requester
    app.dependency_overrides[stack["routes_requester"].get_required_auth_session] = lambda: auth_session
    app.dependency_overrides[stack["routes_requester"].get_settings] = lambda: settings

    with stack["TestClient"](app) as client:
        response = client.post(f"/app/tickets/{ticket.reference}/reply", data={"csrf_token": "csrf-token"}, follow_redirects=False)

    assert response.status_code == 400
    assert f"/ui-language?locale=en&amp;next=%2Fapp%2Ftickets%2F{ticket.reference}" in response.text
    assert f"/ui-language?locale=pt-BR&amp;next=%2Fapp%2Ftickets%2F{ticket.reference}" in response.text


def test_ops_user_create_error_uses_users_get_path_for_language_switch(monkeypatch):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    ops_user = SimpleNamespace(id=uuid.uuid4(), display_name="Ops", role="admin")
    auth_session = SimpleNamespace(csrf_token="csrf-token")

    def fail_create_user(*args, **kwargs):
        raise ValueError("User already exists: existing@example.com")

    monkeypatch.setattr(stack["routes_ops"], "create_user", fail_create_user)
    monkeypatch.setattr(stack["routes_ops"], "_load_users_for_admin", lambda db: [])

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_ops"].require_ops_user] = lambda: ops_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session

    with stack["TestClient"](app) as client:
        response = client.post(
            "/ops/users/create",
            data={
                "csrf_token": "csrf-token",
                "email": "existing@example.com",
                "display_name": "Existing User",
                "password": "password123",
                "role": "requester",
            },
            follow_redirects=False,
        )

    assert response.status_code == 400
    assert "/ui-language?locale=en&amp;next=%2Fops%2Fusers%3Fcreate%3D1" in response.text
    assert "/ui-language?locale=pt-BR&amp;next=%2Fops%2Fusers%3Fcreate%3D1" in response.text


def test_ops_user_update_error_uses_editing_users_get_path_for_language_switch(monkeypatch):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    ops_user = SimpleNamespace(id=uuid.uuid4(), display_name="Ops", role="admin")
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    target_user = SimpleNamespace(
        id=uuid.uuid4(),
        email="existing@example.com",
        display_name="Existing User",
        role="dev_ti",
        slack_user_id="UEXISTING",
        is_active=True,
    )

    monkeypatch.setattr(stack["routes_ops"], "_load_user_or_404", lambda db, user_id: target_user)
    monkeypatch.setattr(stack["routes_ops"], "_load_users_for_admin", lambda db: [target_user])
    monkeypatch.setattr(
        stack["routes_ops"],
        "update_user",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("Display name is required.")),
    )

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_ops"].require_ops_user] = lambda: ops_user
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
    assert f"/ui-language?locale=en&amp;next=%2Fops%2Fusers%3Fedit_user%3D{target_user.id}" in response.text
    assert f"/ui-language?locale=pt-BR&amp;next=%2Fops%2Fusers%3Fedit_user%3D{target_user.id}" in response.text


def test_ops_slack_integration_error_uses_slack_get_path_for_language_switch(monkeypatch, tmp_path):
    from shared.config import SlackSettings
    from shared.slack_dm import SlackWebApiResponse

    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    settings = _make_settings(tmp_path)
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
    app.dependency_overrides[stack["routes_ops"].require_admin_user] = lambda: admin_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session
    app.dependency_overrides[stack["routes_ops"].get_settings] = lambda: settings

    with stack["TestClient"](app) as client:
        response = client.post(
            "/ops/integrations/slack",
            data={
                "csrf_token": "csrf-token",
                "enabled": "on",
                "bot_token": "xoxb-invalid",
                "notify_ticket_created": "on",
                "notify_public_message_added": "on",
                "notify_status_changed": "on",
                "message_preview_max_chars": "200",
                "http_timeout_seconds": "10",
                "delivery_batch_size": "10",
                "delivery_max_attempts": "5",
                "delivery_stale_lock_seconds": "120",
            },
            follow_redirects=False,
        )

    assert response.status_code == 400
    assert "/ui-language?locale=en&amp;next=%2Fops%2Fintegrations%2Fslack" in response.text
    assert "/ui-language?locale=pt-BR&amp;next=%2Fops%2Fintegrations%2Fslack" in response.text


def test_ops_slack_integration_numeric_validation_error_translates_to_portuguese(monkeypatch, tmp_path):
    from shared.config import SlackSettings

    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    settings = _make_settings(tmp_path)
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
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("auth.test should not run for invalid numeric settings")),
    )

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_ops"].require_admin_user] = lambda: admin_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session
    app.dependency_overrides[stack["routes_ops"].get_settings] = lambda: settings

    with stack["TestClient"](app) as client:
        response = client.post(
            "/ops/integrations/slack",
            data={
                "csrf_token": "csrf-token",
                "message_preview_max_chars": "3",
                "http_timeout_seconds": "10",
                "delivery_batch_size": "10",
                "delivery_max_attempts": "5",
                "delivery_stale_lock_seconds": "120",
            },
            headers={"Accept-Language": "pt-BR"},
            follow_redirects=False,
        )

    assert response.status_code == 400
    assert "message_preview_max_chars deve ser maior ou igual a 4" in response.text
    assert "/ui-language?locale=en&amp;next=%2Fops%2Fintegrations%2Fslack" in response.text
    assert "/ui-language?locale=pt-BR&amp;next=%2Fops%2Fintegrations%2Fslack" in response.text


def test_ops_set_user_active_invalid_state_is_translated(monkeypatch):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    ops_user = SimpleNamespace(id=uuid.uuid4(), display_name="Ops", role="admin")
    auth_session = SimpleNamespace(csrf_token="csrf-token")
    target_user = SimpleNamespace(
        id=uuid.uuid4(),
        email="existing@example.com",
        display_name="Existing User",
        role="requester",
        is_active=True,
    )

    monkeypatch.setattr(stack["routes_ops"], "_load_user_or_404", lambda db, user_id: target_user)

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["routes_ops"].require_ops_user] = lambda: ops_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: auth_session

    with stack["TestClient"](app) as client:
        response = client.post(
            f"/ops/users/{target_user.id}/set-active",
            data={"csrf_token": "csrf-token", "is_active": "maybe"},
            headers={"Accept-Language": "pt-BR"},
            follow_redirects=False,
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "Estado de ativação inválido."


def test_admin_only_slack_route_error_translates_to_portuguese(tmp_path):
    stack = _load_web_stack()
    app = stack["create_app"]()
    db = _RouteDb()
    settings = _make_settings(tmp_path)
    dev_ti_user = SimpleNamespace(id=uuid.uuid4(), display_name="Dev", role="dev_ti", is_active=True)

    app.dependency_overrides[stack["db_session_dependency"]] = lambda: db
    app.dependency_overrides[stack["auth"].get_current_user] = lambda: dev_ti_user
    app.dependency_overrides[stack["routes_ops"].get_required_auth_session] = lambda: SimpleNamespace(csrf_token="csrf")
    app.dependency_overrides[stack["routes_ops"].get_settings] = lambda: settings

    with stack["TestClient"](app) as client:
        response = client.get("/ops/integrations/slack", headers={"Accept-Language": "pt-BR"})

    assert response.status_code == 403
    assert response.json() == {"detail": "Acesso de admin é obrigatório"}


def test_requester_timeline_uses_portuguese_status_labels(monkeypatch):
    stack = _load_web_stack()
    ticket_id = uuid.uuid4()
    from datetime import datetime, timedelta, timezone

    start = datetime(2026, 4, 2, 12, 0, tzinfo=timezone.utc)
    public_thread = [
        {
            "kind": "message",
            "id": str(uuid.uuid4()),
            "created_at": start,
            "author_label": "Você",
            "body_html": "<p>Pedido original</p>",
            "attachments": [],
        },
        {
            "kind": "message",
            "id": str(uuid.uuid4()),
            "created_at": start + timedelta(minutes=5),
            "author_label": "IA",
            "body_html": "<p>Preciso de mais um detalhe.</p>",
            "attachments": [],
        },
    ]
    history = [
        SimpleNamespace(
            id=uuid.uuid4(),
            created_at=start - timedelta(minutes=1),
            from_status=None,
            to_status="new",
            changed_by_type="requester",
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            created_at=start,
            from_status="new",
            to_status="ai_triage",
            changed_by_type="requester",
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            created_at=start + timedelta(minutes=5),
            from_status="ai_triage",
            to_status="waiting_on_user",
            changed_by_type="ai",
        ),
    ]

    monkeypatch.setattr(stack["routes_requester"], "_serialize_public_thread", lambda *args, **kwargs: public_thread)
    monkeypatch.setattr(stack["routes_requester"], "load_ticket_status_history", lambda *args, **kwargs: history)

    timeline = stack["routes_requester"]._build_requester_timeline(object(), ticket_id=ticket_id, ui_locale="pt-BR")

    assert timeline[1]["summary"] == "Status alterado para Em análise"
    assert timeline[3]["summary"] == "Status alterado para Aguardando sua resposta"
