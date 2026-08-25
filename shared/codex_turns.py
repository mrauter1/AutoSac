from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shared.models import CodexTurn, CodexTurnOutcome


def _scalar_one_or_none_compatible(result):
    scalar_one_or_none = getattr(result, "scalar_one_or_none", None)
    if callable(scalar_one_or_none):
        return scalar_one_or_none()
    first = getattr(result, "first", None)
    if callable(first):
        return first()
    scalar_one = getattr(result, "scalar_one", None)
    if callable(scalar_one):
        return scalar_one()
    raise AttributeError("Query result does not support scalar_one_or_none, first, or scalar_one.")


def load_codex_turn_for_ai_run(db, *, ai_run_id) -> CodexTurn | None:
    execute = getattr(db, "execute", None)
    if execute is None:
        return None
    turn = _scalar_one_or_none_compatible(
        db.execute(
            select(CodexTurn)
            .where(CodexTurn.ai_run_id == ai_run_id)
            .limit(1)
        )
    )
    return turn if getattr(turn, "id", None) is not None else None


def lock_codex_turn(db, *, turn_id) -> CodexTurn | None:
    """Serialize all outcome/publication mutations for one persistent turn."""
    turn = _scalar_one_or_none_compatible(
        db.execute(
            select(CodexTurn)
            .where(CodexTurn.id == turn_id)
            .limit(1)
            .with_for_update()
        )
    )
    return turn if getattr(turn, "id", None) is not None else None


def next_codex_turn_outcome_index(db, *, turn_id) -> int:
    return int(
        db.execute(
            select(func.coalesce(func.max(CodexTurnOutcome.outcome_index), 0))
            .where(CodexTurnOutcome.turn_id == turn_id)
        ).scalar_one()
        or 0
    ) + 1


def append_codex_turn_outcome(
    db,
    *,
    turn_id,
    outcome_kind: str,
    payload_json: dict | None,
) -> CodexTurnOutcome:
    if isinstance(db, Session) and lock_codex_turn(db, turn_id=turn_id) is None:
        raise ValueError(f"Codex turn {turn_id} does not exist")
    outcome = CodexTurnOutcome(
        id=uuid.uuid4(),
        turn_id=turn_id,
        outcome_index=next_codex_turn_outcome_index(db, turn_id=turn_id),
        outcome_kind=outcome_kind,
        payload_json=payload_json,
    )
    db.add(outcome)
    if isinstance(db, Session):
        db.flush()
    return outcome


def append_codex_turn_outcome_for_ai_run(
    db,
    *,
    ai_run_id,
    outcome_kind: str,
    payload_json: dict | None,
) -> CodexTurnOutcome | None:
    turn = load_codex_turn_for_ai_run(db, ai_run_id=ai_run_id)
    if turn is None:
        return None
    return append_codex_turn_outcome(
        db,
        turn_id=turn.id,
        outcome_kind=outcome_kind,
        payload_json=payload_json,
    )
