from __future__ import annotations

from dataclasses import dataclass

from shared.routing_registry import RouteTarget
from worker.output_contracts import SpecialistResult

_RESPONSE_CONFIDENCE_ORDER = ("very_low", "low", "medium", "high", "very_high")
_RISK_LEVEL_ORDER = ("none", "low", "medium", "high", "critical")


class PublicationPolicyError(RuntimeError):
    """Raised when registry policy and specialist output cannot be reconciled safely."""


@dataclass(frozen=True)
class PublicationDecision:
    effective_mode: str
    downgrade_reason: str | None = None


def _rank(order: tuple[str, ...], value: str) -> int:
    try:
        return order.index(value)
    except ValueError as exc:
        raise PublicationPolicyError(f"Unknown policy enum value: {value}") from exc


def _has_auto_publish_prerequisites(route_target: RouteTarget, result: SpecialistResult) -> bool:
    policy = route_target.publish_policy
    return (
        policy.allow_auto_publish
        and _rank(_RESPONSE_CONFIDENCE_ORDER, result.response_confidence)
        >= _rank(_RESPONSE_CONFIDENCE_ORDER, policy.min_response_confidence_for_auto_publish)
        and _rank(_RISK_LEVEL_ORDER, result.risk_level)
        <= _rank(_RISK_LEVEL_ORDER, policy.max_risk_level_for_auto_publish)
        and bool(result.public_reply_markdown.strip())
    )


def resolve_effective_publication_mode(route_target: RouteTarget, result: SpecialistResult) -> PublicationDecision:
    policy = route_target.publish_policy

    if route_target.kind == "human_assist":
        effective_mode = "draft_for_human" if result.public_reply_markdown.strip() else "manual_only"
        if effective_mode == "draft_for_human" and not policy.allow_draft_for_human:
            effective_mode = "manual_only"
        if effective_mode == "manual_only" and not policy.allow_manual_only:
            raise PublicationPolicyError(
                f"Route target {route_target.id} does not allow manual_only but human-assist requires human review"
            )
        return PublicationDecision(effective_mode=effective_mode)

    effective_mode = result.publish_mode_recommendation
    downgrade_reasons: list[str] = []
    if effective_mode == "auto_publish" and not _has_auto_publish_prerequisites(route_target, result):
        effective_mode = "draft_for_human"
        downgrade_reasons.append("auto_publish recommendation was downgraded by route-target policy")

    if effective_mode == "draft_for_human" and not policy.allow_draft_for_human:
        effective_mode = "manual_only"
        downgrade_reasons.append("draft_for_human is disabled by route-target policy")

    if effective_mode == "manual_only" and not policy.allow_manual_only:
        raise PublicationPolicyError(
            f"Route target {route_target.id} does not allow manual_only for publication fallback"
        )

    return PublicationDecision(
        effective_mode=effective_mode,
        downgrade_reason="; ".join(downgrade_reasons) or None,
    )
