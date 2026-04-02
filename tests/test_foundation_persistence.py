from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from shared.config import Settings, get_database_url
from shared.contracts import (
    APP_ROUTES,
    CLI_COMMAND_NAMES,
    WORKSPACE_AGENTS_CONTENT,
    WORKSPACE_SKILL_CONTENT,
)
from shared.workspace import bootstrap_workspace


class _FakeNestedTransaction:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeSession:
    def __init__(self, flush_exc: Exception | None = None):
        self.added = []
        self.flush_exc = flush_exc
        self.flush_calls = 0
        self.begin_nested_calls = 0

    def add(self, item):
        self.added.append(item)

    def flush(self):
        self.flush_calls += 1
        if self.flush_exc is not None:
            raise self.flush_exc

    def begin_nested(self):
        self.begin_nested_calls += 1
        return _FakeNestedTransaction()


class _FakeScalarOneOrNoneResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakePreauthSession:
    def __init__(self, scalar_result=None):
        self.added = []
        self.executed = []
        self.scalar_result = scalar_result

    def add(self, item):
        self.added.append(item)

    def execute(self, statement):
        self.executed.append(statement)
        return _FakeScalarOneOrNoneResult(self.scalar_result)


def _make_settings(tmp_path: Path) -> Settings:
    workspace_dir = tmp_path / "workspace"
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


def _load_ticketing_dependencies():
    pytest.importorskip("sqlalchemy")
    from sqlalchemy.exc import IntegrityError
    from shared.models import AIRun, Ticket
    from shared.ticketing import create_pending_ai_run, record_status_change

    return IntegrityError, AIRun, Ticket, create_pending_ai_run, record_status_change


def _make_ticket(*, status: str = "new"):
    _, _, Ticket, _, _ = _load_ticketing_dependencies()
    return Ticket(
        id=uuid.uuid4(),
        reference_num=1,
        reference="T-000001",
        title="Ticket",
        created_by_user_id=uuid.uuid4(),
        status=status,
        urgent=False,
    )


def test_create_pending_ai_run_returns_run_on_success():
    _, AIRun, _, create_pending_ai_run, _ = _load_ticketing_dependencies()
    session = _FakeSession()

    run = create_pending_ai_run(
        session,
        ticket_id=uuid.uuid4(),
        triggered_by="manual_rerun",
    )

    assert isinstance(run, AIRun)
    assert run.status == "pending"
    assert run.triggered_by == "manual_rerun"
    assert session.added == [run]
    assert session.begin_nested_calls == 1
    assert session.flush_calls == 1


def test_create_pending_ai_run_returns_none_for_active_run_conflict():
    IntegrityError, _, _, create_pending_ai_run, _ = _load_ticketing_dependencies()
    conflict = IntegrityError(
        "INSERT INTO ai_runs ...",
        params={},
        orig=Exception("duplicate key value violates unique constraint \"uq_ai_runs_active_ticket\""),
    )
    session = _FakeSession(flush_exc=conflict)

    run = create_pending_ai_run(
        session,
        ticket_id=uuid.uuid4(),
        triggered_by="requester_reply",
    )

    assert run is None
    assert session.begin_nested_calls == 1
    assert session.flush_calls == 1


def test_create_pending_ai_run_reraises_unrelated_integrity_errors():
    IntegrityError, _, _, create_pending_ai_run, _ = _load_ticketing_dependencies()
    unrelated = IntegrityError(
        "INSERT INTO ai_runs ...",
        params={},
        orig=Exception("violates foreign key constraint"),
    )
    session = _FakeSession(flush_exc=unrelated)

    with pytest.raises(IntegrityError):
        create_pending_ai_run(
            session,
            ticket_id=uuid.uuid4(),
            triggered_by="requester_reply",
        )


def test_record_status_change_supports_initial_null_transition():
    _, _, _, _, record_status_change = _load_ticketing_dependencies()
    ticket = _make_ticket(status="new")
    session = _FakeSession()

    history = record_status_change(
        session,
        ticket=ticket,
        to_status="new",
        changed_by_type="system",
        from_status_override=None,
    )

    assert history.from_status is None
    assert history.to_status == "new"
    assert ticket.status == "new"
    assert session.added == [history]


def test_get_database_url_reads_only_database_url(monkeypatch):
    monkeypatch.delenv("APP_SECRET_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://triage:triage@localhost:5432/triage")

    assert get_database_url() == "postgresql+psycopg://triage:triage@localhost:5432/triage"


def test_alembic_env_uses_database_url_helper():
    env_source = Path("shared/migrations/env.py").read_text(encoding="utf-8")

    assert "from shared.config import get_database_url" in env_source
    assert "database_url = get_database_url()" in env_source
    assert "get_settings" not in env_source


def test_bootstrap_workspace_writes_exact_files_and_initializes_git_repo(tmp_path):
    settings = _make_settings(tmp_path)
    settings.repo_mount_dir.mkdir(parents=True)
    settings.manuals_mount_dir.mkdir(parents=True)

    bootstrap_workspace(settings)

    assert settings.workspace_agents_path.read_text(encoding="utf-8") == WORKSPACE_AGENTS_CONTENT
    assert settings.workspace_skill_path.read_text(encoding="utf-8") == WORKSPACE_SKILL_CONTENT
    assert (settings.triage_workspace_dir / ".git").is_dir()
    subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=settings.triage_workspace_dir,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_bootstrap_workspace_requires_repo_and_manual_mounts(tmp_path):
    settings = _make_settings(tmp_path)
    settings.repo_mount_dir.mkdir(parents=True)

    with pytest.raises(FileNotFoundError):
        bootstrap_workspace(settings)


def test_contract_constants_expose_required_route_and_cli_contracts():
    assert "create-admin" in CLI_COMMAND_NAMES
    assert "create-user" in CLI_COMMAND_NAMES
    assert "/login" in APP_ROUTES
    assert "/ops/drafts/{draft_id}/approve-publish" in APP_ROUTES


def test_initial_migration_contains_required_sessions_table_and_active_run_index():
    migration_source = Path("shared/migrations/versions/20260323_0001_initial.py").read_text(encoding="utf-8")

    assert 'op.create_table(\n        "sessions"' in migration_source
    assert 'op.create_table(\n        "ai_runs"' in migration_source
    assert "CREATE UNIQUE INDEX uq_ai_runs_active_ticket ON ai_runs (ticket_id) WHERE status IN ('pending', 'running')" in migration_source


def test_preauth_login_session_creation_hashes_browser_token_and_sets_short_expiry(monkeypatch):
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("argon2")
    from datetime import datetime, timezone

    from shared import preauth_login

    fixed_now = datetime(2026, 3, 24, 1, 0, tzinfo=timezone.utc)
    session = _FakePreauthSession()

    monkeypatch.setattr(preauth_login, "generate_opaque_token", lambda: "opaque-browser-token")
    monkeypatch.setattr(preauth_login, "generate_csrf_token", lambda: "csrf-token")
    monkeypatch.setattr(preauth_login, "hash_token", lambda raw_token: f"hashed::{raw_token}")
    monkeypatch.setattr(preauth_login, "utc_now", lambda: fixed_now)

    record, raw_token = preauth_login.create_preauth_login_session(
        session,
        next_path="/ops?status=new",
        user_agent="pytest-agent",
        ip_address="127.0.0.1",
    )

    assert raw_token == "opaque-browser-token"
    assert record.token_hash == "hashed::opaque-browser-token"
    assert record.csrf_token == "csrf-token"
    assert record.next_path == "/ops?status=new"
    assert record.created_at == fixed_now
    assert record.expires_at == fixed_now + preauth_login.PREAUTH_LOGIN_TTL
    assert record.user_agent == "pytest-agent"
    assert record.ip_address == "127.0.0.1"
    assert session.added == [record]
    assert len(session.executed) == 1
    assert "DELETE FROM preauth_login_sessions" in str(session.executed[0])


def test_preauth_login_lookup_and_invalidation_use_hashed_tokens(monkeypatch):
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("argon2")
    from datetime import datetime, timezone

    from shared import preauth_login

    fixed_now = datetime(2026, 3, 24, 1, 5, tzinfo=timezone.utc)
    expected_record = object()
    session = _FakePreauthSession(scalar_result=expected_record)
    observed_tokens = []

    monkeypatch.setattr(
        preauth_login,
        "hash_token",
        lambda raw_token: observed_tokens.append(raw_token) or f"hashed::{raw_token}",
    )
    monkeypatch.setattr(preauth_login, "utc_now", lambda: fixed_now)

    record = preauth_login.get_valid_preauth_login_session(session, "opaque-browser-token")
    preauth_login.invalidate_preauth_login_session(session, "opaque-browser-token")

    assert record is expected_record
    assert observed_tokens == ["opaque-browser-token", "opaque-browser-token"]
    assert len(session.executed) == 3
    assert "DELETE FROM preauth_login_sessions" in str(session.executed[0])
    assert "SELECT preauth_login_sessions.id" in str(session.executed[1])
    assert "DELETE FROM preauth_login_sessions" in str(session.executed[2])


def test_verify_password_returns_false_for_malformed_hashes():
    pytest.importorskip("argon2")
    from shared.security import verify_password

    assert verify_password("secret", "") is False
    assert verify_password("secret", "not-an-argon2-hash") is False
    assert verify_password("secret", "$argon2id$v=19$m=oops,t=3,p=4$bad$hash") is False


def test_ensure_admin_user_is_idempotent_for_matching_existing_admin():
    pytest.importorskip("argon2")
    from shared.user_admin import create_user, ensure_admin_user

    class _AdminSession:
        def __init__(self):
            self.users = {}
            self.added = []

        def add(self, item):
            self.added.append(item)
            email = getattr(item, "email", None)
            if email:
                self.users[email] = item

        def execute(self, statement):
            class _Result:
                def __init__(self, user):
                    self._user = user

                def scalar_one_or_none(self):
                    return self._user

            compiled = str(statement)
            marker = "users.email = :email_1"
            assert marker in compiled
            params = statement.compile().params
            return _Result(self.users.get(params["email_1"]))

    db = _AdminSession()
    created, status = ensure_admin_user(
        db,
        email="admin@example.com",
        display_name="Admin",
        password="secret-pass",
    )
    assert status == "created"

    same_user, same_status = ensure_admin_user(
        db,
        email="admin@example.com",
        display_name="Admin",
        password="secret-pass",
    )
    assert same_status == "unchanged"
    assert same_user is created


def test_ensure_admin_user_rejects_conflicting_existing_user():
    pytest.importorskip("argon2")
    from shared.user_admin import create_user, ensure_admin_user

    class _AdminSession:
        def __init__(self):
            self.users = {}

        def add(self, item):
            email = getattr(item, "email", None)
            if email:
                self.users[email] = item

        def execute(self, statement):
            class _Result:
                def __init__(self, user):
                    self._user = user

                def scalar_one_or_none(self):
                    return self._user

            params = statement.compile().params
            return _Result(self.users.get(params["email_1"]))

    db = _AdminSession()
    create_user(
        db,
        email="admin@example.com",
        display_name="Requester",
        password="secret-pass",
        role="requester",
    )

    with pytest.raises(ValueError, match="not admin"):
        ensure_admin_user(
            db,
            email="admin@example.com",
            display_name="Admin",
            password="secret-pass",
        )


def test_create_admin_script_reports_matching_admin_as_success(monkeypatch, capsys):
    import argparse

    from scripts import create_admin

    observed = {}

    @contextmanager
    def fake_session_scope():
        yield "db"

    monkeypatch.setattr(create_admin, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        argparse.ArgumentParser,
        "parse_args",
        lambda self: argparse.Namespace(
            email="admin@example.com",
            display_name="Admin",
            password="secret-pass",
        ),
    )
    monkeypatch.setattr(
        create_admin,
        "ensure_system_state_defaults",
        lambda db, version: observed.update({"defaults": (db, version)}),
    )
    monkeypatch.setattr(
        create_admin,
        "ensure_admin_user",
        lambda db, **kwargs: (
            type("UserStub", (), {"email": kwargs["email"]})(),
            "unchanged",
        ),
    )

    create_admin.main()

    assert observed["defaults"] == ("db", "stage1-v1")
    assert capsys.readouterr().out.strip() == "Admin user admin@example.com already matched the requested bootstrap state"


def test_bootstrap_workspace_script_seeds_system_state_defaults(monkeypatch, capsys, tmp_path):
    from scripts import bootstrap_workspace as bootstrap_script

    settings = _make_settings(tmp_path)
    observed: list[tuple[object, ...]] = []

    @contextmanager
    def fake_session_scope(resolved_settings):
        observed.append(("session_scope", resolved_settings))
        yield "db"

    monkeypatch.setattr(bootstrap_script, "get_settings", lambda: settings)
    monkeypatch.setattr(
        bootstrap_script,
        "ensure_uploads_dir",
        lambda resolved_settings: observed.append(("ensure_uploads_dir", resolved_settings)),
    )
    monkeypatch.setattr(
        bootstrap_script,
        "bootstrap_workspace",
        lambda resolved_settings: observed.append(("bootstrap_workspace", resolved_settings)),
    )
    monkeypatch.setattr(bootstrap_script, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        bootstrap_script,
        "ensure_system_state_defaults",
        lambda db, version: observed.append(("ensure_system_state_defaults", db, version)),
    )
    monkeypatch.setattr(
        bootstrap_script,
        "workspace_contract_snapshot",
        lambda resolved_settings: {"bootstrap_version": "stage1-v1", "workspace_dir": str(resolved_settings.triage_workspace_dir)},
    )

    bootstrap_script.main()

    assert observed == [
        ("ensure_uploads_dir", settings),
        ("bootstrap_workspace", settings),
        ("session_scope", settings),
        ("ensure_system_state_defaults", "db", "stage1-v1"),
    ]
    assert json.loads(capsys.readouterr().out) == {
        "bootstrap_version": "stage1-v1",
        "workspace_dir": str(settings.triage_workspace_dir),
    }


def test_preflight_setup_script_can_prepare_dirs_and_report_ready(monkeypatch, capsys, tmp_path):
    from scripts import preflight_setup

    settings = _make_settings(tmp_path)
    settings = Settings(
        app_base_url=settings.app_base_url,
        app_secret_key=settings.app_secret_key,
        database_url=settings.database_url,
        uploads_dir=tmp_path / "uploads",
        triage_workspace_dir=tmp_path / "workspace",
        repo_mount_dir=tmp_path / "workspace" / "app",
        manuals_mount_dir=tmp_path / "workspace" / "manuals",
        codex_bin="codex",
        codex_api_key=settings.codex_api_key,
        codex_model=settings.codex_model,
        codex_timeout_seconds=settings.codex_timeout_seconds,
        worker_poll_seconds=settings.worker_poll_seconds,
        auto_support_reply_min_confidence=settings.auto_support_reply_min_confidence,
        auto_confirm_intent_min_confidence=settings.auto_confirm_intent_min_confidence,
        max_images_per_message=settings.max_images_per_message,
        max_image_bytes=settings.max_image_bytes,
        session_default_hours=settings.session_default_hours,
        session_remember_days=settings.session_remember_days,
    )

    monkeypatch.setattr(preflight_setup, "get_settings", lambda: settings)
    monkeypatch.setattr(preflight_setup, "_resolve_executable", lambda command: "/usr/bin/codex")
    monkeypatch.setattr(preflight_setup, "ping_database", lambda resolved_settings: None)
    monkeypatch.setattr(sys, "argv", ["preflight_setup.py", "--ensure-workspace-dirs"])

    preflight_setup.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["database"]["ok"] is True
    assert settings.uploads_dir.is_dir()
    assert settings.repo_mount_dir.is_dir()
    assert settings.manuals_mount_dir.is_dir()


def test_preflight_setup_script_can_run_local_postgres_setup(monkeypatch, capsys, tmp_path):
    from scripts import preflight_setup

    settings = Settings(
        app_base_url="http://localhost:8000",
        app_secret_key="test-secret",
        database_url="postgresql+psycopg://triage:triage@localhost:5432/triage",
        uploads_dir=tmp_path / "uploads",
        triage_workspace_dir=tmp_path / "workspace",
        repo_mount_dir=tmp_path / "workspace" / "app",
        manuals_mount_dir=tmp_path / "workspace" / "manuals",
        codex_bin="codex",
        codex_api_key="test-key",
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
    observed: list[object] = []
    ping_attempts = {"count": 0}

    def fake_ping_database(_resolved_settings):
        ping_attempts["count"] += 1
        if ping_attempts["count"] == 1:
            raise RuntimeError("db unavailable")

    monkeypatch.setattr(preflight_setup, "get_settings", lambda: settings)
    monkeypatch.setattr(preflight_setup, "_resolve_executable", lambda command: "/usr/bin/codex")
    monkeypatch.setattr(preflight_setup, "ping_database", fake_ping_database)
    monkeypatch.setattr(preflight_setup, "_run_local_postgres_setup", lambda resolved_settings: observed.append(resolved_settings))
    monkeypatch.setattr(sys, "argv", ["preflight_setup.py", "--ensure-workspace-dirs", "--setup-postgres-local"])

    preflight_setup.main()

    payload = json.loads(capsys.readouterr().out)
    assert observed == [settings]
    assert payload["status"] == "ok"
    assert payload["database_setup_attempted"] is True


def test_additive_preauth_migration_declares_store_and_expiry_index():
    migration_source = Path("shared/migrations/versions/20260324_0002_preauth_login_sessions.py").read_text(encoding="utf-8")

    assert 'op.create_table(\n        "preauth_login_sessions"' in migration_source
    assert 'sa.Column("token_hash", sa.Text(), nullable=False)' in migration_source
    assert 'sa.Column("csrf_token", sa.Text(), nullable=False)' in migration_source
    assert 'sa.Column("next_path", sa.Text(), nullable=True)' in migration_source
    assert 'sa.UniqueConstraint("token_hash"' in migration_source
    assert 'op.create_index(\n        "ix_preauth_login_sessions_expires_at"' in migration_source
