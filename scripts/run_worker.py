from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.config import get_settings
from shared.db import ping_database
from shared.run_history import assert_ai_run_history_ready
from shared.workspace import create_missing_workspace_contract_files, verify_workspace_contract_paths
from worker.main import main


def verify_startup_readiness(*, create_missing_workspace_files: bool = False) -> None:
    settings = get_settings()
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
