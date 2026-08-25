from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Any
import uuid

from sqlalchemy import select

from shared.models import AIDraft, AIRun, CodexConversation, CodexSession, CodexTurn, CodexTurnInput, CodexTurnOutcome, TicketMessage, TicketStatusHistory
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
        "author_type": getattr(message, "author_type", None),
        "visibility": visibility,
        "source": getattr(message, "source", None),
        "created_at": _isoformat(getattr(message, "created_at", None)),
        "body_text": getattr(message, "body_text", None),
    }
    return hashlib.sha256(_serialize_payload(payload).encode("utf-8")).hexdigest()


def _build_message_event(message: TicketMessage, *, visibility: str) -> OrderedInputEvent:
    message_id = getattr(message, "id", None)
    message_id_text = str(message_id) if message_id is not None else _synthetic_message_id(message, visibility=visibility)
    return OrderedInputEvent(
        event_kind="ticket_message",
        source_kind="ticket_message",
        source_id=message_id,
        dedupe_key=f"ticket-message:{message_id_text}",
        payload_json={
            "message_id": message_id_text,
            "author_type": getattr(message, "author_type", None),
            "visibility": visibility,
            "source": getattr(message, "source", None),
            "created_at": _isoformat(getattr(message, "created_at", None)),
            "body_text": getattr(message, "body_text", None),
        },
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
) -> tuple[OrderedInputEvent, ...]:
    events: list[OrderedInputEvent] = [_build_ticket_state_event(context)]
    run_trigger_event = _build_run_trigger_event(run)
    if run_trigger_event is not None:
        events.append(run_trigger_event)
    for message in context.public_messages:
        events.append(_build_message_event(message, visibility="public"))
    for message in context.internal_messages:
        if message.source != "human_internal_note":
            continue
        events.append(_build_message_event(message, visibility="internal"))
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


def _load_consumed_dedupe_keys(db, *, conversation_id: uuid.UUID, exclude_ai_run_id=None) -> set[str]:
    inputs = list(
        db.execute(
            select(CodexTurnInput.dedupe_key, CodexTurn.ai_run_id)
            .join(CodexTurn, CodexTurn.id == CodexTurnInput.turn_id)
            .where(CodexTurn.conversation_id == conversation_id)
            .order_by(CodexTurn.turn_index.asc(), CodexTurnInput.input_index.asc())
        ).all()
    )
    consumed: set[str] = set()
    for dedupe_key, ai_run_id in inputs:
        if exclude_ai_run_id is not None and ai_run_id == exclude_ai_run_id:
            continue
        consumed.add(str(dedupe_key))
    return consumed


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
    consumed_keys = _load_consumed_dedupe_keys(db, conversation_id=conversation.id, exclude_ai_run_id=run.id)
    pending_events = tuple(event for event in current_events if event.dedupe_key not in consumed_keys)
    recovery_required = (
        conversation.status == "recovery_required"
        or active_session is None
        or not (active_session.thread_id or "").strip()
    )
    if not pending_events:
        pending_events = current_events
    prompt_mode = "resume_delta"
    if recovery_required:
        prompt_mode = "recovery_replay"
    elif not feature_enabled:
        prompt_mode = "fallback_replay"
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
