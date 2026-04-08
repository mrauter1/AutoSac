from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Literal
import uuid

from shared.agent_specs import PIPELINE_VERSION
from shared.config import Settings
from shared.db import session_scope
from shared.logging import log_worker_event
from shared.models import AIRun, Ticket
from shared.permissions import ADMIN_ROLE, DEV_TI_ROLE
from shared.security import utc_now
from shared.ticketing import (
    apply_ai_route_target,
    create_ai_draft,
    process_deferred_requeue,
    publish_ai_failure_note,
    publish_ai_internal_note,
    publish_ai_public_reply,
    record_status_change,
    request_requeue,
    route_ticket_after_ai,
)
from worker.output_contracts import HumanHandoffResult, SpecialistResult
from worker.pipeline import PipelineExecutionResult, execute_triage_pipeline
from worker.publication_policy import PublicationDecision, PublicationPolicyError, resolve_effective_publication_mode
from worker.run_ownership import RunOwnershipLost, load_owned_running_run
from worker.step_runner import StepRunError, write_run_manifest_snapshot
from worker.ticket_loader import LoadedTicketContext, load_ticket_context


@dataclass(frozen=True)
class PreparedRunContext:
    run_id: uuid.UUID
    ticket_id: uuid.UUID
    worker_instance_id: str
    input_hash: str
    context: LoadedTicketContext
    forced_route_target_id: str | None
    forced_specialist_id: str | None


@dataclass(frozen=True)
class ResolvedRunOutcome:
    run_status: Literal["succeeded", "human_review"]
    effective_publication_mode: Literal["auto_publish", "draft_for_human", "manual_only"]
    public_reply_markdown: str
    internal_note_markdown: str
    next_status: str
    last_ai_action: str


def _is_internal_requester(context: LoadedTicketContext) -> bool:
    return context.requester_role in {DEV_TI_ROLE, ADMIN_ROLE}


def build_requester_visible_fingerprint(context: LoadedTicketContext) -> str:
    payload = {
        "title": context.ticket.title,
        "urgent": context.ticket.urgent,
        "status": context.ticket.status,
        "requester_can_view_internal_messages": context.requester_can_view_internal_messages,
        "public_messages": [
            {
                "body_text": message.body_text,
                "author_type": message.author_type,
                "source": message.source,
            }
            for message in context.public_messages
        ],
        "attachment_sha256": [attachment.sha256 for attachment in context.public_attachments],
        "attachment_count": len(context.public_attachments),
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _prepare_run(settings: Settings, *, run_id, worker_instance_id: str) -> PreparedRunContext | None:
    with session_scope(settings) as db:
        run = load_owned_running_run(
            db,
            run_id=run_id,
            worker_instance_id=worker_instance_id,
        )
        if run is None:
            return None

        context = load_ticket_context(db, run.ticket_id)
        current_fingerprint = build_requester_visible_fingerprint(context)

        if run.triggered_by != "manual_rerun" and current_fingerprint == context.ticket.last_processed_hash:
            run.input_hash = current_fingerprint
            run.model_name = settings.codex_model or None
            run.pipeline_version = PIPELINE_VERSION
            run.final_step_id = None
            run.final_agent_spec_id = None
            run.final_output_contract = None
            run.final_output_json = None
            run.error_text = None
            run.status = "skipped"
            run.ended_at = utc_now()
            process_deferred_requeue(db, ticket=context.ticket)
            return None

        if context.ticket.status != "ai_triage":
            record_status_change(
                db,
                ticket=context.ticket,
                to_status="ai_triage",
                changed_by_type="system",
                changed_at=utc_now(),
            )
            context = load_ticket_context(db, run.ticket_id)

        input_hash = build_requester_visible_fingerprint(context)
        run.input_hash = input_hash
        run.model_name = None
        run.pipeline_version = PIPELINE_VERSION
        run.final_step_id = None
        run.final_agent_spec_id = None
        run.final_output_contract = None
        run.final_output_json = None
        run.error_text = None

        return PreparedRunContext(
            run_id=run.id,
            ticket_id=context.ticket.id,
            worker_instance_id=worker_instance_id,
            input_hash=input_hash,
            context=context,
            forced_route_target_id=getattr(run, "forced_route_target_id", None),
            forced_specialist_id=getattr(run, "forced_specialist_id", None),
        )


def _mark_superseded_due_to_stale_input(db, *, run: AIRun, ticket: Ticket) -> None:
    if not ticket.requeue_requested:
        request_requeue(ticket, ticket.requeue_trigger or "requester_reply")
    run.status = "superseded"
    run.ended_at = utc_now()
    run.error_text = None
    process_deferred_requeue(db, ticket=ticket)


def _synthesized_direct_ai_internal_note(result: SpecialistResult) -> str:
    internal_note = result.internal_note_markdown.strip()
    if internal_note:
        return internal_note
    parts = [
        result.summary_internal.strip(),
        f"Risk level: {result.risk_level}",
        f"Risk reason: {result.risk_reason.strip()}",
    ]
    return "\n\n".join(part for part in parts if part)


def _synthesized_human_assist_internal_note(result: SpecialistResult, *, router_rationale: str) -> str:
    internal_note = result.internal_note_markdown.strip()
    if internal_note:
        return internal_note
    parts = [
        result.summary_internal.strip(),
        f"Risk level: {result.risk_level}",
        f"Risk reason: {result.risk_reason.strip()}",
        f"Router rationale: {router_rationale.strip()}",
    ]
    return "\n\n".join(part for part in parts if part)


def _synthesized_handoff_result(pipeline_result: PipelineExecutionResult) -> HumanHandoffResult:
    route_target = pipeline_result.route_target
    router_result = pipeline_result.router_result
    summary_internal = f"Route to {route_target.label} for human handling."
    internal_note = "\n\n".join(
        [
            summary_internal,
            f"Route target description: {route_target.router_description}",
            f"Router rationale: {router_result.routing_rationale}",
        ]
    )
    return HumanHandoffResult(
        route_target_id=route_target.id,
        handoff_reason=route_target.router_description,
        summary_internal=summary_internal,
        internal_note_markdown=internal_note,
        public_reply_markdown="",
        assistant_used=False,
        assistant_specialist_id=None,
    )


def _resolve_specialist_outcome(
    pipeline_result: PipelineExecutionResult,
    specialist_result: SpecialistResult,
) -> tuple[ResolvedRunOutcome, PublicationDecision]:
    route_target = pipeline_result.route_target
    decision = resolve_effective_publication_mode(route_target, specialist_result)
    public_reply_markdown = specialist_result.public_reply_markdown.strip()
    effective_public_reply_markdown = public_reply_markdown if decision.effective_mode != "manual_only" else ""

    if route_target.kind == "direct_ai":
        if decision.effective_mode == "auto_publish":
            return (
                ResolvedRunOutcome(
                    run_status="succeeded",
                    effective_publication_mode="auto_publish",
                    public_reply_markdown=effective_public_reply_markdown,
                    internal_note_markdown=specialist_result.internal_note_markdown.strip(),
                    next_status="waiting_on_user",
                    last_ai_action="auto_public_reply",
                ),
                decision,
            )
        return (
            ResolvedRunOutcome(
                run_status="human_review",
                effective_publication_mode=decision.effective_mode,
                public_reply_markdown=effective_public_reply_markdown,
                internal_note_markdown=_synthesized_direct_ai_internal_note(specialist_result),
                next_status="ai_triage",
                last_ai_action="draft_public_reply" if decision.effective_mode == "draft_for_human" else "manual_only",
            ),
            decision,
        )

    human_queue_status = route_target.handler.human_queue_status
    if human_queue_status is None:
        raise PublicationPolicyError(f"Human-assist route target {route_target.id} is missing handler.human_queue_status")
    return (
        ResolvedRunOutcome(
            run_status="human_review",
            effective_publication_mode=decision.effective_mode,
            public_reply_markdown=effective_public_reply_markdown,
            internal_note_markdown=_synthesized_human_assist_internal_note(
                specialist_result,
                router_rationale=pipeline_result.router_result.routing_rationale,
            ),
            next_status=human_queue_status,
            last_ai_action="draft_public_reply" if decision.effective_mode == "draft_for_human" else "manual_only",
        ),
        decision,
    )


def _extract_public_reply_markdown(payload: dict[str, object]) -> str:
    public_reply = payload.get("public_reply_markdown")
    if isinstance(public_reply, str):
        return public_reply.strip()
    return ""


def _override_internal_requester_publication(
    context: LoadedTicketContext,
    *,
    outcome: ResolvedRunOutcome,
    final_output_contract: str,
    final_output_json: dict[str, object],
) -> tuple[ResolvedRunOutcome, dict[str, object]]:
    if not _is_internal_requester(context):
        return outcome, final_output_json

    public_reply_markdown = _extract_public_reply_markdown(final_output_json)
    if not public_reply_markdown:
        return outcome, final_output_json

    normalized_final_output_json = final_output_json
    if final_output_contract == "specialist_result":
        normalized_final_output_json = dict(final_output_json)
        normalized_final_output_json["publish_mode_recommendation"] = "auto_publish"

    return (
        ResolvedRunOutcome(
            run_status="succeeded",
            effective_publication_mode="auto_publish",
            public_reply_markdown=public_reply_markdown,
            internal_note_markdown=outcome.internal_note_markdown,
            next_status="waiting_on_user",
            last_ai_action="auto_public_reply",
        ),
        normalized_final_output_json,
    )


def _apply_success_result(
    settings: Settings,
    *,
    run_id,
    worker_instance_id: str,
    pipeline_result: PipelineExecutionResult,
) -> None:
    should_write_manifest = False
    with session_scope(settings) as db:
        run = load_owned_running_run(
            db,
            run_id=run_id,
            worker_instance_id=worker_instance_id,
        )
        if run is None:
            raise RunOwnershipLost(
                f"Run {run_id} is no longer running for worker {worker_instance_id} during finalization."
            )
        context = load_ticket_context(db, run.ticket_id)
        publication_hash = build_requester_visible_fingerprint(context)

        if context.ticket.requeue_requested or publication_hash != run.input_hash:
            _mark_superseded_due_to_stale_input(db, run=run, ticket=context.ticket)
            should_write_manifest = True
        else:
            completed_at = utc_now()
            route_target = pipeline_result.route_target
            if pipeline_result.specialist_result is None:
                final_output_model = _synthesized_handoff_result(pipeline_result)
                outcome = ResolvedRunOutcome(
                    run_status="human_review",
                    effective_publication_mode="manual_only",
                    public_reply_markdown="",
                    internal_note_markdown=final_output_model.internal_note_markdown,
                    next_status=route_target.handler.human_queue_status or "waiting_on_dev_ti",
                    last_ai_action="manual_only",
                )
                final_output_contract = "human_handoff_result"
                final_output_json = final_output_model.model_dump()
                final_step_id = pipeline_result.final_step.step_id
                final_agent_spec_id = None
                final_model_name = pipeline_result.final_step.prepared.model_name
                requester_language = context.ticket.requester_language
            else:
                specialist_result = pipeline_result.specialist_result
                outcome, _decision = _resolve_specialist_outcome(pipeline_result, specialist_result)
                final_output_contract = pipeline_result.specialist_step.prepared.spec.output_contract
                final_output_json = pipeline_result.specialist_step.output_payload
                final_step_id = pipeline_result.specialist_step.step_id
                final_agent_spec_id = pipeline_result.specialist_step.prepared.spec.id
                final_model_name = pipeline_result.specialist_step.prepared.model_name
                requester_language = specialist_result.requester_language

            outcome, final_output_json = _override_internal_requester_publication(
                context,
                outcome=outcome,
                final_output_contract=final_output_contract,
                final_output_json=final_output_json,
            )

            apply_ai_route_target(
                context.ticket,
                route_target_id=route_target.id,
                requester_language=requester_language,
            )

            run.pipeline_version = PIPELINE_VERSION
            run.final_step_id = final_step_id
            run.final_agent_spec_id = final_agent_spec_id
            run.final_output_contract = final_output_contract
            run.final_output_json = final_output_json
            run.model_name = final_model_name

            if outcome.internal_note_markdown:
                publish_ai_internal_note(
                    db,
                    ticket=context.ticket,
                    ai_run_id=run.id,
                    body_markdown=outcome.internal_note_markdown,
                    created_at=completed_at,
                )

            if outcome.effective_publication_mode == "auto_publish":
                publish_ai_public_reply(
                    db,
                    ticket=context.ticket,
                    ai_run_id=run.id,
                    body_markdown=outcome.public_reply_markdown,
                    next_status=outcome.next_status,
                    last_ai_action=outcome.last_ai_action,
                    created_at=completed_at,
                )
            elif outcome.public_reply_markdown:
                create_ai_draft(
                    db,
                    ticket=context.ticket,
                    ai_run_id=run.id,
                    body_markdown=outcome.public_reply_markdown,
                    next_status=outcome.next_status,
                    last_ai_action=outcome.last_ai_action,
                    created_at=completed_at,
                )
            else:
                route_ticket_after_ai(
                    db,
                    ticket=context.ticket,
                    next_status=outcome.next_status,
                    last_ai_action=outcome.last_ai_action,
                    created_at=completed_at,
                )

            context.ticket.last_processed_hash = publication_hash
            run.status = outcome.run_status
            run.ended_at = completed_at
            run.error_text = None
            process_deferred_requeue(db, ticket=context.ticket)
            should_write_manifest = True
    if should_write_manifest:
        write_run_manifest_snapshot(settings, run_id=run_id)


def _failure_note_body(error_text: str) -> str:
    return "\n".join(
        [
            "AI triage run failed and was routed to Dev/TI for manual review.",
            "",
            f"Error: {error_text}",
        ]
    )


def _mark_failed(settings: Settings, *, run_id, worker_instance_id: str, error_text: str) -> None:
    should_write_manifest = False
    with session_scope(settings) as db:
        run = load_owned_running_run(
            db,
            run_id=run_id,
            worker_instance_id=worker_instance_id,
        )
        if run is None:
            raise RunOwnershipLost(
                f"Run {run_id} is no longer running for worker {worker_instance_id} during failure handling."
            )
        ticket = db.get(Ticket, run.ticket_id)
        failed_at = utc_now()
        if getattr(run, "pipeline_version", None) is None:
            run.pipeline_version = PIPELINE_VERSION
        run.status = "failed"
        run.ended_at = failed_at
        run.error_text = error_text

        if ticket is not None:
            publish_ai_failure_note(
                db,
                ticket=ticket,
                ai_run_id=run.id,
                body_markdown=_failure_note_body(error_text),
                created_at=failed_at,
            )
            if ticket.status != "waiting_on_dev_ti":
                record_status_change(
                    db,
                    ticket=ticket,
                    to_status="waiting_on_dev_ti",
                    changed_by_type="system",
                    changed_at=failed_at,
                )
            process_deferred_requeue(db, ticket=ticket)
        should_write_manifest = True
    if should_write_manifest:
        write_run_manifest_snapshot(settings, run_id=run_id)


def _log_run_ownership_lost(*, run_id, worker_instance_id: str, error_text: str) -> None:
    log_worker_event(
        "run_ownership_lost",
        level="warning",
        run_id=str(run_id),
        worker_instance_id=worker_instance_id,
        error=error_text,
    )


def process_ai_run(settings: Settings, *, run_id, worker_instance_id: str) -> None:
    terminal_run_id = run_id
    try:
        prepared = _prepare_run(settings, run_id=run_id, worker_instance_id=worker_instance_id)
        if prepared is None:
            return
        terminal_run_id = prepared.run_id
        write_run_manifest_snapshot(settings, run_id=prepared.run_id)
        pipeline_result = execute_triage_pipeline(
            settings,
            run_id=prepared.run_id,
            ticket_id=prepared.ticket_id,
            worker_instance_id=prepared.worker_instance_id,
            context=prepared.context,
            forced_route_target_id=getattr(prepared, "forced_route_target_id", None),
            forced_specialist_id=getattr(prepared, "forced_specialist_id", None),
        )
        _apply_success_result(
            settings,
            run_id=prepared.run_id,
            worker_instance_id=prepared.worker_instance_id,
            pipeline_result=pipeline_result,
        )
    except RunOwnershipLost as exc:
        _log_run_ownership_lost(
            run_id=terminal_run_id,
            worker_instance_id=worker_instance_id,
            error_text=str(exc),
        )
    except (StepRunError, PublicationPolicyError) as exc:
        try:
            _mark_failed(
                settings,
                run_id=terminal_run_id,
                worker_instance_id=worker_instance_id,
                error_text=str(exc),
            )
        except RunOwnershipLost as ownership_exc:
            _log_run_ownership_lost(
                run_id=terminal_run_id,
                worker_instance_id=worker_instance_id,
                error_text=str(ownership_exc),
            )
    except Exception as exc:
        try:
            _mark_failed(
                settings,
                run_id=terminal_run_id,
                worker_instance_id=worker_instance_id,
                error_text=f"Unexpected worker error: {exc}",
            )
        except RunOwnershipLost as ownership_exc:
            _log_run_ownership_lost(
                run_id=terminal_run_id,
                worker_instance_id=worker_instance_id,
                error_text=str(ownership_exc),
            )
