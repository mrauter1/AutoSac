from __future__ import annotations

from typing import Any

from shared.routing_registry import load_routing_registry


def present_route_target(route_target_id: str | None, *, fallback_id: str | None = None) -> dict[str, Any]:
    raw_id = (route_target_id or fallback_id or "").strip()
    if not raw_id:
        return {
            "id": None,
            "label": "Unclassified",
            "kind": None,
            "known": False,
        }

    registry = load_routing_registry()
    route_target = registry.get_route_target(raw_id)
    if route_target is None:
        return {
            "id": raw_id,
            "label": raw_id,
            "kind": None,
            "known": False,
        }
    return {
        "id": route_target.id,
        "label": route_target.label,
        "kind": route_target.kind,
        "known": True,
    }


def present_ticket_route_target(ticket) -> dict[str, Any]:
    return present_route_target(
        getattr(ticket, "route_target_id", None),
        fallback_id=getattr(ticket, "ticket_class", None),
    )


def present_ai_run_output(run) -> dict[str, Any]:
    contract_id = getattr(run, "final_output_contract", None)
    payload = getattr(run, "final_output_json", None)
    if not isinstance(payload, dict):
        payload = {}

    presentation = {
        "contract_id": contract_id,
        "summary_short": "",
        "summary_internal": "",
        "relevant_paths": [],
        "public_reply_markdown": "",
        "internal_note_markdown": "",
        "legacy_confidence": None,
        "legacy_impact_level": None,
        "legacy_development_needed": None,
        "response_confidence": None,
        "risk_level": None,
        "risk_reason": "",
        "publish_mode_recommendation": None,
        "handoff_reason": "",
        "assistant_used": None,
        "assistant_specialist_id": None,
    }

    if contract_id == "triage_result":
        presentation.update(
            {
                "summary_short": payload.get("summary_short", ""),
                "summary_internal": payload.get("summary_internal", ""),
                "relevant_paths": payload.get("relevant_paths", []),
                "public_reply_markdown": payload.get("public_reply_markdown", ""),
                "internal_note_markdown": payload.get("internal_note_markdown", ""),
                "legacy_confidence": payload.get("confidence"),
                "legacy_impact_level": payload.get("impact_level"),
                "legacy_development_needed": payload.get("development_needed"),
            }
        )
        return presentation

    if contract_id == "specialist_result":
        presentation.update(
            {
                "summary_internal": payload.get("summary_internal", ""),
                "public_reply_markdown": payload.get("public_reply_markdown", ""),
                "internal_note_markdown": payload.get("internal_note_markdown", ""),
                "response_confidence": payload.get("response_confidence"),
                "risk_level": payload.get("risk_level"),
                "risk_reason": payload.get("risk_reason", ""),
                "publish_mode_recommendation": payload.get("publish_mode_recommendation"),
            }
        )
        return presentation

    if contract_id == "human_handoff_result":
        presentation.update(
            {
                "summary_internal": payload.get("summary_internal", ""),
                "public_reply_markdown": payload.get("public_reply_markdown", ""),
                "internal_note_markdown": payload.get("internal_note_markdown", ""),
                "handoff_reason": payload.get("handoff_reason", ""),
                "assistant_used": payload.get("assistant_used"),
                "assistant_specialist_id": payload.get("assistant_specialist_id"),
            }
        )
        return presentation

    return presentation
