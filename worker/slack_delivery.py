from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import json
import re
import time
import threading
from typing import Any
import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.config import Settings
from shared.db import session_scope
from shared.integrations import SlackRuntimeContext, build_slack_runtime_context
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
    claim_token: uuid.UUID
    payload_json: Any


@dataclass(frozen=True)
class DeliveryOutcome:
    kind: str
    last_error: str | None = None
    http_status: int | None = None
    failure_class: str | None = None
    next_attempt_at: Any | None = None
    terminal_reason: str | None = None

    def __post_init__(self) -> None:
        if self.kind == "sent":
            if self.http_status is None:
                raise ValueError("sent outcomes require http_status")
            if any(value is not None for value in (self.last_error, self.failure_class, self.next_attempt_at, self.terminal_reason)):
                raise ValueError("sent outcomes cannot include failure metadata")
            return
        if self.kind == "retryable_failure":
            if self.last_error is None or self.failure_class is None or self.next_attempt_at is None:
                raise ValueError("retryable_failure outcomes require last_error, failure_class, and next_attempt_at")
            if self.terminal_reason is not None:
                raise ValueError("retryable_failure outcomes cannot include terminal_reason")
            return
        if self.kind == "dead_letter_terminal":
            if self.last_error is None or self.failure_class is None:
                raise ValueError("dead_letter_terminal outcomes require last_error and failure_class")
            if self.next_attempt_at is not None:
                raise ValueError("dead_letter_terminal outcomes cannot include next_attempt_at")
            if self.terminal_reason not in {"terminal_failure", "retry_exhausted"}:
                raise ValueError("dead_letter_terminal outcomes require a valid terminal_reason")
            return
        raise ValueError(f"unsupported delivery outcome kind {self.kind}")

    @classmethod
    def sent(cls, *, http_status: int) -> DeliveryOutcome:
        return cls(kind="sent", http_status=http_status)

    @classmethod
    def retryable_failure(
        cls,
        *,
        last_error: str,
        failure_class: str,
        next_attempt_at,
        http_status: int | None = None,
    ) -> DeliveryOutcome:
        return cls(
            kind="retryable_failure",
            last_error=last_error,
            http_status=http_status,
            failure_class=failure_class,
            next_attempt_at=next_attempt_at,
        )

    @classmethod
    def dead_letter_terminal(
        cls,
        *,
        last_error: str,
        failure_class: str,
        terminal_reason: str,
        http_status: int | None = None,
    ) -> DeliveryOutcome:
        return cls(
            kind="dead_letter_terminal",
            last_error=last_error,
            http_status=http_status,
            failure_class=failure_class,
            terminal_reason=terminal_reason,
        )


def _worker_event_logger(_service: str, event: str, *, level: str = "info", **payload: Any) -> None:
    if level == "info":
        log_worker_event(event, **payload)
        return
    log_worker_event(event, level=level, **payload)


def build_worker_slack_runtime_context(settings: Settings) -> SlackRuntimeContext:
    return build_slack_runtime_context(
        settings,
        clock=utc_now,
        event_logger=_worker_event_logger,
    )


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


def load_claimed_processing_target(
    db: Session,
    *,
    target_id,
    claim_token,
) -> IntegrationEventTarget | None:
    statement = (
        select(IntegrationEventTarget)
        .where(
            IntegrationEventTarget.id == target_id,
            IntegrationEventTarget.delivery_status == "processing",
            IntegrationEventTarget.claim_token == claim_token,
        )
        .limit(1)
        .with_for_update()
    )
    return db.execute(statement).scalar_one_or_none()


def recover_stale_delivery_targets(
    db: Session,
    *,
    slack_runtime: SlackRuntimeContext,
) -> list[IntegrationEventTarget]:
    stale_before = slack_runtime.now() - timedelta(seconds=slack_runtime.settings.slack.delivery_stale_lock_seconds)
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
    recovered_at = slack_runtime.now()
    for target in targets:
        previous_locked_by = target.locked_by
        _apply_failed_state(
            target,
            last_error=_sanitize_operator_summary(
                f"event_id={target.event_id} target_name={target.target_name} stale_lock_recovered previous_locked_by={previous_locked_by} attempt_count={target.attempt_count}",
            ),
            next_attempt_at=recovered_at,
        )
    return targets


def claim_delivery_targets(
    db: Session,
    *,
    slack_runtime: SlackRuntimeContext,
    locked_by: str,
) -> list[ClaimedDeliveryTarget]:
    claimed_at = slack_runtime.now()
    claimed_targets = list(
        db.execute(
            select(IntegrationEventTarget)
            .where(
                IntegrationEventTarget.delivery_status.in_(("pending", "failed")),
                IntegrationEventTarget.next_attempt_at <= claimed_at,
            )
            .order_by(IntegrationEventTarget.next_attempt_at.asc(), IntegrationEventTarget.created_at.asc())
            .limit(slack_runtime.settings.slack.delivery_batch_size)
            .with_for_update(skip_locked=True)
        ).scalars()
    )
    claimed: list[ClaimedDeliveryTarget] = []
    for target in claimed_targets:
        event = db.get(IntegrationEvent, target.event_id)
        if event is None:
            continue
        target.delivery_status = "processing"
        target.attempt_count += 1
        target.locked_at = claimed_at
        target.locked_by = locked_by
        claim_token = uuid.uuid4()
        target.claim_token = claim_token
        claimed.append(
            ClaimedDeliveryTarget(
                target_id=target.id,
                event_id=target.event_id,
                event_type=event.event_type,
                target_name=target.target_name,
                attempt_count=target.attempt_count,
                locked_by=locked_by,
                claim_token=claim_token,
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
    slack_runtime: SlackRuntimeContext,
    *,
    claimed_target: ClaimedDeliveryTarget,
    send_webhook=send_slack_webhook,
) -> None:
    outcome = classify_delivery_attempt(
        slack_runtime,
        claimed_target=claimed_target,
        send_webhook=send_webhook,
    )
    finalization_result = finalize_delivery_claim(
        slack_runtime,
        claimed_target=claimed_target,
        outcome=outcome,
    )
    if finalization_result == "ownership_lost":
        _log_delivery_ownership_lost(slack_runtime, claimed_target=claimed_target)
        return
    _log_delivery_result(
        slack_runtime,
        claimed_target=claimed_target,
        outcome=outcome,
    )


def classify_delivery_attempt(
    slack_runtime: SlackRuntimeContext,
    *,
    claimed_target: ClaimedDeliveryTarget,
    send_webhook=send_slack_webhook,
) -> DeliveryOutcome:
    settings = slack_runtime.settings
    target = settings.slack.get_target(claimed_target.target_name)
    if target is None:
        return DeliveryOutcome.dead_letter_terminal(
            last_error=_sanitize_operator_summary(
                f"event_id={claimed_target.event_id} target_name={claimed_target.target_name} attempt_count={claimed_target.attempt_count} terminal_error=missing_target_config",
            ),
            failure_class="missing_target_config",
            terminal_reason="terminal_failure",
        )
    if not target.enabled:
        return DeliveryOutcome.dead_letter_terminal(
            last_error=_sanitize_operator_summary(
                f"event_id={claimed_target.event_id} target_name={claimed_target.target_name} attempt_count={claimed_target.attempt_count} terminal_error=target_disabled",
            ),
            failure_class="target_disabled",
            terminal_reason="terminal_failure",
        )

    try:
        rendered_text = render_slack_message(
            event_type=claimed_target.event_type,
            payload_json=claimed_target.payload_json,
        )
    except ValueError as exc:
        return DeliveryOutcome.dead_letter_terminal(
            last_error=_sanitize_operator_summary(
                f"event_id={claimed_target.event_id} target_name={claimed_target.target_name} attempt_count={claimed_target.attempt_count} terminal_error={type(exc).__name__}: {exc}",
            ),
            failure_class=type(exc).__name__,
            terminal_reason="terminal_failure",
        )

    try:
        status_code = send_webhook(
            webhook_url=target.webhook_url,
            text=rendered_text,
            timeout_seconds=settings.slack.http_timeout_seconds,
        )
    except httpx.TransportError as exc:
        return _build_retryable_outcome(
            slack_runtime,
            claimed_target=claimed_target,
            last_error=_sanitize_operator_summary(
                f"event_id={claimed_target.event_id} target_name={claimed_target.target_name} attempt_count={claimed_target.attempt_count} retryable_error={type(exc).__name__}: {exc}",
            ),
            failure_class=type(exc).__name__,
        )

    if 200 <= status_code < 300:
        return DeliveryOutcome.sent(http_status=status_code)
    if status_code in _RETRYABLE_HTTP_STATUS_CODES or 500 <= status_code < 600:
        return _build_retryable_outcome(
            slack_runtime,
            claimed_target=claimed_target,
            last_error=_sanitize_operator_summary(
                f"event_id={claimed_target.event_id} target_name={claimed_target.target_name} attempt_count={claimed_target.attempt_count} retryable_http_status={status_code}",
            ),
            http_status=status_code,
            failure_class=_retryable_http_failure_class(status_code),
        )
    return DeliveryOutcome.dead_letter_terminal(
        last_error=_sanitize_operator_summary(
            f"event_id={claimed_target.event_id} target_name={claimed_target.target_name} attempt_count={claimed_target.attempt_count} terminal_http_status={status_code}",
        ),
        http_status=status_code,
        failure_class=_terminal_http_failure_class(status_code),
        terminal_reason="terminal_failure",
    )


def finalize_delivery_claim(
    slack_runtime: SlackRuntimeContext,
    *,
    claimed_target: ClaimedDeliveryTarget,
    outcome: DeliveryOutcome,
) -> str:
    with session_scope(slack_runtime.settings) as db:
        target = load_claimed_processing_target(
            db,
            target_id=claimed_target.target_id,
            claim_token=claimed_target.claim_token,
        )
        if target is None:
            return "ownership_lost"
        return _apply_delivery_outcome(
            target,
            outcome=outcome,
            finalized_at=slack_runtime.now(),
        )


def run_delivery_cycle(slack_runtime: SlackRuntimeContext, *, worker_instance_id: str) -> None:
    suppression = resolve_delivery_suppression(slack_runtime.settings)
    if suppression is not None:
        if suppression.reason == "invalid_config":
            _log_delivery_suppression(slack_runtime, suppression)
        return

    with session_scope(slack_runtime.settings) as db:
        recover_stale_delivery_targets(db, slack_runtime=slack_runtime)

    with session_scope(slack_runtime.settings) as db:
        claimed_targets = claim_delivery_targets(
            db,
            slack_runtime=slack_runtime,
            locked_by=worker_instance_id,
        )

    for claimed_target in claimed_targets:
        _log_worker_runtime_event(
            slack_runtime,
            "slack_target_claimed",
            event_id=str(claimed_target.event_id),
            target_name=claimed_target.target_name,
            delivery_status="processing",
            attempt_count=claimed_target.attempt_count,
            locked_by=claimed_target.locked_by,
            claim_token=str(claimed_target.claim_token),
        )
        deliver_claimed_target(
            slack_runtime,
            claimed_target=claimed_target,
        )


def delivery_loop(
    slack_runtime: SlackRuntimeContext,
    *,
    worker_instance_id: str,
    stop_event: threading.Event | None = None,
    interval_seconds: float | None = None,
) -> None:
    resolved_interval_seconds = slack_runtime.settings.worker_poll_seconds if interval_seconds is None else interval_seconds
    while True:
        try:
            run_delivery_cycle(
                slack_runtime,
                worker_instance_id=worker_instance_id,
            )
        except Exception as exc:
            _log_worker_runtime_event(
                slack_runtime,
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


def _log_delivery_suppression(slack_runtime: SlackRuntimeContext, suppression: DeliverySuppression) -> None:
    payload: dict[str, Any] = {
        "suppression_reason": suppression.reason,
        "claim_skipped": True,
        "stale_lock_recovery_skipped": True,
    }
    if suppression.reason == "invalid_config":
        payload["config_error_code"] = suppression.config_error_code
        payload["config_error_summary"] = suppression.config_error_summary
    _log_worker_runtime_event(slack_runtime, "slack_delivery_suppressed", **payload)


def _log_delivery_ownership_lost(
    slack_runtime: SlackRuntimeContext,
    *,
    claimed_target: ClaimedDeliveryTarget,
) -> None:
    _log_worker_runtime_event(
        slack_runtime,
        "slack_delivery_ownership_lost",
        level="warning",
        event_id=str(claimed_target.event_id),
        target_id=str(claimed_target.target_id),
        target_name=claimed_target.target_name,
        claimed_attempt_count=claimed_target.attempt_count,
        claimed_locked_by=claimed_target.locked_by,
        claim_token=str(claimed_target.claim_token),
    )


def _build_retryable_outcome(
    slack_runtime: SlackRuntimeContext,
    *,
    claimed_target: ClaimedDeliveryTarget,
    last_error: str,
    failure_class: str,
    http_status: int | None = None,
) -> DeliveryOutcome:
    if claimed_target.attempt_count >= slack_runtime.settings.slack.delivery_max_attempts:
        return DeliveryOutcome.dead_letter_terminal(
            last_error=last_error,
            http_status=http_status,
            failure_class=failure_class,
            terminal_reason="retry_exhausted",
        )
    next_attempt_at = slack_runtime.now() + timedelta(
        seconds=build_retry_delay_seconds(attempt_count=claimed_target.attempt_count),
    )
    return DeliveryOutcome.retryable_failure(
        last_error=last_error,
        http_status=http_status,
        failure_class=failure_class,
        next_attempt_at=next_attempt_at,
    )


def _retryable_http_failure_class(status_code: int) -> str:
    if status_code == 408:
        return "http_408"
    if status_code == 429:
        return "http_429"
    if 500 <= status_code < 600:
        return "http_5xx"
    return "http_retryable_status"


def _terminal_http_failure_class(status_code: int) -> str:
    if 300 <= status_code < 400:
        return "http_3xx"
    if 400 <= status_code < 500:
        return "http_4xx"
    if 100 <= status_code < 200:
        return "http_1xx"
    return "http_non_success_status"


def _apply_delivery_outcome(
    target: IntegrationEventTarget,
    *,
    outcome: DeliveryOutcome,
    finalized_at,
) -> str:
    if outcome.kind == "sent":
        _apply_sent_state(target, sent_at=finalized_at)
        return "sent"
    if outcome.kind == "retryable_failure":
        _apply_failed_state(
            target,
            last_error=outcome.last_error,
            next_attempt_at=outcome.next_attempt_at,
        )
        return "failed"
    _apply_dead_letter_state(
        target,
        dead_lettered_at=finalized_at,
        last_error=outcome.last_error,
    )
    return "dead_letter"


def _clear_claim_state(target: IntegrationEventTarget) -> None:
    target.claim_token = None
    target.locked_at = None
    target.locked_by = None


def _apply_sent_state(target: IntegrationEventTarget, *, sent_at) -> None:
    target.delivery_status = "sent"
    target.sent_at = sent_at
    target.dead_lettered_at = None
    target.last_error = None
    _clear_claim_state(target)


def _apply_failed_state(
    target: IntegrationEventTarget,
    *,
    last_error: str,
    next_attempt_at,
) -> None:
    target.delivery_status = "failed"
    target.last_error = last_error
    target.sent_at = None
    target.dead_lettered_at = None
    target.next_attempt_at = next_attempt_at
    _clear_claim_state(target)


def _apply_dead_letter_state(
    target: IntegrationEventTarget,
    *,
    dead_lettered_at,
    last_error: str,
) -> None:
    target.delivery_status = "dead_letter"
    target.dead_lettered_at = dead_lettered_at
    target.sent_at = None
    target.last_error = last_error
    _clear_claim_state(target)


def _log_delivery_result(
    slack_runtime: SlackRuntimeContext,
    *,
    claimed_target: ClaimedDeliveryTarget,
    outcome: DeliveryOutcome,
) -> None:
    payload: dict[str, Any] = {
        "event_id": str(claimed_target.event_id),
        "target_name": claimed_target.target_name,
        "attempt_count": claimed_target.attempt_count,
        "locked_by": claimed_target.locked_by,
        "claim_token": str(claimed_target.claim_token),
    }
    if outcome.kind == "sent":
        payload["delivery_status"] = "sent"
        payload["http_status"] = outcome.http_status
        _log_worker_runtime_event(slack_runtime, "slack_delivery_sent", **payload)
        return
    if outcome.kind == "retryable_failure":
        payload["delivery_status"] = "failed"
        payload["next_attempt_at"] = outcome.next_attempt_at.isoformat()
        if outcome.http_status is not None:
            payload["http_status"] = outcome.http_status
        payload["failure_class"] = outcome.failure_class
        _log_worker_runtime_event(slack_runtime, "slack_delivery_retry_scheduled", **payload)
        return
    payload["delivery_status"] = "dead_letter"
    if outcome.http_status is not None:
        payload["http_status"] = outcome.http_status
    payload["failure_class"] = outcome.failure_class
    if outcome.terminal_reason == "retry_exhausted":
        payload["retry_exhausted"] = True
    _log_worker_runtime_event(slack_runtime, "slack_delivery_dead_lettered", **payload)


def _log_worker_runtime_event(
    slack_runtime: SlackRuntimeContext,
    event: str,
    *,
    level: str = "info",
    **payload: Any,
) -> None:
    if level == "info":
        slack_runtime.event_logger("worker", event, **payload)
        return
    slack_runtime.event_logger("worker", event, level=level, **payload)
