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


def _delivery_state_for_receipt(receipt: object, *, queued_source_id: object | None = None) -> str:
    status = getattr(receipt, "status", None)
    if status == "accepted":
        return "included_active_turn"
    if status in {"prepared", "sending", "ambiguous"}:
        return "delivery_uncertain"
    if status == "rejected" and queued_source_id is not None and getattr(receipt, "source_id", None) == queued_source_id:
        return "queued_another_run"
    return "waiting_future_context"


def _delivery_label_key(state: str) -> str:
    return f"ops.detail.delivery_state.{state}"


def _payload_excerpt(payload: object) -> str:
    if isinstance(payload, dict):
        event = payload.get("event")
        if isinstance(event, dict):
            for key in ("body_text", "body_markdown", "text"):
                value = event.get(key)
                if isinstance(value, str) and value.strip():
                    return _excerpt(value)
        for key in ("body_text", "body_markdown", "text", "error", "error_text"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return _excerpt(value)
    return _excerpt(_pretty_json(payload), max_chars=180) if payload else ""


def _present_steering_receipt(receipt: object, *, queued_source_id: object | None = None) -> dict[str, Any]:
    delivery_state = _delivery_state_for_receipt(receipt, queued_source_id=queued_source_id)
    attempted_at = getattr(receipt, "attempted_at", None)
    acknowledged_at = getattr(receipt, "acknowledged_at", None)
    commit_to_ack_latency_ms = None
    if attempted_at is not None and acknowledged_at is not None:
        try:
            commit_to_ack_latency_ms = int((acknowledged_at - attempted_at).total_seconds() * 1000)
        except (AttributeError, TypeError):
            commit_to_ack_latency_ms = None
    return {
        "id": getattr(receipt, "id", None),
        "event_kind": getattr(receipt, "event_kind", None),
        "source_kind": getattr(receipt, "source_kind", None),
        "source_id": getattr(receipt, "source_id", None),
        "dedupe_key": getattr(receipt, "dedupe_key", None),
        "expected_native_turn_id": getattr(receipt, "expected_native_turn_id", None),
        "rpc_request_id": getattr(receipt, "rpc_request_id", None),
        "payload_hash": getattr(receipt, "payload_hash", None),
        "status": getattr(receipt, "status", None),
        "attempted_at": attempted_at,
        "acknowledged_at": acknowledged_at,
        "resolved_at": getattr(receipt, "resolved_at", None),
        "commit_to_ack_latency_ms": commit_to_ack_latency_ms,
        "error_code": getattr(receipt, "error_code", None),
        "error_text": getattr(receipt, "error_text", None),
        "payload_excerpt": _payload_excerpt(getattr(receipt, "payload_json", None)),
        "payload_json_pretty": _pretty_json(getattr(receipt, "payload_json", None)),
        "delivery_state": delivery_state,
        "delivery_state_label_key": _delivery_label_key(delivery_state),
    }


def _present_delivery_event(event: object) -> dict[str, Any]:
    if isinstance(event, dict):
        delivery_state = str(event.get("delivery_state") or "waiting_future_context")
        payload = event.get("payload_json")
        return {
            **event,
            "delivery_state": delivery_state,
            "delivery_state_label_key": _delivery_label_key(delivery_state),
            "payload_excerpt": event.get("payload_excerpt") or _payload_excerpt(payload),
        }
    delivery_state = str(getattr(event, "delivery_state", None) or "waiting_future_context")
    payload = getattr(event, "payload_json", None)
    return {
        "event_kind": getattr(event, "event_kind", None),
        "source_kind": getattr(event, "source_kind", None),
        "source_id": getattr(event, "source_id", None),
        "dedupe_key": getattr(event, "dedupe_key", None),
        "delivery_state": delivery_state,
        "delivery_state_label_key": _delivery_label_key(delivery_state),
        "payload_excerpt": _payload_excerpt(payload),
    }


def _count_delivery_states(items: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        state = str(item.get("delivery_state") or "waiting_future_context")
        counts[state] = counts.get(state, 0) + 1
    return counts


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
    receipts: Iterable[object] = (),
    delivery_events: Iterable[object] = (),
    queued_source_id: object | None = None,
) -> dict[str, Any]:
    ordered_outcomes = list(outcomes)
    ordered_receipts = [_present_steering_receipt(receipt, queued_source_id=queued_source_id) for receipt in receipts]
    presented_delivery_events = [_present_delivery_event(event) for event in delivery_events]
    delivery_state_counts = _count_delivery_states([*ordered_receipts, *presented_delivery_events])
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
        "transport_kind": getattr(turn, "transport_kind", None),
        "native_turn_id": getattr(turn, "native_turn_id", None),
        "steering_closed_at": getattr(turn, "steering_closed_at", None),
        "effective_input_hash": getattr(turn, "effective_input_hash", None),
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
        "steering_receipt_count": len(ordered_receipts),
        "ambiguous_blocker_count": delivery_state_counts.get("delivery_uncertain", 0),
        "delivery_state_counts": delivery_state_counts,
        "delivery_events": presented_delivery_events,
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
    receipts: Iterable[object] = (),
    inputs: Iterable[object] = (),
    delivery_events: Iterable[object] = (),
    queued_source_id: object | None = None,
) -> dict[str, Any]:
    ordered_outcomes = list(outcomes)
    ordered_items = list(items)
    ordered_receipts = list(receipts)
    receipt_dedupe_keys = {getattr(receipt, "dedupe_key", None) for receipt in ordered_receipts}
    included_events = [
        {
            "event_kind": getattr(input_event, "event_kind", None),
            "source_kind": getattr(input_event, "source_kind", None),
            "source_id": getattr(input_event, "source_id", None),
            "dedupe_key": getattr(input_event, "dedupe_key", None),
            "delivery_state": "included_active_turn",
            "payload_json": getattr(input_event, "payload_json", None),
        }
        for input_event in inputs
        if getattr(input_event, "dedupe_key", None) not in receipt_dedupe_keys
    ]
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
        receipts=ordered_receipts,
        delivery_events=[*included_events, *delivery_events],
        queued_source_id=queued_source_id,
    )
    presented["steering_receipts"] = [
        _present_steering_receipt(receipt, queued_source_id=queued_source_id)
        for receipt in ordered_receipts
    ]
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
