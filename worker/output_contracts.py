from __future__ import annotations

from typing import Literal
import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from shared.routing_registry import RoutingRegistryError, load_routing_registry


class OutputContractError(RuntimeError):
    """Raised when a contract id is unknown or output validation fails."""


class RelevantPathResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    reason: str


class RouterResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route_target_id: str = Field(min_length=1)
    routing_rationale: str = Field(min_length=1)


class SpecialistSelectorResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    specialist_id: str = Field(min_length=1)
    selection_rationale: str = Field(min_length=1)


class SpecialistResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requester_language: str = Field(min_length=1)
    public_reply_markdown: str
    internal_note_markdown: str
    response_confidence: Literal["very_low", "low", "medium", "high", "very_high"]
    risk_level: Literal["none", "low", "medium", "high", "critical"]
    risk_reason: str = Field(min_length=1)
    summary_internal: str = Field(min_length=1)
    publish_mode_recommendation: Literal["auto_publish", "draft_for_human", "manual_only"]

    @model_validator(mode="after")
    def validate_publish_requirements(self) -> SpecialistResult:
        if self.publish_mode_recommendation in {"auto_publish", "draft_for_human"} and not self.public_reply_markdown.strip():
            raise ValueError("public_reply_markdown is required for auto_publish and draft_for_human")
        if self.publish_mode_recommendation == "manual_only" and not self.internal_note_markdown.strip():
            raise ValueError("internal_note_markdown is required for manual_only")
        return self


class HumanHandoffResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route_target_id: str = Field(min_length=1)
    handoff_reason: str = Field(min_length=1)
    summary_internal: str = Field(min_length=1)
    internal_note_markdown: str = Field(min_length=1)
    public_reply_markdown: str
    assistant_used: bool
    assistant_specialist_id: str | None = None

    @model_validator(mode="after")
    def validate_assistant_fields(self) -> HumanHandoffResult:
        if self.assistant_used and not (self.assistant_specialist_id or "").strip():
            raise ValueError("assistant_specialist_id is required when assistant_used=true")
        if not self.assistant_used and self.assistant_specialist_id is not None:
            raise ValueError("assistant_specialist_id must be null when assistant_used=false")
        return self


class TriageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Legacy read model only for historical run hydration/presentation.
    ticket_class: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    impact_level: Literal["low", "medium", "high", "unknown"]
    requester_language: str = Field(min_length=2)
    summary_short: str = Field(min_length=1, max_length=120)
    summary_internal: str = Field(min_length=1)
    development_needed: bool
    needs_clarification: bool
    clarifying_questions: list[str] = Field(max_length=3)
    incorrect_or_conflicting_details: list[str]
    evidence_found: bool
    relevant_paths: list[RelevantPathResult]
    answer_scope: Literal["document_scoped", "general_reasoning"]
    evidence_status: Literal["verified", "not_found_low_risk_guess", "not_applicable"]
    misuse_or_safety_risk: bool
    human_review_reason: str
    recommended_next_action: Literal[
        "ask_clarification",
        "auto_public_reply",
        "auto_confirm_and_route",
        "draft_public_reply",
        "route_dev_ti",
    ]
    auto_public_reply_allowed: bool
    public_reply_markdown: str
    internal_note_markdown: str


CONTRACT_MODELS = {
    "router_result": RouterResult,
    "specialist_selector_result": SpecialistSelectorResult,
    "specialist_result": SpecialistResult,
    "human_handoff_result": HumanHandoffResult,
    "triage_result": TriageResult,
}


def schema_json_for_contract(contract_id: str) -> str:
    model = CONTRACT_MODELS.get(contract_id)
    if model is None:
        raise OutputContractError(f"Unknown output contract: {contract_id}")
    return json.dumps(model.model_json_schema(), indent=2, sort_keys=True)


def validate_contract_output(
    contract_id: str,
    payload: dict[str, object],
    *,
    route_target_id: str | None = None,
    candidate_specialist_ids: tuple[str, ...] | None = None,
    requester_role: str | None = None,
) -> BaseModel:
    model = CONTRACT_MODELS.get(contract_id)
    if model is None:
        raise OutputContractError(f"Unknown output contract: {contract_id}")
    try:
        result = model.model_validate(payload)
    except ValidationError as exc:
        raise OutputContractError(f"Output failed {contract_id} validation: {exc}") from exc
    try:
        _validate_registry_backed_contract(
            contract_id,
            result,
            route_target_id=route_target_id,
            candidate_specialist_ids=candidate_specialist_ids,
            requester_role=requester_role,
        )
    except RoutingRegistryError as exc:
        raise OutputContractError(str(exc)) from exc
    return result


def _validate_registry_backed_contract(
    contract_id: str,
    result: BaseModel,
    *,
    route_target_id: str | None,
    candidate_specialist_ids: tuple[str, ...] | None,
    requester_role: str | None,
) -> None:
    registry = load_routing_registry()

    if contract_id == "router_result":
        if requester_role is None:
            registry.require_enabled_route_target(result.route_target_id)
        else:
            registry.require_enabled_route_target_for_requester(result.route_target_id, requester_role)
        return

    if contract_id == "specialist_selector_result":
        if route_target_id is None:
            raise OutputContractError("specialist_selector_result validation requires route_target_id context")
        allowed_ids = candidate_specialist_ids or tuple(
            specialist.id
            for specialist in registry.candidate_specialists_for_target(
                route_target_id,
                requester_role=requester_role,
            )
        )
        if requester_role is None:
            registry.require_enabled_route_target(route_target_id)
        else:
            registry.require_enabled_route_target_for_requester(route_target_id, requester_role)
        registry.require_specialist(result.specialist_id)
        if result.specialist_id not in allowed_ids:
            raise OutputContractError(
                f"Selector chose specialist {result.specialist_id} outside the allowed candidate set for {route_target_id}"
            )
        return

    if contract_id == "human_handoff_result":
        registry.require_route_target(result.route_target_id)
        if result.assistant_specialist_id is not None:
            registry.require_specialist(result.assistant_specialist_id)
