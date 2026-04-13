from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from shared.agent_specs import load_all_agent_specs
from shared.config import Settings
from shared.contracts import WORKSPACE_AGENTS_CONTENT


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


def _require_python_multipart() -> None:
    try:
        from fastapi.dependencies.utils import ensure_multipart_is_installed
    except Exception:
        if importlib.util.find_spec("python_multipart") is not None:
            return
        if importlib.util.find_spec("multipart") is not None:
            return
        pytest.skip("python-multipart is not installed in this test environment")

    try:
        ensure_multipart_is_installed()
    except RuntimeError:
        pytest.skip("python-multipart is not installed in this test environment")


def _load_web_stack():
    pytest.importorskip("fastapi")
    pytest.importorskip("sqlalchemy")
    _require_python_multipart()
    from fastapi.testclient import TestClient
    import app.main as app_main

    return {
        "TestClient": TestClient,
        "app_main": app_main,
    }


def _seed_workspace_contract(settings: Settings) -> None:
    settings.workspace_agents_path.write_text(WORKSPACE_AGENTS_CONTENT, encoding="utf-8")
    for spec in load_all_agent_specs():
        path = settings.workspace_skill_file_path(spec.skill_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(spec.skill_text, encoding="utf-8")


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

    _seed_workspace_contract(settings)

    verify_workspace_contract_paths(settings)


def test_verify_workspace_contract_paths_rejects_stale_agents_file(tmp_path):
    from shared.workspace import verify_workspace_contract_paths

    settings = _make_settings(tmp_path)
    settings.triage_workspace_dir.mkdir(parents=True)
    settings.runs_dir.mkdir(parents=True)
    settings.repo_mount_dir.mkdir(parents=True)
    settings.manuals_mount_dir.mkdir(parents=True)
    _seed_workspace_contract(settings)
    settings.workspace_agents_path.write_text("stale agents", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Workspace AGENTS.md content is stale"):
        verify_workspace_contract_paths(settings)


def test_verify_workspace_contract_paths_rejects_stale_skill_file(tmp_path):
    from shared.workspace import verify_workspace_contract_paths

    settings = _make_settings(tmp_path)
    settings.triage_workspace_dir.mkdir(parents=True)
    settings.runs_dir.mkdir(parents=True)
    settings.repo_mount_dir.mkdir(parents=True)
    settings.manuals_mount_dir.mkdir(parents=True)
    _seed_workspace_contract(settings)
    first_spec = next(iter(load_all_agent_specs()))
    settings.workspace_skill_file_path(first_spec.skill_id).write_text("stale skill", encoding="utf-8")

    with pytest.raises(RuntimeError, match=f"Workspace workspace skill {first_spec.skill_id} content is stale"):
        verify_workspace_contract_paths(settings)


def test_readyz_returns_ready_when_database_and_workspace_checks_pass(monkeypatch, tmp_path):
    stack = _load_web_stack()
    settings = _make_settings(tmp_path)
    observed: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr(stack["app_main"], "get_settings", lambda: settings)
    monkeypatch.setattr(stack["app_main"], "ping_database", lambda resolved: None)
    monkeypatch.setattr(stack["app_main"], "verify_workspace_contract_paths", lambda resolved: None)
    monkeypatch.setattr(stack["app_main"], "assert_ai_run_history_ready", lambda resolved: None)
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


def test_readyz_returns_503_when_ai_run_history_is_not_backfilled(monkeypatch, tmp_path):
    stack = _load_web_stack()
    settings = _make_settings(tmp_path)

    monkeypatch.setattr(stack["app_main"], "get_settings", lambda: settings)
    monkeypatch.setattr(stack["app_main"], "ping_database", lambda resolved: None)
    monkeypatch.setattr(stack["app_main"], "verify_workspace_contract_paths", lambda resolved: None)
    monkeypatch.setattr(
        stack["app_main"],
        "assert_ai_run_history_ready",
        lambda resolved: (_ for _ in ()).throw(RuntimeError("AI run history is not ready: backfill pending")),
    )

    app = stack["app_main"].create_app()
    with stack["TestClient"](app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "error": "AI run history is not ready: backfill pending"}


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


def test_env_example_and_readme_capture_acceptance_contract():
    env_source = Path(".env.example").read_text(encoding="utf-8")
    readme_source = Path("README.md").read_text(encoding="utf-8")

    for name in (
        "APP_BASE_URL",
        "APP_SECRET_KEY",
        "DATABASE_URL",
        "UI_DEFAULT_LOCALE",
        "CODEX_BIN",
        "CODEX_API_KEY",
        "CODEX_MODEL",
        "CODEX_TIMEOUT_SECONDS",
        "WORKER_POLL_SECONDS",
        "WORKER_HEARTBEAT_SECONDS",
        "AI_RUN_STALE_TIMEOUT_SECONDS",
        "AI_RUN_MAX_RECOVERY_ATTEMPTS",
        "AUTO_SUPPORT_REPLY_MIN_CONFIDENCE",
        "AUTO_CONFIRM_INTENT_MIN_CONFIDENCE",
        "MAX_IMAGES_PER_MESSAGE",
        "MAX_IMAGE_BYTES",
        "SESSION_DEFAULT_HOURS",
        "SESSION_REMEMBER_DAYS",
        "UPLOADS_DIR",
        "TRIAGE_WORKSPACE_DIR",
        "REPO_MOUNT_DIR",
        "MANUALS_MOUNT_DIR",
    ):
        assert f"{name}=" in env_source

    assert ".env.example" in readme_source
    assert "python -m pip install -r requirements.txt" in readme_source
    assert "Runtime scripts load `.env` automatically" in readme_source
    assert "alembic upgrade head" in readme_source
    assert "python scripts/backfill_ai_run_steps.py" in readme_source
    assert "GET /readyz" in readme_source
    assert "python scripts/bootstrap_workspace.py" in readme_source
    assert "python scripts/preflight_setup.py --ensure-workspace-dirs --setup-postgres-local" in readme_source
    assert "python scripts/create_admin.py" in readme_source
    assert "python scripts/create_user.py" in readme_source
    assert "python scripts/set_password.py" in readme_source
    assert "python scripts/deactivate_user.py" in readme_source
    assert "python scripts/run_web.py --check" in readme_source
    assert "python scripts/run_worker.py --check" in readme_source
    assert "scripts/setup_postgres_local.sh" in readme_source
    assert "bootstrap_version" in readme_source
    assert "pytest" in readme_source
    assert "Leave `CODEX_API_KEY` empty" in readme_source
    assert "/ops/integrations/slack" in env_source
    assert "/ops/integrations/slack" in readme_source
    assert "/ops/users" in readme_source
    assert "chat.postMessage" in readme_source
    assert "no authoritative `SLACK_*` runtime env vars" in readme_source


def test_slack_docs_capture_phase1_rollout_posture():
    env_source = Path(".env.example").read_text(encoding="utf-8")
    readme_source = Path("README.md").read_text(encoding="utf-8")
    deployment_source = Path("docs_deployment.md").read_text(encoding="utf-8")

    assert "SLACK_ENABLED=" not in env_source
    assert "SLACK_TARGETS_JSON" not in env_source
    assert "/ops/integrations/slack" in env_source
    assert "does not backfill historical ticket activity" in env_source
    assert "/ops/integrations/slack" in readme_source
    assert "/ops/users" in readme_source
    assert "config-first" in readme_source
    assert "disposable data" in readme_source
    assert "does not backfill historical ticket activity" in readme_source
    assert "/ops/integrations/slack" in deployment_source
    assert "/ops/users" in deployment_source
    assert "DM-capable release" in deployment_source
    assert "disposable pre-launch data" in deployment_source
    assert "does not backfill old ticket activity" in deployment_source


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
            "UI_DEFAULT_LOCALE": "pt-BR",
            "UPLOADS_DIR": str(uploads_dir),
            "TRIAGE_WORKSPACE_DIR": str(workspace_dir),
            "REPO_MOUNT_DIR": str(repo_mount_dir),
            "MANUALS_MOUNT_DIR": str(manuals_mount_dir),
            "CODEX_BIN": "codex",
            "CODEX_MODEL": "",
            "CODEX_TIMEOUT_SECONDS": "3600",
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


def _create_runtime_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE system_state (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE ai_runs (
                id TEXT PRIMARY KEY,
                ticket_id TEXT,
                status TEXT NOT NULL,
                triggered_by TEXT,
                requested_by_user_id TEXT,
                input_hash TEXT,
                model_name TEXT,
                pipeline_version TEXT,
                final_step_id TEXT,
                final_agent_spec_id TEXT,
                final_output_contract TEXT,
                final_output_json TEXT,
                prompt_path TEXT,
                schema_path TEXT,
                final_output_path TEXT,
                stdout_jsonl_path TEXT,
                stderr_path TEXT,
                started_at TEXT,
                ended_at TEXT,
                error_text TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE ai_run_steps (
                id TEXT PRIMARY KEY,
                ai_run_id TEXT NOT NULL,
                step_index INTEGER NOT NULL,
                step_kind TEXT NOT NULL,
                agent_spec_id TEXT NOT NULL,
                agent_spec_version TEXT NOT NULL,
                output_contract TEXT NOT NULL,
                model_name TEXT,
                status TEXT NOT NULL,
                prompt_path TEXT,
                schema_path TEXT,
                final_output_path TEXT,
                stdout_jsonl_path TEXT,
                stderr_path TEXT,
                output_json TEXT,
                error_text TEXT,
                started_at TEXT,
                ended_at TEXT,
                created_at TEXT NOT NULL
            )
            """
        )


def test_get_settings_allows_missing_codex_api_key(monkeypatch, tmp_path):
    workspace_dir = tmp_path / "workspace"
    uploads_dir = tmp_path / "uploads"
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{(tmp_path / 'triage.db').resolve()}")
    monkeypatch.setenv("UI_DEFAULT_LOCALE", "pt-BR")
    monkeypatch.setenv("UPLOADS_DIR", str(uploads_dir))
    monkeypatch.setenv("TRIAGE_WORKSPACE_DIR", str(workspace_dir))
    monkeypatch.setenv("REPO_MOUNT_DIR", str(workspace_dir / "app"))
    monkeypatch.setenv("MANUALS_MOUNT_DIR", str(workspace_dir / "manuals"))
    monkeypatch.setenv("CODEX_BIN", "codex")
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setenv("CODEX_MODEL", "")
    monkeypatch.setenv("CODEX_TIMEOUT_SECONDS", "3600")
    monkeypatch.setenv("WORKER_POLL_SECONDS", "10")
    monkeypatch.setenv("AUTO_SUPPORT_REPLY_MIN_CONFIDENCE", "0.85")
    monkeypatch.setenv("AUTO_CONFIRM_INTENT_MIN_CONFIDENCE", "0.90")
    monkeypatch.setenv("MAX_IMAGES_PER_MESSAGE", "3")
    monkeypatch.setenv("MAX_IMAGE_BYTES", str(5 * 1024 * 1024))
    monkeypatch.setenv("SESSION_DEFAULT_HOURS", "12")
    monkeypatch.setenv("SESSION_REMEMBER_DAYS", "30")

    from shared.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()

    assert settings.codex_api_key is None
    assert settings.default_ui_locale == "pt-BR"
    get_settings.cache_clear()


def test_get_settings_ignores_legacy_slack_env_runtime_config(monkeypatch, tmp_path):
    workspace_dir = tmp_path / "workspace"
    uploads_dir = tmp_path / "uploads"
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{(tmp_path / 'triage.db').resolve()}")
    monkeypatch.setenv("UI_DEFAULT_LOCALE", "pt-BR")
    monkeypatch.setenv("UPLOADS_DIR", str(uploads_dir))
    monkeypatch.setenv("TRIAGE_WORKSPACE_DIR", str(workspace_dir))
    monkeypatch.setenv("REPO_MOUNT_DIR", str(workspace_dir / "app"))
    monkeypatch.setenv("MANUALS_MOUNT_DIR", str(workspace_dir / "manuals"))
    monkeypatch.setenv("CODEX_BIN", "codex")
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setenv("CODEX_MODEL", "")
    monkeypatch.setenv("CODEX_TIMEOUT_SECONDS", "3600")
    monkeypatch.setenv("WORKER_POLL_SECONDS", "10")
    monkeypatch.setenv("AUTO_SUPPORT_REPLY_MIN_CONFIDENCE", "0.85")
    monkeypatch.setenv("AUTO_CONFIRM_INTENT_MIN_CONFIDENCE", "0.90")
    monkeypatch.setenv("MAX_IMAGES_PER_MESSAGE", "3")
    monkeypatch.setenv("MAX_IMAGE_BYTES", str(5 * 1024 * 1024))
    monkeypatch.setenv("SESSION_DEFAULT_HOURS", "12")
    monkeypatch.setenv("SESSION_REMEMBER_DAYS", "30")
    monkeypatch.setenv("SLACK_ENABLED", "true")
    monkeypatch.setenv("SLACK_DEFAULT_TARGET_NAME", "ops_primary")
    monkeypatch.setenv("SLACK_TARGETS_JSON", '{"ops_primary":{"enabled":true,"webhook_url":"https://hooks.slack.com/services/T000/B000/XXXX"}}')
    monkeypatch.setenv("SLACK_NOTIFY_TICKET_CREATED", "true")
    monkeypatch.setenv("SLACK_NOTIFY_PUBLIC_MESSAGE_ADDED", "true")
    monkeypatch.setenv("SLACK_NOTIFY_STATUS_CHANGED", "true")
    monkeypatch.setenv("SLACK_MESSAGE_PREVIEW_MAX_CHARS", "240")
    monkeypatch.setenv("SLACK_HTTP_TIMEOUT_SECONDS", "12")
    monkeypatch.setenv("SLACK_DELIVERY_BATCH_SIZE", "8")
    monkeypatch.setenv("SLACK_DELIVERY_MAX_ATTEMPTS", "6")
    monkeypatch.setenv("SLACK_DELIVERY_STALE_LOCK_SECONDS", "90")

    from shared.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()

    assert settings.slack.enabled is False
    assert settings.slack.is_valid is True
    assert settings.slack.default_target_name is None
    assert settings.slack.targets == ()
    assert settings.slack.any_notify_enabled is False
    assert settings.slack.message_preview_max_chars == 200
    assert settings.slack.http_timeout_seconds == 10
    assert settings.slack.delivery_batch_size == 10
    assert settings.slack.delivery_max_attempts == 5
    assert settings.slack.delivery_stale_lock_seconds == 120
    get_settings.cache_clear()


def test_get_settings_ignores_invalid_legacy_slack_env_runtime_config(monkeypatch, tmp_path):
    workspace_dir = tmp_path / "workspace"
    uploads_dir = tmp_path / "uploads"
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{(tmp_path / 'triage.db').resolve()}")
    monkeypatch.setenv("UI_DEFAULT_LOCALE", "pt-BR")
    monkeypatch.setenv("UPLOADS_DIR", str(uploads_dir))
    monkeypatch.setenv("TRIAGE_WORKSPACE_DIR", str(workspace_dir))
    monkeypatch.setenv("REPO_MOUNT_DIR", str(workspace_dir / "app"))
    monkeypatch.setenv("MANUALS_MOUNT_DIR", str(workspace_dir / "manuals"))
    monkeypatch.setenv("CODEX_BIN", "codex")
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setenv("CODEX_MODEL", "")
    monkeypatch.setenv("CODEX_TIMEOUT_SECONDS", "3600")
    monkeypatch.setenv("WORKER_POLL_SECONDS", "10")
    monkeypatch.setenv("AUTO_SUPPORT_REPLY_MIN_CONFIDENCE", "0.85")
    monkeypatch.setenv("AUTO_CONFIRM_INTENT_MIN_CONFIDENCE", "0.90")
    monkeypatch.setenv("MAX_IMAGES_PER_MESSAGE", "3")
    monkeypatch.setenv("MAX_IMAGE_BYTES", str(5 * 1024 * 1024))
    monkeypatch.setenv("SESSION_DEFAULT_HOURS", "12")
    monkeypatch.setenv("SESSION_REMEMBER_DAYS", "30")
    monkeypatch.setenv("SLACK_ENABLED", "definitely-not-a-bool")
    monkeypatch.setenv("SLACK_TARGETS_JSON", "{not-json")
    monkeypatch.setenv("SLACK_HTTP_TIMEOUT_SECONDS", "999")

    from shared.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()

    assert settings.slack.enabled is False
    assert settings.slack.is_valid is True
    settings.validate_contracts()
    get_settings.cache_clear()


def _run_script(args: list[str], *, env: dict[str, str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=Path.cwd(),
        env=env,
        text=True,
        capture_output=True,
        check=check,
    )


def _last_json_line(output: str) -> dict[str, object]:
    for line in reversed([line.strip() for line in output.splitlines() if line.strip()]):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    raise AssertionError(f"No JSON line found in output: {output}")


def test_bootstrap_web_and_worker_scripts_validate_end_to_end(tmp_path):
    _require_python_multipart()
    env = _script_env(tmp_path)
    db_path = tmp_path / "triage.db"
    _create_runtime_schema(db_path)

    bootstrap = _run_script(["scripts/bootstrap_workspace.py"], env=env)
    assert '"bootstrap_version": "stage1-v5"' in bootstrap.stdout
    with sqlite3.connect(db_path) as connection:
        rows = dict(connection.execute("SELECT key, value_json FROM system_state").fetchall())
    assert "bootstrap_version" in rows
    assert "worker_heartbeat" in rows

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
    _require_python_multipart()
    env = _script_env(tmp_path)

    web = _run_script(["scripts/run_web.py", "--check"], env=env, check=False)
    worker = _run_script(["scripts/run_worker.py", "--check"], env=env, check=False)

    assert web.returncode != 0
    assert "Web smoke check failed: /readyz returned 503" in web.stderr

    assert worker.returncode != 0
    assert "Required " in worker.stderr
    assert "does not exist" in worker.stderr


def test_script_checks_fail_when_backfill_is_pending(tmp_path):
    _require_python_multipart()
    env = _script_env(tmp_path)
    db_path = tmp_path / "triage.db"
    _create_runtime_schema(db_path)

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO ai_runs (
                id,
                ticket_id,
                status,
                pipeline_version,
                final_output_json,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "run-1",
                "ticket-1",
                "succeeded",
                None,
                None,
                "2026-04-06T00:00:00+00:00",
            ),
        )

    _run_script(["scripts/bootstrap_workspace.py"], env=env)
    web = _run_script(["scripts/run_web.py", "--check"], env=env, check=False)
    worker = _run_script(["scripts/run_worker.py", "--check"], env=env, check=False)

    assert web.returncode != 0
    assert "AI run history is not ready" in web.stderr
    assert worker.returncode != 0
    assert "AI run history is not ready" in worker.stderr


def test_setup_postgres_local_check_only_accepts_localhost_url():
    result = subprocess.run(
        ["bash", "scripts/setup_postgres_local.sh", "--check-only", "--database-url", "postgresql+psycopg://triage:triage@localhost:5432/triage"],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        check=True,
    )

    payload = _last_json_line(result.stdout)
    assert payload["script"] == "setup_postgres_local.sh"
    assert payload["mode"] == "check_only"
    assert payload["database_host"] == "localhost"


def test_setup_postgres_local_check_only_rejects_remote_url():
    result = subprocess.run(
        ["bash", "scripts/setup_postgres_local.sh", "--check-only", "--database-url", "postgresql+psycopg://triage:triage@db.example.com:5432/triage"],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "only supports localhost PostgreSQL URLs" in result.stderr
