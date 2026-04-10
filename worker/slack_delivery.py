from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import json
import re
import time
import threading
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.config import Settings
from shared.db import session_scope
from shared.logging import log_worker_event
from shared.models import IntegrationEvent, IntegrationEventTarget
from shared.security import utc_now

_SINGLE_LINE_WHITESPACE_RE = re.compile(r"\s+", re.UNICODE)
_ABSOLUTE_URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
_SLACK_WEBHOOK_FRAGMENT_RE = re.compile(r"hooks\.slack\.com/services/\S+", re.IGNORECASE)
_RETRYABLE_HTTP_STATUS_CODES = {408, 429}


@dataclass(frozen=True)
class DeliverySuppression:
    reason: str
    config_error_code: str | None = None
    config_error_summary: str | None = None


@dataclass(frozen=True)
class ClaimedDeliveryTarget:
    target_id: Any
    event_id: Any
    event_type: str
    target_name: str
    attempt_count: int
    locked_by: str
    payload_json: Any


def resolve_delivery_suppression(settings: Settings) -> DeliverySuppression | None:
    slack = settings.slack
    if not slack.enabled:
        return DeliverySuppression(reason="slack_disabled")
    if not slack.is_valid:
        return DeliverySuppression(
            reason="invalid_config",
            config_error_code=slack.config_error_code,
            config_error_summary=slack.config_error_summary,
        )
    return None


def build_retry_delay_seconds(*, attempt_count: int) -> int:
    return min(60 * (2 ** (attempt_count - 1)), 1800)


def load_owned_processing_target(
    db: Session,
    *,
    target_id,
    locked_by: str,
    attempt_count: int,
) -> IntegrationEventTarget | None:
    statement = (
        select(IntegrationEventTarget)
        .where(
            IntegrationEventTarget.id == target_id,
            IntegrationEventTarget.delivery_status == "processing",
            IntegrationEventTarget.locked_by == locked_by,
            IntegrationEventTarget.attempt_count == attempt_count,
        )
        .limit(1)
        .with_for_update()
    )
    return db.execute(statement).scalar_one_or_none()


def recover_stale_delivery_targets(db: Session, *, settings: Settings) -> list[IntegrationEventTarget]:
    stale_before = utc_now() - timedelta(seconds=settings.slack.delivery_stale_lock_seconds)
    targets = list(
        db.execute(
            select(IntegrationEventTarget)
            .where(
                IntegrationEventTarget.delivery_status == "processing",
                IntegrationEventTarget.locked_at < stale_before,
            )
            .order_by(IntegrationEventTarget.locked_at.asc(), IntegrationEventTarget.created_at.asc())
            .with_for_update(skip_locked=True)
        ).scalars()
    )
    recovered_at = utc_now()
    for target in targets:
        previous_locked_by = target.locked_by
        target.delivery_status = "failed"
        target.locked_at = None
        target.locked_by = None
        target.last_error = _sanitize_operator_summary(
            f"event_id={target.event_id} target_name={target.target_name} stale_lock_recovered previous_locked_by={previous_locked_by} attempt_count={target.attempt_count}",
        )
        target.next_attempt_at = recovered_at
    return targets


def claim_delivery_targets(
    db: Session,
    *,
    settings: Settings,
    locked_by: str,
) -> list[ClaimedDeliveryTarget]:
    claimed_targets = list(
        db.execute(
            select(IntegrationEventTarget)
            .where(
                IntegrationEventTarget.delivery_status.in_(("pending", "failed")),
                IntegrationEventTarget.next_attempt_at <= utc_now(),
            )
            .order_by(IntegrationEventTarget.next_attempt_at.asc(), IntegrationEventTarget.created_at.asc())
            .limit(settings.slack.delivery_batch_size)
            .with_for_update(skip_locked=True)
        ).scalars()
    )
    claimed_at = utc_now()
    claimed: list[ClaimedDeliveryTarget] = []
    for target in claimed_targets:
        event = db.get(IntegrationEvent, target.event_id)
        if event is None:
            continue
        target.delivery_status = "processing"
        target.attempt_count += 1
        target.locked_at = claimed_at
        target.locked_by = locked_by
        claimed.append(
            ClaimedDeliveryTarget(
                target_id=target.id,
                event_id=target.event_id,
                event_type=event.event_type,
                target_name=target.target_name,
                attempt_count=target.attempt_count,
                locked_by=locked_by,
                payload_json=event.payload_json,
            )
        )
    return claimed


def render_slack_message(*, event_type: str, payload_json: Any) -> str:
    if not isinstance(payload_json, dict):
        raise ValueError("payload_json must be an object")
    ticket_reference = _require_payload_text(payload_json, "ticket_reference")
    ticket_url = _require_payload_text(payload_json, "ticket_url")
    if event_type == "ticket.created":
        ticket_title = _escape_slack_text(_require_payload_text(payload_json, "ticket_title"))
        return "\n".join(
            (
                f"Novo ticket {ticket_reference}: {ticket_title}",
                f"Abrir no AutoSac: {ticket_url}",
            )
        )
    if event_type == "ticket.public_message_added":
        message_author_type = _require_payload_text(payload_json, "message_author_type")
        summary = f"Nova mensagem publica em {ticket_reference} por {message_author_type}"
        message_preview = payload_json.get("message_preview", "")
        if not isinstance(message_preview, str):
            raise ValueError("message_preview must be a string")
        escaped_preview = _escape_slack_text(message_preview)
        if escaped_preview:
            summary = f"{summary}: {escaped_preview}"
        return "\n".join(
            (
                summary,
                f"Abrir no AutoSac: {ticket_url}",
            )
        )
    if event_type == "ticket.status_changed":
        status_from = _require_payload_text(payload_json, "status_from")
        status_to = _require_payload_text(payload_json, "status_to")
        return "\n".join(
            (
                f"{ticket_reference} mudou de {status_from} para {status_to}",
                f"Abrir no AutoSac: {ticket_url}",
            )
        )
    raise ValueError(f"unsupported event_type {event_type}")


def send_slack_webhook(
    *,
    webhook_url: str,
    text: str,
    timeout_seconds: int,
) -> int:
    try:
        return asyncio.run(
            asyncio.wait_for(
                _post_slack_webhook_async(
                    webhook_url=webhook_url,
                    text=text,
                    timeout_seconds=timeout_seconds,
                ),
                timeout=timeout_seconds,
            )
        )
    except asyncio.TimeoutError as exc:
        raise httpx.ReadTimeout("request timed out") from exc


async def _post_slack_webhook_async(
    *,
    webhook_url: str,
    text: str,
    timeout_seconds: int,
) -> int:
    body = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(timeout_seconds),
    ) as client:
        response = await client.post(
            webhook_url,
            content=body,
            headers={"Content-Type": "application/json"},
        )
    return int(response.status_code)


def deliver_claimed_target(
    settings: Settings,
    *,
    claimed_target: ClaimedDeliveryTarget,
    send_webhook=send_slack_webhook,
) -> None:
    target = settings.slack.get_target(claimed_target.target_name)
    if target is None:
        _dead_letter_target(
            settings,
            claimed_target=claimed_target,
            last_error=_sanitize_operator_summary(
                f"event_id={claimed_target.event_id} target_name={claimed_target.target_name} attempt_count={claimed_target.attempt_count} terminal_error=missing_target_config",
            ),
            failure_class="missing_target_config",
        )
        return
    if not target.enabled:
        _dead_letter_target(
            settings,
            claimed_target=claimed_target,
            last_error=_sanitize_operator_summary(
                f"event_id={claimed_target.event_id} target_name={claimed_target.target_name} attempt_count={claimed_target.attempt_count} terminal_error=target_disabled",
            ),
            failure_class="target_disabled",
        )
        return

    try:
        rendered_text = render_slack_message(
            event_type=claimed_target.event_type,
            payload_json=claimed_target.payload_json,
        )
    except ValueError as exc:
        _dead_letter_target(
            settings,
            claimed_target=claimed_target,
            last_error=_sanitize_operator_summary(
                f"event_id={claimed_target.event_id} target_name={claimed_target.target_name} attempt_count={claimed_target.attempt_count} terminal_error={type(exc).__name__}: {exc}",
            ),
            failure_class=type(exc).__name__,
        )
        return

    try:
        status_code = send_webhook(
            webhook_url=target.webhook_url,
            text=rendered_text,
            timeout_seconds=settings.slack.http_timeout_seconds,
        )
    except httpx.TransportError as exc:
        _retry_target(
            settings,
            claimed_target=claimed_target,
            last_error=_sanitize_operator_summary(
                f"event_id={claimed_target.event_id} target_name={claimed_target.target_name} attempt_count={claimed_target.attempt_count} retryable_error={type(exc).__name__}: {exc}",
            ),
            failure_class=type(exc).__name__,
        )
        return

    if 200 <= status_code < 300:
        _mark_target_sent(settings, claimed_target=claimed_target, http_status=status_code)
        return
    if status_code in _RETRYABLE_HTTP_STATUS_CODES or 500 <= status_code < 600:
        _retry_target(
            settings,
            claimed_target=claimed_target,
            last_error=_sanitize_operator_summary(
                f"event_id={claimed_target.event_id} target_name={claimed_target.target_name} attempt_count={claimed_target.attempt_count} retryable_http_status={status_code}",
            ),
            http_status=status_code,
        )
        return
    _dead_letter_target(
        settings,
        claimed_target=claimed_target,
        last_error=_sanitize_operator_summary(
            f"event_id={claimed_target.event_id} target_name={claimed_target.target_name} attempt_count={claimed_target.attempt_count} terminal_http_status={status_code}",
        ),
        http_status=status_code,
    )


def run_delivery_cycle(settings: Settings, *, worker_instance_id: str) -> None:
    suppression = resolve_delivery_suppression(settings)
    if suppression is not None:
        if suppression.reason == "invalid_config":
            _log_delivery_suppression(suppression)
        return

    with session_scope(settings) as db:
        recover_stale_delivery_targets(db, settings=settings)

    with session_scope(settings) as db:
        claimed_targets = claim_delivery_targets(
            db,
            settings=settings,
            locked_by=worker_instance_id,
        )

    for claimed_target in claimed_targets:
        log_worker_event(
            "slack_target_claimed",
            event_id=str(claimed_target.event_id),
            target_name=claimed_target.target_name,
            delivery_status="processing",
            attempt_count=claimed_target.attempt_count,
            locked_by=claimed_target.locked_by,
        )
        deliver_claimed_target(
            settings,
            claimed_target=claimed_target,
        )


def delivery_loop(
    settings: Settings,
    *,
    worker_instance_id: str,
    stop_event: threading.Event | None = None,
    interval_seconds: float | None = None,
) -> None:
    resolved_interval_seconds = settings.worker_poll_seconds if interval_seconds is None else interval_seconds
    while True:
        try:
            run_delivery_cycle(
                settings,
                worker_instance_id=worker_instance_id,
            )
        except Exception as exc:
            log_worker_event(
                "slack_delivery_cycle_error",
                level="error",
                error=_sanitize_operator_summary(str(exc)),
                locked_by=worker_instance_id,
            )
        if stop_event is None:
            time.sleep(resolved_interval_seconds)
            continue
        if stop_event.wait(resolved_interval_seconds):
            return


def _mark_target_sent(
    settings: Settings,
    *,
    claimed_target: ClaimedDeliveryTarget,
    http_status: int,
) -> None:
    target_updated = False
    with session_scope(settings) as db:
        target = load_owned_processing_target(
            db,
            target_id=claimed_target.target_id,
            locked_by=claimed_target.locked_by,
            attempt_count=claimed_target.attempt_count,
        )
        if target is None:
            pass
        else:
            target.delivery_status = "sent"
            target.sent_at = utc_now()
            target.dead_lettered_at = None
            target.last_error = None
            target.locked_at = None
            target.locked_by = None
            target_updated = True
    if not target_updated:
        _log_delivery_ownership_lost(claimed_target=claimed_target)
        return
    log_worker_event(
        "slack_delivery_sent",
        event_id=str(claimed_target.event_id),
        target_name=claimed_target.target_name,
        delivery_status="sent",
        attempt_count=claimed_target.attempt_count,
        locked_by=claimed_target.locked_by,
        http_status=http_status,
    )


def _retry_target(
    settings: Settings,
    *,
    claimed_target: ClaimedDeliveryTarget,
    last_error: str,
    http_status: int | None = None,
    failure_class: str | None = None,
) -> None:
    if claimed_target.attempt_count >= settings.slack.delivery_max_attempts:
        _dead_letter_target(
            settings,
            claimed_target=claimed_target,
            last_error=last_error,
            http_status=http_status,
            failure_class=failure_class,
            exhausted=True,
        )
        return

    next_attempt_at = utc_now() + timedelta(
        seconds=build_retry_delay_seconds(attempt_count=claimed_target.attempt_count),
    )
    target_updated = False
    with session_scope(settings) as db:
        target = load_owned_processing_target(
            db,
            target_id=claimed_target.target_id,
            locked_by=claimed_target.locked_by,
            attempt_count=claimed_target.attempt_count,
        )
        if target is None:
            pass
        else:
            target.delivery_status = "failed"
            target.last_error = last_error
            target.sent_at = None
            target.dead_lettered_at = None
            target.locked_at = None
            target.locked_by = None
            target.next_attempt_at = next_attempt_at
            target_updated = True
    if not target_updated:
        _log_delivery_ownership_lost(claimed_target=claimed_target)
        return
    payload: dict[str, Any] = {
        "event_id": str(claimed_target.event_id),
        "target_name": claimed_target.target_name,
        "delivery_status": "failed",
        "attempt_count": claimed_target.attempt_count,
        "locked_by": claimed_target.locked_by,
        "next_attempt_at": next_attempt_at.isoformat(),
    }
    if http_status is not None:
        payload["http_status"] = http_status
    if failure_class is not None:
        payload["failure_class"] = failure_class
    log_worker_event("slack_delivery_retry_scheduled", **payload)


def _dead_letter_target(
    settings: Settings,
    *,
    claimed_target: ClaimedDeliveryTarget,
    last_error: str,
    http_status: int | None = None,
    failure_class: str | None = None,
    exhausted: bool = False,
) -> None:
    target_updated = False
    with session_scope(settings) as db:
        target = load_owned_processing_target(
            db,
            target_id=claimed_target.target_id,
            locked_by=claimed_target.locked_by,
            attempt_count=claimed_target.attempt_count,
        )
        if target is None:
            pass
        else:
            target.delivery_status = "dead_letter"
            target.dead_lettered_at = utc_now()
            target.sent_at = None
            target.last_error = last_error
            target.locked_at = None
            target.locked_by = None
            target_updated = True
    if not target_updated:
        _log_delivery_ownership_lost(claimed_target=claimed_target)
        return
    payload: dict[str, Any] = {
        "event_id": str(claimed_target.event_id),
        "target_name": claimed_target.target_name,
        "delivery_status": "dead_letter",
        "attempt_count": claimed_target.attempt_count,
        "locked_by": claimed_target.locked_by,
    }
    if http_status is not None:
        payload["http_status"] = http_status
    if failure_class is not None:
        payload["failure_class"] = failure_class
    if exhausted:
        payload["retry_exhausted"] = True
    log_worker_event("slack_delivery_dead_lettered", **payload)


def _require_payload_text(payload_json: dict[str, Any], key: str) -> str:
    value = payload_json.get(key)
    if not isinstance(value, str) or value == "":
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _escape_slack_text(value: str) -> str:
    sanitized = _SINGLE_LINE_WHITESPACE_RE.sub(" ", value).strip()
    return sanitized.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _sanitize_operator_summary(value: str) -> str:
    sanitized = _SINGLE_LINE_WHITESPACE_RE.sub(" ", value).strip()
    sanitized = _ABSOLUTE_URL_RE.sub("[redacted-url]", sanitized)
    return _SLACK_WEBHOOK_FRAGMENT_RE.sub("[redacted-url]", sanitized)


def _log_delivery_suppression(suppression: DeliverySuppression) -> None:
    payload: dict[str, Any] = {
        "suppression_reason": suppression.reason,
        "claim_skipped": True,
        "stale_lock_recovery_skipped": True,
    }
    if suppression.reason == "invalid_config":
        payload["config_error_code"] = suppression.config_error_code
        payload["config_error_summary"] = suppression.config_error_summary
    log_worker_event("slack_delivery_suppressed", **payload)


def _log_delivery_ownership_lost(*, claimed_target: ClaimedDeliveryTarget) -> None:
    log_worker_event(
        "slack_delivery_ownership_lost",
        level="warning",
        event_id=str(claimed_target.event_id),
        target_id=str(claimed_target.target_id),
        target_name=claimed_target.target_name,
        claimed_attempt_count=claimed_target.attempt_count,
        claimed_locked_by=claimed_target.locked_by,
    )
