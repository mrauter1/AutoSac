from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.config import Settings, SettingsError, get_settings
from shared.db import ping_database


def _resolve_executable(command: str) -> str | None:
    candidate = Path(command).expanduser()
    if candidate.is_absolute() or "/" in command:
        return str(candidate) if candidate.exists() else None
    return shutil.which(command)


def _ensure_local_paths(settings: Settings) -> None:
    for path in (
        settings.uploads_dir,
        settings.triage_workspace_dir,
        settings.runs_dir,
        settings.repo_mount_dir,
        settings.manuals_mount_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)


def _uses_local_postgres(database_url: str) -> bool:
    parsed = urlparse(database_url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    return scheme.startswith("postgresql") and host in {"", "localhost", "127.0.0.1", "::1"}


def _run_local_postgres_setup(settings: Settings) -> None:
    setup_script = Path(__file__).with_name("setup_postgres_local.sh")
    subprocess.run(
        ["bash", str(setup_script), "--database-url", settings.database_url],
        check=True,
    )


def _check_database(settings: Settings) -> tuple[bool, str | None]:
    try:
        ping_database(settings)
    except Exception as exc:  # pragma: no cover - exercised through script tests
        return False, str(exc)
    return True, None


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate local AutoSac prerequisites and optionally prepare localhost PostgreSQL.")
    parser.add_argument(
        "--ensure-workspace-dirs",
        action="store_true",
        help="Create uploads, workspace, runs, and mount directories if they do not already exist.",
    )
    parser.add_argument(
        "--setup-postgres-local",
        action="store_true",
        help="If DATABASE_URL targets localhost PostgreSQL and the database is unavailable, run setup_postgres_local.sh.",
    )
    args = parser.parse_args()

    summary: dict[str, object] = {
        "script": "preflight_setup.py",
        "status": "not_ready",
        "workspace_dirs_created": bool(args.ensure_workspace_dirs),
        "database_setup_attempted": False,
    }

    try:
        settings = get_settings()
    except SettingsError as exc:
        summary["error"] = str(exc)
        print(json.dumps(summary))
        raise SystemExit(1) from exc

    if args.ensure_workspace_dirs:
        try:
            _ensure_local_paths(settings)
        except OSError as exc:
            summary["error"] = f"Could not create configured local path: {exc}"
            print(json.dumps(summary))
            raise SystemExit(1) from exc

    codex_path = _resolve_executable(settings.codex_bin)
    summary["codex_bin"] = {
        "configured": settings.codex_bin,
        "resolved_path": codex_path,
        "ok": codex_path is not None,
    }
    summary["paths"] = {
        "uploads_dir": str(settings.uploads_dir),
        "triage_workspace_dir": str(settings.triage_workspace_dir),
        "runs_dir": str(settings.runs_dir),
        "repo_mount_dir": str(settings.repo_mount_dir),
        "manuals_mount_dir": str(settings.manuals_mount_dir),
        "triage_workspace_exists": settings.triage_workspace_dir.is_dir(),
        "repo_mount_exists": settings.repo_mount_dir.is_dir(),
        "manuals_mount_exists": settings.manuals_mount_dir.is_dir(),
        "uploads_dir_exists": settings.uploads_dir.is_dir(),
        "runs_dir_exists": settings.runs_dir.is_dir(),
    }

    database_ok, database_error = _check_database(settings)
    local_postgres = _uses_local_postgres(settings.database_url)
    if not database_ok and args.setup_postgres_local and local_postgres:
        summary["database_setup_attempted"] = True
        _run_local_postgres_setup(settings)
        database_ok, database_error = _check_database(settings)

    summary["database"] = {
        "ok": database_ok,
        "error": database_error,
        "local_postgres_url": local_postgres,
    }
    summary["next_steps"] = [
        "python scripts/preflight_setup.py --ensure-workspace-dirs --setup-postgres-local",
        "alembic upgrade head",
        "python scripts/backfill_ai_run_steps.py",
        "python scripts/bootstrap_workspace.py",
    ]

    ready = (
        bool(summary["codex_bin"]["ok"])
        and bool(summary["paths"]["triage_workspace_exists"])
        and bool(summary["paths"]["repo_mount_exists"])
        and bool(summary["paths"]["manuals_mount_exists"])
        and bool(summary["paths"]["uploads_dir_exists"])
        and bool(summary["paths"]["runs_dir_exists"])
        and database_ok
    )

    summary["status"] = "ok" if ready else "not_ready"
    print(json.dumps(summary))
    if not ready:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
