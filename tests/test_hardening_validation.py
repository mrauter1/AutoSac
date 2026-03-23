from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import importlib

import pytest

from shared.config import Settings

_DOCS_ENV_VARIABLES = (
    "APP_BASE_URL",
    "APP_SECRET_KEY",
    "DATABASE_URL",
    "UPLOADS_DIR",
    "TRIAGE_WORKSPACE_DIR",
    "REPO_MOUNT_DIR",
    "MANUALS_MOUNT_DIR",
    "CODEX_BIN",
    "CODEX_API_KEY",
    "CODEX_MODEL",
    "CODEX_TIMEOUT_SECONDS",
    "WORKER_POLL_SECONDS",
    "AUTO_SUPPORT_REPLY_MIN_CONFIDENCE",
    "AUTO_CONFIRM_INTENT_MIN_CONFIDENCE",
    "MAX_IMAGES_PER_MESSAGE",
    "MAX_IMAGE_BYTES",
    "SESSION_DEFAULT_HOURS",
    "SESSION_REMEMBER_DAYS",
)

_README_REQUIRED_SNIPPETS = (
    ".env.example",
    "python -m alembic -c alembic.ini upgrade head",
    "python scripts/bootstrap_workspace.py",
    "python scripts/create_admin.py",
    "--if-missing",
    "python scripts/run_web.py --check",
    "python scripts/run_worker.py --check",
    "GET /readyz",
    "pytest",
    "Slack or email",
    "OAuth or SSO",
    "SPA frontend",
    "worker web search",
    "non-image attachments",
    "automated patch generation",
)


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

    import app.main as app_main

    return {
        "TestClient": TestClient,
        "app_main": app_main,
    }


def test_render_markdown_to_html_sanitizes_untrusted_content():
    pytest.importorskip("markdown_it")
    pytest.importorskip("bleach")
    from app.render import render_markdown_to_html

    html = render_markdown_to_html('**safe**\n\n<script>alert(1)</script>\n\n[docs](https://example.com)')

    assert "<script>" not in html
    assert "<strong>safe</strong>" in html
    assert 'href="https://example.com"' in html


def test_verify_workspace_contract_paths_requires_agents_and_skill_files(tmp_path):
    from shared.workspace import verify_workspace_contract_paths

    settings = _make_settings(tmp_path)
    settings.triage_workspace_dir.mkdir(parents=True)
    settings.runs_dir.mkdir(parents=True)
    settings.repo_mount_dir.mkdir(parents=True)
    settings.manuals_mount_dir.mkdir(parents=True)

    with pytest.raises(FileNotFoundError):
        verify_workspace_contract_paths(settings)

    settings.workspace_agents_path.write_text("agents", encoding="utf-8")
    settings.workspace_skill_path.parent.mkdir(parents=True, exist_ok=True)
    settings.workspace_skill_path.write_text("skill", encoding="utf-8")

    verify_workspace_contract_paths(settings)


def test_readyz_returns_ready_when_database_and_workspace_checks_pass(monkeypatch, tmp_path):
    stack = _load_web_stack()
    settings = _make_settings(tmp_path)
    observed: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr(stack["app_main"], "get_settings", lambda: settings)
    monkeypatch.setattr(stack["app_main"], "ping_database", lambda resolved: None)
    monkeypatch.setattr(stack["app_main"], "verify_workspace_contract_paths", lambda resolved: None)
    monkeypatch.setattr(
        stack["app_main"],
        "log_web_event",
        lambda event, **payload: observed.append((event, payload)),
    )

    app = stack["app_main"].create_app()
    with stack["TestClient"](app) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert any(event == "request_completed" and payload["path"] == "/readyz" for event, payload in observed)


def test_readyz_returns_503_and_logs_failure(monkeypatch, tmp_path):
    stack = _load_web_stack()
    settings = _make_settings(tmp_path)
    observed: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr(stack["app_main"], "get_settings", lambda: settings)
    monkeypatch.setattr(stack["app_main"], "ping_database", lambda resolved: (_ for _ in ()).throw(RuntimeError("db down")))
    monkeypatch.setattr(stack["app_main"], "verify_workspace_contract_paths", lambda resolved: None)
    monkeypatch.setattr(
        stack["app_main"],
        "log_web_event",
        lambda event, **payload: observed.append((event, payload)),
    )

    app = stack["app_main"].create_app()
    with stack["TestClient"](app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "error": "db down"}
    assert ("readiness_failed", {"level": "error", "error": "db down"}) in observed
    assert any(event == "request_completed" and payload["status_code"] == 503 for event, payload in observed)


def test_healthz_request_is_structured_logged(monkeypatch):
    stack = _load_web_stack()
    observed: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr(
        stack["app_main"],
        "log_web_event",
        lambda event, **payload: observed.append((event, payload)),
    )

    app = stack["app_main"].create_app()
    with stack["TestClient"](app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert any(
        event == "request_completed"
        and payload["method"] == "GET"
        and payload["path"] == "/healthz"
        and payload["status_code"] == 200
        for event, payload in observed
    )


def test_verify_password_returns_false_for_malformed_hash():
    pytest.importorskip("argon2")
    from shared.security import hash_password, verify_password

    valid_hash = hash_password("correct horse battery staple")

    assert verify_password("correct horse battery staple", valid_hash) is True
    assert verify_password("wrong-password", valid_hash) is False
    assert verify_password("any-password", "not-an-argon2-hash") is False


def test_create_app_and_template_paths_are_module_relative(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("argon2")

    import app.ui as app_ui
    import app.main as app_main

    monkeypatch.chdir(tmp_path)
    app_ui = importlib.reload(app_ui)
    app_main = importlib.reload(app_main)

    app = app_main.create_app()

    assert app_ui.templates.env.loader.searchpath == [str(Path(app_ui.__file__).resolve().parent / "templates")]
    assert any(getattr(route, "path", None) == "/static" for route in app.routes)


def test_login_template_renders_outside_repo_root(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("argon2")
    from fastapi.testclient import TestClient

    import app.ui as app_ui
    import app.routes_auth as routes_auth
    import app.routes_ops as routes_ops
    import app.routes_requester as routes_requester
    import app.main as app_main

    monkeypatch.chdir(tmp_path)
    app_ui = importlib.reload(app_ui)
    routes_auth = importlib.reload(routes_auth)
    importlib.reload(routes_ops)
    importlib.reload(routes_requester)
    app_main = importlib.reload(app_main)

    app = app_main.create_app()
    app.dependency_overrides[routes_auth.get_settings_dependency] = lambda: _make_settings(tmp_path)
    app.dependency_overrides[routes_auth.db_session_dependency] = lambda: type("FakeDb", (), {"commit": lambda self: None})()
    app.dependency_overrides[routes_auth.get_optional_auth_session] = lambda: None
    app.dependency_overrides[routes_auth.get_current_user_optional] = lambda: None
    monkeypatch.setattr(
        routes_auth,
        "issue_login_preauth_session",
        lambda **_kwargs: type("PreAuth", (), {"csrf_token": "csrf-1"})(),
    )

    with TestClient(app) as client:
        response = client.get("/login")

    assert response.status_code == 200
    assert '<form method="post" action="/login"' in response.text


def test_run_web_check_works_outside_repo_root_after_bootstrap(tmp_path):
    env = _script_env(tmp_path)

    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(Path.cwd() / "alembic.ini"), "upgrade", "head"],
        cwd=Path.cwd(),
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    bootstrap = subprocess.run(
        [sys.executable, "/workspace/AutoSac/scripts/bootstrap_workspace.py"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert '"bootstrap_version": "stage1-v1"' in bootstrap.stdout

    web = subprocess.run(
        [sys.executable, "/workspace/AutoSac/scripts/run_web.py", "--check"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert _last_json_line(web.stdout) == {
        "script": "run_web.py",
        "status": "ok",
        "healthz_status": 200,
        "readyz_status": 200,
    }


def test_env_example_and_readme_capture_acceptance_contract():
    env_source = Path(".env.example").read_text(encoding="utf-8")
    readme_source = Path("README.md").read_text(encoding="utf-8")

    for name in _DOCS_ENV_VARIABLES:
        assert f"{name}=" in env_source

    for snippet in _README_REQUIRED_SNIPPETS:
        assert snippet in readme_source


def _script_env(tmp_path: Path) -> dict[str, str]:
    workspace_dir = tmp_path / "workspace"
    repo_mount_dir = workspace_dir / "app"
    manuals_mount_dir = workspace_dir / "manuals"
    uploads_dir = tmp_path / "uploads"
    repo_mount_dir.mkdir(parents=True)
    manuals_mount_dir.mkdir(parents=True)
    uploads_dir.mkdir(parents=True)

    env = os.environ.copy()
    env.update(
        {
            "APP_BASE_URL": "http://localhost:8000",
            "APP_SECRET_KEY": "test-secret",
            "DATABASE_URL": f"sqlite+pysqlite:///{(tmp_path / 'triage.db').resolve()}",
            "UPLOADS_DIR": str(uploads_dir),
            "TRIAGE_WORKSPACE_DIR": str(workspace_dir),
            "REPO_MOUNT_DIR": str(repo_mount_dir),
            "MANUALS_MOUNT_DIR": str(manuals_mount_dir),
            "CODEX_BIN": "codex",
            "CODEX_API_KEY": "test-key",
            "CODEX_MODEL": "",
            "CODEX_TIMEOUT_SECONDS": "75",
            "WORKER_POLL_SECONDS": "10",
            "AUTO_SUPPORT_REPLY_MIN_CONFIDENCE": "0.85",
            "AUTO_CONFIRM_INTENT_MIN_CONFIDENCE": "0.90",
            "MAX_IMAGES_PER_MESSAGE": "3",
            "MAX_IMAGE_BYTES": str(5 * 1024 * 1024),
            "SESSION_DEFAULT_HOURS": "12",
            "SESSION_REMEMBER_DAYS": "30",
            "PYTHONPATH": str(Path.cwd()),
        }
    )
    return env


def _run_script(args: list[str], *, env: dict[str, str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=Path.cwd(),
        env=env,
        text=True,
        capture_output=True,
        check=check,
    )


def _run_alembic_upgrade(*, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(Path.cwd() / "alembic.ini"), "upgrade", "head"],
        cwd=Path.cwd(),
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )


def _system_state_rows(db_path: Path) -> dict[str, str]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("SELECT key, value_json FROM system_state ORDER BY key").fetchall()
    return {str(key): str(value_json) for key, value_json in rows}


def _user_rows(db_path: Path) -> list[tuple[str, str, int]]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("SELECT email, role, is_active FROM users ORDER BY email").fetchall()
    return [(str(email), str(role), int(is_active)) for email, role, is_active in rows]


def _last_json_line(output: str) -> dict[str, object]:
    for line in reversed([line.strip() for line in output.splitlines() if line.strip()]):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    raise AssertionError(f"No JSON line found in output: {output}")


def test_bootstrap_workspace_script_seeds_required_system_state_defaults(tmp_path):
    env = _script_env(tmp_path)
    db_path = tmp_path / "triage.db"

    _run_alembic_upgrade(env=env)
    bootstrap = _run_script(["scripts/bootstrap_workspace.py"], env=env)

    assert '"bootstrap_version": "stage1-v1"' in bootstrap.stdout
    assert _system_state_rows(db_path) == {
        "bootstrap_version": '{"version": "stage1-v1"}',
        "worker_heartbeat": '{"status": "unknown"}',
    }


def test_bootstrap_workspace_script_requires_migrations_first(tmp_path):
    env = _script_env(tmp_path)

    bootstrap = _run_script(["scripts/bootstrap_workspace.py"], env=env, check=False)

    assert bootstrap.returncode != 0
    assert "system_state" in bootstrap.stderr


def test_create_admin_if_missing_is_idempotent(tmp_path):
    env = _script_env(tmp_path)
    db_path = tmp_path / "triage.db"

    _run_alembic_upgrade(env=env)
    _run_script(["scripts/bootstrap_workspace.py"], env=env)

    first = _run_script(
        [
            "scripts/create_admin.py",
            "--email",
            "Admin@example.com",
            "--display-name",
            "Stage One Admin",
            "--password",
            "correct horse battery staple",
            "--if-missing",
        ],
        env=env,
    )
    second = _run_script(
        [
            "scripts/create_admin.py",
            "--email",
            "admin@example.com",
            "--display-name",
            "Ignored Name",
            "--password",
            "another-password",
            "--if-missing",
        ],
        env=env,
    )

    assert "Created admin user admin@example.com" in first.stdout
    assert "Admin user already exists: admin@example.com" in second.stdout
    assert _user_rows(db_path) == [("admin@example.com", "admin", 1)]


def test_create_admin_if_missing_rejects_existing_non_admin(tmp_path):
    env = _script_env(tmp_path)

    _run_alembic_upgrade(env=env)
    _run_script(["scripts/bootstrap_workspace.py"], env=env)
    _run_script(
        [
            "scripts/create_user.py",
            "--email",
            "requester@example.com",
            "--display-name",
            "Requester",
            "--password",
            "correct horse battery staple",
            "--role",
            "requester",
        ],
        env=env,
    )

    create_admin = _run_script(
        [
            "scripts/create_admin.py",
            "--email",
            "requester@example.com",
            "--display-name",
            "Should Not Promote",
            "--password",
            "another-password",
            "--if-missing",
        ],
        env=env,
        check=False,
    )

    assert create_admin.returncode != 0
    assert "Existing user is not an admin: requester@example.com" in create_admin.stderr


def test_bootstrap_web_and_worker_scripts_validate_end_to_end(tmp_path):
    env = _script_env(tmp_path)

    _run_alembic_upgrade(env=env)
    bootstrap = _run_script(["scripts/bootstrap_workspace.py"], env=env)
    assert '"bootstrap_version": "stage1-v1"' in bootstrap.stdout

    create_admin = _run_script(
        [
            "scripts/create_admin.py",
            "--email",
            "admin@example.com",
            "--display-name",
            "Stage One Admin",
            "--password",
            "correct horse battery staple",
            "--if-missing",
        ],
        env=env,
    )
    assert "admin@example.com" in create_admin.stdout

    web = _run_script(["scripts/run_web.py", "--check"], env=env)
    web_payload = _last_json_line(web.stdout)
    assert web_payload == {
        "script": "run_web.py",
        "status": "ok",
        "healthz_status": 200,
        "readyz_status": 200,
    }

    worker = _run_script(["scripts/run_worker.py", "--check"], env=env)
    worker_payload = _last_json_line(worker.stdout)
    assert worker_payload == {
        "script": "run_worker.py",
        "status": "ok",
        "worker_poll_seconds": 10,
    }


def test_script_checks_fail_before_workspace_bootstrap(tmp_path):
    env = _script_env(tmp_path)

    web = _run_script(["scripts/run_web.py", "--check"], env=env, check=False)
    worker = _run_script(["scripts/run_worker.py", "--check"], env=env, check=False)

    assert web.returncode != 0
    assert "Web smoke check failed: /readyz did not return the expected payload" in web.stderr

    assert worker.returncode != 0
    assert "Required " in worker.stderr
    assert "does not exist" in worker.stderr
