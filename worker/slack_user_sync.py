from __future__ import annotations

import re
import threading
import time

from shared.config import Settings
from shared.db import session_scope
from shared.logging import log_worker_event
from shared.security import utc_now
from shared.slack_user_sync import (
    SlackUserSyncSnapshot,
    claim_requested_slack_user_sync,
    persist_slack_user_sync_state,
    sync_slack_user_ids_by_email,
)

_SINGLE_LINE_WHITESPACE_RE = re.compile(r"\s+", re.UNICODE)


def run_requested_slack_user_sync(settings: Settings, *, worker_instance_id: str) -> bool:
    with session_scope(settings) as db:
        request = claim_requested_slack_user_sync(
            db,
            worker_instance_id=worker_instance_id,
            started_at=utc_now(),
        )
    if request is None:
        return False
    log_worker_event(
        "slack_user_sync_started",
        worker_instance_id=worker_instance_id,
        trigger=request.trigger,
        requested_at=request.requested_at,
    )
    try:
        snapshot = sync_slack_user_ids_by_email(
            settings,
            trigger=request.trigger,
            started_at=request.started_at,
            requested_at=request.requested_at,
            requested_by_user_id=request.requested_by_user_id,
        )
    except Exception as exc:
        snapshot = SlackUserSyncSnapshot(
            status="failed",
            checked_at=utc_now().isoformat(),
            started_at=request.started_at,
            requested_at=request.requested_at,
            trigger=request.trigger,
            requested_by_user_id=request.requested_by_user_id,
            error_code="unexpected_error",
            summary=_sanitize_summary(str(exc)),
        )
    with session_scope(settings) as db:
        persist_slack_user_sync_state(db, snapshot=snapshot, updated_at=utc_now())
    log_worker_event(
        "slack_user_sync_completed",
        worker_instance_id=worker_instance_id,
        trigger=snapshot.trigger,
        status=snapshot.status,
        matched_count=snapshot.matched_count,
        updated_count=snapshot.updated_count,
        no_match_count=snapshot.no_match_count,
        conflict_count=snapshot.conflict_count,
        error_code=snapshot.error_code,
    )
    return True


def _sanitize_summary(value: str) -> str:
    return _SINGLE_LINE_WHITESPACE_RE.sub(" ", value).strip()


def slack_user_sync_loop(
    settings: Settings,
    *,
    worker_instance_id: str,
    stop_event: threading.Event | None = None,
    interval_seconds: float | None = None,
) -> None:
    resolved_interval_seconds = settings.worker_poll_seconds if interval_seconds is None else interval_seconds
    while True:
        try:
            run_requested_slack_user_sync(settings, worker_instance_id=worker_instance_id)
        except Exception as exc:
            log_worker_event(
                "slack_user_sync_error",
                level="error",
                worker_instance_id=worker_instance_id,
                error=str(exc),
            )
        if stop_event is None:
            time.sleep(resolved_interval_seconds)
            continue
        if stop_event.wait(resolved_interval_seconds):
            return
