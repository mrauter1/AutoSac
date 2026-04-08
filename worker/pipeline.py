from __future__ import annotations

from dataclasses import dataclass

from shared.config import Settings
from shared.routing_registry import RouteTarget, SpecialistRegistration, load_routing_registry
from worker.output_contracts import RouterResult, SpecialistResult, SpecialistSelectorResult
from worker.step_runner import (
    StepRunError,
    StepRunResult,
    execute_step,
    prepare_step_run,
    record_synthetic_step_success,
    write_run_manifest_snapshot,
)
from worker.ticket_loader import LoadedTicketContext


@dataclass(frozen=True)
class PipelineExecutionResult:
    route_target: RouteTarget
    router_step: StepRunResult
    router_result: RouterResult
    selector_step: StepRunResult | None
    selector_result: SpecialistSelectorResult | None
    specialist_step: StepRunResult | None
    specialist_result: SpecialistResult | None
    selected_specialist: SpecialistRegistration | None

    @property
    def final_step(self) -> StepRunResult:
        if self.specialist_step is not None:
            return self.specialist_step
        if self.selector_step is not None:
            return self.selector_step
        return self.router_step


def _run_router(
    settings: Settings,
    *,
    run_id,
    ticket_id,
    worker_instance_id: str,
    context: LoadedTicketContext,
) -> tuple[StepRunResult, RouterResult]:
    registry = load_routing_registry()
    prepared_router = prepare_step_run(
        settings,
        run_id=run_id,
        ticket_id=ticket_id,
        worker_instance_id=worker_instance_id,
        step_index=1,
        step_kind="router",
        spec=registry.router_spec,
        context=context,
    )
    router_step = execute_step(settings, prepared=prepared_router)
    write_run_manifest_snapshot(settings, run_id=run_id)
    return router_step, RouterResult.model_validate(router_step.output_payload)


def _run_selector(
    settings: Settings,
    *,
    run_id,
    ticket_id,
    worker_instance_id: str,
    context: LoadedTicketContext,
    route_target: RouteTarget,
    router_result: RouterResult,
) -> tuple[StepRunResult, SpecialistSelectorResult, SpecialistRegistration]:
    registry = load_routing_registry()
    selector_spec = registry.selector_spec
    if selector_spec is None:
        raise StepRunError(f"Route target {route_target.id} requires selector execution but selector_spec_id is not configured")
    candidate_specialists = registry.candidate_specialists_for_target(route_target.id)
    candidate_specialist_ids = tuple(specialist.id for specialist in candidate_specialists)
    prepared_selector = prepare_step_run(
        settings,
        run_id=run_id,
        ticket_id=ticket_id,
        worker_instance_id=worker_instance_id,
        step_index=2,
        step_kind="selector",
        spec=selector_spec,
        context=context,
        router_result=router_result,
        target_route_target_id=route_target.id,
        candidate_specialist_ids=candidate_specialist_ids,
    )
    selector_step = execute_step(settings, prepared=prepared_selector)
    write_run_manifest_snapshot(settings, run_id=run_id)
    selector_result = SpecialistSelectorResult.model_validate(selector_step.output_payload)
    return selector_step, selector_result, registry.require_specialist(selector_result.specialist_id)


def _run_forced_router(
    settings: Settings,
    *,
    run_id,
    ticket_id,
    worker_instance_id: str,
    route_target: RouteTarget,
    specialist: SpecialistRegistration,
) -> tuple[StepRunResult, RouterResult]:
    registry = load_routing_registry()
    router_result = RouterResult(
        route_target_id=route_target.id,
        routing_rationale=(
            f"Forced by ops manual rerun to route target {route_target.id} "
            f"with specialist {specialist.id}."
        ),
    )
    router_step = record_synthetic_step_success(
        settings,
        run_id=run_id,
        ticket_id=ticket_id,
        worker_instance_id=worker_instance_id,
        step_index=1,
        step_kind="router",
        spec=registry.router_spec,
        output_payload=router_result.model_dump(),
        prompt_text=(
            "Synthetic router step.\n\n"
            f"Route target was forced to {route_target.id} ({route_target.label}).\n"
            f"Specialist was forced to {specialist.id} ({specialist.display_name})."
        ),
        route_target_id=route_target.id,
        selected_specialist_id=specialist.id,
    )
    write_run_manifest_snapshot(settings, run_id=run_id)
    return router_step, router_result


def _run_specialist(
    settings: Settings,
    *,
    run_id,
    ticket_id,
    worker_instance_id: str,
    context: LoadedTicketContext,
    route_target: RouteTarget,
    router_result: RouterResult,
    specialist: SpecialistRegistration,
    step_index: int,
) -> tuple[StepRunResult, SpecialistResult]:
    prepared_specialist = prepare_step_run(
        settings,
        run_id=run_id,
        ticket_id=ticket_id,
        worker_instance_id=worker_instance_id,
        step_index=step_index,
        step_kind="specialist",
        spec=specialist.spec,
        context=context,
        router_result=router_result,
        target_route_target_id=route_target.id,
        selected_specialist_id=specialist.id,
    )
    specialist_step = execute_step(settings, prepared=prepared_specialist)
    write_run_manifest_snapshot(settings, run_id=run_id)
    return specialist_step, SpecialistResult.model_validate(specialist_step.output_payload)


def execute_triage_pipeline(
    settings: Settings,
    *,
    run_id,
    ticket_id,
    worker_instance_id: str,
    context: LoadedTicketContext,
    forced_route_target_id: str | None = None,
    forced_specialist_id: str | None = None,
) -> PipelineExecutionResult:
    registry = load_routing_registry()
    if (forced_route_target_id is None) != (forced_specialist_id is None):
        raise StepRunError("Forced reruns require both forced_route_target_id and forced_specialist_id")
    if forced_route_target_id is not None and forced_specialist_id is not None:
        choice = registry.resolve_forced_manual_rerun_choice(
            route_target_id=forced_route_target_id,
            specialist_id=forced_specialist_id,
        )
        route_target = registry.require_route_target(choice.route_target_id)
        selected_specialist = registry.require_specialist(choice.specialist_id)
        router_step, router_result = _run_forced_router(
            settings,
            run_id=run_id,
            ticket_id=ticket_id,
            worker_instance_id=worker_instance_id,
            route_target=route_target,
            specialist=selected_specialist,
        )
        specialist_step, specialist_result = _run_specialist(
            settings,
            run_id=run_id,
            ticket_id=ticket_id,
            worker_instance_id=worker_instance_id,
            context=context,
            route_target=route_target,
            router_result=router_result,
            specialist=selected_specialist,
            step_index=2,
        )
        return PipelineExecutionResult(
            route_target=route_target,
            router_step=router_step,
            router_result=router_result,
            selector_step=None,
            selector_result=None,
            specialist_step=specialist_step,
            specialist_result=specialist_result,
            selected_specialist=selected_specialist,
        )
    router_step, router_result = _run_router(
        settings,
        run_id=run_id,
        ticket_id=ticket_id,
        worker_instance_id=worker_instance_id,
        context=context,
    )
    route_target = registry.require_enabled_route_target(router_result.route_target_id)

    selection = route_target.handler.specialist_selection
    selector_step = None
    selector_result = None
    specialist_step = None
    specialist_result = None
    selected_specialist = None

    if selection.mode == "fixed":
        specialist_id = selection.specialist_id
        if specialist_id is None:
            raise StepRunError(f"Route target {route_target.id} is missing a fixed specialist_id")
        selected_specialist = registry.require_specialist(specialist_id)
        specialist_step, specialist_result = _run_specialist(
            settings,
            run_id=run_id,
            ticket_id=ticket_id,
            worker_instance_id=worker_instance_id,
            context=context,
            route_target=route_target,
            router_result=router_result,
            specialist=selected_specialist,
            step_index=2,
        )
    elif selection.mode == "auto":
        selector_step, selector_result, selected_specialist = _run_selector(
            settings,
            run_id=run_id,
            ticket_id=ticket_id,
            worker_instance_id=worker_instance_id,
            context=context,
            route_target=route_target,
            router_result=router_result,
        )
        specialist_step, specialist_result = _run_specialist(
            settings,
            run_id=run_id,
            ticket_id=ticket_id,
            worker_instance_id=worker_instance_id,
            context=context,
            route_target=route_target,
            router_result=router_result,
            specialist=selected_specialist,
            step_index=3,
        )
    elif selection.mode != "none":
        raise StepRunError(f"Unsupported specialist selection mode: {selection.mode}")

    return PipelineExecutionResult(
        route_target=route_target,
        router_step=router_step,
        router_result=router_result,
        selector_step=selector_step,
        selector_result=selector_result,
        specialist_step=specialist_step,
        specialist_result=specialist_result,
        selected_specialist=selected_specialist,
    )
