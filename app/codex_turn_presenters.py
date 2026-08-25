from __future__ import annotations

from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any
import json

from app.ai_run_presenters import present_ai_run_output, present_route_target
from shared.codex_conversations import present_codex_turn_state


def _normalize_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _excerpt(value: object, *, max_chars: int = 220) -> str:
    normalized = " ".join(_normalize_text(value).split())
    if not normalized:
        return ""
    if len(normalized) <= max_chars:
        return normalized
    return f"{normalized[: max_chars - 1].rstrip()}…"


def _pretty_json(payload: object) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True, default=str)


def _specialist_display_name(specialist_id: str | None) -> str:
    normalized = _normalize_text(specialist_id)
    if not normalized:
        return "Unknown"
    return normalized.replace("-", " ").replace("_", " ").title()


def _publication_summary(
    *,
    generated_body: object,
    published_message: object | None,
    draft: object | None,
    latest_outcome_kind: str | None,
) -> dict[str, Any]:
    generated_markdown = _normalize_text(generated_body)
    published_markdown = _normalize_text(getattr(published_message, "body_markdown", None))
    draft_status = getattr(draft, "status", None)
    if published_markdown:
        state = "edited_before_publish" if generated_markdown and generated_markdown != published_markdown else "matches_published"
    elif draft_status == "pending_approval" or latest_outcome_kind == "draft_created":
        state = "pending_review"
    elif generated_markdown:
        state = "unpublished"
    else:
        state = "no_public_reply"
    return {
        "state": state,
        "generated_excerpt": _excerpt(generated_markdown),
        "published_excerpt": _excerpt(published_markdown),
        "generated_markdown": generated_markdown,
        "published_markdown": published_markdown,
        "draft_status": draft_status,
        "published_message_id": getattr(published_message, "id", None),
    }


def present_codex_conversation_overview(
    conversation: object,
    *,
    sessions: Iterable[object],
    turns: Iterable[object],
) -> dict[str, Any]:
    ordered_sessions = list(sessions)
    ordered_turns = list(turns)
    active_session = next((session for session in reversed(ordered_sessions) if getattr(session, "ended_at", None) is None), None)
    active_segment_index = None
    if active_session is not None:
        for index, session in enumerate(ordered_sessions, start=1):
            if getattr(session, "id", None) == getattr(active_session, "id", None):
                active_segment_index = index
                break
    return {
        "conversation_id": getattr(conversation, "id", None),
        "status": getattr(conversation, "status", None),
        "created_at": getattr(conversation, "created_at", None),
        "updated_at": getattr(conversation, "updated_at", None),
        "closed_at": getattr(conversation, "closed_at", None),
        "turn_count": len(ordered_turns),
        "session_count": len(ordered_sessions),
        "active_session_id": getattr(active_session, "id", None),
        "active_session_status": getattr(active_session, "status", None),
        "active_session_segment_index": active_segment_index,
        "active_thread_id": getattr(active_session, "thread_id", None),
        "lease_owner_run_id": getattr(active_session, "lease_owner_run_id", None),
        "lease_worker_instance_id": getattr(active_session, "lease_worker_instance_id", None),
        "lease_expires_at": getattr(active_session, "lease_expires_at", None),
    }


def present_codex_turn_summary(
    turn: object,
    *,
    run: object | None,
    outcomes: Iterable[object],
    session: object | None,
    session_segment_index: int | None,
    draft: object | None,
    published_message: object | None,
    raw_item_count: int = 0,
    conversation_status: str | None = None,
) -> dict[str, Any]:
    ordered_outcomes = list(outcomes)
    presented_turn = present_codex_turn_state(turn, outcomes=ordered_outcomes)
    output_source = run
    if output_source is None:
        output_source = SimpleNamespace(
            final_output_contract=getattr(turn, "output_contract", None),
            final_output_json=None,
        )
    structured_result = present_ai_run_output(output_source)
    publication = _publication_summary(
        generated_body=structured_result.get("public_reply_markdown"),
        published_message=published_message,
        draft=draft,
        latest_outcome_kind=presented_turn.get("latest_outcome_kind"),
    )
    recovery_marker_keys: list[str] = []
    if conversation_status == "recovery_required":
        recovery_marker_keys.append("ops.detail.recovery_marker.conversation_recovery_required")
    if (session_segment_index or 0) > 1:
        recovery_marker_keys.append("ops.detail.recovery_marker.replacement_session_segment")
    if getattr(session, "status", None) in {"replaced", "expired", "deleted"}:
        recovery_marker_keys.append("ops.detail.recovery_marker.non_active_session_status")
    return {
        **presented_turn,
        "specialist_display_name": _specialist_display_name(getattr(turn, "specialist_id", None)),
        "triggered_by": getattr(run, "triggered_by", None),
        "route_target_display": present_route_target(getattr(turn, "route_target_id", None)),
        "output_contract": getattr(run, "final_output_contract", None) or getattr(turn, "output_contract", None),
        "structured_result": {
            **structured_result,
            "summary_short_excerpt": _excerpt(structured_result.get("summary_short")),
            "summary_internal_excerpt": _excerpt(structured_result.get("summary_internal")),
            "public_reply_excerpt": _excerpt(structured_result.get("public_reply_markdown")),
            "internal_note_excerpt": _excerpt(structured_result.get("internal_note_markdown")),
            "handoff_reason_excerpt": _excerpt(structured_result.get("handoff_reason")),
            "risk_reason_excerpt": _excerpt(structured_result.get("risk_reason")),
        },
        "publication": publication,
        "draft_status": getattr(draft, "status", None),
        "published_message_id": getattr(published_message, "id", None),
        "published_message_created_at": getattr(published_message, "created_at", None),
        "session_status": getattr(session, "status", None),
        "session_thread_id": getattr(session, "thread_id", None),
        "session_segment_index": session_segment_index,
        "lease_owner_run_id": getattr(session, "lease_owner_run_id", None),
        "lease_worker_instance_id": getattr(session, "lease_worker_instance_id", None),
        "lease_expires_at": getattr(session, "lease_expires_at", None),
        "artifact_paths": [
            path
            for path in (
                getattr(turn, "prompt_path", None),
                getattr(turn, "schema_path", None),
                getattr(turn, "final_output_path", None),
                getattr(turn, "stdout_jsonl_path", None),
                getattr(turn, "stderr_path", None),
            )
            if path
        ],
        "recovery_marker_keys": recovery_marker_keys,
        "recovery_boundary": bool(recovery_marker_keys),
        "outcome_count": len(ordered_outcomes),
        "raw_item_count": raw_item_count,
    }


def present_codex_turn_detail(
    turn: object,
    *,
    run: object | None,
    outcomes: Iterable[object],
    items: Iterable[object],
    session: object | None,
    session_segment_index: int | None,
    draft: object | None,
    published_message: object | None,
    conversation_status: str | None,
) -> dict[str, Any]:
    ordered_outcomes = list(outcomes)
    ordered_items = list(items)
    presented = present_codex_turn_summary(
        turn,
        run=run,
        outcomes=ordered_outcomes,
        session=session,
        session_segment_index=session_segment_index,
        draft=draft,
        published_message=published_message,
        raw_item_count=len(ordered_items),
        conversation_status=conversation_status,
    )
    presented["outcomes"] = [
        {
            "id": getattr(outcome, "id", None),
            "outcome_index": getattr(outcome, "outcome_index", None),
            "outcome_kind": getattr(outcome, "outcome_kind", None),
            "created_at": getattr(outcome, "created_at", None),
            "payload_json": getattr(outcome, "payload_json", None),
            "payload_json_pretty": _pretty_json(getattr(outcome, "payload_json", None)),
        }
        for outcome in ordered_outcomes
    ]
    presented["items"] = [
        {
            "id": getattr(item, "id", None),
            "item_index": getattr(item, "item_index", None),
            "item_kind": getattr(item, "item_kind", None),
            "codex_item_id": getattr(item, "codex_item_id", None),
            "created_at": getattr(item, "created_at", None),
            "payload_json": getattr(item, "payload_json", None),
            "payload_json_pretty": _pretty_json(getattr(item, "payload_json", None)),
        }
        for item in ordered_items
    ]
    return presented
