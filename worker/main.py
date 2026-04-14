from __future__ import annotations

from dataclasses import dataclass
import os
import threading
import time
import uuid

from sqlalchemy.orm import Session

from shared.config import get_settings
from shared.db import session_scope
from shared.logging import log_worker_event
from shared.contracts import WORKSPACE_BOOTSTRAP_VERSION
from shared.models import AIRun, SystemState
from shared.security import utc_now
from shared.slack_dm import load_slack_dm_settings
from shared.slack_user_sync import request_slack_user_sync
from shared.ticketing import ensure_system_state_defaults
from worker.queue import claim_oldest_pending_run, recover_stale_runs
from worker.slack_delivery import delivery_loop as slack_delivery_loop
from worker.slack_user_sync import slack_user_sync_loop
from worker.triage import process_ai_run


@dataclass(frozen=True)
class WorkerIdentity:
    worker_pid: int
    worker_instance_id: str


class ActiveRunTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._run_id = None

    def set_run_id(self, run_id) -> None:
        with self._lock:
            self._run_id = run_id

    def clear_run_id(self, run_id=None) -> None:
        with self._lock:
            if run_id is None or self._run_id == run_id:
                self._run_id = None

    def get_run_id(self):
        with self._lock:
            return self._run_id


DEFAULT_WORKER_IDENTITY = WorkerIdentity(worker_pid=os.getpid(), worker_instance_id=f"worker-{uuid.uuid4()}")


def ensure_worker_system_state(db: Session) -> None:
    ensure_system_state_defaults(db, WORKSPACE_BOOTSTRAP_VERSION)


def update_worker_heartbeat(
    db: Session,
    *,
    worker_identity: WorkerIdentity,
    active_run_id=None,
) -> None:
    ensure_worker_system_state(db)
    db.flush()
    heartbeat = db.get(SystemState, "worker_heartbeat")
    now = utc_now()
    payload = {
        "status": "alive",
        "timestamp": now.isoformat(),
        "worker_pid": worker_identity.worker_pid,
        "worker_instance_id": worker_identity.worker_instance_id,
        "active_run_id": str(active_run_id) if active_run_id is not None else None,
    }
    if heartbeat is None:
        heartbeat = SystemState(key="worker_heartbeat", value_json=payload, updated_at=now)
        db.add(heartbeat)
    else:
        heartbeat.value_json = payload
        heartbeat.updated_at = now

    if active_run_id is None:
        return
    run = db.get(AIRun, active_run_id)
    if run is None or run.status != "running":
        return
    if run.worker_instance_id != worker_identity.worker_instance_id:
        return
    run.last_heartbeat_at = now


def emit_worker_heartbeat(
    settings,
    *,
    worker_identity: WorkerIdentity = DEFAULT_WORKER_IDENTITY,
    active_run_tracker: ActiveRunTracker | None = None,
) -> None:
    active_run_id = active_run_tracker.get_run_id() if active_run_tracker is not None else None
    with session_scope(settings) as db:
        update_worker_heartbeat(
            db,
            worker_identity=worker_identity,
            active_run_id=active_run_id,
        )
    log_worker_event(
        "heartbeat",
        worker_pid=worker_identity.worker_pid,
        worker_instance_id=worker_identity.worker_instance_id,
        active_run_id=str(active_run_id) if active_run_id is not None else None,
    )


def heartbeat_loop(
    settings,
    *,
    worker_identity: WorkerIdentity = DEFAULT_WORKER_IDENTITY,
    active_run_tracker: ActiveRunTracker | None = None,
    stop_event: threading.Event | None = None,
    interval_seconds: float | None = None,
) -> None:
    resolved_interval_seconds = settings.worker_heartbeat_seconds if interval_seconds is None else interval_seconds
    while True:
        try:
            emit_worker_heartbeat(
                settings,
                worker_identity=worker_identity,
                active_run_tracker=active_run_tracker,
            )
        except Exception as exc:
            log_worker_event("heartbeat_error", level="error", error=str(exc))
        if stop_event is None:
            time.sleep(resolved_interval_seconds)
            continue
        if stop_event.wait(resolved_interval_seconds):
            return


def start_heartbeat_thread(
    settings,
    *,
    worker_identity: WorkerIdentity,
    active_run_tracker: ActiveRunTracker,
) -> threading.Thread:
    thread = threading.Thread(
        target=heartbeat_loop,
        kwargs={
            "settings": settings,
            "worker_identity": worker_identity,
            "active_run_tracker": active_run_tracker,
        },
        name="worker-heartbeat",
        daemon=True,
    )
    thread.start()
    return thread


def start_slack_delivery_thread(
    settings,
    *,
    worker_identity: WorkerIdentity,
) -> threading.Thread:
    thread = threading.Thread(
        target=slack_delivery_loop,
        kwargs={
            "slack_runtime": settings,
            "worker_instance_id": worker_identity.worker_instance_id,
        },
        name="worker-slack-delivery",
        daemon=True,
    )
    thread.start()
    return thread


def start_slack_user_sync_thread(
    settings,
    *,
    worker_identity: WorkerIdentity,
) -> threading.Thread:
    thread = threading.Thread(
        target=slack_user_sync_loop,
        kwargs={
            "settings": settings,
            "worker_instance_id": worker_identity.worker_instance_id,
        },
        name="worker-slack-user-sync",
        daemon=True,
    )
    thread.start()
    return thread


def build_worker_identity() -> WorkerIdentity:
    return WorkerIdentity(worker_pid=os.getpid(), worker_instance_id=f"worker-{uuid.uuid4()}")


def main() -> None:
    settings = get_settings()
    worker_identity = build_worker_identity()
    active_run_tracker = ActiveRunTracker()
    with session_scope(settings) as db:
        ensure_worker_system_state(db)
        if load_slack_dm_settings(db, app_settings=settings).has_stored_token:
            request_slack_user_sync(db, trigger="worker_started")
    log_worker_event(
        "worker_started",
        poll_seconds=settings.worker_poll_seconds,
        worker_pid=worker_identity.worker_pid,
        worker_instance_id=worker_identity.worker_instance_id,
    )
    start_heartbeat_thread(
        settings,
        worker_identity=worker_identity,
        active_run_tracker=active_run_tracker,
    )
    start_slack_user_sync_thread(
        settings,
        worker_identity=worker_identity,
    )
    start_slack_delivery_thread(
        settings,
        worker_identity=worker_identity,
    )
    while True:
        claimed_run_id = None
        recovered_count = recover_stale_runs(settings)
        if recovered_count:
            log_worker_event("stale_run_sweep_completed", recovered_count=recovered_count)
        with session_scope(settings) as db:
            run = claim_oldest_pending_run(
                db,
                worker_pid=worker_identity.worker_pid,
                worker_instance_id=worker_identity.worker_instance_id,
            )
            if run is not None:
                claimed_run_id = run.id
        if claimed_run_id is not None:
            active_run_tracker.set_run_id(claimed_run_id)
            try:
                process_ai_run(
                    settings,
                    run_id=claimed_run_id,
                    worker_instance_id=worker_identity.worker_instance_id,
                )
            except Exception as exc:
                log_worker_event("run_crash", level="error", run_id=str(claimed_run_id), error=str(exc))
            finally:
                active_run_tracker.clear_run_id(claimed_run_id)
        time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    main()
