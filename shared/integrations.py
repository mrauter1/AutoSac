from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from contextlib import nullcontext
import re
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shared.config import Settings, SlackSettings
from shared.logging import log_event
from shared.models import (
    IntegrationEvent,
    IntegrationEventLink,
    IntegrationEventTarget,
    Ticket,
    TicketMessage,
    TicketStatusHistory,
)
from shared.slack_dm import load_slack_dm_settings
from shared.security import utc_now

INTEGRATION_ROUTING_RESULTS = (
    "created",
    "suppressed_slack_disabled",
    "suppressed_invalid_config",
    "suppressed_notify_disabled",
    "suppressed_target_disabled",
)

_EVENT_TYPE_TO_NOTIFY_ENABLED = {
    "ticket.created": lambda slack: slack.notify_ticket_created,
    "ticket.public_message_added": lambda slack: slack.notify_public_message_added,
    "ticket.status_changed": lambda slack: slack.notify_status_changed,
}
_WHITESPACE_RE = re.compile(r"\s+", re.UNICODE)


@dataclass(frozen=True)
class RoutingDecision:
    routing_result: str
    target_name: str | None = None
    config_error_code: str | None = None
    config_error_summary: str | None = None


@dataclass(frozen=True)
class SlackRuntimeContext:
    settings: Settings
    slack: SlackSettings
    clock: Callable[[], object] = utc_now
    event_logger: Callable[..., None] = log_event

    def now(self):
        return self.clock()


@dataclass(frozen=True)
class EmissionResult:
    event: IntegrationEvent
    routing_result: str
    target_name: str | None = None
    config_error_code: str | None = None
    config_error_summary: str | None = None
    event_reused: bool = False


def build_slack_runtime_context(
    settings: Settings,
    *,
    db: Session | None = None,
    slack: SlackSettings | None = None,
    clock: Callable[[], object] = utc_now,
    event_logger: Callable[..., None] = log_event,
) -> SlackRuntimeContext:
    resolved_slack = slack or (
        load_slack_dm_settings(db, app_settings=settings) if db is not None and hasattr(db, "get") else settings.slack
    )
    return SlackRuntimeContext(
        settings=settings,
        slack=resolved_slack,
        clock=clock,
        event_logger=event_logger,
    )


def normalize_app_base_url(app_base_url: str) -> str:
    return app_base_url[:-1] if app_base_url.endswith("/") else app_base_url


def build_ticket_url(*, settings: Settings, ticket_reference: str) -> str:
    return f"{normalize_app_base_url(settings.app_base_url)}/ops/tickets/{ticket_reference}"


def normalize_message_preview_source(body_text: str) -> str:
    return _WHITESPACE_RE.sub(" ", body_text).strip()


def build_message_preview(body_text: str, *, max_chars: int) -> str:
    normalized = normalize_message_preview_source(body_text)
    if len(normalized) <= max_chars:
        return normalized
    return f"{normalized[: max_chars - 3]}..."


def build_ticket_created_payload(
    settings: Settings,
    *,
    ticket: Ticket,
    occurred_at,
) -> dict[str, str]:
    return _build_common_payload(settings, ticket=ticket, occurred_at=occurred_at)


def build_ticket_public_message_payload(
    settings: Settings,
    *,
    ticket: Ticket,
    message: TicketMessage,
    slack: SlackSettings | None = None,
) -> dict[str, str]:
    resolved_slack = slack or settings.slack
    payload = _build_common_payload(settings, ticket=ticket, occurred_at=message.created_at)
    payload.update(
        message_id=str(message.id),
        message_author_type=message.author_type,
        message_source=message.source,
        message_preview=build_message_preview(
            message.body_text,
            max_chars=resolved_slack.message_preview_max_chars,
        ),
    )
    return payload


def build_ticket_status_changed_payload(
    settings: Settings,
    *,
    ticket: Ticket,
    history: TicketStatusHistory,
) -> dict[str, str | None]:
    payload = _build_common_payload(settings, ticket=ticket, occurred_at=history.created_at)
    payload.update(
        status_from=history.from_status,
        status_to=history.to_status,
    )
    return payload


def record_ticket_created_event(
    db: Session,
    *,
    slack_runtime: SlackRuntimeContext,
    ticket: Ticket,
    initial_message: TicketMessage,
) -> EmissionResult:
    return _record_integration_event(
        db,
        slack_runtime=slack_runtime,
        event_type="ticket.created",
        ticket=ticket,
        dedupe_key=f"ticket.created:{ticket.id}",
        payload_json=build_ticket_created_payload(
            slack_runtime.settings,
            ticket=ticket,
            occurred_at=ticket.created_at,
        ),
        links=(
            ("ticket", ticket.id, "primary"),
            ("ticket_message", initial_message.id, "message"),
        ),
    )


def record_ticket_public_message_added_event(
    db: Session,
    *,
    slack_runtime: SlackRuntimeContext,
    ticket: Ticket,
    message: TicketMessage,
) -> EmissionResult | None:
    if message.visibility != "public" or message.source == "ticket_create":
        return None
    return _record_integration_event(
        db,
        slack_runtime=slack_runtime,
        event_type="ticket.public_message_added",
        ticket=ticket,
        dedupe_key=f"ticket.public_message_added:{message.id}",
        payload_json=build_ticket_public_message_payload(
            slack_runtime.settings,
            ticket=ticket,
            message=message,
            slack=slack_runtime.slack,
        ),
        links=(
            ("ticket", ticket.id, "primary"),
            ("ticket_message", message.id, "message"),
        ),
    )


def record_ticket_status_changed_event(
    db: Session,
    *,
    slack_runtime: SlackRuntimeContext,
    ticket: Ticket,
    history: TicketStatusHistory,
) -> EmissionResult | None:
    if history.from_status is None or history.from_status == history.to_status:
        return None
    return _record_integration_event(
        db,
        slack_runtime=slack_runtime,
        event_type="ticket.status_changed",
        ticket=ticket,
        dedupe_key=f"ticket.status_changed:{history.id}",
        payload_json=build_ticket_status_changed_payload(
            slack_runtime.settings,
            ticket=ticket,
            history=history,
        ),
        links=(
            ("ticket", ticket.id, "primary"),
            ("ticket_status_history", history.id, "status_history"),
        ),
    )


def load_integration_event_by_dedupe_key(db: Session, *, dedupe_key: str) -> IntegrationEvent | None:
    cached = getattr(db, "events_by_dedupe_key", None)
    if isinstance(cached, dict):
        return cached.get(dedupe_key)
    result = db.execute(select(IntegrationEvent).where(IntegrationEvent.dedupe_key == dedupe_key))
    scalar_one_or_none = getattr(result, "scalar_one_or_none", None)
    if callable(scalar_one_or_none):
        return scalar_one_or_none()
    return None


def load_integration_event_targets(db: Session, *, event_id) -> list[IntegrationEventTarget]:
    cached = getattr(db, "targets_by_event_id", None)
    if isinstance(cached, dict):
        return list(cached.get(event_id, ()))
    result = db.execute(
        select(IntegrationEventTarget)
        .where(IntegrationEventTarget.event_id == event_id)
        .order_by(IntegrationEventTarget.created_at.asc(), IntegrationEventTarget.id.asc())
    )
    scalars = getattr(result, "scalars", None)
    if callable(scalars):
        return list(scalars())
    return []


def resolve_routing_decision(slack: SlackSettings, *, event_type: str) -> RoutingDecision:
    if not slack.enabled:
        return RoutingDecision(routing_result="suppressed_slack_disabled")
    if not slack.is_valid:
        return RoutingDecision(
            routing_result="suppressed_invalid_config",
            config_error_code=slack.config_error_code,
            config_error_summary=slack.config_error_summary,
        )
    notify_enabled = _EVENT_TYPE_TO_NOTIFY_ENABLED[event_type](slack)
    if not notify_enabled:
        return RoutingDecision(routing_result="suppressed_notify_disabled")
    target = slack.get_target(slack.default_target_name)
    if target is None:
        return RoutingDecision(
            routing_result="suppressed_invalid_config",
            config_error_code=slack.config_error_code or "slack_default_target_not_found",
            config_error_summary=slack.config_error_summary
            or "SLACK_DEFAULT_TARGET_NAME must reference a target defined in SLACK_TARGETS_JSON",
        )
    if not target.enabled:
        return RoutingDecision(
            routing_result="suppressed_target_disabled",
            target_name=target.name,
        )
    return RoutingDecision(routing_result="created", target_name=target.name)


def _build_common_payload(
    settings: Settings,
    *,
    ticket: Ticket,
    occurred_at,
) -> dict[str, str]:
    return {
        "ticket_id": str(ticket.id),
        "ticket_reference": ticket.reference,
        "ticket_title": ticket.title,
        "ticket_status": ticket.status,
        "ticket_url": build_ticket_url(settings=settings, ticket_reference=ticket.reference),
        "occurred_at": occurred_at.isoformat(),
    }


def _record_integration_event(
    db: Session,
    *,
    slack_runtime: SlackRuntimeContext,
    event_type: str,
    ticket: Ticket,
    dedupe_key: str,
    payload_json: dict[str, object],
    links: tuple[tuple[str, uuid.UUID, str], ...],
) -> EmissionResult:
    routing = resolve_routing_decision(slack_runtime.slack, event_type=event_type)
    existing_event = load_integration_event_by_dedupe_key(db, dedupe_key=dedupe_key)
    if existing_event is not None:
        result = _build_duplicate_result(db, event=existing_event)
        _log_emission(
            slack_runtime,
            result,
            ticket=ticket,
            event_type=event_type,
            dedupe_key=dedupe_key,
        )
        return result

    recorded_at = slack_runtime.now()
    event = IntegrationEvent(
        id=uuid.uuid4(),
        source_system="autosac",
        event_type=event_type,
        aggregate_type="ticket",
        aggregate_id=ticket.id,
        dedupe_key=dedupe_key,
        payload_json=payload_json,
        routing_result=routing.routing_result,
        routing_target_name=routing.target_name,
        routing_config_error_code=routing.config_error_code,
        routing_config_error_summary=routing.config_error_summary,
        created_at=recorded_at,
    )
    try:
        nested_transaction = db.begin_nested() if hasattr(db, "begin_nested") else nullcontext()
        with nested_transaction:
            db.add(event)
            db.flush()
    except IntegrityError as exc:
        if not _is_dedupe_integrity_error(exc):
            raise
        existing_event = load_integration_event_by_dedupe_key(db, dedupe_key=dedupe_key)
        if existing_event is None:
            raise
        result = _build_duplicate_result(db, event=existing_event)
        _log_emission(
            slack_runtime,
            result,
            ticket=ticket,
            event_type=event_type,
            dedupe_key=dedupe_key,
        )
        return result

    for entity_type, entity_id, relation_kind in links:
        db.add(
            IntegrationEventLink(
                id=uuid.uuid4(),
                event_id=event.id,
                entity_type=entity_type,
                entity_id=entity_id,
                relation_kind=relation_kind,
                created_at=recorded_at,
            )
        )
    if routing.routing_result == "created" and routing.target_name is not None:
        db.add(
            IntegrationEventTarget(
                id=uuid.uuid4(),
                event_id=event.id,
                target_name=routing.target_name,
                target_kind="slack_webhook",
                delivery_status="pending",
                attempt_count=0,
                next_attempt_at=recorded_at,
                created_at=recorded_at,
            )
        )

    result = EmissionResult(
        event=event,
        routing_result=routing.routing_result,
        target_name=routing.target_name,
        config_error_code=routing.config_error_code,
        config_error_summary=routing.config_error_summary,
        event_reused=False,
    )
    _log_emission(
        slack_runtime,
        result,
        ticket=ticket,
        event_type=event_type,
        dedupe_key=dedupe_key,
    )
    return result


def _build_duplicate_result(
    db: Session,
    *,
    event: IntegrationEvent,
) -> EmissionResult:
    existing_targets = load_integration_event_targets(db, event_id=event.id)
    if existing_targets:
        return EmissionResult(
            event=event,
            routing_result="created",
            target_name=existing_targets[0].target_name,
            event_reused=True,
        )
    preserved_routing = _routing_decision_from_event(event)
    selected_routing = _safe_zero_target_duplicate_routing(
        preserved_routing or RoutingDecision(routing_result="suppressed_notify_disabled")
    )
    return EmissionResult(
        event=event,
        routing_result=selected_routing.routing_result,
        target_name=selected_routing.target_name if selected_routing.routing_result == "suppressed_target_disabled" else None,
        config_error_code=selected_routing.config_error_code,
        config_error_summary=selected_routing.config_error_summary,
        event_reused=True,
    )


def _is_dedupe_integrity_error(exc: IntegrityError) -> bool:
    message = str(getattr(exc, "orig", exc))
    return "dedupe_key" in message or "uq_integration_events_dedupe_key" in message


def _routing_decision_from_event(event: IntegrationEvent) -> RoutingDecision | None:
    routing_result = getattr(event, "routing_result", None)
    if routing_result not in INTEGRATION_ROUTING_RESULTS:
        return None
    target_name = getattr(event, "routing_target_name", None)
    if target_name is not None and not isinstance(target_name, str):
        return None
    config_error_code = getattr(event, "routing_config_error_code", None)
    if config_error_code is not None and not isinstance(config_error_code, str):
        return None
    config_error_summary = getattr(event, "routing_config_error_summary", None)
    if config_error_summary is not None and not isinstance(config_error_summary, str):
        return None
    return RoutingDecision(
        routing_result=routing_result,
        target_name=target_name,
        config_error_code=config_error_code,
        config_error_summary=config_error_summary,
    )


def _safe_zero_target_duplicate_routing(routing: RoutingDecision) -> RoutingDecision:
    if routing.routing_result == "created":
        return RoutingDecision(routing_result="suppressed_notify_disabled")
    return routing


def _log_emission(
    slack_runtime: SlackRuntimeContext,
    result: EmissionResult,
    *,
    ticket: Ticket,
    event_type: str,
    dedupe_key: str,
) -> None:
    payload: dict[str, object] = {
        "event_id": str(result.event.id),
        "event_type": event_type,
        "aggregate_type": "ticket",
        "aggregate_id": str(ticket.id),
        "dedupe_key": dedupe_key,
        "routing_result": result.routing_result,
    }
    if result.routing_result in {"created", "suppressed_target_disabled"} and result.target_name is not None:
        payload["target_name"] = result.target_name
    if result.routing_result == "suppressed_invalid_config":
        payload["config_error_code"] = result.config_error_code
        payload["config_error_summary"] = result.config_error_summary
    if result.event_reused:
        payload["event_reused"] = True
    slack_runtime.event_logger("integration", "integration_event_recorded", **payload)
