from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable
import uuid

from sqlalchemy import select

from shared.models import CodexTurn, CodexTurnInput, CodexTurnOutcome, CodexTurnSteer


@dataclass(frozen=True)
class KnownConversationInputs:
    dedupe_keys: frozenset[str]
    causal_ai_run_ids: frozenset[uuid.UUID]
    causal_outcome_payloads: dict[uuid.UUID, dict[str, Any]]
    outcome_ai_run_ids: dict[uuid.UUID, uuid.UUID]
    ticket_message_ids: frozenset[uuid.UUID] = frozenset()


def _coerce_uuid(value: object) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def published_message_matches_causal_outcome(
    *,
    author_type: object,
    source: object,
    body_candidates: Iterable[object],
    outcome_payload: dict[str, Any] | None,
) -> bool:
    if author_type != "ai" or not outcome_payload:
        return False
    bodies = {str(value) for value in body_candidates if value is not None}
    if source == "ai_auto_public":
        expected = (outcome_payload.get("body_markdown"), outcome_payload.get("public_reply_markdown"))
        return any(value is not None and str(value) in bodies for value in expected)
    if source == "ai_internal_note":
        expected = (outcome_payload.get("body_markdown"), outcome_payload.get("internal_note_markdown"))
        return any(value is not None and str(value) in bodies for value in expected)
    if source != "ai_draft_published" or outcome_payload.get("edited") is True:
        return False
    published_body = outcome_payload.get("published_body_markdown")
    original_body = outcome_payload.get("original_draft_body_markdown")
    if published_body is not None:
        return str(published_body) in bodies
    if original_body is not None:
        return str(original_body) in bodies
    return False


def causal_message_is_known_to_conversation(
    *,
    known_inputs: KnownConversationInputs,
    author_type: object,
    source: object,
    body_candidates: Iterable[object],
    ai_run_id: object,
    outcome_id: object,
    exclude_ai_run_id=None,
) -> bool:
    causal_ai_run_id = _coerce_uuid(ai_run_id)
    causal_outcome_id = _coerce_uuid(outcome_id)
    excluded_run_id = _coerce_uuid(exclude_ai_run_id)
    if excluded_run_id is not None and causal_ai_run_id == excluded_run_id:
        return True
    if (
        excluded_run_id is not None
        and causal_outcome_id is not None
        and known_inputs.outcome_ai_run_ids.get(causal_outcome_id) == excluded_run_id
    ):
        return True
    outcome_payload = (
        known_inputs.causal_outcome_payloads.get(causal_outcome_id)
        if causal_outcome_id is not None
        else None
    )
    if not published_message_matches_causal_outcome(
        author_type=author_type,
        source=source,
        body_candidates=body_candidates,
        outcome_payload=outcome_payload,
    ):
        return False
    if causal_outcome_id is None:
        return False
    outcome_ai_run_id = known_inputs.outcome_ai_run_ids.get(causal_outcome_id)
    if outcome_ai_run_id is None or outcome_ai_run_id not in known_inputs.causal_ai_run_ids:
        return False
    if causal_ai_run_id is not None and causal_ai_run_id != outcome_ai_run_id:
        return False
    return True


def load_conversation_known_inputs(
    db,
    *,
    conversation_id: uuid.UUID,
    exclude_ai_run_id=None,
) -> KnownConversationInputs:
    consumed_rows = list(
        db.execute(
            select(
                CodexTurnInput.dedupe_key,
                CodexTurnInput.event_kind,
                CodexTurnInput.source_kind,
                CodexTurnInput.source_id,
                CodexTurnInput.payload_json,
                CodexTurn.ai_run_id,
            )
            .join(CodexTurn, CodexTurn.id == CodexTurnInput.turn_id)
            .where(CodexTurn.conversation_id == conversation_id, CodexTurn.accepted_at.is_not(None))
            .order_by(CodexTurn.turn_index.asc(), CodexTurnInput.input_index.asc())
        ).all()
    )
    dedupe_keys: set[str] = set()
    ticket_message_ids: set[uuid.UUID] = set()
    for dedupe_key, event_kind, source_kind, source_id, payload_json, _ai_run_id in consumed_rows:
        dedupe_keys.add(str(dedupe_key))
        if source_kind == "ticket_message" and source_id is not None:
            ticket_message_ids.add(source_id)
            dedupe_keys.add(f"ticket-message:{source_id}")
        if event_kind == "prior_turn_summary" and isinstance(payload_json, dict):
            draft = payload_json.get("draft") or {}
            published_message = payload_json.get("published_message") or {}
            published_message_id = draft.get("published_message_id") or published_message.get("message_id")
            if published_message_id:
                try:
                    message_id = uuid.UUID(str(published_message_id))
                except (TypeError, ValueError, AttributeError):
                    message_id = None
                if message_id is not None:
                    ticket_message_ids.add(message_id)
                    dedupe_keys.add(f"ticket-message:{message_id}")

    steer_rows = list(
        db.execute(
            select(CodexTurnSteer.dedupe_key, CodexTurnSteer.source_kind, CodexTurnSteer.source_id, CodexTurn.ai_run_id)
            .join(CodexTurn, CodexTurn.id == CodexTurnSteer.turn_id)
            .where(CodexTurn.conversation_id == conversation_id, CodexTurnSteer.status == "accepted")
            .order_by(CodexTurnSteer.acknowledged_at.asc(), CodexTurnSteer.created_at.asc())
        ).all()
    )
    for dedupe_key, source_kind, source_id, _ai_run_id in steer_rows:
        dedupe_keys.add(str(dedupe_key))
        if source_kind == "ticket_message" and source_id is not None:
            ticket_message_ids.add(source_id)
            dedupe_keys.add(f"ticket-message:{source_id}")

    turn_rows = list(
        db.execute(
            select(CodexTurn.ai_run_id)
            .where(CodexTurn.conversation_id == conversation_id)
            .order_by(CodexTurn.turn_index.asc(), CodexTurn.id.asc())
        ).all()
    )
    causal_ai_run_ids = {
        ai_run_id
        for (ai_run_id,) in turn_rows
        if ai_run_id is not None and (exclude_ai_run_id is None or ai_run_id != exclude_ai_run_id)
    }
    outcome_rows = list(
        db.execute(
            select(CodexTurnOutcome.id, CodexTurnOutcome.payload_json, CodexTurn.ai_run_id)
            .join(CodexTurn, CodexTurn.id == CodexTurnOutcome.turn_id)
            .where(CodexTurn.conversation_id == conversation_id)
            .order_by(CodexTurn.turn_index.asc(), CodexTurnOutcome.outcome_index.asc())
        ).all()
    )
    causal_outcome_payloads: dict[uuid.UUID, dict[str, Any]] = {}
    outcome_ai_run_ids: dict[uuid.UUID, uuid.UUID] = {}
    for outcome_id, payload_json, ai_run_id in outcome_rows:
        if outcome_id is None:
            continue
        causal_outcome_payloads[outcome_id] = payload_json if isinstance(payload_json, dict) else {}
        if ai_run_id is not None:
            outcome_ai_run_ids[outcome_id] = ai_run_id

    return KnownConversationInputs(
        dedupe_keys=frozenset(dedupe_keys),
        causal_ai_run_ids=frozenset(causal_ai_run_ids),
        causal_outcome_payloads=causal_outcome_payloads,
        outcome_ai_run_ids=outcome_ai_run_ids,
        ticket_message_ids=frozenset(ticket_message_ids),
    )
