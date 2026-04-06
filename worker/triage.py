from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Literal
import uuid

from shared.agent_specs import PIPELINE_VERSION
from shared.config import Settings
from shared.db import session_scope
from shared.models import AIRun, Ticket
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
from worker.output_contracts import TriageResult
from worker.pipeline import execute_triage_pipeline
from worker.step_runner import StepRunError, write_run_manifest_snapshot
from worker.ticket_loader import LoadedTicketContext, load_ticket_context
from worker.triage_validation import TriageResultError, validate_triage_result


@dataclass(frozen=True)
class PreparedRunContext:
    run_id: uuid.UUID
    ticket_id: uuid.UUID
    input_hash: str
    context: LoadedTicketContext


@dataclass(frozen=True)
class ResolvedTriageOutcome:
    run_status: Literal["succeeded", "human_review"]
    effective_action: Literal[
        "ask_clarification",
        "auto_public_reply",
        "auto_confirm_and_route",
        "draft_public_reply",
        "route_dev_ti",
    ]
    warning_text: str | None = None


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


def _clarification_public_reply(result: TriageResult) -> str:
    public_reply = result.public_reply_markdown.strip()
    if public_reply:
        return public_reply
    questions = [question.strip() for question in result.clarifying_questions if question.strip()]
    if not questions:
        raise TriageResultError("ask_clarification requires a non-empty public reply or clarifying questions")
    bullet_list = "\n".join(f"- {question}" for question in questions)
    return "\n".join(
        [
            "I need a bit more detail before I can answer confidently. Please clarify:",
            "",
            bullet_list,
        ]
    )


def _auto_action_warnings(
    result: TriageResult,
) -> list[str]:
    warnings: list[str] = []
    if result.needs_clarification:
        warnings.append("the result still requires clarification from the requester")
    if result.clarifying_questions:
        warnings.append("clarifying questions were included in the output")
    if not result.auto_public_reply_allowed:
        warnings.append("the reply was not marked safe for automatic publication")
    return warnings


def _format_human_review_warning(summary: str, details: list[str] | None = None) -> str:
    if not details:
        return summary
    return f"{summary} Warning: {'; '.join(details)}."


def _resolve_human_review_outcome(
    result: TriageResult,
    *,
    summary: str,
    details: list[str] | None = None,
) -> ResolvedTriageOutcome:
    combined_details: list[str] = []
    if result.human_review_reason.strip():
        combined_details.append(result.human_review_reason.strip())
    if details:
        combined_details.extend(details)
    return ResolvedTriageOutcome(
        run_status="human_review",
        effective_action="draft_public_reply",
        warning_text=_format_human_review_warning(summary, combined_details),
    )


def _resolve_internal_requester_outcome(
    result: TriageResult,
    *,
    effective_action: str,
) -> ResolvedTriageOutcome:
    if effective_action == "draft_public_reply":
        return ResolvedTriageOutcome(run_status="succeeded", effective_action="auto_public_reply")
    if effective_action == "route_dev_ti" and result.public_reply_markdown.strip():
        return ResolvedTriageOutcome(run_status="succeeded", effective_action="auto_confirm_and_route")
    if effective_action in {"ask_clarification", "auto_public_reply", "auto_confirm_and_route", "route_dev_ti"}:
        return ResolvedTriageOutcome(run_status="succeeded", effective_action=effective_action)
    raise TriageResultError(f"Unsupported recommended_next_action: {effective_action}")


def resolve_triage_outcome(
    ticket: Ticket,
    result: TriageResult,
    settings: Settings,
    *,
    requester_can_view_internal_messages: bool = False,
) -> ResolvedTriageOutcome:
    action = _effective_next_action(ticket, result)

    if result.misuse_or_safety_risk:
        return _resolve_human_review_outcome(
            result,
            summary="Human review required: misuse or safety risk was identified.",
        )

    if requester_can_view_internal_messages:
        return _resolve_internal_requester_outcome(result, effective_action=action)

    if result.recommended_next_action == "ask_clarification" and action == "route_dev_ti":
        return _resolve_human_review_outcome(
            result,
            summary="Human review required: clarification limit reached, so the ticket was routed to Dev/TI.",
        )
    if action == "ask_clarification":
        return ResolvedTriageOutcome(
            run_status="succeeded",
            effective_action="ask_clarification",
        )
    if action == "draft_public_reply":
        return _resolve_human_review_outcome(
            result,
            summary="Human review required: AI prepared a draft public reply for manual approval.",
        )
    if action == "route_dev_ti":
        return _resolve_human_review_outcome(
            result,
            summary="Human review required: the ticket was routed to Dev/TI.",
        )
    if action == "auto_public_reply":
        warnings = _auto_action_warnings(result)
        if warnings:
            return _resolve_human_review_outcome(
                result,
                summary="Human review required: automatic public reply was downgraded.",
                details=warnings,
            )
        return ResolvedTriageOutcome(run_status="succeeded", effective_action="auto_public_reply")
    if action == "auto_confirm_and_route":
        warnings = _auto_action_warnings(result)
        if warnings:
            return _resolve_human_review_outcome(
                result,
                summary="Human review required: automatic confirm-and-route was downgraded.",
                details=warnings,
            )
        return ResolvedTriageOutcome(run_status="succeeded", effective_action="auto_confirm_and_route")
    raise TriageResultError(f"Unsupported recommended_next_action: {action}")


def _prepare_run(settings: Settings, *, run_id) -> PreparedRunContext | None:
    with session_scope(settings) as db:
        run = db.get(AIRun, run_id)
        if run is None or run.status != "running":
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
            input_hash=input_hash,
            context=context,
        )


def _effective_next_action(ticket: Ticket, result: TriageResult) -> str:
    if result.recommended_next_action == "ask_clarification" and ticket.clarification_rounds >= 2:
        return "route_dev_ti"
    return result.recommended_next_action


def _mark_superseded_due_to_stale_input(db, *, run: AIRun, ticket: Ticket) -> None:
    if not ticket.requeue_requested:
        request_requeue(ticket, ticket.requeue_trigger or "requester_reply")
    run.status = "superseded"
    run.ended_at = utc_now()
    run.error_text = None
    process_deferred_requeue(db, ticket=ticket)


def _human_review_internal_note(result: TriageResult, *, extra_reason: str | None = None) -> str:
    internal_note = result.internal_note_markdown.strip()
    if internal_note:
        if extra_reason:
            return "\n\n".join((internal_note, f"Additional review reason: {extra_reason}"))
        return internal_note

    note_parts = [result.summary_internal.strip()]
    if result.human_review_reason.strip():
        note_parts.append(f"Human review reason: {result.human_review_reason.strip()}")
    if extra_reason:
        note_parts.append(f"Additional review reason: {extra_reason}")
    if result.answer_scope == "document_scoped" and result.evidence_status == "not_found_low_risk_guess":
        note_parts.append(
            "No confirming evidence was found in manuals/ or app/. Treat any suggested answer as a low-risk best guess."
        )
    if result.relevant_paths:
        checked_paths = ", ".join(path.path for path in result.relevant_paths)
        note_parts.append(f"Relevant paths checked: {checked_paths}")
    if result.clarifying_questions:
        note_parts.append("Open questions: " + "; ".join(question.strip() for question in result.clarifying_questions if question.strip()))
    return "\n\n".join(part for part in note_parts if part)


def _human_review_public_reply(result: TriageResult) -> str:
    public_reply = result.public_reply_markdown.strip()
    if public_reply:
        return public_reply
    return "Thanks for the details. The internal team is reviewing this request and will follow up."


def _apply_success_result(
    settings: Settings,
    *,
    run_id,
    result: TriageResult,
    final_step_id,
    final_agent_spec_id: str,
    final_output_contract: str,
    final_output_json: dict[str, object],
    final_model_name: str | None,
    force_human_review_reason: str | None = None,
) -> None:
    should_write_manifest = False
    with session_scope(settings) as db:
        run = db.get(AIRun, run_id)
        if run is None:
            return
        context = load_ticket_context(db, run.ticket_id)
        publication_hash = build_requester_visible_fingerprint(context)

        if context.ticket.requeue_requested or publication_hash != run.input_hash:
            _mark_superseded_due_to_stale_input(db, run=run, ticket=context.ticket)
            should_write_manifest = True
        else:
            completed_at = utc_now()
            if force_human_review_reason:
                outcome = ResolvedTriageOutcome(
                    run_status="human_review",
                    effective_action="draft_public_reply",
                    warning_text=force_human_review_reason,
                )
            else:
                outcome = resolve_triage_outcome(
                    context.ticket,
                    result,
                    settings,
                    requester_can_view_internal_messages=context.requester_can_view_internal_messages,
                )
            apply_ai_route_target(
                context.ticket,
                route_target_id=result.ticket_class,
                requester_language=result.requester_language,
            )
            # Legacy ops screens still read these compatibility-era fields until the route-target cutover lands.
            context.ticket.ai_confidence = result.confidence
            context.ticket.impact_level = result.impact_level
            context.ticket.development_needed = result.development_needed
            internal_note_markdown = result.internal_note_markdown.strip()
            if outcome.run_status == "human_review":
                internal_note_markdown = _human_review_internal_note(result, extra_reason=force_human_review_reason)

            run.pipeline_version = PIPELINE_VERSION
            run.final_step_id = final_step_id
            run.final_agent_spec_id = final_agent_spec_id
            run.final_output_contract = final_output_contract
            run.final_output_json = final_output_json
            run.model_name = final_model_name

            if internal_note_markdown:
                publish_ai_internal_note(
                    db,
                    ticket=context.ticket,
                    ai_run_id=run.id,
                    body_markdown=internal_note_markdown,
                    created_at=completed_at,
                )

            action = outcome.effective_action
            if action == "ask_clarification":
                clarification_reply = _clarification_public_reply(result)
                publish_ai_public_reply(
                    db,
                    ticket=context.ticket,
                    ai_run_id=run.id,
                    body_markdown=clarification_reply,
                    next_status="waiting_on_user",
                    last_ai_action="ask_clarification",
                    increment_clarification_rounds=True,
                    created_at=completed_at,
                )
            elif action == "auto_public_reply":
                publish_ai_public_reply(
                    db,
                    ticket=context.ticket,
                    ai_run_id=run.id,
                    body_markdown=result.public_reply_markdown,
                    next_status="waiting_on_user",
                    last_ai_action="auto_public_reply",
                    created_at=completed_at,
                )
            elif action == "auto_confirm_and_route":
                publish_ai_public_reply(
                    db,
                    ticket=context.ticket,
                    ai_run_id=run.id,
                    body_markdown=result.public_reply_markdown,
                    next_status="waiting_on_dev_ti",
                    last_ai_action="auto_confirm_and_route",
                    created_at=completed_at,
                )
            elif action == "draft_public_reply":
                create_ai_draft(
                    db,
                    ticket=context.ticket,
                    ai_run_id=run.id,
                    body_markdown=_human_review_public_reply(result),
                    created_at=completed_at,
                )
            elif action == "route_dev_ti":
                route_ticket_after_ai(
                    db,
                    ticket=context.ticket,
                    next_status="waiting_on_dev_ti",
                    last_ai_action="route_dev_ti",
                    created_at=completed_at,
                )

            context.ticket.last_processed_hash = publication_hash
            run.status = outcome.run_status
            run.ended_at = completed_at
            run.error_text = outcome.warning_text
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


def _mark_failed(settings: Settings, *, run_id, error_text: str) -> None:
    should_write_manifest = False
    with session_scope(settings) as db:
        run = db.get(AIRun, run_id)
        if run is None:
            return
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


def process_ai_run(settings: Settings, *, run_id) -> None:
    terminal_run_id = run_id
    try:
        prepared = _prepare_run(settings, run_id=run_id)
        if prepared is None:
            return
        terminal_run_id = prepared.run_id
        write_run_manifest_snapshot(settings, run_id=prepared.run_id)
        pipeline_result = execute_triage_pipeline(
            settings,
            run_id=prepared.run_id,
            ticket_id=prepared.ticket_id,
            context=prepared.context,
        )
        _apply_success_result(
            settings,
            run_id=prepared.run_id,
            result=pipeline_result.triage_result,
            final_step_id=pipeline_result.specialist_step.step_id,
            final_agent_spec_id=pipeline_result.specialist_step.prepared.spec.id,
            final_output_contract=pipeline_result.specialist_step.prepared.spec.output_contract,
            final_output_json=pipeline_result.specialist_step.output_payload,
            final_model_name=pipeline_result.specialist_step.prepared.model_name,
            force_human_review_reason=pipeline_result.force_human_review_reason,
        )
    except (StepRunError, TriageResultError) as exc:
        _mark_failed(settings, run_id=terminal_run_id, error_text=str(exc))
    except Exception as exc:
        _mark_failed(settings, run_id=terminal_run_id, error_text=f"Unexpected worker error: {exc}")
