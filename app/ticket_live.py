from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models import AIRun, AIRunStep, Ticket


TicketLiveAudience = Literal["ops", "requester"]
_ACTIVE_RUN_STATUSES = frozenset(("pending", "running"))
_REQUESTER_VISIBLE_ACTIVE_TICKET_STATUSES = frozenset(("new", "ai_triage"))


@dataclass(frozen=True)
class TicketLiveState:
    active: bool
    phase: str
    started_at: datetime | None
    delayed: bool
    version: str
    content_version: str
    run_key: str | None


def _canonical_value(value: object) -> object:
    if isinstance(value, datetime):
        normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return normalized.astimezone(timezone.utc).isoformat()
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _version_for(*values: object) -> str:
    payload = json.dumps(
        [_canonical_value(value) for value in values],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def _run_is_delayed(run: AIRun, *, now: datetime, stale_timeout_seconds: int) -> bool:
    activity_at = run.last_heartbeat_at or run.started_at or run.created_at
    if activity_at is None:
        return False
    normalized = activity_at if activity_at.tzinfo is not None else activity_at.replace(tzinfo=timezone.utc)
    return normalized <= now - timedelta(seconds=stale_timeout_seconds)


def _active_phase(run: AIRun, step: AIRunStep | None, *, delayed: bool, audience: TicketLiveAudience) -> str:
    if delayed:
        return "taking_longer"
    if run.status == "pending":
        return "queued"
    if audience == "requester":
        return "working"
    if step is None:
        return "preparing"
    if step.status in _ACTIVE_RUN_STATUSES:
        return {
            "router": "routing",
            "selector": "selecting_specialist",
            "specialist": "analyzing",
        }.get(step.step_kind, "analyzing")
    if step.step_kind == "specialist" and step.status in {"succeeded", "human_review"}:
        return "finalizing"
    return "preparing"


def build_ticket_live_state(
    *,
    ticket: Ticket,
    latest_run: AIRun | None,
    latest_step: AIRunStep | None,
    audience: TicketLiveAudience,
    stale_timeout_seconds: int,
    now: datetime | None = None,
) -> TicketLiveState:
    if audience not in {"ops", "requester"}:
        raise ValueError(f"Unsupported ticket live-state audience: {audience}")

    visible_run = latest_run
    if audience == "requester" and ticket.status not in _REQUESTER_VISIBLE_ACTIVE_TICKET_STATUSES:
        visible_run = None
        latest_step = None

    active = visible_run is not None and visible_run.status in _ACTIVE_RUN_STATUSES
    resolved_now = now or datetime.now(timezone.utc)
    delayed = bool(
        active
        and visible_run is not None
        and _run_is_delayed(visible_run, now=resolved_now, stale_timeout_seconds=stale_timeout_seconds)
    )
    phase = (
        _active_phase(visible_run, latest_step, delayed=delayed, audience=audience)
        if active and visible_run is not None
        else "idle"
    )

    run_id = getattr(visible_run, "id", None)
    run_status = getattr(visible_run, "status", None)
    step_id = getattr(latest_step, "id", None) if active else None
    step_kind = getattr(latest_step, "step_kind", None) if active else None
    step_status = getattr(latest_step, "status", None) if active else None
    state_version = _version_for(
        "ticket-live-v1",
        audience,
        ticket.updated_at,
        ticket.status,
        run_id,
        run_status,
        getattr(visible_run, "started_at", None),
        getattr(visible_run, "ended_at", None),
        step_id,
        step_kind,
        step_status,
        delayed,
    )

    content_values: list[object] = [
        "ticket-content-v1",
        audience,
        ticket.updated_at,
        ticket.status,
        ticket.title,
        ticket.urgent,
    ]
    if audience == "ops":
        content_values.extend(
            (
                ticket.assigned_to_user_id,
                ticket.route_target_id,
                run_id,
                run_status,
                getattr(visible_run, "final_step_id", None),
                getattr(visible_run, "ended_at", None),
            )
        )

    return TicketLiveState(
        active=active,
        phase=phase,
        started_at=getattr(visible_run, "started_at", None) if active else None,
        delayed=delayed,
        version=state_version,
        content_version=_version_for(*content_values),
        run_key=_version_for("ticket-live-run", audience, run_id)[:16] if run_id is not None else None,
    )


def load_ticket_live_state(
    db: Session,
    *,
    ticket: Ticket,
    audience: TicketLiveAudience,
    stale_timeout_seconds: int,
    now: datetime | None = None,
) -> TicketLiveState:
    latest_run = None
    should_load_run = audience == "ops" or ticket.status in _REQUESTER_VISIBLE_ACTIVE_TICKET_STATUSES
    if should_load_run:
        latest_run = db.execute(
            select(AIRun)
            .where(AIRun.ticket_id == ticket.id)
            .order_by(AIRun.created_at.desc(), AIRun.id.desc())
            .limit(1)
        ).scalars().first()
    latest_step = None
    if latest_run is not None and latest_run.status in _ACTIVE_RUN_STATUSES:
        latest_step = db.execute(
            select(AIRunStep)
            .where(AIRunStep.ai_run_id == latest_run.id)
            .order_by(AIRunStep.step_index.desc())
            .limit(1)
        ).scalars().first()
    return build_ticket_live_state(
        ticket=ticket,
        latest_run=latest_run,
        latest_step=latest_step,
        audience=audience,
        stale_timeout_seconds=stale_timeout_seconds,
        now=now,
    )


def if_none_match_matches(value: str | None, *, etag: str) -> bool:
    if not value:
        return False
    return any(candidate.strip() in {etag, "*"} for candidate in value.split(","))


def ticket_live_representation_etag(*, version: str, ui_locale: str) -> str:
    return f'"{_version_for("ticket-live-representation-v1", version, ui_locale)}"'
