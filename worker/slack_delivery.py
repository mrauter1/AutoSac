from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import re
import time
import threading
from typing import Any
import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.config import Settings, SlackSettings
from shared.db import session_scope
from shared.integrations import SlackRuntimeContext, build_slack_runtime_context, load_user_by_id
from shared.logging import log_worker_event
from shared.models import IntegrationEvent, IntegrationEventTarget, User
from shared.security import utc_now
from shared.slack_dm import (
    SlackDMTokenError,
    SlackDeliveryHealthSnapshot,
    SlackWebApiResponse,
    parse_slack_auth_test_result,
    persist_slack_delivery_health,
    resolve_slack_bot_token,
    slack_api_auth_test,
    slack_api_chat_post_message,
    slack_api_conversations_open,
)

_SINGLE_LINE_WHITESPACE_RE = re.compile(r"\s+", re.UNICODE)
_ABSOLUTE_URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
_SLACK_WEBHOOK_FRAGMENT_RE = re.compile(r"hooks\.slack\.com/services/\S+", re.IGNORECASE)
_RETRYABLE_HTTP_STATUS_CODES = {408, 429}
_AUTH_FAILURE_ERROR_CODES = {"account_inactive", "invalid_auth", "not_authed", "token_revoked"}
_SCOPE_FAILURE_ERROR_CODES = {"missing_scope", "no_permission", "not_allowed_token_type"}
_RECIPIENT_FAILURE_ERROR_CODES = {
    "cannot_dm_bot",
    "channel_not_found",
    "is_bot",
    "user_disabled",
    "user_not_found",
}
_RETRYABLE_SLACK_ERROR_CODES = {
    "fatal_error",
    "internal_error",
    "ratelimited",
    "request_timeout",
    "service_unavailable",
    "temporarily_unavailable",
}


@dataclass(frozen=True)
class DeliverySuppression:
    reason: str
    config_error_code: str | None = None
    config_error_summary: str | None = None
    claim_skipped: bool = True
    stale_lock_recovery_skipped: bool = True
    delivery_halted: bool = False


@dataclass(frozen=True)
class ClaimedDeliveryTarget:
    target_id: Any
    event_id: Any
    event_type: str
    target_name: str
    recipient_user_id: Any
    recipient_reason: str | None
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


def build_worker_slack_runtime_context(
    settings: Settings,
    *,
    db: Session | None = None,
    slack: SlackSettings | None = None,
) -> SlackRuntimeContext:
    return build_slack_runtime_context(
        settings,
        db=db,
        slack=slack,
        clock=utc_now,
        event_logger=_worker_event_logger,
    )


def resolve_delivery_suppression(slack: SlackSettings) -> DeliverySuppression | None:
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


def load_delivery_runtime(settings: Settings) -> SlackRuntimeContext:
    with session_scope(settings) as db:
        return build_worker_slack_runtime_context(settings, db=db)


def load_delivery_recipient(settings: Settings, *, recipient_user_id) -> User | None:
    with session_scope(settings) as db:
        return load_user_by_id(db, user_id=recipient_user_id)


def persist_delivery_health_snapshot(
    slack_runtime: SlackRuntimeContext,
    *,
    snapshot: SlackDeliveryHealthSnapshot,
) -> None:
    with session_scope(slack_runtime.settings) as db:
        persist_slack_delivery_health(db, snapshot=snapshot, updated_at=slack_runtime.now())


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
    stale_before = slack_runtime.now() - timedelta(seconds=slack_runtime.slack.delivery_stale_lock_seconds)
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
                (
                    f"event_id={target.event_id} target_name={target.target_name} "
                    f"recipient_user_id={target.recipient_user_id} recipient_reason={target.recipient_reason} "
                    f"stale_lock_recovered previous_locked_by={previous_locked_by} attempt_count={target.attempt_count}"
                ),
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
            .limit(slack_runtime.slack.delivery_batch_size)
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
                recipient_user_id=target.recipient_user_id,
                recipient_reason=target.recipient_reason,
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


def deliver_claimed_target(
    slack_runtime: SlackRuntimeContext,
    *,
    claimed_target: ClaimedDeliveryTarget,
    bot_token: str,
    load_recipient=None,
    open_conversation=None,
    post_message=None,
) -> DeliverySuppression | None:
    classification = classify_delivery_attempt(
        slack_runtime,
        claimed_target=claimed_target,
        bot_token=bot_token,
        load_recipient=load_recipient,
        open_conversation=open_conversation,
        post_message=post_message,
    )
    if isinstance(classification, DeliverySuppression):
        return classification
    finalization_result = finalize_delivery_claim(
        slack_runtime,
        claimed_target=claimed_target,
        outcome=classification,
    )
    if finalization_result == "ownership_lost":
        _log_delivery_ownership_lost(slack_runtime, claimed_target=claimed_target)
        return None
    _log_delivery_result(
        slack_runtime,
        claimed_target=claimed_target,
        outcome=classification,
    )
    return None


def classify_delivery_attempt(
    slack_runtime: SlackRuntimeContext,
    *,
    claimed_target: ClaimedDeliveryTarget,
    bot_token: str,
    load_recipient=None,
    open_conversation=None,
    post_message=None,
) -> DeliveryOutcome | DeliverySuppression:
    resolved_load_recipient = load_delivery_recipient if load_recipient is None else load_recipient
    resolved_open_conversation = slack_api_conversations_open if open_conversation is None else open_conversation
    resolved_post_message = slack_api_chat_post_message if post_message is None else post_message
    recipient = resolved_load_recipient(
        slack_runtime.settings,
        recipient_user_id=claimed_target.recipient_user_id,
    )
    if recipient is None:
        return _build_terminal_recipient_outcome(
            claimed_target=claimed_target,
            failure_class="missing_recipient_user",
            terminal_error="missing_recipient_user",
        )
    if not bool(getattr(recipient, "is_active", False)):
        return _build_terminal_recipient_outcome(
            claimed_target=claimed_target,
            failure_class="inactive_recipient_user",
            terminal_error="inactive_recipient_user",
        )
    slack_user_id = getattr(recipient, "slack_user_id", None)
    if not isinstance(slack_user_id, str) or not slack_user_id.strip():
        return _build_terminal_recipient_outcome(
            claimed_target=claimed_target,
            failure_class="missing_recipient_slack_user_id",
            terminal_error="missing_recipient_slack_user_id",
        )

    try:
        rendered_text = render_slack_message(
            event_type=claimed_target.event_type,
            payload_json=claimed_target.payload_json,
        )
    except ValueError as exc:
        return DeliveryOutcome.dead_letter_terminal(
            last_error=_sanitize_operator_summary(
                (
                    f"event_id={claimed_target.event_id} target_name={claimed_target.target_name} "
                    f"recipient_user_id={claimed_target.recipient_user_id} recipient_reason={claimed_target.recipient_reason} "
                    f"attempt_count={claimed_target.attempt_count} terminal_error={type(exc).__name__}: {exc}"
                ),
            ),
            failure_class=type(exc).__name__,
            terminal_reason="terminal_failure",
        )

    try:
        open_response = resolved_open_conversation(
            bot_token=bot_token,
            slack_user_id=slack_user_id.strip(),
            timeout_seconds=slack_runtime.slack.http_timeout_seconds,
        )
    except httpx.TransportError as exc:
        return _build_retryable_outcome(
            slack_runtime,
            claimed_target=claimed_target,
            last_error=_sanitize_operator_summary(
                (
                    f"event_id={claimed_target.event_id} target_name={claimed_target.target_name} "
                    f"recipient_user_id={claimed_target.recipient_user_id} recipient_reason={claimed_target.recipient_reason} "
                    f"attempt_count={claimed_target.attempt_count} retryable_error={type(exc).__name__}: {exc}"
                ),
            ),
            failure_class=type(exc).__name__,
        )

    invalid_config_suppression = _build_invalid_config_suppression_from_response(
        open_response,
        claim_skipped=False,
        stale_lock_recovery_skipped=False,
        delivery_halted=True,
    )
    if invalid_config_suppression is not None:
        return invalid_config_suppression
    if not open_response.ok:
        return _classify_slack_web_api_failure(
            slack_runtime,
            claimed_target=claimed_target,
            response=open_response,
        )

    try:
        channel_id = _extract_channel_id(open_response)
    except ValueError as exc:
        return DeliveryOutcome.dead_letter_terminal(
            last_error=_sanitize_operator_summary(
                (
                    f"event_id={claimed_target.event_id} target_name={claimed_target.target_name} "
                    f"recipient_user_id={claimed_target.recipient_user_id} recipient_reason={claimed_target.recipient_reason} "
                    f"attempt_count={claimed_target.attempt_count} terminal_error={type(exc).__name__}: {exc}"
                ),
            ),
            failure_class="invalid_conversations_open_response",
            terminal_reason="terminal_failure",
            http_status=open_response.http_status,
        )

    try:
        post_response = resolved_post_message(
            bot_token=bot_token,
            channel_id=channel_id,
            text=rendered_text,
            timeout_seconds=slack_runtime.slack.http_timeout_seconds,
        )
    except httpx.TransportError as exc:
        return _build_retryable_outcome(
            slack_runtime,
            claimed_target=claimed_target,
            last_error=_sanitize_operator_summary(
                (
                    f"event_id={claimed_target.event_id} target_name={claimed_target.target_name} "
                    f"recipient_user_id={claimed_target.recipient_user_id} recipient_reason={claimed_target.recipient_reason} "
                    f"attempt_count={claimed_target.attempt_count} retryable_error={type(exc).__name__}: {exc}"
                ),
            ),
            failure_class=type(exc).__name__,
        )

    invalid_config_suppression = _build_invalid_config_suppression_from_response(
        post_response,
        claim_skipped=False,
        stale_lock_recovery_skipped=False,
        delivery_halted=True,
    )
    if invalid_config_suppression is not None:
        return invalid_config_suppression
    if not post_response.ok:
        return _classify_slack_web_api_failure(
            slack_runtime,
            claimed_target=claimed_target,
            response=post_response,
        )
    return DeliveryOutcome.sent(http_status=post_response.http_status)


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


def run_delivery_cycle_preflight(
    slack_runtime: SlackRuntimeContext,
    *,
    auth_test=None,
) -> tuple[str | None, DeliverySuppression | None]:
    resolved_auth_test = slack_api_auth_test if auth_test is None else auth_test
    suppression = resolve_delivery_suppression(slack_runtime.slack)
    if suppression is not None:
        return None, suppression
    try:
        bot_token = resolve_slack_bot_token(
            slack_runtime.slack,
            app_secret_key=slack_runtime.settings.app_secret_key,
        )
    except SlackDMTokenError as exc:
        return None, DeliverySuppression(
            reason="invalid_config",
            config_error_code="slack_bot_token_unusable",
            config_error_summary=_sanitize_operator_summary(str(exc)),
        )

    try:
        auth_response = resolved_auth_test(
            bot_token=bot_token,
            timeout_seconds=slack_runtime.slack.http_timeout_seconds,
        )
    except httpx.TransportError:
        return bot_token, None

    invalid_config_suppression = _build_invalid_config_suppression_from_response(
        auth_response,
        claim_skipped=True,
        stale_lock_recovery_skipped=True,
    )
    if invalid_config_suppression is not None:
        persist_delivery_health_snapshot(
            slack_runtime,
            snapshot=_build_invalid_config_health_snapshot(slack_runtime, invalid_config_suppression),
        )
        return None, invalid_config_suppression

    if auth_response.ok:
        persist_delivery_health_snapshot(
            slack_runtime,
            snapshot=_build_healthy_health_snapshot(slack_runtime, auth_response),
        )
    return bot_token, None


def _run_delivery_cycle_with_runtime(slack_runtime: SlackRuntimeContext, *, worker_instance_id: str) -> None:
    bot_token, suppression = run_delivery_cycle_preflight(slack_runtime)
    if suppression is not None:
        if suppression.reason == "invalid_config":
            _log_delivery_suppression(slack_runtime, suppression)
        return
    if bot_token is None:
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
            recipient_user_id=str(claimed_target.recipient_user_id),
            recipient_reason=claimed_target.recipient_reason,
            delivery_status="processing",
            attempt_count=claimed_target.attempt_count,
            locked_by=claimed_target.locked_by,
            claim_token=str(claimed_target.claim_token),
        )
        delivery_suppression = deliver_claimed_target(
            slack_runtime,
            claimed_target=claimed_target,
            bot_token=bot_token,
        )
        if delivery_suppression is None:
            continue
        persist_delivery_health_snapshot(
            slack_runtime,
            snapshot=_build_invalid_config_health_snapshot(slack_runtime, delivery_suppression),
        )
        _log_delivery_suppression(slack_runtime, delivery_suppression)
        return


def run_delivery_cycle(slack_runtime: SlackRuntimeContext | Settings, *, worker_instance_id: str) -> None:
    if isinstance(slack_runtime, SlackRuntimeContext):
        _run_delivery_cycle_with_runtime(slack_runtime, worker_instance_id=worker_instance_id)
        return
    cycle_runtime = load_delivery_runtime(slack_runtime)
    _run_delivery_cycle_with_runtime(cycle_runtime, worker_instance_id=worker_instance_id)


def delivery_loop(
    slack_runtime: SlackRuntimeContext | Settings,
    *,
    worker_instance_id: str,
    stop_event: threading.Event | None = None,
    interval_seconds: float | None = None,
) -> None:
    resolved_settings = slack_runtime.settings if isinstance(slack_runtime, SlackRuntimeContext) else slack_runtime
    resolved_interval_seconds = resolved_settings.worker_poll_seconds if interval_seconds is None else interval_seconds
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


def _build_invalid_config_suppression_from_response(
    response: SlackWebApiResponse,
    *,
    claim_skipped: bool,
    stale_lock_recovery_skipped: bool,
    delivery_halted: bool = False,
) -> DeliverySuppression | None:
    error_code = response.error_code
    if error_code not in _AUTH_FAILURE_ERROR_CODES and error_code not in _SCOPE_FAILURE_ERROR_CODES:
        if response.http_status not in {401, 403}:
            return None
    return DeliverySuppression(
        reason="invalid_config",
        config_error_code=error_code or _build_http_error_code(response),
        config_error_summary=_build_invalid_config_summary(response),
        claim_skipped=claim_skipped,
        stale_lock_recovery_skipped=stale_lock_recovery_skipped,
        delivery_halted=delivery_halted,
    )


def _build_invalid_config_health_snapshot(
    slack_runtime: SlackRuntimeContext,
    suppression: DeliverySuppression,
) -> SlackDeliveryHealthSnapshot:
    return SlackDeliveryHealthSnapshot(
        status="invalid_config",
        checked_at=slack_runtime.now().isoformat(),
        error_code=suppression.config_error_code,
        summary=suppression.config_error_summary,
        team_id=slack_runtime.slack.team_id,
        team_name=slack_runtime.slack.team_name,
        bot_user_id=slack_runtime.slack.bot_user_id,
    )


def _build_healthy_health_snapshot(
    slack_runtime: SlackRuntimeContext,
    response: SlackWebApiResponse,
) -> SlackDeliveryHealthSnapshot:
    auth_result = parse_slack_auth_test_result(response)
    return SlackDeliveryHealthSnapshot(
        status="healthy",
        checked_at=slack_runtime.now().isoformat(),
        team_id=auth_result.team_id or slack_runtime.slack.team_id,
        team_name=auth_result.team_name or slack_runtime.slack.team_name,
        bot_user_id=auth_result.bot_user_id or slack_runtime.slack.bot_user_id,
    )


def _build_invalid_config_summary(response: SlackWebApiResponse) -> str:
    if response.error_code:
        return f"Slack {response.method} returned {response.error_code}"
    return f"Slack {response.method} returned HTTP {response.http_status}"


def _build_http_error_code(response: SlackWebApiResponse) -> str:
    method_name = response.method.replace(".", "_")
    return f"{method_name}_http_{response.http_status}"


def _extract_channel_id(response: SlackWebApiResponse) -> str:
    body = response.body_json
    if not isinstance(body, dict):
        raise ValueError("conversations.open response missing body_json")
    channel = body.get("channel")
    if not isinstance(channel, dict):
        raise ValueError("conversations.open response missing channel")
    channel_id = channel.get("id")
    if not isinstance(channel_id, str) or not channel_id.strip():
        raise ValueError("conversations.open response missing channel.id")
    return channel_id.strip()


def _build_terminal_recipient_outcome(
    *,
    claimed_target: ClaimedDeliveryTarget,
    failure_class: str,
    terminal_error: str,
    http_status: int | None = None,
) -> DeliveryOutcome:
    return DeliveryOutcome.dead_letter_terminal(
        last_error=_sanitize_operator_summary(
            (
                f"event_id={claimed_target.event_id} target_name={claimed_target.target_name} "
                f"recipient_user_id={claimed_target.recipient_user_id} recipient_reason={claimed_target.recipient_reason} "
                f"attempt_count={claimed_target.attempt_count} terminal_error={terminal_error}"
            ),
        ),
        failure_class=failure_class,
        terminal_reason="terminal_failure",
        http_status=http_status,
    )


def _classify_slack_web_api_failure(
    slack_runtime: SlackRuntimeContext,
    *,
    claimed_target: ClaimedDeliveryTarget,
    response: SlackWebApiResponse,
) -> DeliveryOutcome:
    error_code = response.error_code
    if response.http_status in _RETRYABLE_HTTP_STATUS_CODES or 500 <= response.http_status < 600:
        return _build_retryable_outcome(
            slack_runtime,
            claimed_target=claimed_target,
            last_error=_sanitize_operator_summary(
                (
                    f"event_id={claimed_target.event_id} target_name={claimed_target.target_name} "
                    f"recipient_user_id={claimed_target.recipient_user_id} recipient_reason={claimed_target.recipient_reason} "
                    f"attempt_count={claimed_target.attempt_count} retryable_http_status={response.http_status} "
                    f"method={response.method}"
                ),
            ),
            http_status=response.http_status,
            failure_class=_retryable_http_failure_class(response.http_status),
            retry_after_seconds=response.retry_after_seconds,
        )
    if error_code in _RETRYABLE_SLACK_ERROR_CODES:
        return _build_retryable_outcome(
            slack_runtime,
            claimed_target=claimed_target,
            last_error=_sanitize_operator_summary(
                (
                    f"event_id={claimed_target.event_id} target_name={claimed_target.target_name} "
                    f"recipient_user_id={claimed_target.recipient_user_id} recipient_reason={claimed_target.recipient_reason} "
                    f"attempt_count={claimed_target.attempt_count} retryable_slack_error={error_code} method={response.method}"
                ),
            ),
            http_status=response.http_status,
            failure_class=error_code,
            retry_after_seconds=response.retry_after_seconds,
        )
    if error_code in _RECIPIENT_FAILURE_ERROR_CODES:
        return _build_terminal_recipient_outcome(
            claimed_target=claimed_target,
            failure_class=error_code,
            terminal_error=f"recipient_error={error_code} method={response.method}",
            http_status=response.http_status,
        )
    if error_code:
        return DeliveryOutcome.dead_letter_terminal(
            last_error=_sanitize_operator_summary(
                (
                    f"event_id={claimed_target.event_id} target_name={claimed_target.target_name} "
                    f"recipient_user_id={claimed_target.recipient_user_id} recipient_reason={claimed_target.recipient_reason} "
                    f"attempt_count={claimed_target.attempt_count} terminal_slack_error={error_code} method={response.method}"
                ),
            ),
            http_status=response.http_status,
            failure_class=error_code,
            terminal_reason="terminal_failure",
        )
    if 200 <= response.http_status < 300:
        return DeliveryOutcome.dead_letter_terminal(
            last_error=_sanitize_operator_summary(
                (
                    f"event_id={claimed_target.event_id} target_name={claimed_target.target_name} "
                    f"recipient_user_id={claimed_target.recipient_user_id} recipient_reason={claimed_target.recipient_reason} "
                    f"attempt_count={claimed_target.attempt_count} terminal_slack_response_not_ok method={response.method}"
                ),
            ),
            http_status=response.http_status,
            failure_class="slack_api_error",
            terminal_reason="terminal_failure",
        )
    return DeliveryOutcome.dead_letter_terminal(
        last_error=_sanitize_operator_summary(
            (
                f"event_id={claimed_target.event_id} target_name={claimed_target.target_name} "
                f"recipient_user_id={claimed_target.recipient_user_id} recipient_reason={claimed_target.recipient_reason} "
                f"attempt_count={claimed_target.attempt_count} terminal_http_status={response.http_status} method={response.method}"
            ),
        ),
        http_status=response.http_status,
        failure_class=_terminal_http_failure_class(response.http_status),
        terminal_reason="terminal_failure",
    )


def _log_delivery_suppression(slack_runtime: SlackRuntimeContext, suppression: DeliverySuppression) -> None:
    payload: dict[str, Any] = {
        "suppression_reason": suppression.reason,
        "claim_skipped": suppression.claim_skipped,
        "stale_lock_recovery_skipped": suppression.stale_lock_recovery_skipped,
    }
    if suppression.delivery_halted:
        payload["delivery_halted"] = True
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
        recipient_user_id=str(claimed_target.recipient_user_id),
        recipient_reason=claimed_target.recipient_reason,
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
    retry_after_seconds: int | None = None,
) -> DeliveryOutcome:
    if claimed_target.attempt_count >= slack_runtime.slack.delivery_max_attempts:
        return DeliveryOutcome.dead_letter_terminal(
            last_error=last_error,
            http_status=http_status,
            failure_class=failure_class,
            terminal_reason="retry_exhausted",
        )
    next_attempt_at = slack_runtime.now() + timedelta(
        seconds=build_retry_delay_seconds(attempt_count=claimed_target.attempt_count),
    )
    if retry_after_seconds is not None:
        retry_after_at = slack_runtime.now() + timedelta(seconds=retry_after_seconds)
        if retry_after_at > next_attempt_at:
            next_attempt_at = retry_after_at
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
        "recipient_user_id": str(claimed_target.recipient_user_id),
        "recipient_reason": claimed_target.recipient_reason,
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
    slack_runtime: SlackRuntimeContext | Settings,
    event: str,
    *,
    level: str = "info",
    **payload: Any,
) -> None:
    if isinstance(slack_runtime, Settings):
        if level == "info":
            log_worker_event(event, **payload)
            return
        log_worker_event(event, level=level, **payload)
        return
    if level == "info":
        slack_runtime.event_logger("worker", event, **payload)
        return
    slack_runtime.event_logger("worker", event, level=level, **payload)
