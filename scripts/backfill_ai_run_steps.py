from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.agent_specs import (
    LEGACY_AGENT_SPEC_ID,
    LEGACY_AGENT_SPEC_VERSION,
    LEGACY_PIPELINE_VERSION,
)
from shared.config import get_settings
from shared.db import session_scope
from shared.models import AIRun, AIRunStep
from worker.output_contracts import OutputContractError, validate_contract_output

ACCEPTED_ANALYSIS_STATUSES = ("succeeded", "human_review")
TERMINAL_BACKFILL_STATUSES = ("skipped", "succeeded", "human_review", "failed", "superseded")
TRIAGE_OUTPUT_CONTRACT = "triage_result"


def _candidate_runs(db) -> list[AIRun]:
    return list(
        db.execute(
            select(AIRun)
            .where(
                AIRun.status.in_(TERMINAL_BACKFILL_STATUSES),
                AIRun.pipeline_version.is_(None),
            )
            .order_by(AIRun.created_at.asc())
        ).scalars()
    )


def _load_existing_step(db, *, run_id) -> AIRunStep | None:
    return db.execute(
        select(AIRunStep)
        .where(AIRunStep.ai_run_id == run_id)
        .order_by(AIRunStep.step_index.asc())
        .limit(1)
    ).scalar_one_or_none()


def _load_legacy_output(run: AIRun) -> tuple[dict[str, object] | None, str | None]:
    if not run.final_output_path:
        return None, "missing final_output_path"
    output_path = Path(run.final_output_path)
    if not output_path.is_file():
        return None, f"missing final_output_path file: {output_path}"
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"invalid final_output_path JSON: {exc}"
    if not isinstance(payload, dict):
        return None, "legacy final_output_path did not contain a JSON object"
    try:
        validate_contract_output(TRIAGE_OUTPUT_CONTRACT, payload)
    except OutputContractError as exc:
        return None, str(exc)
    return payload, None


def _new_step_from_run(run: AIRun, *, output_json: dict[str, object] | None) -> AIRunStep:
    return AIRunStep(
        ai_run_id=run.id,
        step_index=1,
        step_kind="specialist",
        agent_spec_id=LEGACY_AGENT_SPEC_ID,
        agent_spec_version=LEGACY_AGENT_SPEC_VERSION,
        output_contract=TRIAGE_OUTPUT_CONTRACT,
        model_name=run.model_name,
        status=run.status,
        prompt_path=run.prompt_path,
        schema_path=run.schema_path,
        final_output_path=run.final_output_path,
        stdout_jsonl_path=run.stdout_jsonl_path,
        stderr_path=run.stderr_path,
        output_json=output_json,
        error_text=run.error_text,
        started_at=run.started_at,
        ended_at=run.ended_at,
        created_at=run.created_at,
    )


def _sync_step_from_run(step: AIRunStep, run: AIRun, *, output_json: dict[str, object] | None) -> None:
    step.step_index = 1
    step.step_kind = "specialist"
    step.agent_spec_id = LEGACY_AGENT_SPEC_ID
    step.agent_spec_version = LEGACY_AGENT_SPEC_VERSION
    step.output_contract = TRIAGE_OUTPUT_CONTRACT
    step.model_name = run.model_name
    step.status = run.status
    step.prompt_path = run.prompt_path
    step.schema_path = run.schema_path
    step.final_output_path = run.final_output_path
    step.stdout_jsonl_path = run.stdout_jsonl_path
    step.stderr_path = run.stderr_path
    if output_json is not None:
        step.output_json = output_json
    step.error_text = run.error_text
    step.started_at = run.started_at
    step.ended_at = run.ended_at
    step.created_at = run.created_at


def _hydrate_run(run: AIRun, *, step_id, output_json: dict[str, object] | None) -> None:
    run.pipeline_version = LEGACY_PIPELINE_VERSION
    run.final_step_id = step_id
    run.final_agent_spec_id = LEGACY_AGENT_SPEC_ID
    run.final_output_contract = TRIAGE_OUTPUT_CONTRACT
    if output_json is not None:
        run.final_output_json = output_json


def _backfill_run(db, *, run: AIRun, dry_run: bool, summary: dict[str, object]) -> None:
    summary["processed_runs"] += 1
    existing_step = _load_existing_step(db, run_id=run.id)
    output_json: dict[str, object] | None = None

    if run.status in ACCEPTED_ANALYSIS_STATUSES:
        output_json, output_error = _load_legacy_output(run)
        if output_json is None:
            summary["blocking_errors"] += 1
            summary["accepted_runs_missing_output"] += 1
            summary["errors"].append(
                {
                    "run_id": str(run.id),
                    "ticket_id": str(run.ticket_id),
                    "status": run.status,
                    "error": output_error,
                }
            )
            return
        summary["validated_outputs"] += 1

    if dry_run:
        if existing_step is None:
            summary["created_steps"] += 1
        else:
            summary["updated_steps"] += 1
        summary["updated_runs"] += 1
        return

    if existing_step is None:
        step = _new_step_from_run(run, output_json=output_json)
        db.add(step)
        db.flush()
        summary["created_steps"] += 1
    else:
        step = existing_step
        _sync_step_from_run(step, run, output_json=output_json)
        summary["updated_steps"] += 1

    _hydrate_run(run, step_id=step.id, output_json=output_json)
    summary["updated_runs"] += 1


def run_backfill(*, dry_run: bool) -> dict[str, object]:
    settings = get_settings()
    summary: dict[str, object] = {
        "script": "backfill_ai_run_steps.py",
        "dry_run": dry_run,
        "processed_runs": 0,
        "created_steps": 0,
        "updated_steps": 0,
        "updated_runs": 0,
        "validated_outputs": 0,
        "accepted_runs_missing_output": 0,
        "blocking_errors": 0,
        "errors": [],
    }

    with session_scope(settings) as db:
        for run in _candidate_runs(db):
            _backfill_run(db, run=run, dry_run=dry_run, summary=summary)

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill legacy ai_runs into ai_run_steps and structured output fields.")
    parser.add_argument("--dry-run", action="store_true", help="Inspect and report without writing changes.")
    args = parser.parse_args()

    summary = run_backfill(dry_run=args.dry_run)
    print(json.dumps(summary, indent=2, sort_keys=True))
    if int(summary["blocking_errors"]) > 0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
