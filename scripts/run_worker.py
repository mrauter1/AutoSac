from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.config import Settings, SettingsError, get_settings
from shared.db import ping_database
from shared.run_history import assert_ai_run_history_ready
from shared.workspace import create_missing_workspace_contract_files, verify_workspace_contract_paths
from worker.main import main


def verify_persistent_codex_authentication(settings: Settings) -> None:
    if not settings.codex_conversations_enabled or settings.codex_api_key:
        return
    env = os.environ.copy()
    env["CODEX_HOME"] = str(settings.resolved_codex_home)
    env.pop("CODEX_API_KEY", None)
    try:
        result = subprocess.run(
            [settings.codex_bin, "login", "status"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SettingsError(f"Could not verify Codex authentication for CODEX_HOME: {exc}") from exc
    if result.returncode != 0:
        raise SettingsError(
            "Codex is not authenticated for the configured CODEX_HOME. "
            f"Run `CODEX_HOME={settings.resolved_codex_home} {settings.codex_bin} login` as the worker user."
        )


def verify_startup_readiness(*, create_missing_workspace_files: bool = False) -> None:
    settings = get_settings()
    settings.validate_worker_contracts()
    verify_persistent_codex_authentication(settings)
    ping_database(settings)
    if create_missing_workspace_files:
        create_missing_workspace_contract_files(settings)
    verify_workspace_contract_paths(settings)
    assert_ai_run_history_ready(settings)


def smoke_check() -> None:
    verify_startup_readiness(create_missing_workspace_files=False)
    settings = get_settings()
    print(
        json.dumps(
            {
                "script": "run_worker.py",
                "status": "ok",
                "worker_poll_seconds": settings.worker_poll_seconds,
            }
        )
    )


def cli() -> None:
    parser = argparse.ArgumentParser(description="Run the Stage 1 worker.")
    parser.add_argument("--check", action="store_true", help="Run a deterministic smoke check and exit.")
    args = parser.parse_args()
    if args.check:
        try:
            smoke_check()
        except Exception as exc:
            raise SystemExit(str(exc)) from exc
        return
    try:
        verify_startup_readiness(create_missing_workspace_files=True)
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
    main()


if __name__ == "__main__":
    cli()
