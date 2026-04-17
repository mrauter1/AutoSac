from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

import uvicorn

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.config import get_settings
from shared.db import ping_database
from shared.logging import log_web_event
from shared.run_history import assert_ai_run_history_ready
from shared.workspace import is_missing_workspace_skill_file_error, verify_workspace_contract_paths


def verify_startup_readiness(*, allow_missing_workspace_skill: bool = False) -> None:
    settings = get_settings()
    settings.validate_contracts()
    ping_database(settings)
    try:
        verify_workspace_contract_paths(settings)
    except FileNotFoundError as exc:
        if not allow_missing_workspace_skill or not is_missing_workspace_skill_file_error(exc):
            raise
        log_web_event("workspace_skill_missing_startup_warning", level="warning", error=str(exc))
    assert_ai_run_history_ready(settings)


def smoke_check() -> None:
    from fastapi.testclient import TestClient
    from app.main import create_app

    app = create_app()
    with TestClient(app) as client:
        health_response = client.get("/healthz")
        ready_response = client.get("/readyz")

    if health_response.status_code != 200 or health_response.json() != {"status": "ok"}:
        raise SystemExit("Web smoke check failed: /healthz did not return the expected payload")
    if ready_response.status_code != 200 or ready_response.json() != {"status": "ready"}:
        raise SystemExit(
            f"Web smoke check failed: /readyz returned {ready_response.status_code}: {ready_response.text}"
        )

    print(
        json.dumps(
            {
                "script": "run_web.py",
                "status": "ok",
                "healthz_status": health_response.status_code,
                "readyz_status": ready_response.status_code,
            }
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Stage 1 web app.")
    parser.add_argument("--check", action="store_true", help="Run a deterministic smoke check and exit.")
    args = parser.parse_args()
    if args.check:
        smoke_check()
        return
    try:
        verify_startup_readiness(allow_missing_workspace_skill=True)
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=False)


if __name__ == "__main__":
    main()
