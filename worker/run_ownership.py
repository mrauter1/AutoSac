from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models import AIRun


class RunOwnershipLost(RuntimeError):
    """Raised when a worker tries to mutate a run it no longer owns."""


def load_owned_running_run(db: Session, *, run_id, worker_instance_id: str) -> AIRun | None:
    statement = (
        select(AIRun)
        .where(
            AIRun.id == run_id,
            AIRun.status == "running",
            AIRun.worker_instance_id == worker_instance_id,
        )
        .limit(1)
        .with_for_update()
    )
    return db.execute(statement).scalar_one_or_none()
