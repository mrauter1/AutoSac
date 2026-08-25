from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any


def summarize_turn_outcomes(outcomes: Iterable[object]) -> dict[str, Any]:
    ordered = sorted(
        outcomes,
        key=lambda outcome: (
            getattr(outcome, "created_at", None) or datetime.min,
            getattr(outcome, "outcome_index", 0),
        ),
    )
    latest = ordered[-1] if ordered else None
    return {
        "count": len(ordered),
        "latest_outcome_kind": getattr(latest, "outcome_kind", None),
        "latest_outcome_at": getattr(latest, "created_at", None),
    }


def present_codex_turn_state(turn: object, *, outcomes: Iterable[object] = ()) -> dict[str, Any]:
    summary = summarize_turn_outcomes(outcomes)
    return {
        "turn_id": getattr(turn, "id", None),
        "conversation_id": getattr(turn, "conversation_id", None),
        "session_id": getattr(turn, "session_id", None),
        "ai_run_id": getattr(turn, "ai_run_id", None),
        "turn_index": getattr(turn, "turn_index", None),
        "status": getattr(turn, "status", None),
        "specialist_id": getattr(turn, "specialist_id", None),
        "route_target_id": getattr(turn, "route_target_id", None),
        "accepted_at": getattr(turn, "accepted_at", None),
        "started_at": getattr(turn, "started_at", None),
        "ended_at": getattr(turn, "ended_at", None),
        **summary,
    }
