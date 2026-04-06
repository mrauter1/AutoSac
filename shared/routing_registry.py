from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import json
from pathlib import Path
import re
from typing import Any

from shared.agent_specs import AGENT_SPECS_DIR, AgentSpec, AgentSpecError, load_agent_spec

ROUTING_REGISTRY_PATH = AGENT_SPECS_DIR / "registry.json"

ROUTE_TARGET_KINDS = ("direct_ai", "human_assist")
SPECIALIST_SELECTION_MODES = ("fixed", "auto", "none")
RESPONSE_CONFIDENCE_VALUES = ("very_low", "low", "medium", "high", "very_high")
RISK_LEVEL_VALUES = ("none", "low", "medium", "high", "critical")
PUBLISH_MODE_RECOMMENDATION_VALUES = ("auto_publish", "draft_for_human", "manual_only")

_REQUIRED_TOP_LEVEL_KEYS = ("version", "router_spec_id", "selector_spec_id", "route_targets", "specialists")
_ROUTE_TARGET_KEYS = ("id", "label", "kind", "enabled", "ops_visible", "router_description", "handler", "publish_policy")
_HANDLER_REQUIRED_KEYS = ("specialist_selection",)
_HANDLER_ALLOWED_KEYS = ("human_queue_status", "specialist_selection")
_SPECIALIST_SELECTION_REQUIRED_KEYS = ("mode",)
_SPECIALIST_SELECTION_ALLOWED_KEYS = ("mode", "specialist_id", "candidate_specialist_ids")
_PUBLISH_POLICY_KEYS = (
    "allow_auto_publish",
    "min_response_confidence_for_auto_publish",
    "max_risk_level_for_auto_publish",
    "allow_draft_for_human",
    "allow_manual_only",
)
_SPECIALIST_REQUIRED_KEYS = ("id", "display_name", "spec_id", "enabled")
_SPECIALIST_ALLOWED_KEYS = ("id", "display_name", "spec_id", "enabled", "can_assist_human")
_SNAKE_CASE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class RoutingRegistryError(RuntimeError):
    """Raised when the route-target registry is missing or invalid."""


@dataclass(frozen=True)
class SpecialistSelection:
    mode: str
    specialist_id: str | None
    candidate_specialist_ids: tuple[str, ...]


@dataclass(frozen=True)
class PublishPolicy:
    allow_auto_publish: bool
    min_response_confidence_for_auto_publish: str
    max_risk_level_for_auto_publish: str
    allow_draft_for_human: bool
    allow_manual_only: bool


@dataclass(frozen=True)
class RouteTargetHandler:
    human_queue_status: str | None
    specialist_selection: SpecialistSelection


@dataclass(frozen=True)
class RouteTarget:
    id: str
    label: str
    kind: str
    enabled: bool
    ops_visible: bool
    router_description: str
    handler: RouteTargetHandler
    publish_policy: PublishPolicy


@dataclass(frozen=True)
class SpecialistRegistration:
    id: str
    display_name: str
    spec_id: str
    enabled: bool
    can_assist_human: bool
    spec: AgentSpec


@dataclass(frozen=True)
class RoutingRegistry:
    version: int
    router_spec_id: str
    selector_spec_id: str | None
    route_targets: tuple[RouteTarget, ...]
    specialists: tuple[SpecialistRegistration, ...]
    router_spec: AgentSpec
    selector_spec: AgentSpec | None
    _route_targets_by_id: dict[str, RouteTarget] = field(repr=False)
    _specialists_by_id: dict[str, SpecialistRegistration] = field(repr=False)

    def get_route_target(self, route_target_id: str) -> RouteTarget | None:
        return self._route_targets_by_id.get(route_target_id)

    def require_route_target(self, route_target_id: str) -> RouteTarget:
        route_target = self.get_route_target(route_target_id)
        if route_target is None:
            raise RoutingRegistryError(f"Unknown route target: {route_target_id}")
        return route_target

    def require_enabled_route_target(self, route_target_id: str) -> RouteTarget:
        route_target = self.require_route_target(route_target_id)
        if not route_target.enabled:
            raise RoutingRegistryError(f"Route target {route_target_id} is disabled for new runs")
        return route_target

    def get_specialist(self, specialist_id: str) -> SpecialistRegistration | None:
        return self._specialists_by_id.get(specialist_id)

    def require_specialist(self, specialist_id: str) -> SpecialistRegistration:
        specialist = self.get_specialist(specialist_id)
        if specialist is None:
            raise RoutingRegistryError(f"Unknown specialist: {specialist_id}")
        return specialist

    def enabled_route_targets(self) -> tuple[RouteTarget, ...]:
        return tuple(route_target for route_target in self.route_targets if route_target.enabled)

    def ops_visible_route_targets(self) -> tuple[RouteTarget, ...]:
        return tuple(route_target for route_target in self.route_targets if route_target.ops_visible)

    def candidate_specialists_for_target(
        self,
        route_target_id: str,
        *,
        allow_disabled_target: bool = False,
    ) -> tuple[SpecialistRegistration, ...]:
        route_target = self.require_route_target(route_target_id)
        if not route_target.enabled and not allow_disabled_target:
            raise RoutingRegistryError(f"Route target {route_target_id} is disabled for new runs")

        selection = route_target.handler.specialist_selection
        if selection.mode == "none":
            return ()
        if selection.mode == "fixed":
            specialist = self.require_specialist(selection.specialist_id or "")
            if not specialist.enabled and not allow_disabled_target:
                raise RoutingRegistryError(
                    f"Route target {route_target.id} references disabled specialist {specialist.id} for new runs"
                )
            return (specialist,)
        if selection.candidate_specialist_ids:
            candidates = tuple(self.require_specialist(specialist_id) for specialist_id in selection.candidate_specialist_ids)
            if not allow_disabled_target:
                disabled = tuple(candidate.id for candidate in candidates if not candidate.enabled)
                if disabled:
                    raise RoutingRegistryError(
                        f"Route target {route_target.id} references disabled candidate specialists: {', '.join(disabled)}"
                    )
            return candidates
        return tuple(
            specialist
            for specialist in self.specialists
            if specialist.enabled and specialist.can_assist_human
        )


def clear_routing_registry_cache() -> None:
    _load_routing_registry_cached.cache_clear()


def load_routing_registry(path: Path | None = None) -> RoutingRegistry:
    resolved = (path or ROUTING_REGISTRY_PATH).resolve()
    if path is None:
        return _load_routing_registry_cached(str(resolved))
    return _load_routing_registry_impl(resolved)


@lru_cache(maxsize=4)
def _load_routing_registry_cached(path_str: str) -> RoutingRegistry:
    return _load_routing_registry_impl(Path(path_str))


def _load_routing_registry_impl(path: Path) -> RoutingRegistry:
    raw = _read_registry_json(path)
    _validate_exact_keys(raw, _REQUIRED_TOP_LEVEL_KEYS, f"Routing registry {path}")

    version = raw["version"]
    if not isinstance(version, int) or version <= 0:
        raise RoutingRegistryError("Routing registry version must be a positive integer")

    router_spec_id = _read_non_empty_string(raw["router_spec_id"], "Routing registry router_spec_id")
    selector_spec_id = _read_optional_string(raw["selector_spec_id"], "Routing registry selector_spec_id")
    route_targets_raw = _read_list(raw["route_targets"], "Routing registry route_targets")
    specialists_raw = _read_list(raw["specialists"], "Routing registry specialists")
    if not route_targets_raw:
        raise RoutingRegistryError("Routing registry must define at least one route target")
    if not specialists_raw:
        raise RoutingRegistryError("Routing registry must define at least one specialist")

    router_spec = _load_spec(router_spec_id, expected_kind="router", context="routing registry router_spec_id")
    specialists = tuple(_parse_specialist(item, index=index) for index, item in enumerate(specialists_raw, start=1))
    route_targets = tuple(_parse_route_target(item, index=index) for index, item in enumerate(route_targets_raw, start=1))

    specialists_by_id = _index_unique(specialists, kind_label="specialist")
    route_targets_by_id = _index_unique(route_targets, kind_label="route target")

    auto_targets = tuple(
        route_target.id
        for route_target in route_targets
        if route_target.handler.specialist_selection.mode == "auto"
    )
    selector_spec = None
    if auto_targets:
        if selector_spec_id is None:
            raise RoutingRegistryError(
                "Routing registry selector_spec_id is required when any route target uses specialist_selection.mode=auto"
            )
        selector_spec = _load_spec(selector_spec_id, expected_kind="selector", context="routing registry selector_spec_id")
    elif selector_spec_id is not None:
        selector_spec = _load_spec(selector_spec_id, expected_kind="selector", context="routing registry selector_spec_id")

    for route_target in route_targets:
        _validate_route_target_cross_references(route_target, specialists_by_id)

    return RoutingRegistry(
        version=version,
        router_spec_id=router_spec_id,
        selector_spec_id=selector_spec_id,
        route_targets=route_targets,
        specialists=specialists,
        router_spec=router_spec,
        selector_spec=selector_spec,
        _route_targets_by_id=route_targets_by_id,
        _specialists_by_id=specialists_by_id,
    )


def _read_registry_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RoutingRegistryError(f"Routing registry file was not found: {path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RoutingRegistryError(f"Routing registry file is unreadable or invalid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise RoutingRegistryError(f"Routing registry must contain a JSON object: {path}")
    return payload


def _validate_exact_keys(raw: dict[str, Any], expected_keys: tuple[str, ...], context: str) -> None:
    actual = set(raw.keys())
    expected = set(expected_keys)
    if actual != expected:
        missing = sorted(expected - actual)
        extras = sorted(actual - expected)
        problems: list[str] = []
        if missing:
            problems.append(f"missing keys: {', '.join(missing)}")
        if extras:
            problems.append(f"unexpected keys: {', '.join(extras)}")
        raise RoutingRegistryError(f"{context} has invalid keys ({'; '.join(problems)})")


def _validate_keys(
    raw: dict[str, Any],
    *,
    required_keys: tuple[str, ...],
    allowed_keys: tuple[str, ...],
    context: str,
) -> None:
    actual = set(raw.keys())
    required = set(required_keys)
    allowed = set(allowed_keys)
    missing = sorted(required - actual)
    extras = sorted(actual - allowed)
    if not missing and not extras:
        return
    problems: list[str] = []
    if missing:
        problems.append(f"missing keys: {', '.join(missing)}")
    if extras:
        problems.append(f"unexpected keys: {', '.join(extras)}")
    raise RoutingRegistryError(f"{context} has invalid keys ({'; '.join(problems)})")


def _read_dict(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RoutingRegistryError(f"{context} must be a JSON object")
    return value


def _read_list(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise RoutingRegistryError(f"{context} must be a JSON array")
    return value


def _read_non_empty_string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RoutingRegistryError(f"{context} must be a non-empty string")
    return value.strip()


def _read_optional_string(value: Any, context: str) -> str | None:
    if value is None:
        return None
    return _read_non_empty_string(value, context)


def _read_bool(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise RoutingRegistryError(f"{context} must be a boolean")
    return value


def _parse_specialist(item: Any, *, index: int) -> SpecialistRegistration:
    raw = _read_dict(item, f"Routing registry specialist #{index}")
    _validate_keys(
        raw,
        required_keys=_SPECIALIST_REQUIRED_KEYS,
        allowed_keys=_SPECIALIST_ALLOWED_KEYS,
        context=f"Routing registry specialist #{index}",
    )

    specialist_id = _read_non_empty_string(raw["id"], f"Routing registry specialist #{index} id")
    display_name = _read_non_empty_string(raw["display_name"], f"Routing registry specialist #{index} display_name")
    spec_id = _read_non_empty_string(raw["spec_id"], f"Routing registry specialist #{index} spec_id")
    enabled = _read_bool(raw["enabled"], f"Routing registry specialist #{index} enabled")
    can_assist_human = _read_bool(
        raw.get("can_assist_human", True),
        f"Routing registry specialist #{index} can_assist_human",
    )
    spec = _load_spec(spec_id, expected_kind="specialist", context=f"Routing registry specialist {specialist_id}")
    return SpecialistRegistration(
        id=specialist_id,
        display_name=display_name,
        spec_id=spec_id,
        enabled=enabled,
        can_assist_human=can_assist_human,
        spec=spec,
    )


def _parse_route_target(item: Any, *, index: int) -> RouteTarget:
    raw = _read_dict(item, f"Routing registry route target #{index}")
    _validate_exact_keys(raw, _ROUTE_TARGET_KEYS, f"Routing registry route target #{index}")

    route_target_id = _read_non_empty_string(raw["id"], f"Routing registry route target #{index} id")
    if not _SNAKE_CASE_RE.fullmatch(route_target_id):
        raise RoutingRegistryError(f"Route target id must use snake_case: {route_target_id}")
    label = _read_non_empty_string(raw["label"], f"Routing registry route target {route_target_id} label")
    kind = _read_non_empty_string(raw["kind"], f"Routing registry route target {route_target_id} kind")
    if kind not in ROUTE_TARGET_KINDS:
        raise RoutingRegistryError(f"Route target {route_target_id} has invalid kind: {kind}")
    enabled = _read_bool(raw["enabled"], f"Routing registry route target {route_target_id} enabled")
    ops_visible = _read_bool(raw["ops_visible"], f"Routing registry route target {route_target_id} ops_visible")
    router_description = _read_non_empty_string(
        raw["router_description"],
        f"Routing registry route target {route_target_id} router_description",
    )
    handler = _parse_handler(raw["handler"], route_target_id=route_target_id, kind=kind)
    publish_policy = _parse_publish_policy(raw["publish_policy"], route_target_id=route_target_id, kind=kind)
    return RouteTarget(
        id=route_target_id,
        label=label,
        kind=kind,
        enabled=enabled,
        ops_visible=ops_visible,
        router_description=router_description,
        handler=handler,
        publish_policy=publish_policy,
    )


def _parse_handler(value: Any, *, route_target_id: str, kind: str) -> RouteTargetHandler:
    raw = _read_dict(value, f"Route target {route_target_id} handler")
    _validate_keys(
        raw,
        required_keys=_HANDLER_REQUIRED_KEYS,
        allowed_keys=_HANDLER_ALLOWED_KEYS,
        context=f"Route target {route_target_id} handler",
    )

    human_queue_status = _read_optional_string(
        raw.get("human_queue_status"),
        f"Route target {route_target_id} human_queue_status",
    )
    if kind == "human_assist" and human_queue_status is None:
        raise RoutingRegistryError(f"Human-assist route target {route_target_id} must define handler.human_queue_status")
    if kind == "direct_ai" and human_queue_status is not None:
        raise RoutingRegistryError(f"Direct-AI route target {route_target_id} must not define handler.human_queue_status")

    selection = _parse_specialist_selection(
        raw["specialist_selection"],
        route_target_id=route_target_id,
        kind=kind,
    )
    return RouteTargetHandler(human_queue_status=human_queue_status, specialist_selection=selection)


def _parse_specialist_selection(value: Any, *, route_target_id: str, kind: str) -> SpecialistSelection:
    raw = _read_dict(value, f"Route target {route_target_id} specialist_selection")
    _validate_keys(
        raw,
        required_keys=_SPECIALIST_SELECTION_REQUIRED_KEYS,
        allowed_keys=_SPECIALIST_SELECTION_ALLOWED_KEYS,
        context=f"Route target {route_target_id} specialist_selection",
    )

    mode = _read_non_empty_string(raw["mode"], f"Route target {route_target_id} specialist_selection.mode")
    if mode not in SPECIALIST_SELECTION_MODES:
        raise RoutingRegistryError(f"Route target {route_target_id} has invalid specialist_selection.mode: {mode}")
    specialist_id = _read_optional_string(
        raw.get("specialist_id"),
        f"Route target {route_target_id} specialist_selection.specialist_id",
    )
    candidate_specialist_ids = _parse_candidate_ids(
        raw.get("candidate_specialist_ids"),
        route_target_id=route_target_id,
    )

    if mode == "fixed":
        if specialist_id is None:
            raise RoutingRegistryError(f"Route target {route_target_id} with mode=fixed must define specialist_id")
        if candidate_specialist_ids:
            raise RoutingRegistryError(f"Route target {route_target_id} with mode=fixed must not define candidate_specialist_ids")
    elif mode == "auto":
        if specialist_id is not None:
            raise RoutingRegistryError(f"Route target {route_target_id} with mode=auto must not define specialist_id")
        if kind == "direct_ai" and not candidate_specialist_ids:
            raise RoutingRegistryError(
                f"Direct-AI route target {route_target_id} with mode=auto must define candidate_specialist_ids"
            )
    else:
        if kind != "human_assist":
            raise RoutingRegistryError(f"Route target {route_target_id} with mode=none must use kind=human_assist")
        if specialist_id is not None or candidate_specialist_ids:
            raise RoutingRegistryError(
                f"Route target {route_target_id} with mode=none must not define specialist_id or candidate_specialist_ids"
            )

    return SpecialistSelection(
        mode=mode,
        specialist_id=specialist_id,
        candidate_specialist_ids=candidate_specialist_ids,
    )


def _parse_candidate_ids(value: Any, *, route_target_id: str) -> tuple[str, ...]:
    if value is None:
        return ()
    raw_ids = _read_list(value, f"Route target {route_target_id} candidate_specialist_ids")
    candidate_ids = tuple(
        _read_non_empty_string(candidate_id, f"Route target {route_target_id} candidate_specialist_ids")
        for candidate_id in raw_ids
    )
    duplicates = sorted({candidate_id for candidate_id in candidate_ids if candidate_ids.count(candidate_id) > 1})
    if duplicates:
        raise RoutingRegistryError(
            f"Route target {route_target_id} candidate_specialist_ids contains duplicates: {', '.join(duplicates)}"
        )
    return candidate_ids


def _parse_publish_policy(value: Any, *, route_target_id: str, kind: str) -> PublishPolicy:
    raw = _read_dict(value, f"Route target {route_target_id} publish_policy")
    _validate_exact_keys(raw, _PUBLISH_POLICY_KEYS, f"Route target {route_target_id} publish_policy")

    allow_auto_publish = _read_bool(
        raw["allow_auto_publish"],
        f"Route target {route_target_id} publish_policy.allow_auto_publish",
    )
    min_response_confidence = _read_non_empty_string(
        raw["min_response_confidence_for_auto_publish"],
        f"Route target {route_target_id} publish_policy.min_response_confidence_for_auto_publish",
    )
    if min_response_confidence not in RESPONSE_CONFIDENCE_VALUES:
        raise RoutingRegistryError(
            f"Route target {route_target_id} has invalid min_response_confidence_for_auto_publish: {min_response_confidence}"
        )
    max_risk_level = _read_non_empty_string(
        raw["max_risk_level_for_auto_publish"],
        f"Route target {route_target_id} publish_policy.max_risk_level_for_auto_publish",
    )
    if max_risk_level not in RISK_LEVEL_VALUES:
        raise RoutingRegistryError(f"Route target {route_target_id} has invalid max_risk_level_for_auto_publish: {max_risk_level}")
    allow_draft_for_human = _read_bool(
        raw["allow_draft_for_human"],
        f"Route target {route_target_id} publish_policy.allow_draft_for_human",
    )
    allow_manual_only = _read_bool(
        raw["allow_manual_only"],
        f"Route target {route_target_id} publish_policy.allow_manual_only",
    )
    if kind == "human_assist" and allow_auto_publish:
        raise RoutingRegistryError(f"Human-assist route target {route_target_id} must not allow auto publish")
    if not allow_draft_for_human and not allow_manual_only:
        raise RoutingRegistryError(
            f"Route target {route_target_id} publish_policy must allow at least one human fallback"
        )
    return PublishPolicy(
        allow_auto_publish=allow_auto_publish,
        min_response_confidence_for_auto_publish=min_response_confidence,
        max_risk_level_for_auto_publish=max_risk_level,
        allow_draft_for_human=allow_draft_for_human,
        allow_manual_only=allow_manual_only,
    )


def _load_spec(spec_id: str, *, expected_kind: str, context: str) -> AgentSpec:
    try:
        spec = load_agent_spec(spec_id)
    except AgentSpecError as exc:
        raise RoutingRegistryError(f"{context} references missing spec {spec_id}") from exc
    if spec.kind != expected_kind:
        raise RoutingRegistryError(f"{context} must reference a {expected_kind} spec, got {spec.kind}: {spec_id}")
    return spec


def _index_unique(items: tuple[Any, ...], *, kind_label: str) -> dict[str, Any]:
    indexed: dict[str, Any] = {}
    duplicates: list[str] = []
    for item in items:
        item_id = item.id
        if item_id in indexed:
            duplicates.append(item_id)
            continue
        indexed[item_id] = item
    if duplicates:
        duplicate_ids = ", ".join(sorted(set(duplicates)))
        raise RoutingRegistryError(f"Routing registry contains duplicate {kind_label} ids: {duplicate_ids}")
    return indexed


def _validate_route_target_cross_references(
    route_target: RouteTarget,
    specialists_by_id: dict[str, SpecialistRegistration],
) -> None:
    selection = route_target.handler.specialist_selection

    if selection.mode == "fixed":
        specialist = specialists_by_id.get(selection.specialist_id or "")
        if specialist is None:
            raise RoutingRegistryError(
                f"Route target {route_target.id} references unknown specialist_id: {selection.specialist_id}"
            )
        _validate_specialist_allowed_for_target(route_target, specialist)
        return

    if selection.mode == "auto":
        if selection.candidate_specialist_ids:
            candidates = []
            for specialist_id in selection.candidate_specialist_ids:
                specialist = specialists_by_id.get(specialist_id)
                if specialist is None:
                    raise RoutingRegistryError(
                        f"Route target {route_target.id} references unknown candidate specialist_id: {specialist_id}"
                    )
                candidates.append(specialist)
            if route_target.enabled:
                disabled = tuple(candidate.id for candidate in candidates if not candidate.enabled)
                if disabled:
                    raise RoutingRegistryError(
                        f"Enabled route target {route_target.id} references disabled candidate specialists: {', '.join(disabled)}"
                    )
                if route_target.kind == "human_assist":
                    ineligible = tuple(candidate.id for candidate in candidates if not candidate.can_assist_human)
                    if ineligible:
                        raise RoutingRegistryError(
                            f"Human-assist route target {route_target.id} references non-human-assist specialists: {', '.join(ineligible)}"
                        )
            return

        if route_target.kind != "human_assist":
            raise RoutingRegistryError(
                f"Direct-AI route target {route_target.id} with mode=auto must define candidate_specialist_ids"
            )
        if route_target.enabled and not any(
            specialist.enabled and specialist.can_assist_human for specialist in specialists_by_id.values()
        ):
            raise RoutingRegistryError(
                f"Enabled human-assist route target {route_target.id} with mode=auto has no eligible human-assist specialists"
            )
        return

    if route_target.kind != "human_assist":
        raise RoutingRegistryError(f"Route target {route_target.id} with mode=none must use kind=human_assist")


def _validate_specialist_allowed_for_target(route_target: RouteTarget, specialist: SpecialistRegistration) -> None:
    if route_target.enabled and not specialist.enabled:
        raise RoutingRegistryError(
            f"Enabled route target {route_target.id} references disabled specialist {specialist.id}"
        )
    if route_target.kind == "human_assist" and route_target.enabled and not specialist.can_assist_human:
        raise RoutingRegistryError(
            f"Enabled human-assist route target {route_target.id} references non-human-assist specialist {specialist.id}"
        )
