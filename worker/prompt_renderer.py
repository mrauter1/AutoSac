from __future__ import annotations

from dataclasses import asdict, dataclass

from shared.agent_specs import AgentSpec, load_specialist_shared_policy_template
from shared.routing_registry import RoutingRegistryError, load_routing_registry
from worker.ticket_loader import LoadedTicketContext
from worker.output_contracts import RouterResult


class PromptRenderError(RuntimeError):
    """Raised when prompt rendering fails."""


@dataclass(frozen=True)
class PromptAttachment:
    attachment_id: str
    original_filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    workspace_path: str
    absolute_path: str
    is_image: bool

    def as_payload(self) -> dict[str, object]:
        return asdict(self)


def _format_messages(messages) -> str:
    if not messages:
        return "(none)"
    blocks: list[str] = []
    for index, message in enumerate(messages, start=1):
        blocks.append(
            "\n".join(
                [
                    f"{index}. author_type={message.author_type}; source={message.source}; created_at={message.created_at.isoformat()}",
                    message.body_text,
                ]
            )
        )
    return "\n\n".join(blocks)


def _format_attachments(attachments: tuple[PromptAttachment, ...]) -> str:
    if not attachments:
        return "(none)"
    blocks: list[str] = []
    for index, attachment in enumerate(attachments, start=1):
        blocks.append(
            "\n".join(
                [
                    (
                        f"{index}. original_filename={attachment.original_filename}; mime_type={attachment.mime_type}; "
                        f"size_bytes={attachment.size_bytes}; sha256={attachment.sha256}; "
                        f"image_attachment={'yes' if attachment.is_image else 'no'}"
                    ),
                    f"   workspace_path={attachment.workspace_path}",
                    f"   absolute_path={attachment.absolute_path}",
                ]
            )
        )
    return "\n\n".join(blocks)


def _base_prompt_values(
    context: LoadedTicketContext,
    *,
    public_attachments: tuple[PromptAttachment, ...],
    attachments_root: str | None,
) -> dict[str, str]:
    return {
        "REFERENCE": context.ticket.reference,
        "TITLE": context.ticket.title,
        "REQUESTER_ROLE": context.requester_role,
        "REQUESTER_CAN_VIEW_INTERNAL_MESSAGES": "yes" if context.requester_can_view_internal_messages else "no",
        "STATUS": context.ticket.status,
        "URGENT": "yes" if context.ticket.urgent else "no",
        "PUBLIC_MESSAGES": _format_messages(context.public_messages),
        "INTERNAL_MESSAGES": _format_messages(context.internal_messages),
        "ATTACHMENTS_ROOT": attachments_root or "(none)",
        "PUBLIC_ATTACHMENTS": _format_attachments(public_attachments),
    }


def _format_route_target_catalog(route_targets) -> str:
    if not route_targets:
        return "(none)"
    return "\n\n".join(
        "\n".join(
            [
                f"- id: {route_target.id}",
                f"  label: {route_target.label}",
                f"  kind: {route_target.kind}",
                f"  description: {route_target.router_description}",
            ]
        )
        for route_target in route_targets
    )


def _format_specialist_catalog(specialists) -> str:
    if not specialists:
        return "(none)"
    return "\n\n".join(
        "\n".join(
            [
                f"- id: {specialist.id}",
                f"  display_name: {specialist.display_name}",
                f"  spec_id: {specialist.spec_id}",
            ]
        )
        for specialist in specialists
    )


def _render_template(template: str, values: dict[str, str], *, label: str, agent_id: str) -> str:
    try:
        return template.format(**values)
    except KeyError as exc:
        raise PromptRenderError(f"Missing {label} placeholder value: {exc.args[0]} for agent {agent_id}") from exc


def render_agent_prompt(
    spec: AgentSpec,
    *,
    context: LoadedTicketContext,
    public_attachments: tuple[PromptAttachment, ...] = (),
    attachments_root: str | None = None,
    router_result: RouterResult | None = None,
    target_route_target_id: str | None = None,
    candidate_specialist_ids: tuple[str, ...] | None = None,
) -> str:
    registry = load_routing_registry()
    resolved_route_target_id = target_route_target_id or (router_result.route_target_id if router_result is not None else None)
    route_target = None
    candidate_specialists = ()
    try:
        if resolved_route_target_id is not None:
            route_target = registry.require_route_target(resolved_route_target_id)
        if candidate_specialist_ids is not None:
            candidate_specialists = tuple(
                registry.require_specialist(specialist_id) for specialist_id in candidate_specialist_ids
            )
        elif spec.kind == "selector" and resolved_route_target_id is not None:
            candidate_specialists = registry.candidate_specialists_for_target(
                resolved_route_target_id,
                requester_role=context.requester_role,
            )
    except RoutingRegistryError as exc:
        raise PromptRenderError(str(exc)) from exc

    if spec.kind in {"selector", "specialist"} and route_target is None:
        raise PromptRenderError(f"Route target context is required for {spec.kind} prompt rendering")

    values = _base_prompt_values(
        context,
        public_attachments=public_attachments,
        attachments_root=attachments_root,
    )
    shared_policy = ""
    if spec.kind == "specialist":
        shared_policy = _render_template(
            load_specialist_shared_policy_template(),
            values,
            label="shared specialist policy",
            agent_id=spec.id,
        )
    values.update(
        {
            "ROUTE_TARGET_CATALOG": _format_route_target_catalog(
                registry.enabled_route_targets_for_requester(context.requester_role)
            ),
            "ROUTE_TARGET_ID": route_target.id if route_target is not None else "(none)",
            "ROUTE_TARGET_LABEL": route_target.label if route_target is not None else "(none)",
            "ROUTE_TARGET_KIND": route_target.kind if route_target is not None else "(none)",
            "ROUTE_TARGET_ROUTER_DESCRIPTION": route_target.router_description if route_target is not None else "(none)",
            "ROUTER_RATIONALE": router_result.routing_rationale if router_result is not None else "(none)",
            "SPECIALIST_CANDIDATE_CATALOG": _format_specialist_catalog(candidate_specialists),
            "SPECIALIST_SHARED_POLICY": shared_policy,
        }
    )
    body = _render_template(spec.prompt_template, values, label="prompt", agent_id=spec.id)
    return f"${spec.skill_id}\n\n{body.strip()}\n"
