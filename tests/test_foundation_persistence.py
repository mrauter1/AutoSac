from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
import uuid

import pytest

from shared.config import Settings, get_database_url
from shared.agent_specs import load_all_agent_specs
from shared.contracts import (
    APP_ROUTES,
    CLI_COMMAND_NAMES,
    WORKSPACE_AGENTS_CONTENT,
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


def test_create_pending_ai_run_persists_forced_override_fields():
    _, AIRun, _, create_pending_ai_run, _ = _load_ticketing_dependencies()
    session = _FakeSession()

    run = create_pending_ai_run(
        session,
        ticket_id=uuid.uuid4(),
        triggered_by="manual_rerun",
        forced_route_target_id="software_architect",
        forced_specialist_id="software-architect",
    )

    assert isinstance(run, AIRun)
    assert run.forced_route_target_id == "software_architect"
    assert run.forced_specialist_id == "software-architect"


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
    for spec in load_all_agent_specs():
        assert settings.workspace_skill_file_path(spec.skill_id).read_text(encoding="utf-8") == spec.skill_text
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


def test_agent_pipeline_migration_adds_run_steps_and_final_output_fields():
    migration_source = Path("shared/migrations/versions/20260406_0004_agent_pipeline.py").read_text(encoding="utf-8")

    assert 'op.create_table(\n        "ai_run_steps"' in migration_source
    assert 'op.add_column("ai_runs", sa.Column("final_output_json"' in migration_source
    assert 'op.add_column("ai_runs", sa.Column("final_agent_spec_id"' in migration_source


def test_route_target_compatibility_migration_adds_backfill_and_selector_step_kind():
    migration_source = Path("shared/migrations/versions/20260406_0005_route_target_compatibility.py").read_text(
        encoding="utf-8"
    )

    assert 'op.add_column("tickets", sa.Column("route_target_id", sa.Text(), nullable=True))' in migration_source
    assert "UPDATE tickets SET route_target_id = ticket_class" in migration_source
    assert "step_kind IN ('router', 'selector', 'specialist')" in migration_source
    assert "route_target_id IN" not in migration_source


def test_route_target_cleanup_migration_drops_legacy_ticket_class_storage():
    migration_source = Path("shared/migrations/versions/20260406_0006_drop_ticket_class.py").read_text(
        encoding="utf-8"
    )

    assert 'batch_op.drop_constraint(op.f("ck_tickets_tickets_ticket_class"), type_="check")' in migration_source
    assert 'batch_op.drop_column("ticket_class")' in migration_source


def test_manual_rerun_override_migration_adds_forced_override_columns():
    migration_source = Path(
        "shared/migrations/versions/20260406_0007_manual_rerun_specialist_overrides.py"
    ).read_text(encoding="utf-8")

    assert 'op.add_column("tickets", sa.Column("requeue_forced_route_target_id", sa.Text(), nullable=True))' in migration_source
    assert 'op.add_column("tickets", sa.Column("requeue_forced_specialist_id", sa.Text(), nullable=True))' in migration_source
    assert 'op.add_column("ai_runs", sa.Column("forced_route_target_id", sa.Text(), nullable=True))' in migration_source
    assert 'op.add_column("ai_runs", sa.Column("forced_specialist_id", sa.Text(), nullable=True))' in migration_source


def test_deferred_requeue_requester_migration_adds_requesting_user_fk():
    migration_source = Path(
        "shared/migrations/versions/20260407_0008_deferred_requeue_requester.py"
    ).read_text(encoding="utf-8")

    assert 'sa.Column("requeue_requested_by_user_id"' in migration_source
    assert 'op.create_foreign_key(' in migration_source
    assert '"fk_tickets_requeue_requested_by_user_id_users"' in migration_source


def test_route_target_compatibility_persistence_backfills_and_allows_selector_steps(tmp_path):
    db_path = tmp_path / "route-target-compatibility.db"

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE tickets (
                id TEXT PRIMARY KEY,
                ticket_class TEXT,
                requester_language TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE ai_run_steps (
                id TEXT PRIMARY KEY,
                ai_run_id TEXT NOT NULL,
                step_index INTEGER NOT NULL,
                step_kind TEXT NOT NULL CHECK(step_kind IN ('router', 'specialist')),
                agent_spec_id TEXT NOT NULL,
                agent_spec_version TEXT NOT NULL,
                output_contract TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO tickets (id, ticket_class, requester_language) VALUES (?, ?, ?)",
            ("ticket-1", "support", "en"),
        )
        connection.execute(
            "INSERT INTO tickets (id, ticket_class, requester_language) VALUES (?, ?, ?)",
            ("ticket-2", None, None),
        )
        connection.execute(
            """
            INSERT INTO ai_run_steps (
                id,
                ai_run_id,
                step_index,
                step_kind,
                agent_spec_id,
                agent_spec_version,
                output_contract,
                status,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "step-1",
                "run-1",
                1,
                "router",
                "router",
                "1.0.0",
                "router_result",
                "succeeded",
                "2026-04-06T00:00:00+00:00",
            ),
        )

        connection.execute("ALTER TABLE tickets ADD COLUMN route_target_id TEXT")
        connection.execute(
            "UPDATE tickets SET route_target_id = ticket_class WHERE route_target_id IS NULL AND ticket_class IS NOT NULL"
        )

        connection.execute(
            """
            CREATE TABLE ai_run_steps_v2 (
                id TEXT PRIMARY KEY,
                ai_run_id TEXT NOT NULL,
                step_index INTEGER NOT NULL,
                step_kind TEXT NOT NULL CHECK(step_kind IN ('router', 'selector', 'specialist')),
                agent_spec_id TEXT NOT NULL,
                agent_spec_version TEXT NOT NULL,
                output_contract TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO ai_run_steps_v2 (
                id,
                ai_run_id,
                step_index,
                step_kind,
                agent_spec_id,
                agent_spec_version,
                output_contract,
                status,
                created_at
            )
            SELECT
                id,
                ai_run_id,
                step_index,
                step_kind,
                agent_spec_id,
                agent_spec_version,
                output_contract,
                status,
                created_at
            FROM ai_run_steps
            """
        )
        connection.execute("DROP TABLE ai_run_steps")
        connection.execute("ALTER TABLE ai_run_steps_v2 RENAME TO ai_run_steps")
        connection.execute(
            """
            INSERT INTO ai_run_steps (
                id,
                ai_run_id,
                step_index,
                step_kind,
                agent_spec_id,
                agent_spec_version,
                output_contract,
                status,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "step-2",
                "run-1",
                2,
                "selector",
                "specialist-selector",
                "1.0.0",
                "specialist_selector_result",
                "succeeded",
                "2026-04-06T00:00:01+00:00",
            ),
        )

        ticket_rows = connection.execute(
            "SELECT id, route_target_id FROM tickets ORDER BY id"
        ).fetchall()
        step_rows = connection.execute(
            "SELECT id, step_kind FROM ai_run_steps ORDER BY step_index"
        ).fetchall()

    assert ticket_rows == [("ticket-1", "support"), ("ticket-2", None)]
    assert step_rows == [("step-1", "router"), ("step-2", "selector")]


def test_route_target_cleanup_persistence_drops_ticket_class_column_and_keeps_route_target_data(tmp_path):
    db_path = tmp_path / "route-target-cleanup.db"

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE tickets (
                id TEXT PRIMARY KEY,
                ticket_class TEXT,
                route_target_id TEXT,
                requester_language TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO tickets (id, ticket_class, route_target_id, requester_language) VALUES (?, ?, ?, ?)",
            ("ticket-1", "support", "support", "en"),
        )
        connection.execute(
            "INSERT INTO tickets (id, ticket_class, route_target_id, requester_language) VALUES (?, ?, ?, ?)",
            ("ticket-2", None, "manual_review", "pt-BR"),
        )

        connection.execute(
            """
            CREATE TABLE tickets_v2 (
                id TEXT PRIMARY KEY,
                route_target_id TEXT,
                requester_language TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO tickets_v2 (id, route_target_id, requester_language)
            SELECT id, route_target_id, requester_language
            FROM tickets
            """
        )
        connection.execute("DROP TABLE tickets")
        connection.execute("ALTER TABLE tickets_v2 RENAME TO tickets")

        ticket_rows = connection.execute(
            "SELECT id, route_target_id, requester_language FROM tickets ORDER BY id"
        ).fetchall()
        ticket_columns = [row[1] for row in connection.execute("PRAGMA table_info(tickets)").fetchall()]

    assert ticket_rows == [("ticket-1", "support", "en"), ("ticket-2", "manual_review", "pt-BR")]
    assert ticket_columns == ["id", "route_target_id", "requester_language"]


def test_models_expose_route_target_storage_without_db_taxonomy_constraints_or_ticket_class_column():
    pytest.importorskip("sqlalchemy")
    from sqlalchemy import CheckConstraint

    from shared.models import AI_RUN_STEP_KINDS, Ticket

    assert AI_RUN_STEP_KINDS == ("router", "selector", "specialist")
    assert "route_target_id" in Ticket.__table__.c
    assert "ticket_class" not in Ticket.__table__.c
    route_target_constraints = [
        str(constraint.sqltext)
        for constraint in Ticket.__table__.constraints
        if isinstance(constraint, CheckConstraint) and "route_target_id" in str(constraint.sqltext)
    ]
    assert route_target_constraints == []
    assert "requeue_requested_by_user_id" in Ticket.__table__.c
    assert "requeue_forced_route_target_id" in Ticket.__table__.c
    assert "requeue_forced_specialist_id" in Ticket.__table__.c
    legacy_ticket_class_constraints = [
        str(constraint.sqltext)
        for constraint in Ticket.__table__.constraints
        if isinstance(constraint, CheckConstraint) and "ticket_class" in str(constraint.sqltext)
    ]
    assert legacy_ticket_class_constraints == []
    from shared.models import AIRun

    assert "forced_route_target_id" in AIRun.__table__.c
    assert "forced_specialist_id" in AIRun.__table__.c


def test_apply_ai_route_target_sets_route_target_and_requester_language():
    pytest.importorskip("sqlalchemy")
    from shared.ticketing import apply_ai_route_target

    ticket = _make_ticket()

    apply_ai_route_target(ticket, route_target_id="support", requester_language="en")

    assert ticket.route_target_id == "support"
    assert ticket.requester_language == "en"


def test_apply_ai_route_target_allows_manual_review_after_cutover():
    pytest.importorskip("sqlalchemy")
    from shared.ticketing import apply_ai_route_target

    ticket = _make_ticket()

    apply_ai_route_target(ticket, route_target_id="manual_review", requester_language="en")

    assert ticket.route_target_id == "manual_review"
    assert ticket.requester_language == "en"


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

    assert observed["defaults"] == ("db", "stage1-v4")
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
        lambda resolved_settings: {"bootstrap_version": "stage1-v4", "workspace_dir": str(resolved_settings.triage_workspace_dir)},
    )

    bootstrap_script.main()

    assert observed == [
        ("ensure_uploads_dir", settings),
        ("bootstrap_workspace", settings),
        ("session_scope", settings),
        ("ensure_system_state_defaults", "db", "stage1-v4"),
    ]
    assert json.loads(capsys.readouterr().out) == {
        "bootstrap_version": "stage1-v4",
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


def test_backfill_ai_run_steps_hydrates_accepted_legacy_run(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")
    from scripts import backfill_ai_run_steps
    from shared.agent_specs import LEGACY_AGENT_SPEC_ID, LEGACY_PIPELINE_VERSION

    output_path = tmp_path / "final.json"
    output_path.write_text(
        json.dumps(
            {
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
            }
        ),
        encoding="utf-8",
    )
    run = SimpleNamespace(
        id=uuid.uuid4(),
        ticket_id=uuid.uuid4(),
        status="succeeded",
        pipeline_version=None,
        final_output_path=str(output_path),
        final_output_json=None,
        final_step_id=None,
        final_agent_spec_id=None,
        final_output_contract=None,
        prompt_path="/tmp/prompt.txt",
        schema_path="/tmp/schema.json",
        stdout_jsonl_path="/tmp/stdout.jsonl",
        stderr_path="/tmp/stderr.txt",
        model_name="gpt-test",
        error_text=None,
        started_at=None,
        ended_at=None,
        created_at="2026-04-06T00:00:00+00:00",
    )

    class _FakeBackfillDb:
        def __init__(self):
            self.added = []

        def add(self, item):
            self.added.append(item)

        def flush(self):
            for item in self.added:
                if getattr(item, "id", None) is None:
                    item.id = uuid.uuid4()

    fake_db = _FakeBackfillDb()
    settings = _make_settings(tmp_path)

    monkeypatch.setattr(backfill_ai_run_steps, "get_settings", lambda: settings)

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr(backfill_ai_run_steps, "session_scope", fake_session_scope)
    monkeypatch.setattr(backfill_ai_run_steps, "_candidate_runs", lambda db: [run])
    monkeypatch.setattr(backfill_ai_run_steps, "_load_existing_step", lambda db, run_id: None)

    summary = backfill_ai_run_steps.run_backfill(dry_run=False)

    assert summary["blocking_errors"] == 0
    assert summary["created_steps"] == 1
    assert run.pipeline_version == LEGACY_PIPELINE_VERSION
    assert run.final_agent_spec_id == LEGACY_AGENT_SPEC_ID
    assert run.final_output_contract == "triage_result"
    assert run.final_output_json["ticket_class"] == "support"
    assert run.final_step_id is not None


def test_backfill_ai_run_steps_fails_for_accepted_run_without_valid_output(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")
    from scripts import backfill_ai_run_steps

    run = SimpleNamespace(
        id=uuid.uuid4(),
        ticket_id=uuid.uuid4(),
        status="human_review",
        pipeline_version=None,
        final_output_path=str(tmp_path / "missing.json"),
        final_output_json=None,
        final_step_id=None,
        final_agent_spec_id=None,
        final_output_contract=None,
        prompt_path=None,
        schema_path=None,
        stdout_jsonl_path=None,
        stderr_path=None,
        model_name="gpt-test",
        error_text=None,
        started_at=None,
        ended_at=None,
        created_at="2026-04-06T00:00:00+00:00",
    )

    class _FakeBackfillDb:
        def __init__(self):
            self.added = []

        def add(self, item):
            self.added.append(item)

        def flush(self):
            raise AssertionError("flush should not be called for blocking accepted runs")

    settings = _make_settings(tmp_path)

    monkeypatch.setattr(backfill_ai_run_steps, "get_settings", lambda: settings)

    @contextmanager
    def fake_session_scope(_settings):
        yield _FakeBackfillDb()

    monkeypatch.setattr(backfill_ai_run_steps, "session_scope", fake_session_scope)
    monkeypatch.setattr(backfill_ai_run_steps, "_candidate_runs", lambda db: [run])
    monkeypatch.setattr(backfill_ai_run_steps, "_load_existing_step", lambda db, run_id: None)

    summary = backfill_ai_run_steps.run_backfill(dry_run=False)

    assert summary["blocking_errors"] == 1
    assert summary["accepted_runs_missing_output"] == 1
    assert run.pipeline_version is None
    assert run.final_step_id is None
