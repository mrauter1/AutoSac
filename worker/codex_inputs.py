from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any
import uuid

from sqlalchemy import select

from shared.codex_knowledge import (
    KnownConversationInputs,
    causal_message_is_known_to_conversation,
    load_conversation_known_inputs as _load_conversation_known_inputs,
)
from shared.models import (
    AIDraft,
    AIRun,
    CodexConversation,
    CodexSession,
    CodexTurn,
    CodexTurnOutcome,
    TicketAttachment,
    TicketMessage,
    TicketStatusHistory,
)
from worker.ticket_loader import LoadedTicketContext


@dataclass(frozen=True)
class OrderedInputEvent:
    event_kind: str
    source_kind: str
    source_id: uuid.UUID | None
    dedupe_key: str
    payload_json: dict[str, Any]
    order_key: tuple[Any, ...]


@dataclass(frozen=True)
class PromptConversationState:
    input_hash: str
    prompt_context: LoadedTicketContext
    prompt_appendix: str
    prompt_mode: str
    conversation_id: uuid.UUID | None
    active_session_id: uuid.UUID | None
    recovery_required: bool
    pending_events: tuple[OrderedInputEvent, ...]
    current_events: tuple[OrderedInputEvent, ...]


class UnsupportedInputBundleError(ValueError):
    """Raised when an ordered input event cannot be sent without losing content."""


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _serialize_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _ticket_state_payload(context: LoadedTicketContext) -> dict[str, Any]:
    return {
        "ticket_id": str(context.ticket.id),
        "reference": getattr(context.ticket, "reference", None),
        "title": getattr(context.ticket, "title", None),
        "status": getattr(context.ticket, "status", None),
        "urgent": bool(getattr(context.ticket, "urgent", False)),
        "route_target_id": getattr(context.ticket, "route_target_id", None),
        "requester_language": getattr(context.ticket, "requester_language", None),
        "last_ai_action": getattr(context.ticket, "last_ai_action", None),
        "clarification_rounds": int(getattr(context.ticket, "clarification_rounds", 0) or 0),
        "requester_role": context.requester_role,
        "requester_can_view_internal_messages": context.requester_can_view_internal_messages,
    }


def _build_ticket_state_event(context: LoadedTicketContext) -> OrderedInputEvent:
    payload = _ticket_state_payload(context)
    state_hash = hashlib.sha256(_serialize_payload(payload).encode("utf-8")).hexdigest()
    return OrderedInputEvent(
        event_kind="ticket_state_snapshot",
        source_kind="ticket",
        source_id=context.ticket.id,
        dedupe_key=f"ticket-state:{state_hash}",
        payload_json=payload,
        order_key=(0, "ticket", state_hash),
    )


def _build_run_trigger_event(run: AIRun) -> OrderedInputEvent | None:
    triggered_by = getattr(run, "triggered_by", None)
    requested_by_user_id = getattr(run, "requested_by_user_id", None)
    forced_route_target_id = getattr(run, "forced_route_target_id", None)
    forced_specialist_id = getattr(run, "forced_specialist_id", None)
    recovered_from_run_id = getattr(run, "recovered_from_run_id", None)
    recovery_attempt_count = int(getattr(run, "recovery_attempt_count", 0) or 0)
    if (
        triggered_by != "manual_rerun"
        and recovered_from_run_id is None
        and not forced_route_target_id
        and not forced_specialist_id
    ):
        return None
    payload = {
        "ai_run_id": str(run.id),
        "triggered_by": triggered_by,
        "requested_by_user_id": str(requested_by_user_id) if requested_by_user_id else None,
        "forced_route_target_id": forced_route_target_id,
        "forced_specialist_id": forced_specialist_id,
        "recovered_from_run_id": str(recovered_from_run_id) if recovered_from_run_id else None,
        "recovery_attempt_count": recovery_attempt_count,
    }
    return OrderedInputEvent(
        event_kind="run_trigger",
        source_kind="ai_run",
        source_id=run.id,
        dedupe_key=f"ai-run:{run.id}",
        payload_json=payload,
        order_key=(1, "ai_run", str(run.id)),
    )


def _synthetic_message_id(message: TicketMessage, *, visibility: str) -> str:
    payload = {
        "ticket_id": str(getattr(message, "ticket_id", None)) if getattr(message, "ticket_id", None) else None,
        "author_type": getattr(message, "author_type", None),
        "author_user_id": str(getattr(message, "author_user_id", None)) if getattr(message, "author_user_id", None) else None,
        "visibility": visibility,
        "source": getattr(message, "source", None),
        "created_at": _isoformat(getattr(message, "created_at", None)),
        "body_text": getattr(message, "body_text", None),
        "body_markdown": getattr(message, "body_markdown", None),
    }
    return hashlib.sha256(_serialize_payload(payload).encode("utf-8")).hexdigest()


def _is_image_attachment(attachment: TicketAttachment) -> bool:
    return getattr(attachment, "width", None) is not None and getattr(attachment, "height", None) is not None


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        return path.resolve(strict=False).is_relative_to(root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return False


def _attachment_safe_payload(
    attachment: TicketAttachment,
    *,
    max_attachment_bytes: int | None = None,
) -> dict[str, Any]:
    attachment_id = str(getattr(attachment, "id", ""))
    stored_path = str(getattr(attachment, "stored_path", "") or "")
    size_bytes = getattr(attachment, "size_bytes", None)
    representation_errors: list[str] = []
    if not attachment_id:
        representation_errors.append("missing_attachment_id")
    if not stored_path.strip():
        representation_errors.append("missing_stored_path")
    if size_bytes is None:
        representation_errors.append("missing_size_bytes")
    elif max_attachment_bytes is not None and int(size_bytes) > max_attachment_bytes:
        representation_errors.append("attachment_too_large")
    safe_input = None
    if not representation_errors:
        safe_input = {
            "kind": "file_path",
            "stored_path": stored_path,
            "is_image": _is_image_attachment(attachment),
        }
    return {
        "attachment_id": attachment_id,
        "message_id": str(getattr(attachment, "message_id", "")),
        "visibility": getattr(attachment, "visibility", None),
        "original_filename": getattr(attachment, "original_filename", None),
        "mime_type": getattr(attachment, "mime_type", None),
        "sha256": getattr(attachment, "sha256", None),
        "size_bytes": int(size_bytes) if size_bytes is not None else None,
        "width": getattr(attachment, "width", None),
        "height": getattr(attachment, "height", None),
        "created_at": _isoformat(getattr(attachment, "created_at", None)),
        "safe_input": safe_input,
        "representation_status": "supported" if not representation_errors else "unsupported",
        "representation_errors": tuple(representation_errors),
    }


def _message_bundle_payload(
    message: TicketMessage,
    *,
    visibility: str,
    message_id_text: str,
    ticket_id: uuid.UUID | None = None,
    attachments: tuple[TicketAttachment, ...] = (),
    max_attachment_bytes: int | None = None,
) -> dict[str, Any]:
    attachment_payloads = tuple(
        _attachment_safe_payload(attachment, max_attachment_bytes=max_attachment_bytes)
        for attachment in sorted(
            attachments,
            key=lambda attachment: (
                _isoformat(getattr(attachment, "created_at", None)) or "",
                str(getattr(attachment, "id", "")),
            ),
        )
    )
    bundle_errors: list[str] = []
    if any(payload.get("representation_status") != "supported" for payload in attachment_payloads):
        bundle_errors.append("unsupported_attachment")
    author_user_id = getattr(message, "author_user_id", None)
    ai_run_id = getattr(message, "ai_run_id", None)
    codex_turn_outcome_id = getattr(message, "codex_turn_outcome_id", None)
    resolved_ticket_id = ticket_id or getattr(message, "ticket_id", None)
    body_text = getattr(message, "body_text", None)
    body_markdown = getattr(message, "body_markdown", None)
    payload = {
        "message_id": message_id_text,
        "dedupe_key": f"ticket-message:{message_id_text}",
        "ticket_id": str(resolved_ticket_id) if resolved_ticket_id else None,
        "author": {
            "user_id": str(author_user_id) if author_user_id else None,
            "type": getattr(message, "author_type", None),
        },
        "author_user_id": str(author_user_id) if author_user_id else None,
        "author_type": getattr(message, "author_type", None),
        "visibility": visibility,
        "source": getattr(message, "source", None),
        "created_at": _isoformat(getattr(message, "created_at", None)),
        "body": {
            "text": body_text,
            "markdown": body_markdown,
        },
        "body_text": body_text,
        "body_markdown": body_markdown,
        "attachments": attachment_payloads,
        "bundle": {
            "logical_input": "ticket_message_with_attachments",
            "attachment_count": len(attachment_payloads),
            "representation_status": "supported" if not bundle_errors else "unsupported",
            "representation_errors": tuple(bundle_errors),
        },
        "causal": {
            "ai_run_id": str(ai_run_id) if ai_run_id else None,
            "codex_turn_outcome_id": str(codex_turn_outcome_id) if codex_turn_outcome_id else None,
        },
        "ai_run_id": str(ai_run_id) if ai_run_id else None,
        "codex_turn_outcome_id": str(codex_turn_outcome_id) if codex_turn_outcome_id else None,
    }
    return payload


def _build_message_event(
    message: TicketMessage,
    *,
    visibility: str,
    ticket_id: uuid.UUID | None = None,
    attachments: tuple[TicketAttachment, ...] = (),
    max_attachment_bytes: int | None = None,
) -> OrderedInputEvent:
    message_id = getattr(message, "id", None)
    message_id_text = str(message_id) if message_id is not None else _synthetic_message_id(message, visibility=visibility)
    payload = _message_bundle_payload(
        message,
        visibility=visibility,
        message_id_text=message_id_text,
        ticket_id=ticket_id,
        attachments=attachments,
        max_attachment_bytes=max_attachment_bytes,
    )
    return OrderedInputEvent(
        event_kind="ticket_message",
        source_kind="ticket_message",
        source_id=message_id,
        dedupe_key=f"ticket-message:{message_id_text}",
        payload_json=payload,
        order_key=(2, _isoformat(getattr(message, "created_at", None)), message_id_text),
    )


def _build_status_event(history: TicketStatusHistory) -> OrderedInputEvent:
    return OrderedInputEvent(
        event_kind="ticket_status_changed",
        source_kind="ticket_status_history",
        source_id=history.id,
        dedupe_key=f"ticket-status:{history.id}",
        payload_json={
            "history_id": str(history.id),
            "from_status": history.from_status,
            "to_status": history.to_status,
            "changed_by_type": history.changed_by_type,
            "changed_by_user_id": str(history.changed_by_user_id) if history.changed_by_user_id else None,
            "note": history.note,
            "created_at": _isoformat(history.created_at),
        },
        order_key=(3, _isoformat(history.created_at), str(history.id)),
    )


def _load_turn_publication_summary(db, *, ai_run_id) -> dict[str, Any]:
    draft = db.execute(
        select(AIDraft)
        .where(AIDraft.ai_run_id == ai_run_id)
        .order_by(AIDraft.created_at.desc(), AIDraft.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    published_message = db.execute(
        select(TicketMessage)
        .where(
            TicketMessage.ai_run_id == ai_run_id,
            TicketMessage.visibility == "public",
            TicketMessage.author_type == "ai",
        )
        .order_by(TicketMessage.created_at.desc(), TicketMessage.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return {
        "draft": None
        if draft is None
        else {
            "draft_id": str(draft.id),
            "status": draft.status,
            "body_markdown": draft.body_markdown,
            "reviewed_at": _isoformat(draft.reviewed_at),
            "reviewed_by_user_id": str(draft.reviewed_by_user_id) if draft.reviewed_by_user_id else None,
            "published_message_id": str(draft.published_message_id) if draft.published_message_id else None,
        },
        "published_message": None
        if published_message is None
        else {
            "message_id": str(published_message.id),
            "source": published_message.source,
            "created_at": _isoformat(published_message.created_at),
            "body_markdown": published_message.body_markdown,
        },
    }


def _build_turn_summary_event(db, *, turn: CodexTurn, latest_outcome: CodexTurnOutcome | None) -> OrderedInputEvent:
    latest_outcome_index = latest_outcome.outcome_index if latest_outcome is not None else 0
    final_output_json = None
    public_reply_markdown = None
    internal_note_markdown = None
    summary_internal = None
    if hasattr(turn, "ai_run_id"):
        ai_run = getattr(turn, "_cached_ai_run", None)
        if ai_run is None:
            ai_run = db.get(AIRun, turn.ai_run_id)
            setattr(turn, "_cached_ai_run", ai_run)
        if ai_run is not None and isinstance(ai_run.final_output_json, dict):
            final_output_json = ai_run.final_output_json
            public_reply_markdown = final_output_json.get("public_reply_markdown")
            internal_note_markdown = final_output_json.get("internal_note_markdown")
            summary_internal = final_output_json.get("summary_internal")
    payload = {
        "turn_id": str(turn.id),
        "ai_run_id": str(turn.ai_run_id),
        "turn_index": turn.turn_index,
        "status": turn.status,
        "specialist_id": turn.specialist_id,
        "route_target_id": turn.route_target_id,
        "accepted_at": _isoformat(turn.accepted_at),
        "started_at": _isoformat(turn.started_at),
        "ended_at": _isoformat(turn.ended_at),
        "latest_outcome_kind": latest_outcome.outcome_kind if latest_outcome is not None else None,
        "latest_outcome_index": latest_outcome_index,
        "latest_outcome_payload": latest_outcome.payload_json if latest_outcome is not None else None,
        "public_reply_markdown": public_reply_markdown,
        "internal_note_markdown": internal_note_markdown,
        "summary_internal": summary_internal,
        **_load_turn_publication_summary(db, ai_run_id=turn.ai_run_id),
    }
    return OrderedInputEvent(
        event_kind="prior_turn_summary",
        source_kind="ai_run",
        source_id=turn.ai_run_id,
        dedupe_key=f"turn-summary:{turn.id}:{latest_outcome_index}",
        payload_json=payload,
        order_key=(4, turn.turn_index, latest_outcome_index, str(turn.id)),
    )


def build_ordered_input_events(
    db,
    *,
    context: LoadedTicketContext,
    run: AIRun,
    conversation_id: uuid.UUID | None,
    include_turn_summaries: bool = True,
    exclude_ai_run_id=None,
    max_attachment_bytes: int | None = None,
) -> tuple[OrderedInputEvent, ...]:
    events: list[OrderedInputEvent] = [_build_ticket_state_event(context)]
    run_trigger_event = _build_run_trigger_event(run)
    if run_trigger_event is not None:
        events.append(run_trigger_event)
    public_attachments_by_message: dict[uuid.UUID, list[TicketAttachment]] = {}
    for attachment in context.public_attachments:
        public_attachments_by_message.setdefault(attachment.message_id, []).append(attachment)
    for message in context.public_messages:
        message_id = getattr(message, "id", None)
        events.append(
            _build_message_event(
                message,
                visibility="public",
                ticket_id=context.ticket.id,
                attachments=tuple(public_attachments_by_message.get(message_id, ())) if message_id is not None else (),
                max_attachment_bytes=max_attachment_bytes,
            )
        )
    for message in context.internal_messages:
        events.append(
            _build_message_event(
                message,
                visibility="internal",
                ticket_id=context.ticket.id,
                max_attachment_bytes=max_attachment_bytes,
            )
        )
    status_history = list(
        db.execute(
            select(TicketStatusHistory)
            .where(TicketStatusHistory.ticket_id == context.ticket.id)
            .order_by(TicketStatusHistory.created_at.asc(), TicketStatusHistory.id.asc())
        ).scalars()
    )
    for history in status_history:
        events.append(_build_status_event(history))
    if include_turn_summaries and conversation_id is not None:
        turns = list(
            db.execute(
                select(CodexTurn)
                .where(CodexTurn.conversation_id == conversation_id)
                .order_by(CodexTurn.turn_index.asc(), CodexTurn.id.asc())
            ).scalars()
        )
        for turn in turns:
            if exclude_ai_run_id is not None and turn.ai_run_id == exclude_ai_run_id:
                continue
            outcomes = list(
                db.execute(
                    select(CodexTurnOutcome)
                    .where(CodexTurnOutcome.turn_id == turn.id)
                    .order_by(CodexTurnOutcome.outcome_index.asc(), CodexTurnOutcome.created_at.asc(), CodexTurnOutcome.id.asc())
                ).scalars()
            )
            latest_outcome = outcomes[-1] if outcomes else None
            events.append(_build_turn_summary_event(db, turn=turn, latest_outcome=latest_outcome))
    return tuple(sorted(events, key=lambda event: event.order_key))


def hash_input_events(events: tuple[OrderedInputEvent, ...] | list[OrderedInputEvent]) -> str:
    serialized = json.dumps(
        [
            {
                "event_kind": event.event_kind,
                "source_kind": event.source_kind,
                "source_id": str(event.source_id) if event.source_id is not None else None,
                "dedupe_key": event.dedupe_key,
                "payload_json": event.payload_json,
            }
            for event in events
        ],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _event_body_candidates(event: OrderedInputEvent) -> tuple[str, ...]:
    body = event.payload_json.get("body") if isinstance(event.payload_json.get("body"), dict) else {}
    values = (
        event.payload_json.get("body_markdown"),
        event.payload_json.get("body_text"),
        body.get("markdown"),
        body.get("text"),
    )
    return tuple(str(value) for value in values if value is not None)


def event_is_known_to_conversation(
    db,
    *,
    event: OrderedInputEvent,
    conversation_id: uuid.UUID,
    known_inputs: KnownConversationInputs | None = None,
    exclude_ai_run_id=None,
) -> bool:
    known = known_inputs or _load_conversation_known_inputs(
        db,
        conversation_id=conversation_id,
        exclude_ai_run_id=exclude_ai_run_id,
    )
    if event.dedupe_key in known.dedupe_keys:
        return True
    if event.source_kind != "ticket_message":
        return False
    causal = event.payload_json.get("causal") or {}
    return causal_message_is_known_to_conversation(
        known_inputs=known,
        author_type=event.payload_json.get("author_type"),
        source=event.payload_json.get("source"),
        body_candidates=_event_body_candidates(event),
        ai_run_id=causal.get("ai_run_id") or event.payload_json.get("ai_run_id"),
        outcome_id=causal.get("codex_turn_outcome_id") or event.payload_json.get("codex_turn_outcome_id"),
        exclude_ai_run_id=exclude_ai_run_id,
    )


def _filter_strictly_unseen_input_events(
    db,
    *,
    events: tuple[OrderedInputEvent, ...],
    conversation_id: uuid.UUID,
    known_inputs: KnownConversationInputs,
    exclude_ai_run_id=None,
) -> tuple[OrderedInputEvent, ...]:
    return tuple(
        event
        for event in events
        if not event_is_known_to_conversation(
            db,
            event=event,
            conversation_id=conversation_id,
            known_inputs=known_inputs,
            exclude_ai_run_id=exclude_ai_run_id,
        )
    )


def load_strictly_unseen_input_events(
    db,
    *,
    context: LoadedTicketContext,
    run: AIRun,
    conversation_id: uuid.UUID | None = None,
    include_turn_summaries: bool = True,
    max_attachment_bytes: int | None = None,
) -> tuple[OrderedInputEvent, ...]:
    if conversation_id is None:
        conversation = db.execute(
            select(CodexConversation)
            .where(CodexConversation.ticket_id == context.ticket.id)
            .limit(1)
        ).scalar_one_or_none()
        conversation_id = conversation.id if conversation is not None else None
    if conversation_id is None:
        return ()
    current_events = build_ordered_input_events(
        db,
        context=context,
        run=run,
        conversation_id=conversation_id,
        include_turn_summaries=include_turn_summaries,
        exclude_ai_run_id=run.id,
        max_attachment_bytes=max_attachment_bytes,
    )
    known_inputs = _load_conversation_known_inputs(db, conversation_id=conversation_id, exclude_ai_run_id=run.id)
    return _filter_strictly_unseen_input_events(
        db,
        events=current_events,
        conversation_id=conversation_id,
        known_inputs=known_inputs,
        exclude_ai_run_id=run.id,
    )

def _validated_bundle_attachments(
    event: OrderedInputEvent,
    *,
    trusted_attachment_root: Path | None = None,
    max_attachment_bytes: int | None = None,
) -> tuple[tuple[dict[str, Any], ...], tuple[str, ...]]:
    attachments = tuple(event.payload_json.get("attachments") or ())
    unsupported_attachments = [
        attachment
        for attachment in attachments
        if attachment.get("representation_status") != "supported" or attachment.get("safe_input") is None
    ]
    if unsupported_attachments:
        raise UnsupportedInputBundleError("Ticket message bundle has unsupported attachments")
    if not attachments:
        return (), ()

    local_image_paths: list[str] = []
    if trusted_attachment_root is None:
        return attachments, ()

    if not trusted_attachment_root.exists():
        raise UnsupportedInputBundleError("Trusted attachment root is unavailable")
    if not trusted_attachment_root.is_dir():
        raise UnsupportedInputBundleError("Trusted attachment root is not a directory")

    validated_attachments: list[dict[str, Any]] = []
    for attachment in attachments:
        safe_input = attachment.get("safe_input") or {}
        stored_path = safe_input.get("stored_path")
        if not isinstance(stored_path, str) or not stored_path.strip():
            raise UnsupportedInputBundleError("Ticket message attachment is missing a stored path")
        attachment_path = Path(stored_path)
        if not _path_is_within(attachment_path, trusted_attachment_root):
            raise UnsupportedInputBundleError("Ticket message attachment escaped the trusted upload boundary")
        try:
            resolved_path = attachment_path.resolve(strict=True)
            stat_result = resolved_path.stat()
            if not resolved_path.is_file():
                raise UnsupportedInputBundleError("Ticket message attachment is not a readable file")
            with resolved_path.open("rb") as handle:
                handle.read(1)
        except UnsupportedInputBundleError:
            raise
        except (FileNotFoundError, OSError) as exc:
            raise UnsupportedInputBundleError(
                f"Ticket message attachment is unavailable: {stored_path}"
            ) from exc
        actual_size = stat_result.st_size
        size_bytes = attachment.get("size_bytes")
        if size_bytes is None:
            raise UnsupportedInputBundleError("Ticket message attachment is missing size metadata")
        try:
            expected_size = int(size_bytes)
        except (TypeError, ValueError) as exc:
            raise UnsupportedInputBundleError("Ticket message attachment has invalid size metadata") from exc
        if actual_size != expected_size:
            raise UnsupportedInputBundleError("Ticket message attachment size no longer matches stored metadata")
        if max_attachment_bytes is not None and actual_size > max_attachment_bytes:
            raise UnsupportedInputBundleError("Ticket message attachment exceeds the active-turn size limit")
        if safe_input.get("is_image") is True:
            local_image_paths.append(str(resolved_path))
        validated_attachments.append(attachment)
    return tuple(validated_attachments), tuple(local_image_paths)


def render_ticket_message_bundle(
    event: OrderedInputEvent,
    *,
    trusted_attachment_root: Path | None = None,
    max_attachment_bytes: int | None = None,
) -> dict[str, Any]:
    if event.source_kind != "ticket_message":
        raise UnsupportedInputBundleError(f"Event is not a ticket message bundle: {event.source_kind}")
    bundle = event.payload_json.get("bundle") or {}
    if bundle.get("representation_status") != "supported":
        errors = bundle.get("representation_errors") or ("unsupported_bundle",)
        raise UnsupportedInputBundleError(f"Ticket message bundle cannot be represented: {', '.join(map(str, errors))}")
    attachments, _local_image_paths = _validated_bundle_attachments(
        event,
        trusted_attachment_root=trusted_attachment_root,
        max_attachment_bytes=max_attachment_bytes,
    )
    return {
        "kind": "ticket_message",
        "dedupe_key": event.dedupe_key,
        "message": event.payload_json,
        "body_text": event.payload_json.get("body_text") or "",
        "attachments": tuple(attachments),
    }


def render_ordered_input_event_for_codex(
    event: OrderedInputEvent,
    *,
    trusted_attachment_root: Path | None = None,
    max_attachment_bytes: int | None = None,
) -> dict[str, Any]:
    if event.source_kind == "ticket_message":
        return render_ticket_message_bundle(
            event,
            trusted_attachment_root=trusted_attachment_root,
            max_attachment_bytes=max_attachment_bytes,
        )
    return {
        "kind": event.event_kind,
        "dedupe_key": event.dedupe_key,
        "payload": event.payload_json,
    }


def render_ordered_input_events_for_codex(
    events: tuple[OrderedInputEvent, ...],
    *,
    trusted_attachment_root: Path | None = None,
    max_attachment_bytes: int | None = None,
) -> tuple[dict[str, Any], ...]:
    return tuple(
        render_ordered_input_event_for_codex(
            event,
            trusted_attachment_root=trusted_attachment_root,
            max_attachment_bytes=max_attachment_bytes,
        )
        for event in events
    )


def local_image_input_items_for_events(
    events: tuple[OrderedInputEvent, ...],
    *,
    trusted_attachment_root: Path | None,
    max_attachment_bytes: int | None = None,
) -> tuple[dict[str, Any], ...]:
    image_paths: list[str] = []
    for event in events:
        if event.source_kind != "ticket_message":
            continue
        _attachments, event_image_paths = _validated_bundle_attachments(
            event,
            trusted_attachment_root=trusted_attachment_root,
            max_attachment_bytes=max_attachment_bytes,
        )
        image_paths.extend(event_image_paths)
    return tuple({"type": "localImage", "path": path} for path in image_paths)


def _prompt_context_from_pending_events(
    base_context: LoadedTicketContext,
    *,
    pending_events: tuple[OrderedInputEvent, ...],
) -> LoadedTicketContext:
    pending_public_ids = {
        event.source_id
        for event in pending_events
        if event.source_kind == "ticket_message"
        and event.payload_json.get("visibility") == "public"
    }
    pending_internal_ids = {
        event.source_id
        for event in pending_events
        if event.source_kind == "ticket_message"
        and event.payload_json.get("visibility") == "internal"
    }
    public_messages = tuple(message for message in base_context.public_messages if message.id in pending_public_ids)
    internal_messages = tuple(message for message in base_context.internal_messages if message.id in pending_internal_ids)
    public_attachments = tuple(
        attachment for attachment in base_context.public_attachments if attachment.message_id in pending_public_ids
    )
    return LoadedTicketContext(
        ticket=base_context.ticket,
        requester_role=base_context.requester_role,
        requester_can_view_internal_messages=base_context.requester_can_view_internal_messages,
        public_messages=public_messages,
        internal_messages=internal_messages,
        public_attachments=public_attachments,
    )


def _format_message_events(events: tuple[OrderedInputEvent, ...], *, visibility: str) -> str:
    message_events = [
        event
        for event in events
        if event.source_kind == "ticket_message" and event.payload_json.get("visibility") == visibility
    ]
    if not message_events:
        return "(none)"
    return "\n\n".join(
        "\n".join(
            [
                f"{index}. author_type={event.payload_json.get('author_type')}; source={event.payload_json.get('source')}; created_at={event.payload_json.get('created_at')}",
                str(event.payload_json.get("body_text") or ""),
            ]
        )
        for index, event in enumerate(message_events, start=1)
    )


def _format_non_message_event(event: OrderedInputEvent) -> str:
    if event.event_kind == "ticket_state_snapshot":
        return (
            "ticket_state_snapshot: "
            f"status={event.payload_json.get('status')}; urgent={'yes' if event.payload_json.get('urgent') else 'no'}; "
            f"route_target_id={event.payload_json.get('route_target_id') or '(none)'}; "
            f"requester_language={event.payload_json.get('requester_language') or '(unknown)'}; "
            f"last_ai_action={event.payload_json.get('last_ai_action') or '(none)'}"
        )
    if event.event_kind == "run_trigger":
        return (
            "run_trigger: "
            f"triggered_by={event.payload_json.get('triggered_by')}; "
            f"forced_route_target_id={event.payload_json.get('forced_route_target_id') or '(none)'}; "
            f"forced_specialist_id={event.payload_json.get('forced_specialist_id') or '(none)'}; "
            f"recovered_from_run_id={event.payload_json.get('recovered_from_run_id') or '(none)'}"
        )
    if event.event_kind == "ticket_status_changed":
        return (
            "ticket_status_changed: "
            f"{event.payload_json.get('from_status') or '(none)'} -> {event.payload_json.get('to_status')}; "
            f"changed_by_type={event.payload_json.get('changed_by_type')}; "
            f"note={event.payload_json.get('note') or '(none)'}"
        )
    if event.event_kind == "prior_turn_summary":
        draft = event.payload_json.get("draft") or {}
        published_message = event.payload_json.get("published_message") or {}
        return "\n".join(
            [
                (
                    "prior_turn_summary: "
                    f"turn_index={event.payload_json.get('turn_index')}; "
                    f"specialist_id={event.payload_json.get('specialist_id')}; "
                    f"route_target_id={event.payload_json.get('route_target_id') or '(none)'}; "
                    f"status={event.payload_json.get('status')}; "
                    f"latest_outcome_kind={event.payload_json.get('latest_outcome_kind') or '(none)'}"
                ),
                f"  summary_internal={event.payload_json.get('summary_internal') or '(none)'}",
                f"  public_reply_markdown={event.payload_json.get('public_reply_markdown') or '(none)'}",
                f"  draft_status={draft.get('status') or '(none)'}",
                f"  published_body={published_message.get('body_markdown') or '(none)'}",
            ]
        )
    return _serialize_payload(event.payload_json)


def _format_replay_turn(db, *, turn: CodexTurn) -> str:
    outcomes = list(
        db.execute(
            select(CodexTurnOutcome)
            .where(CodexTurnOutcome.turn_id == turn.id)
            .order_by(CodexTurnOutcome.outcome_index.asc(), CodexTurnOutcome.created_at.asc(), CodexTurnOutcome.id.asc())
        ).scalars()
    )
    ai_run = db.get(AIRun, turn.ai_run_id)
    final_output = ai_run.final_output_json if ai_run is not None and isinstance(ai_run.final_output_json, dict) else {}
    draft = db.execute(
        select(AIDraft)
        .where(AIDraft.ai_run_id == turn.ai_run_id)
        .order_by(AIDraft.created_at.desc(), AIDraft.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    published_message = db.execute(
        select(TicketMessage)
        .where(
            TicketMessage.ai_run_id == turn.ai_run_id,
            TicketMessage.visibility == "public",
            TicketMessage.author_type == "ai",
        )
        .order_by(TicketMessage.created_at.desc(), TicketMessage.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    outcome_lines = [
        f"  - outcome_index={outcome.outcome_index}; outcome_kind={outcome.outcome_kind}; payload={_serialize_payload(outcome.payload_json or {})}"
        for outcome in outcomes
    ] or ["  - (none)"]
    published_body = published_message.body_markdown if published_message is not None else "(none)"
    draft_body = draft.body_markdown if draft is not None else "(none)"
    return "\n".join(
        [
            (
                f"- turn_index={turn.turn_index}; specialist_id={turn.specialist_id}; route_target_id={turn.route_target_id or '(none)'}; "
                f"status={turn.status}; accepted_at={_isoformat(turn.accepted_at) or '(none)'}; ended_at={_isoformat(turn.ended_at) or '(none)'}"
            ),
            f"  generated_public_reply={final_output.get('public_reply_markdown') or '(none)'}",
            f"  generated_internal_note={final_output.get('internal_note_markdown') or '(none)'}",
            f"  generated_summary_internal={final_output.get('summary_internal') or '(none)'}",
            f"  draft_status={draft.status if draft is not None else '(none)'}",
            f"  draft_body={draft_body}",
            f"  published_body={published_body}",
            *outcome_lines,
        ]
    )


def _build_replay_section(db, *, conversation_id: uuid.UUID) -> str:
    turns = list(
        db.execute(
            select(CodexTurn)
            .where(CodexTurn.conversation_id == conversation_id)
            .order_by(CodexTurn.turn_index.asc(), CodexTurn.id.asc())
        ).scalars()
    )
    if not turns:
        return "(none)"
    return "\n\n".join(_format_replay_turn(db, turn=turn) for turn in turns)


def _build_prompt_appendix(
    db,
    *,
    prompt_mode: str,
    recovery_required: bool,
    conversation_id: uuid.UUID | None,
    pending_events: tuple[OrderedInputEvent, ...],
) -> str:
    non_message_events = [
        event
        for event in pending_events
        if event.source_kind != "ticket_message"
    ]
    lines = [
        "",
        "Persistent conversation instructions:",
    ]
    if prompt_mode == "resume_delta":
        lines.extend(
            [
                "- Continue the existing logical ticket conversation in the stored native Codex thread.",
                "- The Public messages and Internal messages sections above contain only new durable ticket events that were not already injected into the native conversation.",
                "- Do not restate earlier assistant outputs unless the new events require a correction or explicit follow-up.",
            ]
        )
    else:
        lines.extend(
            [
                "- Continue the same logical ticket conversation even though native conversation continuity is unavailable for this turn.",
                "- Use the recovery replay below as the authoritative conversation continuity boundary before answering.",
                "- Treat previously published requester-visible text as authoritative over generated drafts when they differ.",
            ]
        )
    if recovery_required:
        lines.append("- This turn is crossing an explicit recovery boundary because the previous native session is missing or corrupt.")
    lines.extend(
        [
            "",
            "Ordered public message events included above:",
            _format_message_events(pending_events, visibility="public"),
            "",
            "Ordered internal note events included above:",
            _format_message_events(pending_events, visibility="internal"),
            "",
            "Ordered non-message input events for this turn:",
            "\n".join(
                f"{index}. {_format_non_message_event(event)}"
                for index, event in enumerate(non_message_events, start=1)
            )
            or "(none)",
        ]
    )
    if prompt_mode in {"recovery_replay", "fallback_replay"} and conversation_id is not None:
        lines.extend(
            [
                "",
                "Replay of prior AutoSac turns and outcomes:",
                _build_replay_section(db, conversation_id=conversation_id),
            ]
        )
    return "\n".join(lines).strip()


def build_prompt_conversation_state(
    db,
    *,
    context: LoadedTicketContext,
    run: AIRun,
    feature_enabled: bool,
) -> PromptConversationState:
    conversation = db.execute(
        select(CodexConversation)
        .where(CodexConversation.ticket_id == context.ticket.id)
        .limit(1)
    ).scalar_one_or_none()
    active_session = None
    if conversation is not None:
        active_session = db.execute(
            select(CodexSession)
            .where(CodexSession.conversation_id == conversation.id, CodexSession.ended_at.is_(None))
            .limit(1)
        ).scalar_one_or_none()
    conversation_id = conversation.id if conversation is not None else None
    prior_turn_count = 0
    if conversation_id is not None:
        prior_turn_count = len(
            db.execute(
                select(CodexTurn.id)
                .where(CodexTurn.conversation_id == conversation_id)
                .order_by(CodexTurn.turn_index.asc(), CodexTurn.id.asc())
            ).all()
        )
    current_events = build_ordered_input_events(
        db,
        context=context,
        run=run,
        conversation_id=conversation_id,
        exclude_ai_run_id=run.id,
    )
    input_hash = hash_input_events(current_events)
    if conversation is None or prior_turn_count == 0:
        return PromptConversationState(
            input_hash=input_hash,
            prompt_context=context,
            prompt_appendix="",
            prompt_mode="initial_full",
            conversation_id=conversation_id,
            active_session_id=active_session.id if active_session is not None else None,
            recovery_required=False,
            pending_events=current_events,
            current_events=current_events,
        )
    known_inputs = _load_conversation_known_inputs(db, conversation_id=conversation.id, exclude_ai_run_id=run.id)
    pending_events = _filter_strictly_unseen_input_events(
        db,
        events=current_events,
        conversation_id=conversation.id,
        known_inputs=known_inputs,
        exclude_ai_run_id=run.id,
    )
    recovery_required = (
        conversation.status == "recovery_required"
        or active_session is None
        or not (active_session.thread_id or "").strip()
    )
    prompt_mode = "resume_delta"
    if recovery_required:
        prompt_mode = "recovery_replay"
    elif not feature_enabled:
        prompt_mode = "fallback_replay"
    if not pending_events and prompt_mode in {"recovery_replay", "fallback_replay"}:
        pending_events = current_events
    prompt_context = context if prompt_mode == "initial_full" else _prompt_context_from_pending_events(context, pending_events=pending_events)
    prompt_appendix = ""
    if prompt_mode != "initial_full":
        prompt_appendix = _build_prompt_appendix(
            db,
            prompt_mode=prompt_mode,
            recovery_required=recovery_required,
            conversation_id=conversation.id,
            pending_events=pending_events,
        )
    return PromptConversationState(
        input_hash=input_hash,
        prompt_context=prompt_context,
        prompt_appendix=prompt_appendix,
        prompt_mode=prompt_mode,
        conversation_id=conversation.id,
        active_session_id=active_session.id if active_session is not None else None,
        recovery_required=recovery_required,
        pending_events=pending_events,
        current_events=current_events,
    )
