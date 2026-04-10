from __future__ import annotations

from dataclasses import dataclass
from contextlib import nullcontext
import re
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shared.config import Settings
from shared.logging import log_event
from shared.models import (
    IntegrationEvent,
    IntegrationEventLink,
    IntegrationEventTarget,
    Ticket,
    TicketMessage,
    TicketStatusHistory,
)
from shared.security import utc_now

INTEGRATION_ROUTING_RESULTS = (
    "created",
    "suppressed_slack_disabled",
    "suppressed_invalid_config",
    "suppressed_notify_disabled",
    "suppressed_target_disabled",
)

_PRESERVED_ROUTING_PAYLOAD_KEY = "_integration_routing"
_EVENT_TYPE_TO_NOTIFY_ENABLED = {
    "ticket.created": lambda settings: settings.slack.notify_ticket_created,
    "ticket.public_message_added": lambda settings: settings.slack.notify_public_message_added,
    "ticket.status_changed": lambda settings: settings.slack.notify_status_changed,
}
_WHITESPACE_RE = re.compile(r"\s+", re.UNICODE)


@dataclass(frozen=True)
class RoutingDecision:
    routing_result: str
    target_name: str | None = None
    config_error_code: str | None = None
    config_error_summary: str | None = None


@dataclass(frozen=True)
class EmissionResult:
    event: IntegrationEvent
    routing_result: str
    target_name: str | None = None
    config_error_code: str | None = None
    config_error_summary: str | None = None
    event_reused: bool = False


def resolve_integration_settings(db: Session, settings: Settings | None = None) -> Settings | None:
    if settings is not None:
        return settings
    info = getattr(db, "info", None)
    if isinstance(info, dict):
        resolved = info.get("settings")
        if isinstance(resolved, Settings):
            return resolved
    return None


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
) -> dict[str, str]:
    payload = _build_common_payload(settings, ticket=ticket, occurred_at=message.created_at)
    payload.update(
        message_id=str(message.id),
        message_author_type=message.author_type,
        message_source=message.source,
        message_preview=build_message_preview(
            message.body_text,
            max_chars=settings.slack.message_preview_max_chars,
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
    settings: Settings | None,
    ticket: Ticket,
    initial_message: TicketMessage,
) -> EmissionResult | None:
    resolved_settings = resolve_integration_settings(db, settings)
    if resolved_settings is None:
        return None
    return _record_integration_event(
        db,
        settings=resolved_settings,
        event_type="ticket.created",
        ticket=ticket,
        dedupe_key=f"ticket.created:{ticket.id}",
        payload_json=build_ticket_created_payload(
            resolved_settings,
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
    settings: Settings | None = None,
    ticket: Ticket,
    message: TicketMessage,
) -> EmissionResult | None:
    if message.visibility != "public" or message.source == "ticket_create":
        return None
    resolved_settings = resolve_integration_settings(db, settings)
    if resolved_settings is None:
        return None
    return _record_integration_event(
        db,
        settings=resolved_settings,
        event_type="ticket.public_message_added",
        ticket=ticket,
        dedupe_key=f"ticket.public_message_added:{message.id}",
        payload_json=build_ticket_public_message_payload(
            resolved_settings,
            ticket=ticket,
            message=message,
        ),
        links=(
            ("ticket", ticket.id, "primary"),
            ("ticket_message", message.id, "message"),
        ),
    )


def record_ticket_status_changed_event(
    db: Session,
    *,
    settings: Settings | None = None,
    ticket: Ticket,
    history: TicketStatusHistory,
) -> EmissionResult | None:
    if history.from_status is None or history.from_status == history.to_status:
        return None
    resolved_settings = resolve_integration_settings(db, settings)
    if resolved_settings is None:
        return None
    return _record_integration_event(
        db,
        settings=resolved_settings,
        event_type="ticket.status_changed",
        ticket=ticket,
        dedupe_key=f"ticket.status_changed:{history.id}",
        payload_json=build_ticket_status_changed_payload(
            resolved_settings,
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


def resolve_routing_decision(settings: Settings, *, event_type: str) -> RoutingDecision:
    slack = settings.slack
    if not slack.enabled:
        return RoutingDecision(routing_result="suppressed_slack_disabled")
    if not slack.is_valid:
        return RoutingDecision(
            routing_result="suppressed_invalid_config",
            config_error_code=slack.config_error_code,
            config_error_summary=slack.config_error_summary,
        )
    notify_enabled = _EVENT_TYPE_TO_NOTIFY_ENABLED[event_type](settings)
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
    settings: Settings,
    event_type: str,
    ticket: Ticket,
    dedupe_key: str,
    payload_json: dict[str, object],
    links: tuple[tuple[str, uuid.UUID, str], ...],
) -> EmissionResult:
    routing = resolve_routing_decision(settings, event_type=event_type)
    persisted_payload = _with_preserved_routing_metadata(payload_json, routing=routing)
    existing_event = load_integration_event_by_dedupe_key(db, dedupe_key=dedupe_key)
    if existing_event is not None:
        result = _build_duplicate_result(db, event=existing_event, routing=routing)
        _log_emission(
            result,
            ticket=ticket,
            event_type=event_type,
            dedupe_key=dedupe_key,
        )
        return result

    event = IntegrationEvent(
        id=uuid.uuid4(),
        source_system="autosac",
        event_type=event_type,
        aggregate_type="ticket",
        aggregate_id=ticket.id,
        dedupe_key=dedupe_key,
        payload_json=persisted_payload,
        created_at=utc_now(),
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
        result = _build_duplicate_result(db, event=existing_event, routing=routing)
        _log_emission(
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
                created_at=utc_now(),
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
                next_attempt_at=utc_now(),
                created_at=utc_now(),
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
    routing: RoutingDecision,
) -> EmissionResult:
    existing_targets = load_integration_event_targets(db, event_id=event.id)
    if existing_targets:
        return EmissionResult(
            event=event,
            routing_result="created",
            target_name=existing_targets[0].target_name,
            event_reused=True,
        )
    preserved_routing = _extract_preserved_routing(event)
    selected_routing = _safe_zero_target_duplicate_routing(preserved_routing or routing)
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


def _with_preserved_routing_metadata(
    payload_json: dict[str, object],
    *,
    routing: RoutingDecision,
) -> dict[str, object]:
    metadata: dict[str, object] = {"routing_result": routing.routing_result}
    if routing.target_name is not None:
        metadata["target_name"] = routing.target_name
    if routing.config_error_code is not None:
        metadata["config_error_code"] = routing.config_error_code
    if routing.config_error_summary is not None:
        metadata["config_error_summary"] = routing.config_error_summary
    return {
        **payload_json,
        _PRESERVED_ROUTING_PAYLOAD_KEY: metadata,
    }


def _extract_preserved_routing(event: IntegrationEvent) -> RoutingDecision | None:
    payload_json = getattr(event, "payload_json", None)
    if not isinstance(payload_json, dict):
        return None
    metadata = payload_json.get(_PRESERVED_ROUTING_PAYLOAD_KEY)
    if not isinstance(metadata, dict):
        return None
    routing_result = metadata.get("routing_result")
    if routing_result not in INTEGRATION_ROUTING_RESULTS:
        return None
    target_name = metadata.get("target_name")
    if target_name is not None and not isinstance(target_name, str):
        return None
    config_error_code = metadata.get("config_error_code")
    if config_error_code is not None and not isinstance(config_error_code, str):
        return None
    config_error_summary = metadata.get("config_error_summary")
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
    log_event("integration", "integration_event_recorded", **payload)
