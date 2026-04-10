from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shared.config import Settings
from shared.db import session_scope
from shared.integrations import build_slack_runtime_context
from shared.logging import log_worker_event
from shared.models import AIRun, AIRunStep, Ticket
from shared.security import utc_now
from shared.ticketing import (
    clear_requeue_request,
    create_pending_ai_run,
    process_deferred_requeue,
    publish_ai_failure_note,
    record_status_change,
)
from worker.step_runner import write_run_manifest_snapshot

STALE_RUN_RECOVERY_BATCH_SIZE = 20


def _append_manifest_run_id(manifest_run_ids: list, run_id) -> None:
    if run_id not in manifest_run_ids:
        manifest_run_ids.append(run_id)


def claim_oldest_pending_run(db: Session, *, worker_pid: int, worker_instance_id: str) -> AIRun | None:
    statement = (
        select(AIRun)
        .where(AIRun.status == "pending")
        .order_by(AIRun.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    run = db.execute(statement).scalar_one_or_none()
    if run is None:
        return None
    started_at = utc_now()
    run.status = "running"
    run.worker_pid = worker_pid
    run.worker_instance_id = worker_instance_id
    run.started_at = started_at
    run.last_heartbeat_at = started_at
    run.ended_at = None
    run.error_text = None
    return run


def _stale_reference_expr():
    return func.coalesce(AIRun.last_heartbeat_at, AIRun.started_at, AIRun.created_at)


def _stale_run_error_text(run: AIRun, *, stale_timeout_seconds: int) -> str:
    if run.last_heartbeat_at is not None:
        return f"Run became stale after no worker heartbeat update for at least {stale_timeout_seconds} seconds."
    return f"Run became stale before any per-run heartbeat was recorded and was recovered after at least {stale_timeout_seconds} seconds."


def _recovery_exhausted_note_body(*, retry_budget: int, stale_timeout_seconds: int) -> str:
    return "\n".join(
        [
            "AI triage run stalled repeatedly and automatic recovery was exhausted.",
            "",
            f"Recovery attempts exhausted after {retry_budget} retries.",
            f"Stale-run threshold: {stale_timeout_seconds} seconds without a per-run heartbeat update.",
        ]
    )


def _mark_running_steps_failed(db: Session, *, run_id, error_text: str, failed_at) -> None:
    running_steps = list(
        db.execute(
            select(AIRunStep)
            .where(AIRunStep.ai_run_id == run_id, AIRunStep.status == "running")
            .order_by(AIRunStep.step_index.asc())
        ).scalars()
    )
    for step in running_steps:
        step.status = "failed"
        step.error_text = error_text
        step.ended_at = failed_at


def recover_stale_runs(settings: Settings) -> int:
    stale_before = utc_now() - timedelta(seconds=settings.ai_run_stale_timeout_seconds)
    manifest_run_ids: list = []
    recovered_count = 0
    with session_scope(settings) as db:
        slack_runtime = build_slack_runtime_context(settings, db=db)
        stale_runs = list(
            db.execute(
                select(AIRun)
                .where(
                    AIRun.status == "running",
                    _stale_reference_expr() < stale_before,
                )
                .order_by(_stale_reference_expr().asc(), AIRun.created_at.asc())
                .limit(STALE_RUN_RECOVERY_BATCH_SIZE)
                .with_for_update(skip_locked=True)
            ).scalars()
        )
        for run in stale_runs:
            failed_at = utc_now()
            error_text = _stale_run_error_text(
                run,
                stale_timeout_seconds=settings.ai_run_stale_timeout_seconds,
            )
            run.status = "failed"
            run.ended_at = failed_at
            run.error_text = error_text
            _mark_running_steps_failed(db, run_id=run.id, error_text=error_text, failed_at=failed_at)
            _append_manifest_run_id(manifest_run_ids, run.id)
            ticket = db.get(Ticket, run.ticket_id)
            if ticket is None:
                log_worker_event(
                    "stale_run_recovered_without_ticket",
                    level="error",
                    run_id=str(run.id),
                    ticket_id=str(run.ticket_id),
                )
                recovered_count += 1
                continue

            if run.recovery_attempt_count >= settings.ai_run_max_recovery_attempts:
                if ticket.requeue_requested:
                    clear_requeue_request(ticket, touched_at=failed_at)
                publish_ai_failure_note(
                    db,
                    ticket=ticket,
                    ai_run_id=run.id,
                    body_markdown=_recovery_exhausted_note_body(
                        retry_budget=settings.ai_run_max_recovery_attempts,
                        stale_timeout_seconds=settings.ai_run_stale_timeout_seconds,
                    ),
                    created_at=failed_at,
                )
                if ticket.status != "waiting_on_dev_ti":
                    record_status_change(
                        db,
                        slack_runtime=slack_runtime,
                        ticket=ticket,
                        to_status="waiting_on_dev_ti",
                        changed_by_type="system",
                        changed_at=failed_at,
                    )
                log_worker_event(
                    "stale_run_recovery_exhausted",
                    level="error",
                    run_id=str(run.id),
                    ticket_id=str(ticket.id),
                    recovery_attempt_count=run.recovery_attempt_count,
                )
                recovered_count += 1
                continue

            replacement_run = None
            if ticket.requeue_requested and ticket.requeue_trigger:
                replacement_run = process_deferred_requeue(db, ticket=ticket)
            else:
                replacement_run = create_pending_ai_run(
                    db,
                    ticket_id=run.ticket_id,
                    triggered_by=run.triggered_by,
                    requested_by_user_id=run.requested_by_user_id,
                    forced_route_target_id=run.forced_route_target_id,
                    forced_specialist_id=run.forced_specialist_id,
                    recovered_from_run_id=run.id,
                    recovery_attempt_count=run.recovery_attempt_count + 1,
                )
            if replacement_run is not None:
                _append_manifest_run_id(manifest_run_ids, replacement_run.id)
            log_worker_event(
                "stale_run_recovered",
                run_id=str(run.id),
                ticket_id=str(ticket.id),
                replacement_run_id=str(replacement_run.id) if replacement_run is not None else None,
                recovery_attempt_count=run.recovery_attempt_count,
            )
            recovered_count += 1

    for run_id in manifest_run_ids:
        write_run_manifest_snapshot(settings, run_id=run_id)
    return recovered_count
