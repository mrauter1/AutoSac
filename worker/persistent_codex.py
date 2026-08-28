from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shared.config import Settings
from shared.codex_turns import append_codex_turn_outcome
from shared.db import session_scope
from shared.models import (
    AIRun,
    AIRunStep,
    CodexConversation,
    CodexSession,
    CodexTurn,
    CodexTurnInput,
    CodexTurnItem,
    CodexTurnOutcome,
    CodexTurnSteer,
    Ticket,
)
from shared.security import utc_now
from shared.ticketing import clear_matching_ticket_content_requeue, requeue_request_is_stronger_control
from worker.artifacts import write_step_manifest
from worker.codex_app_server import (
    CodexAppServerClient,
    CodexAppServerAmbiguousError,
    CodexAppServerError,
    CodexAppServerRejectedError,
    CodexAppServerTurnPersistence,
    app_server_input_for_events,
    build_codex_app_server_command,
    classify_app_server_failure,
)
from worker.codex_inputs import (
    OrderedInputEvent,
    PromptConversationState,
    UnsupportedInputBundleError,
    build_prompt_conversation_state,
    hash_input_events,
    load_strictly_unseen_input_events,
)
from worker.output_contracts import OutputContractError, validate_contract_output
from worker.run_ownership import RunOwnershipLost, load_owned_running_run
from worker.step_runner import PreparedStepRun, StepRunError, StepRunResult, _step_manifest_metadata
from worker.ticket_loader import load_ticket_context


_STREAM_JOIN_SECONDS = 5.0
_PROCESS_TERMINATION_GRACE_SECONDS = 5.0
_PROCESS_KILL_WAIT_SECONDS = 5.0
_WRITER_CLEANUP_JOIN_SECONDS = 1.0
_ACTIVE_STEERING_POLL_SECONDS = 0.25
_ACTIVE_STEERING_MIN_REMAINING_SECONDS = 1.0
_ACTIVE_STEERING_MAX_PAYLOAD_BYTES = 64 * 1024


class PersistentCodexNonQuiescentCleanupError(StepRunError):
    """Raised when persistent output cleanup cannot prove durable writers stopped."""


@dataclass(frozen=True)
class PersistentCommandSpec:
    command: list[str]
    env: dict[str, str]
    runtime_codex_home: Path
    resumed: bool


@dataclass(frozen=True)
class PreparedPersistentSpecialistStep:
    step_id: uuid.UUID
    turn_id: uuid.UUID
    conversation_id: uuid.UUID
    session_id: uuid.UUID
    command_spec: PersistentCommandSpec
    transport_kind: str
    stored_thread_id: str | None
    pending_events: tuple[OrderedInputEvent, ...]
    effective_input_hash: str


@dataclass(frozen=True)
class ActiveSteeringChangeToken:
    updated_at: datetime
    status: str
    requeue_requested: bool
    requeue_trigger: str | None
    requeue_source_message_id: uuid.UUID | None


@dataclass
class ActiveSteeringPollState:
    initialized: bool = False
    change_token: ActiveSteeringChangeToken | None = None


@dataclass
class PersistentStreamState:
    next_item_index: int = 1
    usage: dict[str, object] | None = None
    accepted: bool = False
    completed: bool = False
    thread_id: str | None = None
    persistence_error: Exception | None = None
    stop_streams: threading.Event = field(default_factory=threading.Event)
    no_active_stream_writes: threading.Event = field(default_factory=threading.Event)
    active_stream_writes: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.no_active_stream_writes.set()


@dataclass
class PromptWriterState:
    error: Exception | None = None
    finished: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def mark_finished(self, *, error: Exception | None = None) -> None:
        with self.lock:
            self.error = error
            self.finished = True

    def captured_error(self) -> Exception | None:
        with self.lock:
            return self.error


def build_persistent_codex_command(
    settings: Settings,
    *,
    prepared: PreparedStepRun,
    thread_id: str | None,
) -> PersistentCommandSpec:
    runtime_codex_home = settings.resolved_codex_home
    command = [
        settings.codex_bin,
        "--ask-for-approval",
        "never",
        "exec",
    ]
    if thread_id:
        command.extend(["resume"])
    else:
        command.extend(["--sandbox", "read-only"])
    command.extend(
        [
            "--strict-config",
            "-c",
            'sandbox_mode="read-only"',
            "-c",
            'web_search="disabled"',
            "-c",
            "tools.web_search=false",
            "--disable",
            "web_search_request",
            "--disable",
            "standalone_web_search",
            "--json",
            "--output-schema",
            str(prepared.paths.schema_path),
            "--output-last-message",
            str(prepared.paths.final_output_path),
        ]
    )
    if prepared.model_name:
        command.extend(["--model", prepared.model_name])
    for image_path in prepared.image_paths:
        command.extend(["--image", str(image_path)])
    if thread_id:
        command.extend([thread_id, "-"])
    else:
        command.append("-")
    env = os.environ.copy()
    env["CODEX_HOME"] = str(runtime_codex_home)
    if settings.codex_api_key:
        env["CODEX_API_KEY"] = settings.codex_api_key
    else:
        env.pop("CODEX_API_KEY", None)
    return PersistentCommandSpec(
        command=command,
        env=env,
        runtime_codex_home=runtime_codex_home,
        resumed=thread_id is not None,
    )


def _conversation_lease_conflict_reason(
    session: CodexSession,
    *,
    run_id,
    worker_instance_id: str,
    now,
) -> str | None:
    if session.lease_owner_run_id is None:
        return None
    if session.lease_owner_run_id == run_id and session.lease_worker_instance_id == worker_instance_id:
        return None
    if session.lease_expires_at is not None and session.lease_expires_at > now:
        return (
            f"Conversation is already leased by run {session.lease_owner_run_id} "
            f"for worker {session.lease_worker_instance_id}."
        )
    return None


def _next_turn_index(db, *, conversation_id) -> int:
    return int(
        db.execute(
            select(func.coalesce(func.max(CodexTurn.turn_index), 0))
            .where(CodexTurn.conversation_id == conversation_id)
        ).scalar_one()
        or 0
    ) + 1


def _append_turn_outcome(db, *, turn_id, outcome_kind: str, payload_json: dict | None) -> CodexTurnOutcome:
    return append_codex_turn_outcome(
        db,
        turn_id=turn_id,
        outcome_kind=outcome_kind,
        payload_json=payload_json,
    )


def _persist_accepted_initial_inputs(db, *, turn: CodexTurn, events: tuple[OrderedInputEvent, ...], effective_input_hash: str) -> None:
    existing_dedupe_keys = {
        str(dedupe_key)
        for (dedupe_key,) in db.execute(
            select(CodexTurnInput.dedupe_key).where(CodexTurnInput.turn_id == turn.id)
        ).all()
    }
    if existing_dedupe_keys:
        expected = {event.dedupe_key for event in events}
        if existing_dedupe_keys != expected:
            raise StepRunError(f"Codex turn {turn.id} already has a different accepted input set.")
        current_hash = getattr(turn, "effective_input_hash", None)
        if current_hash is not None and current_hash != effective_input_hash:
            raise StepRunError(f"Codex turn {turn.id} already has a different effective input hash.")
        turn.effective_input_hash = current_hash or effective_input_hash
        return
    for input_index, event in enumerate(events, start=1):
        db.add(
            CodexTurnInput(
                id=uuid.uuid4(),
                turn_id=turn.id,
                input_index=input_index,
                event_kind=event.event_kind,
                source_kind=event.source_kind,
                source_id=event.source_id,
                dedupe_key=event.dedupe_key,
                payload_json=event.payload_json,
            )
        )
    turn.effective_input_hash = effective_input_hash


def _accepted_outcome_exists(db, *, turn_id) -> bool:
    return bool(
        db.execute(
            select(func.count())
            .select_from(CodexTurnOutcome)
            .where(CodexTurnOutcome.turn_id == turn_id, CodexTurnOutcome.outcome_kind == "accepted")
        ).scalar_one()
        or 0
    )


def _mark_turn_accepted(
    db,
    *,
    turn: CodexTurn,
    events: tuple[OrderedInputEvent, ...],
    effective_input_hash: str,
    now,
    native_turn_id: str | None = None,
    thread_id: str | None = None,
    event_type: str,
) -> None:
    if native_turn_id is not None and native_turn_id.strip():
        turn.native_turn_id = native_turn_id
    first_acceptance = turn.accepted_at is None
    turn.accepted_at = turn.accepted_at or now
    _persist_accepted_initial_inputs(
        db,
        turn=turn,
        events=events,
        effective_input_hash=effective_input_hash,
    )
    if first_acceptance or not _accepted_outcome_exists(db, turn_id=turn.id):
        _append_turn_outcome(
            db,
            turn_id=turn.id,
            outcome_kind="accepted",
            payload_json={
                "event_type": event_type,
                "thread_id": thread_id,
                "native_turn_id": getattr(turn, "native_turn_id", None),
                "effective_input_hash": getattr(turn, "effective_input_hash", None),
            },
        )


def _json_hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _initial_effective_input_hash(prompt_state: PromptConversationState) -> str:
    """Return the full accepted ticket snapshot hash, not only this turn's delta."""
    return prompt_state.input_hash


def _current_effective_input_hash(
    db,
    *,
    settings: Settings,
    run: AIRun,
    turn: CodexTurn,
    ticket_id,
) -> str:
    # Production uses a real Session. Focused unit fakes do not implement the
    # complete ticket loader/query surface, so retain their local row hash.
    if not isinstance(db, Session):
        return _hash_accepted_turn_inputs(db, turn_id=turn.id)
    context = load_ticket_context(db, ticket_id)
    return build_prompt_conversation_state(
        db,
        context=context,
        run=run,
        feature_enabled=settings.codex_conversations_enabled,
    ).input_hash


def _load_relevant_strictly_unseen_input_events(
    db,
    *,
    settings: Settings,
    run: AIRun,
    conversation_id,
    ticket_id,
) -> tuple[OrderedInputEvent, ...]:
    try:
        context = load_ticket_context(db, ticket_id)
    except Exception:
        if not isinstance(db, Session):
            return ()
        raise
    return load_strictly_unseen_input_events(
        db,
        context=context,
        run=run,
        conversation_id=conversation_id,
        include_turn_summaries=False,
        max_attachment_bytes=settings.max_image_bytes,
    )


def _advance_effective_input_hash_if_frontier_complete(
    db,
    *,
    settings: Settings,
    run: AIRun,
    turn: CodexTurn,
    conversation_id,
    ticket_id,
) -> bool:
    unseen_events = _load_relevant_strictly_unseen_input_events(
        db,
        settings=settings,
        run=run,
        conversation_id=conversation_id,
        ticket_id=ticket_id,
    )
    if unseen_events:
        return False
    turn.effective_input_hash = _current_effective_input_hash(
        db,
        settings=settings,
        run=run,
        turn=turn,
        ticket_id=ticket_id,
    )
    return True


def _hash_accepted_turn_inputs(db, *, turn_id) -> str:
    rows = list(
        db.execute(
            select(
                CodexTurnInput.event_kind,
                CodexTurnInput.source_kind,
                CodexTurnInput.source_id,
                CodexTurnInput.dedupe_key,
                CodexTurnInput.payload_json,
            )
            .where(CodexTurnInput.turn_id == turn_id)
            .order_by(CodexTurnInput.input_index.asc())
        ).all()
    )
    serialized = json.dumps(
        [
            {
                "event_kind": event_kind,
                "source_kind": source_kind,
                "source_id": str(source_id) if source_id is not None else None,
                "dedupe_key": dedupe_key,
                "payload_json": payload_json,
            }
            for event_kind, source_kind, source_id, dedupe_key, payload_json in rows
        ],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _next_turn_input_index(db, *, turn_id) -> int:
    return int(
        db.execute(
            select(func.coalesce(func.max(CodexTurnInput.input_index), 0))
            .where(CodexTurnInput.turn_id == turn_id)
        ).scalar_one()
        or 0
    ) + 1


def _load_locked_ticket(db, *, ticket_id) -> Ticket | None:
    return db.execute(
        select(Ticket)
        .where(Ticket.id == ticket_id)
        .limit(1)
        .with_for_update()
    ).scalar_one_or_none()


def _revalidate_active_steering_locked(
    db,
    *,
    prepared: PreparedStepRun,
    persistent: PreparedPersistentSpecialistStep,
    expected_thread_id: str,
    expected_native_turn_id: str,
    deadline: float,
) -> tuple[object, CodexSession, CodexTurn, object, Ticket]:
    if _remaining_seconds(deadline) < _ACTIVE_STEERING_MIN_REMAINING_SECONDS:
        raise CodexAppServerRejectedError("Active-turn steering deadline budget is exhausted.", error_code="deadline_exhausted")
    run, session, turn, step = _load_locked_owned_runtime_records(db, prepared=prepared, persistent=persistent)
    ticket = _load_locked_ticket(db, ticket_id=prepared.ticket_id)
    if ticket is None:
        raise CodexAppServerRejectedError("Ticket disappeared before active-turn steering.", error_code="ticket_missing")
    if ticket.status != "ai_triage":
        raise CodexAppServerRejectedError("Ticket is no longer in AI Triage.", error_code="ticket_status_changed")
    if getattr(run, "ticket_id", prepared.ticket_id) != prepared.ticket_id:
        raise CodexAppServerRejectedError("Active run no longer belongs to the ticket.", error_code="run_ticket_mismatch")
    if getattr(run, "forced_route_target_id", None) is not None or getattr(run, "forced_specialist_id", None) is not None:
        raise CodexAppServerRejectedError("Forced route or specialist runs cannot be satisfied by steering.", error_code="forced_run")
    if requeue_request_is_stronger_control(ticket):
        raise CodexAppServerRejectedError("A stronger ticket control request is pending.", error_code="stronger_control_request")
    if session.thread_id != expected_thread_id:
        raise CodexAppServerRejectedError("Native thread changed before active-turn steering.", error_code="thread_mismatch")
    if turn is None:
        raise CodexAppServerRejectedError("No active native turn is open for steering.", error_code="no_active_turn")
    if turn.status != "running" or turn.steering_closed_at is not None:
        raise CodexAppServerRejectedError("Native turn is not open for steering.", error_code="steering_closed")
    if turn.native_turn_id != expected_native_turn_id:
        raise CodexAppServerRejectedError("Native turn changed before active-turn steering.", error_code="expected_turn_mismatch")
    return run, session, turn, step, ticket


def _existing_steer_dedupe_keys(db, *, turn_id) -> set[str]:
    return {
        str(dedupe_key)
        for (dedupe_key,) in db.execute(
            select(CodexTurnSteer.dedupe_key).where(CodexTurnSteer.turn_id == turn_id)
        ).all()
    }


def _prepare_steering_receipt(
    settings: Settings,
    *,
    prepared: PreparedStepRun,
    persistent: PreparedPersistentSpecialistStep,
    event: OrderedInputEvent,
    input_payload: list[dict[str, object]],
    thread_id: str,
    native_turn_id: str,
    deadline: float,
) -> uuid.UUID:
    if event.source_id is None:
        raise CodexAppServerRejectedError("Steered ticket-message event has no source id.", error_code="missing_source_id")
    payload_json = {
        "thread_id": thread_id,
        "expected_native_turn_id": native_turn_id,
        "event": {
            "event_kind": event.event_kind,
            "source_kind": event.source_kind,
            "source_id": str(event.source_id),
            "dedupe_key": event.dedupe_key,
        },
        "input": input_payload,
    }
    payload_hash = _json_hash(payload_json)
    with session_scope(settings) as db:
        _run, _session, turn, _step, _ticket = _revalidate_active_steering_locked(
            db,
            prepared=prepared,
            persistent=persistent,
            expected_thread_id=thread_id,
            expected_native_turn_id=native_turn_id,
            deadline=deadline,
        )
        existing = db.execute(
            select(CodexTurnSteer)
            .where(CodexTurnSteer.turn_id == turn.id, CodexTurnSteer.dedupe_key == event.dedupe_key)
            .limit(1)
            .with_for_update()
        ).scalar_one_or_none()
        if existing is not None:
            if existing.status in {"prepared", "sending"}:
                raise CodexAppServerAmbiguousError(
                    f"Unresolved active-turn steering receipt already exists for {event.dedupe_key}.",
                    error_code="unresolved_steer_receipt",
                )
            raise CodexAppServerRejectedError(
                f"Active-turn steering receipt already exists for {event.dedupe_key}.",
                error_code="duplicate_steer_receipt",
            )
        receipt = CodexTurnSteer(
            id=uuid.uuid4(),
            turn_id=turn.id,
            event_kind=event.event_kind,
            source_kind=event.source_kind,
            source_id=event.source_id,
            dedupe_key=event.dedupe_key,
            expected_native_turn_id=native_turn_id,
            payload_json=payload_json,
            payload_hash=payload_hash,
            status="prepared",
            attempted_at=utc_now(),
        )
        db.add(receipt)
        db.flush()
        receipt.status = "sending"
        return receipt.id


def _mark_steering_receipt_terminal(
    settings: Settings,
    *,
    receipt_id,
    status: str,
    error_code: str | None,
    error_text: str | None,
) -> None:
    with session_scope(settings) as db:
        receipt = db.execute(
            select(CodexTurnSteer)
            .where(CodexTurnSteer.id == receipt_id)
            .limit(1)
            .with_for_update()
        ).scalar_one_or_none()
        if receipt is None or receipt.status == "accepted":
            return
        now = utc_now()
        receipt.status = status
        receipt.resolved_at = now
        receipt.error_code = error_code
        receipt.error_text = error_text
        _append_turn_outcome(
            db,
            turn_id=receipt.turn_id,
            outcome_kind=status,
            payload_json={
                "event_type": "turn/steer",
                "receipt_id": str(receipt.id),
                "dedupe_key": receipt.dedupe_key,
                "steering_disposition": status,
                "error_code": error_code,
                "error_text": error_text,
            },
        )


def _has_unconsumed_authorized_ticket_content(
    db,
    *,
    ticket: Ticket,
    run: AIRun,
    conversation_id,
    just_accepted_dedupe_key: str,
    settings: Settings,
) -> bool:
    if ticket.status != "ai_triage":
        return False
    unseen_events = _load_relevant_strictly_unseen_input_events(
        db,
        settings=settings,
        run=run,
        conversation_id=conversation_id,
        ticket_id=ticket.id,
    )
    return any(
        event.source_kind == "ticket_message" and event.dedupe_key != just_accepted_dedupe_key
        for event in unseen_events
    )


def _reject_steering_event(
    settings: Settings,
    *,
    prepared: PreparedStepRun,
    persistent: PreparedPersistentSpecialistStep,
    event: OrderedInputEvent,
    thread_id: str,
    native_turn_id: str,
    deadline: float,
    error_code: str,
    error_text: str,
) -> None:
    if event.source_id is None:
        return
    payload_json = {
        "thread_id": thread_id,
        "expected_native_turn_id": native_turn_id,
        "event": {
            "event_kind": event.event_kind,
            "source_kind": event.source_kind,
            "source_id": str(event.source_id),
            "dedupe_key": event.dedupe_key,
        },
        "error": error_text,
    }
    with session_scope(settings) as db:
        try:
            _run, _session, turn, _step, _ticket = _revalidate_active_steering_locked(
                db,
                prepared=prepared,
                persistent=persistent,
                expected_thread_id=thread_id,
                expected_native_turn_id=native_turn_id,
                deadline=deadline,
            )
        except CodexAppServerRejectedError:
            return
        existing = db.execute(
            select(CodexTurnSteer)
            .where(CodexTurnSteer.turn_id == turn.id, CodexTurnSteer.dedupe_key == event.dedupe_key)
            .limit(1)
            .with_for_update()
        ).scalar_one_or_none()
        if existing is not None:
            return
        now = utc_now()
        receipt = CodexTurnSteer(
            id=uuid.uuid4(),
            turn_id=turn.id,
            event_kind=event.event_kind,
            source_kind=event.source_kind,
            source_id=event.source_id,
            dedupe_key=event.dedupe_key,
            expected_native_turn_id=native_turn_id,
            payload_json=payload_json,
            payload_hash=_json_hash(payload_json),
            status="rejected",
            attempted_at=now,
            resolved_at=now,
            error_code=error_code,
            error_text=error_text,
        )
        db.add(receipt)
        _append_turn_outcome(
            db,
            turn_id=turn.id,
            outcome_kind="rejected",
            payload_json={
                "event_type": "turn/steer",
                "receipt_id": str(receipt.id),
                "dedupe_key": event.dedupe_key,
                "steering_disposition": "rejected",
                "error_code": error_code,
                "error_text": error_text,
            },
        )


def _accept_steering_receipt(
    settings: Settings,
    *,
    prepared: PreparedStepRun,
    persistent: PreparedPersistentSpecialistStep,
    receipt_id,
    event: OrderedInputEvent,
    rpc_request_id: str,
    thread_id: str,
    native_turn_id: str,
    deadline: float,
) -> None:
    with session_scope(settings) as db:
        run, _session, turn, _step, ticket = _revalidate_active_steering_locked(
            db,
            prepared=prepared,
            persistent=persistent,
            expected_thread_id=thread_id,
            expected_native_turn_id=native_turn_id,
            deadline=deadline,
        )
        receipt = db.execute(
            select(CodexTurnSteer)
            .where(CodexTurnSteer.id == receipt_id)
            .limit(1)
            .with_for_update()
        ).scalar_one()
        if receipt.status != "sending":
            raise CodexAppServerAmbiguousError(
                f"Active-turn steering receipt {receipt.id} was {receipt.status} before acknowledgement commit.",
                error_code="receipt_not_sending",
            )
        existing_input = db.execute(
            select(CodexTurnInput.id)
            .where(CodexTurnInput.turn_id == turn.id, CodexTurnInput.dedupe_key == event.dedupe_key)
            .limit(1)
        ).scalar_one_or_none()
        if existing_input is not None:
            raise CodexAppServerAmbiguousError(
                f"CodexTurnInput already exists before accepting steering receipt {receipt.id}.",
                error_code="duplicate_turn_input",
            )
        now = utc_now()
        receipt.status = "accepted"
        receipt.rpc_request_id = rpc_request_id
        receipt.acknowledged_at = now
        receipt.resolved_at = now
        commit_to_ack_latency_ms = int((now - receipt.attempted_at).total_seconds() * 1000) if receipt.attempted_at else None
        db.add(
            CodexTurnInput(
                id=uuid.uuid4(),
                turn_id=turn.id,
                input_index=_next_turn_input_index(db, turn_id=turn.id),
                event_kind=event.event_kind,
                source_kind=event.source_kind,
                source_id=event.source_id,
                dedupe_key=event.dedupe_key,
                payload_json=event.payload_json,
            )
        )
        db.flush()
        if (
            event.source_kind == "ticket_message"
            and event.source_id is not None
            and not _has_unconsumed_authorized_ticket_content(
                db,
                ticket=ticket,
                run=run,
                conversation_id=persistent.conversation_id,
                just_accepted_dedupe_key=event.dedupe_key,
                settings=settings,
            )
        ):
            clear_matching_ticket_content_requeue(ticket, source_message_id=event.source_id, touched_at=now)
        _advance_effective_input_hash_if_frontier_complete(
            db,
            settings=settings,
            run=run,
            turn=turn,
            conversation_id=persistent.conversation_id,
            ticket_id=ticket.id,
        )
        _append_turn_outcome(
            db,
            turn_id=turn.id,
            outcome_kind="accepted",
            payload_json={
                "event_type": "turn/steer",
                "receipt_id": str(receipt.id),
                "rpc_request_id": rpc_request_id,
                "thread_id": thread_id,
                "native_turn_id": native_turn_id,
                "dedupe_key": event.dedupe_key,
                "effective_input_hash": turn.effective_input_hash,
                "steering_disposition": "accepted",
                "commit_to_ack_latency_ms": commit_to_ack_latency_ms,
            },
        )


def _mark_unresolved_steering_receipts_ambiguous(
    settings: Settings,
    *,
    turn_id,
    reason: str,
) -> int:
    with session_scope(settings) as db:
        return _mark_unresolved_steering_receipts_ambiguous_in_db(db, turn_id=turn_id, reason=reason)


def _mark_unresolved_steering_receipts_ambiguous_in_db(
    db,
    *,
    turn_id,
    reason: str,
) -> int:
    receipts = list(
        db.execute(
            select(CodexTurnSteer)
            .where(CodexTurnSteer.turn_id == turn_id, CodexTurnSteer.status.in_(("prepared", "sending")))
            .with_for_update()
        ).scalars()
    )
    now = utc_now()
    for receipt in receipts:
        receipt.status = "ambiguous"
        receipt.resolved_at = now
        receipt.error_code = reason
        receipt.error_text = f"Active-turn steering delivery is ambiguous: {reason}."
        _append_turn_outcome(
            db,
            turn_id=receipt.turn_id,
            outcome_kind="ambiguous",
            payload_json={
                "event_type": "turn/steer",
                "receipt_id": str(receipt.id),
                "dedupe_key": receipt.dedupe_key,
                "steering_disposition": "ambiguous",
                "reason": reason,
            },
        )
    return len(receipts)


def _steering_receipt_status_counts_in_db(db, *, turn_id) -> dict[str, int]:
    counts = {status: 0 for status in ("prepared", "sending", "accepted", "rejected", "ambiguous")}
    receipts = list(
        db.execute(
            select(CodexTurnSteer)
            .where(CodexTurnSteer.turn_id == turn_id)
        ).scalars()
    )
    for receipt in receipts:
        status = str(getattr(receipt, "status", "") or "")
        if status in counts:
            counts[status] += 1
    return counts


def _accepted_input_count_in_db(db, *, turn_id) -> int:
    try:
        return int(
            db.execute(
                select(func.count())
                .select_from(CodexTurnInput)
                .where(CodexTurnInput.turn_id == turn_id)
            ).scalar_one()
            or 0
        )
    except (AttributeError, TypeError):
        return 0


def _completed_message_turn_id(completed_message: dict[str, object] | None) -> str | None:
    if not isinstance(completed_message, dict):
        return None
    params = completed_message.get("params")
    if not isinstance(params, dict):
        return None
    turn = params.get("turn")
    if not isinstance(turn, dict):
        return None
    turn_id = turn.get("id") or turn.get("turnId") or turn.get("turn_id")
    return turn_id if isinstance(turn_id, str) and turn_id.strip() else None


def _commit_app_server_completion_fence(
    settings: Settings,
    *,
    prepared: PreparedStepRun,
    persistent: PreparedPersistentSpecialistStep,
    completed_message: dict[str, object] | None,
    expected_thread_id: str,
    expected_native_turn_id: str,
) -> int:
    completed_turn_id = _completed_message_turn_id(completed_message)
    if completed_turn_id is not None and completed_turn_id != expected_native_turn_id:
        raise CodexAppServerAmbiguousError(
            f"turn/completed for {completed_turn_id} does not match active turn {expected_native_turn_id}.",
            error_code="turn_completed_turn_mismatch",
        )
    with session_scope(settings) as db:
        run, session, turn, step = _load_locked_owned_runtime_records(db, prepared=prepared, persistent=persistent)
        if isinstance(db, Session):
            locked_turn = db.execute(
                select(CodexTurn)
                .where(CodexTurn.id == persistent.turn_id)
                .limit(1)
                .with_for_update()
            ).scalar_one()
            turn = locked_turn
        ticket = _load_locked_ticket(db, ticket_id=prepared.ticket_id)
        if ticket is None:
            raise StepRunError("Persistent specialist runtime rows disappeared during completion fence.")
        if session.thread_id != expected_thread_id:
            raise CodexAppServerAmbiguousError(
                f"Completion fence saw thread {session.thread_id}, expected {expected_thread_id}.",
                error_code="completion_fence_thread_mismatch",
            )
        if turn.transport_kind != "app_server":
            raise StepRunError("Completion fence is only valid for app-server persistent turns.")
        if turn.native_turn_id != expected_native_turn_id:
            raise CodexAppServerAmbiguousError(
                f"Completion fence saw native turn {turn.native_turn_id}, expected {expected_native_turn_id}.",
                error_code="completion_fence_turn_mismatch",
            )
        now = utc_now()
        turn.steering_closed_at = turn.steering_closed_at or now
        ambiguous_receipts = _mark_unresolved_steering_receipts_ambiguous_in_db(
            db,
            turn_id=turn.id,
            reason="turn_completed_with_unresolved_steer",
        )
        run.last_heartbeat_at = now
        session.lease_heartbeat_at = now
        session.lease_expires_at = now + timedelta(seconds=settings.ai_run_stale_timeout_seconds)
        step.ended_at = None
        _append_turn_outcome(
            db,
            turn_id=turn.id,
            outcome_kind="completed",
            payload_json={
                "event_type": "completion_fence",
                "thread_id": expected_thread_id,
                "native_turn_id": expected_native_turn_id,
                "effective_input_hash": turn.effective_input_hash,
                "ambiguous_steering_receipts": ambiguous_receipts,
                "completion_race_fallback": ambiguous_receipts > 0,
            },
        )
        return ambiguous_receipts


def _load_locked_conversation(db, *, ticket_id) -> CodexConversation | None:
    statement = (
        select(CodexConversation)
        .where(CodexConversation.ticket_id == ticket_id)
        .limit(1)
        .with_for_update()
    )
    return db.execute(statement).scalar_one_or_none()


def _load_locked_active_session(db, *, conversation_id) -> CodexSession | None:
    statement = (
        select(CodexSession)
        .where(
            CodexSession.conversation_id == conversation_id,
            CodexSession.ended_at.is_(None),
        )
        .limit(1)
        .with_for_update()
    )
    return db.execute(statement).scalar_one_or_none()


def _load_locked_owned_runtime_records(db, *, prepared: PreparedStepRun, persistent: PreparedPersistentSpecialistStep):
    run = load_owned_running_run(
        db,
        run_id=prepared.run_id,
        worker_instance_id=prepared.worker_instance_id,
    )
    if run is None:
        raise RunOwnershipLost(
            f"Run {prepared.run_id} is no longer running for worker {prepared.worker_instance_id} during persistent step update."
        )
    session = db.execute(
        select(CodexSession)
        .where(CodexSession.id == persistent.session_id)
        .limit(1)
        .with_for_update()
    ).scalar_one()
    if session.lease_owner_run_id != prepared.run_id or session.lease_worker_instance_id != prepared.worker_instance_id:
        raise RunOwnershipLost(
            f"Conversation lease for session {persistent.session_id} is no longer owned by run {prepared.run_id}."
        )
    turn = db.get(CodexTurn, persistent.turn_id)
    step = db.get(AIRunStep, persistent.step_id)
    if turn is None or step is None:
        raise StepRunError("Persistent specialist runtime rows disappeared during execution.")
    return run, session, turn, step


def _refresh_owned_persistent_session_lease(
    settings: Settings,
    *,
    prepared: PreparedStepRun,
    persistent: PreparedPersistentSpecialistStep,
) -> None:
    with session_scope(settings) as db:
        run, session, _turn, step = _load_locked_owned_runtime_records(db, prepared=prepared, persistent=persistent)
        now = utc_now()
        run.last_heartbeat_at = now
        session.lease_heartbeat_at = now
        session.lease_expires_at = now + timedelta(seconds=settings.ai_run_stale_timeout_seconds)
        step.ended_at = None


def _append_app_server_lifecycle_outcome(
    settings: Settings,
    *,
    prepared: PreparedStepRun,
    persistent: PreparedPersistentSpecialistStep,
    event_type: str,
    payload_json: dict[str, object] | None = None,
) -> None:
    with session_scope(settings) as db:
        run, session, turn, step = _load_locked_owned_runtime_records(db, prepared=prepared, persistent=persistent)
        now = utc_now()
        run.last_heartbeat_at = now
        session.lease_heartbeat_at = now
        session.lease_expires_at = now + timedelta(seconds=settings.ai_run_stale_timeout_seconds)
        step.ended_at = None
        _append_turn_outcome(
            db,
            turn_id=turn.id,
            outcome_kind="attempted",
            payload_json={
                "event_type": event_type,
                **(payload_json or {}),
            },
        )


def _mark_app_server_turn_accepted(
    settings: Settings,
    *,
    prepared: PreparedStepRun,
    persistent: PreparedPersistentSpecialistStep,
    native_turn_id: str,
    thread_id: str | None = None,
) -> None:
    with session_scope(settings) as db:
        _run, session, turn, _step = _load_locked_owned_runtime_records(db, prepared=prepared, persistent=persistent)
        _mark_turn_accepted(
            db,
            turn=turn,
            events=persistent.pending_events,
            effective_input_hash=persistent.effective_input_hash,
            now=utc_now(),
            native_turn_id=native_turn_id,
            thread_id=thread_id or session.thread_id,
            event_type="turn/start",
        )


def _release_session_lease(session: CodexSession) -> None:
    session.lease_owner_run_id = None
    session.lease_worker_instance_id = None
    session.lease_acquired_at = None
    session.lease_heartbeat_at = None
    session.lease_expires_at = None


def _retire_session_for_recovery(
    db,
    *,
    session: CodexSession,
    conversation_id,
    ended_at,
) -> None:
    session.status = "replaced"
    session.ended_at = ended_at
    _release_session_lease(session)
    conversation = db.get(CodexConversation, conversation_id)
    if conversation is not None:
        conversation.status = "recovery_required"


def _replacement_session_required(
    *,
    conversation: CodexConversation,
    session: CodexSession | None,
    prior_turn_count: int,
) -> bool:
    if prior_turn_count <= 0:
        return False
    if conversation.status == "recovery_required":
        return True
    if session is None:
        return True
    return not (session.thread_id or "").strip()


def _create_replacement_session(
    db,
    *,
    conversation: CodexConversation,
    current_session: CodexSession | None,
    now,
) -> CodexSession:
    if current_session is not None:
        current_session.status = "replaced"
        current_session.ended_at = now
        _release_session_lease(current_session)
    replacement = CodexSession(
        conversation_id=conversation.id,
        status="pending",
    )
    db.add(replacement)
    db.flush()
    conversation.status = "active"
    return replacement


def prepare_persistent_specialist_step(
    settings: Settings,
    *,
    prepared: PreparedStepRun,
    prompt_state: PromptConversationState,
) -> PreparedPersistentSpecialistStep:
    if not settings.codex_conversations_enabled:
        raise StepRunError("Persistent specialist execution is disabled.")
    if prepared.step_kind != "specialist":
        raise StepRunError("Persistent specialist execution only supports specialist steps.")

    with session_scope(settings) as db:
        run = load_owned_running_run(
            db,
            run_id=prepared.run_id,
            worker_instance_id=prepared.worker_instance_id,
        )
        if run is None:
            raise RunOwnershipLost(
                f"Run {prepared.run_id} is no longer running for worker {prepared.worker_instance_id} during persistent specialist start."
            )

        conversation = _load_locked_conversation(db, ticket_id=prepared.ticket_id)
        if conversation is None:
            if prompt_state.conversation_id is not None:
                raise StepRunError("Frozen conversation state no longer matches the ticket conversation")
            conversation = CodexConversation(ticket_id=prepared.ticket_id, status="active")
            db.add(conversation)
            db.flush()
        elif prompt_state.conversation_id != conversation.id:
            raise StepRunError("Frozen conversation state no longer matches the ticket conversation")
        session = _load_locked_active_session(db, conversation_id=conversation.id)
        if (
            not prompt_state.recovery_required
            and prompt_state.active_session_id is not None
            and (session is None or session.id != prompt_state.active_session_id)
        ):
            raise StepRunError("Frozen Codex session state changed before specialist execution")
        prior_turn_count = int(
            db.execute(
                select(func.count())
                .select_from(CodexTurn)
                .where(CodexTurn.conversation_id == conversation.id)
            ).scalar_one()
            or 0
        )
        now = utc_now()
        if session is not None:
            conflict_reason = _conversation_lease_conflict_reason(
                session,
                run_id=prepared.run_id,
                worker_instance_id=prepared.worker_instance_id,
                now=now,
            )
            if conflict_reason is not None:
                raise StepRunError(conflict_reason)
        if session is None:
            session = CodexSession(
                conversation_id=conversation.id,
                status="pending",
            )
            db.add(session)
            db.flush()
            conversation.status = "active"
        elif _replacement_session_required(
            conversation=conversation,
            session=session,
            prior_turn_count=prior_turn_count,
        ):
            session = _create_replacement_session(
                db,
                conversation=conversation,
                current_session=session,
                now=now,
            )
        session.lease_owner_run_id = run.id
        session.lease_worker_instance_id = prepared.worker_instance_id
        session.lease_acquired_at = now
        session.lease_heartbeat_at = now
        session.lease_expires_at = now + timedelta(seconds=settings.ai_run_stale_timeout_seconds)

        if settings.codex_app_server_specialist_transport_enabled:
            app_command_spec = build_codex_app_server_command(settings)
            command_spec = PersistentCommandSpec(
                command=app_command_spec.command,
                env=app_command_spec.env,
                runtime_codex_home=app_command_spec.runtime_codex_home,
                resumed=session.thread_id is not None,
            )
            transport_kind = "app_server"
        else:
            command_spec = build_persistent_codex_command(
                settings,
                prepared=prepared,
                thread_id=session.thread_id,
            )
            transport_kind = "exec"
        accepted_input_hash = _initial_effective_input_hash(prompt_state)
        run.last_heartbeat_at = now
        step = AIRunStep(
            ai_run_id=prepared.run_id,
            step_index=prepared.step_index,
            step_kind=prepared.step_kind,
            agent_spec_id=prepared.spec.id,
            agent_spec_version=prepared.spec.version,
            output_contract=prepared.spec.output_contract,
            model_name=prepared.model_name,
            status="running",
            prompt_path=str(prepared.paths.prompt_path),
            schema_path=str(prepared.paths.schema_path),
            final_output_path=str(prepared.paths.final_output_path),
            stdout_jsonl_path=str(prepared.paths.stdout_jsonl_path),
            stderr_path=str(prepared.paths.stderr_path),
            started_at=now,
        )
        db.add(step)
        db.flush()
        turn = CodexTurn(
            conversation_id=conversation.id,
            session_id=session.id,
            ai_run_id=prepared.run_id,
            turn_index=_next_turn_index(db, conversation_id=conversation.id),
            status="running",
            transport_kind=transport_kind,
            specialist_id=prepared.spec.id,
            agent_spec_version=prepared.spec.version,
            output_contract=prepared.spec.output_contract,
            model_name=prepared.model_name,
            route_target_id=prepared.route_target_id,
            prompt_path=str(prepared.paths.prompt_path),
            schema_path=str(prepared.paths.schema_path),
            final_output_path=str(prepared.paths.final_output_path),
            stdout_jsonl_path=str(prepared.paths.stdout_jsonl_path),
            stderr_path=str(prepared.paths.stderr_path),
            started_at=now,
        )
        db.add(turn)
        db.flush()
        _append_turn_outcome(
            db,
            turn_id=turn.id,
            outcome_kind="attempted",
            payload_json={
                "command": command_spec.command,
                "runtime_codex_home": str(command_spec.runtime_codex_home),
                "resumed": command_spec.resumed,
                "transport_kind": transport_kind,
                "route_target_id": prepared.route_target_id,
                "selected_specialist_id": prepared.selected_specialist_id,
                "prompt_mode": prompt_state.prompt_mode,
                "recovery_required": prompt_state.recovery_required,
                "input_hash": prompt_state.input_hash,
                "effective_input_hash": accepted_input_hash,
                "prompt_sha256": hashlib.sha256(prepared.prompt.encode("utf-8")).hexdigest(),
            },
        )
        persistent = PreparedPersistentSpecialistStep(
            step_id=step.id,
            turn_id=turn.id,
            conversation_id=conversation.id,
            session_id=session.id,
            command_spec=command_spec,
            transport_kind=transport_kind,
            stored_thread_id=session.thread_id,
            pending_events=prompt_state.pending_events,
            effective_input_hash=accepted_input_hash,
        )

    write_step_manifest(
        prepared.paths,
        step_id=persistent.step_id,
        run_id=prepared.run_id,
        ticket_id=prepared.ticket_id,
        step_index=prepared.step_index,
        step_kind=prepared.step_kind,
        spec=prepared.spec,
        status="running",
        model_name=prepared.model_name,
        output_contract=prepared.spec.output_contract,
        metadata={
            **_step_manifest_metadata(prepared, output_payload=None),
            "codex_conversation_id": str(persistent.conversation_id),
            "codex_session_id": str(persistent.session_id),
            "codex_turn_id": str(persistent.turn_id),
            "codex_command": list(persistent.command_spec.command),
            "codex_runtime_home": str(persistent.command_spec.runtime_codex_home),
            "codex_resumed": persistent.command_spec.resumed,
            "codex_transport_kind": persistent.transport_kind,
            "codex_effective_input_hash": persistent.effective_input_hash,
        },
    )
    return persistent


def refresh_persistent_session_leases(
    db,
    *,
    run_id,
    worker_instance_id: str,
    stale_timeout_seconds: int,
) -> int:
    now = utc_now()
    sessions = list(
        db.execute(
            select(CodexSession)
            .where(
                CodexSession.lease_owner_run_id == run_id,
                CodexSession.lease_worker_instance_id == worker_instance_id,
            )
            .with_for_update()
        ).scalars()
    )
    for session in sessions:
        session.lease_heartbeat_at = now
        session.lease_expires_at = now + timedelta(seconds=stale_timeout_seconds)
    return len(sessions)


def classify_persistent_failure(
    *,
    accepted: bool,
    timed_out: bool,
    stderr_text: str,
) -> tuple[str, str]:
    if timed_out:
        if accepted:
            return "ambiguous", "Persistent specialist turn timed out after Codex accepted the turn."
        return "timed_out", "Persistent specialist turn timed out before Codex completion."
    if "thread_not_found" in stderr_text or "thread not found" in stderr_text.lower():
        return "failed", "Persistent specialist turn could not resume because the stored thread was not found."
    if accepted:
        return "ambiguous", "Persistent specialist turn ended after Codex acceptance without a validated final output."
    return "failed", "Persistent specialist turn failed before Codex acceptance."


def handle_stale_persistent_run(db, *, run, stale_timeout_seconds: int) -> bool:
    turn = db.execute(
        select(CodexTurn)
        .where(CodexTurn.ai_run_id == run.id)
        .limit(1)
        .with_for_update()
    ).scalar_one_or_none()
    if turn is None:
        return False
    session = db.execute(
        select(CodexSession)
        .where(CodexSession.id == turn.session_id)
        .limit(1)
        .with_for_update()
    ).scalar_one_or_none()
    now = utc_now()
    # An already-known thread id belongs to the session, not this attempt. Only
    # turn.started is durable evidence that this specific turn was accepted.
    accepted = turn.accepted_at is not None
    turn.status = "ambiguous" if accepted else "interrupted"
    turn.ended_at = now
    if getattr(turn, "transport_kind", None) == "app_server":
        turn.steering_closed_at = turn.steering_closed_at or now
    unresolved_steering_receipts = _mark_unresolved_steering_receipts_ambiguous_in_db(
        db,
        turn_id=turn.id,
        reason="stale_run_recovery",
    )
    steering_receipt_status_counts = _steering_receipt_status_counts_in_db(db, turn_id=turn.id)
    accepted_input_count = _accepted_input_count_in_db(db, turn_id=turn.id)
    _append_turn_outcome(
        db,
        turn_id=turn.id,
        outcome_kind="ambiguous" if accepted else "interrupted",
        payload_json={
            "reason": "stale_run_recovery",
            "event_type": "stale_persistent_run_recovery",
            "stale_timeout_seconds": stale_timeout_seconds,
            "accepted": accepted,
            "accepted_input_count": accepted_input_count,
            "ambiguous_steering_receipts": unresolved_steering_receipts,
            "steering_receipt_status_counts": steering_receipt_status_counts,
            "accepted_inputs_remain_consumed": accepted,
            "unaccepted_inputs_remain_discoverable": not accepted,
            "rejected_and_dormant_inputs_remain_discoverable": True,
            "ambiguous_receipts_force_recovery": unresolved_steering_receipts > 0,
            "late_retired_session_output_publishable": False,
            "retired_session_id": str(session.id) if session is not None else None,
        },
    )
    if session is not None:
        _retire_session_for_recovery(
            db,
            session=session,
            conversation_id=turn.conversation_id,
            ended_at=now,
        )
    return True


def _record_stdout_event(
    settings: Settings,
    *,
    prepared: PreparedStepRun,
    persistent: PreparedPersistentSpecialistStep,
    state: PersistentStreamState,
    raw_line: str,
) -> None:
    event: dict[str, object]
    try:
        parsed = json.loads(raw_line)
        event = parsed if isinstance(parsed, dict) else {"raw_event": parsed}
    except json.JSONDecodeError:
        event = {"raw_line": raw_line}
    item_kind = str(event.get("type") or "jsonl_line")
    codex_item_id = None
    nested_item = event.get("item")
    if isinstance(nested_item, dict):
        nested_id = nested_item.get("id")
        if isinstance(nested_id, str):
            codex_item_id = nested_id
    with state.lock:
        item_index = state.next_item_index
        state.next_item_index += 1
    with session_scope(settings) as db:
        run, session, turn, step = _load_locked_owned_runtime_records(db, prepared=prepared, persistent=persistent)
        db.add(
            CodexTurnItem(
                turn_id=turn.id,
                item_index=item_index,
                item_kind=item_kind,
                codex_item_id=codex_item_id,
                payload_json=event,
            )
        )
        now = utc_now()
        if item_kind == "thread.started":
            thread_id = event.get("thread_id")
            if not isinstance(thread_id, str) or not thread_id.strip():
                raise StepRunError("Codex emitted thread.started without a thread_id.")
            if session.thread_id is None:
                session.thread_id = thread_id
            elif session.thread_id != thread_id:
                conversation = db.get(CodexConversation, persistent.conversation_id)
                if conversation is not None:
                    conversation.status = "recovery_required"
                raise StepRunError(
                    f"Persistent session {session.id} thread id changed from {session.thread_id} to {thread_id}."
                )
            session.status = "active"
            session.started_at = session.started_at or now
            state.thread_id = thread_id
        if item_kind == "turn.started" and turn.accepted_at is None:
            native_turn_id = event.get("turn_id") or event.get("turnId")
            _mark_turn_accepted(
                db,
                turn=turn,
                events=persistent.pending_events,
                effective_input_hash=persistent.effective_input_hash,
                now=now,
                native_turn_id=native_turn_id if isinstance(native_turn_id, str) else None,
                thread_id=session.thread_id,
                event_type=item_kind,
            )
            with state.lock:
                state.accepted = True
        if item_kind == "turn.completed":
            usage = event.get("usage")
            with state.lock:
                state.completed = True
                if isinstance(usage, dict):
                    state.usage = usage
        run.last_heartbeat_at = now
        session.lease_heartbeat_at = now
        session.lease_expires_at = now + timedelta(seconds=settings.ai_run_stale_timeout_seconds)
        step.ended_at = None


def _capture_stream_error(state: PersistentStreamState, exc: Exception) -> None:
    with state.lock:
        if state.persistence_error is None:
            state.persistence_error = exc


def _begin_stream_write(state: PersistentStreamState) -> None:
    with state.lock:
        state.active_stream_writes += 1
        state.no_active_stream_writes.clear()


def _end_stream_write(state: PersistentStreamState) -> None:
    with state.lock:
        state.active_stream_writes -= 1
        if state.active_stream_writes <= 0:
            state.active_stream_writes = 0
            state.no_active_stream_writes.set()


def _stderr_pump(
    stderr,
    *,
    output_path: Path,
    sink: list[str],
    state: PersistentStreamState,
) -> None:
    try:
        with output_path.open("w", encoding="utf-8") as handle:
            while not state.stop_streams.is_set():
                line = stderr.readline()
                if line == "":
                    break
                _begin_stream_write(state)
                try:
                    if state.stop_streams.is_set():
                        break
                    handle.write(line)
                    handle.flush()
                    if state.stop_streams.is_set():
                        break
                    sink.append(line)
                finally:
                    _end_stream_write(state)
    except Exception as exc:  # pragma: no cover - defensive I/O boundary
        if not state.stop_streams.is_set():
            _capture_stream_error(state, exc)


def _stdout_pump(
    stdout,
    *,
    output_path: Path,
    settings: Settings,
    prepared: PreparedStepRun,
    persistent: PreparedPersistentSpecialistStep,
    state: PersistentStreamState,
) -> None:
    try:
        with output_path.open("w", encoding="utf-8") as handle:
            while not state.stop_streams.is_set():
                line = stdout.readline()
                if line == "":
                    break
                _begin_stream_write(state)
                try:
                    if state.stop_streams.is_set():
                        break
                    handle.write(line)
                    handle.flush()
                    if state.stop_streams.is_set():
                        break
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        _record_stdout_event(
                            settings,
                            prepared=prepared,
                            persistent=persistent,
                            state=state,
                            raw_line=stripped,
                        )
                    except Exception as exc:  # pragma: no cover - exercised via integration path
                        _capture_stream_error(state, exc)
                finally:
                    _end_stream_write(state)
    except Exception as exc:  # pragma: no cover - defensive I/O boundary
        if not state.stop_streams.is_set():
            _capture_stream_error(state, exc)


def _finalize_persistent_step(
    settings: Settings,
    *,
    prepared: PreparedStepRun,
    persistent: PreparedPersistentSpecialistStep,
    output_payload: dict[str, object] | None,
    turn_status: str,
    step_status: str,
    error_text: str | None,
    usage: dict[str, object] | None,
    returncode: int | None,
    retire_session: bool = False,
) -> None:
    with session_scope(settings) as db:
        run, session, turn, step = _load_locked_owned_runtime_records(db, prepared=prepared, persistent=persistent)
        finished_at = utc_now()
        step.status = step_status
        step.output_json = output_payload
        step.error_text = error_text
        step.ended_at = finished_at
        turn.status = turn_status
        turn.ended_at = finished_at
        run.last_heartbeat_at = finished_at
        payload = {"returncode": returncode}
        if usage is not None:
            payload["usage"] = usage
        if output_payload is not None:
            payload["output_json"] = output_payload
        if error_text:
            payload["error"] = error_text
        if step_status == "succeeded":
            _append_turn_outcome(
                db,
                turn_id=turn.id,
                outcome_kind="completed",
                payload_json=payload,
            )
        else:
            _append_turn_outcome(
                db,
                turn_id=turn.id,
                outcome_kind=turn_status if turn_status in {"ambiguous", "timed_out", "interrupted"} else "failed",
                payload_json=payload,
            )
        if retire_session:
            _retire_session_for_recovery(
                db,
                session=session,
                conversation_id=persistent.conversation_id,
                ended_at=finished_at,
            )
        else:
            _release_session_lease(session)

    write_step_manifest(
        prepared.paths,
        step_id=persistent.step_id,
        run_id=prepared.run_id,
        ticket_id=prepared.ticket_id,
        step_index=prepared.step_index,
        step_kind=prepared.step_kind,
        spec=prepared.spec,
        status=step_status,
        model_name=prepared.model_name,
        output_contract=prepared.spec.output_contract,
        error_text=error_text,
        metadata={
            **_step_manifest_metadata(prepared, output_payload=output_payload),
            "codex_conversation_id": str(persistent.conversation_id),
            "codex_session_id": str(persistent.session_id),
            "codex_turn_id": str(persistent.turn_id),
            "codex_command": list(persistent.command_spec.command),
            "codex_runtime_home": str(persistent.command_spec.runtime_codex_home),
            "codex_resumed": persistent.command_spec.resumed,
            "codex_transport_kind": persistent.transport_kind,
            "codex_native_turn_id": getattr(turn, "native_turn_id", None),
            "codex_effective_input_hash": getattr(turn, "effective_input_hash", None),
            "usage": usage,
        },
    )


def _extract_completed_turn_payload(completed_message: dict[str, object] | None) -> dict[str, object] | None:
    if not isinstance(completed_message, dict):
        return None
    params = completed_message.get("params")
    if not isinstance(params, dict):
        return None
    turn = params.get("turn")
    if not isinstance(turn, dict):
        return None
    items = turn.get("items")
    if not isinstance(items, list):
        return None
    agent_messages = [
        item
        for item in items
        if isinstance(item, dict)
        and item.get("type") == "agentMessage"
        and isinstance(item.get("text"), str)
        and item.get("text", "").strip()
    ]
    final_messages = [item for item in agent_messages if item.get("phase") == "final_answer"]
    for item in reversed(final_messages):
        try:
            payload = json.loads(str(item["text"]))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _load_validated_persistent_output(
    *,
    prepared: PreparedStepRun,
    completed_message: dict[str, object] | None = None,
) -> dict[str, object]:
    try:
        payload = json.loads(prepared.paths.final_output_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        payload = _extract_completed_turn_payload(completed_message)
        if payload is None:
            raise StepRunError("Codex did not write final.json or return a completed final assistant message")
    except (json.JSONDecodeError, OSError) as exc:
        raise StepRunError("Codex final.json was missing or invalid JSON") from exc
    if not isinstance(payload, dict):
        raise StepRunError("Codex final.json must contain a JSON object")
    validated_output = validate_contract_output(
        prepared.spec.output_contract,
        payload,
        route_target_id=prepared.route_target_id,
        candidate_specialist_ids=prepared.candidate_specialist_ids,
        requester_role=prepared.requester_role,
    )
    return validated_output.model_dump()


def _close_pipe(pipe) -> None:
    if pipe is None:
        return
    try:
        pipe.close()
    except (BrokenPipeError, OSError, ValueError, AttributeError):
        pass


def _write_prompt_to_stdin(stdin, *, prompt: str, state: PromptWriterState) -> None:
    try:
        stdin.write(prompt)
        stdin.close()
    except (BrokenPipeError, OSError) as exc:
        state.mark_finished(error=exc)
        return
    except Exception as exc:  # pragma: no cover - defensive writer boundary
        state.mark_finished(error=exc)
        return
    state.mark_finished()


def _remaining_seconds(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _terminate_process_group(
    process: subprocess.Popen,
    *,
    grace_seconds: float | None = None,
    kill_wait_seconds: float | None = None,
) -> int | None:
    """Terminate Codex and any children without leaving pipe-holding descendants."""
    grace_seconds = _PROCESS_TERMINATION_GRACE_SECONDS if grace_seconds is None else grace_seconds
    kill_wait_seconds = _PROCESS_KILL_WAIT_SECONDS if kill_wait_seconds is None else kill_wait_seconds
    pid = getattr(process, "pid", None)
    group_pid = pid if isinstance(pid, int) and pid > 0 else None
    if isinstance(pid, int) and pid > 0:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            process.terminate()
    else:  # Compatibility for test doubles and non-POSIX implementations.
        process.terminate()
    grace_deadline = time.monotonic() + grace_seconds
    leader_returncode: int | None = None
    try:
        leader_returncode = process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        leader_returncode = None
    if group_pid is not None:
        _wait_process_group_exit(group_pid, timeout=_remaining_seconds(grace_deadline))
    if group_pid is not None:
        try:
            os.killpg(group_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            process.kill()
    elif leader_returncode is None:
        process.kill()
    if leader_returncode is None:
        try:
            leader_returncode = process.wait(timeout=kill_wait_seconds)
        except subprocess.TimeoutExpired:
            leader_returncode = getattr(process, "returncode", None)
    if group_pid is not None:
        _wait_process_group_exit(group_pid, timeout=kill_wait_seconds)
    return leader_returncode


def _wait_process_group_exit(group_pid: int, *, timeout: float) -> None:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            os.killpg(group_pid, 0)
        except ProcessLookupError:
            return
        except OSError:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.01, remaining))


def _cleanup_stream_pumps(
    process: subprocess.Popen,
    *,
    state: PersistentStreamState,
    stdout_thread: threading.Thread,
    stderr_thread: threading.Thread,
) -> StepRunError | None:
    stdout_thread.join(timeout=_STREAM_JOIN_SECONDS)
    stderr_thread.join(timeout=_STREAM_JOIN_SECONDS)
    if not (stdout_thread.is_alive() or stderr_thread.is_alive()):
        return None

    cleanup_error = StepRunError("Codex output streams did not close after the process exited")
    state.stop_streams.set()
    _terminate_process_group(process)
    _close_pipe(getattr(process, "stdout", None))
    _close_pipe(getattr(process, "stderr", None))
    stdout_thread.join(timeout=_STREAM_JOIN_SECONDS)
    stderr_thread.join(timeout=_STREAM_JOIN_SECONDS)
    if not state.no_active_stream_writes.wait(timeout=_STREAM_JOIN_SECONDS):
        raise PersistentCodexNonQuiescentCleanupError(
            "Codex output stream persistence did not quiesce after cleanup"
        )
    if stdout_thread.is_alive() or stderr_thread.is_alive():
        raise PersistentCodexNonQuiescentCleanupError("Codex output streams did not close after cleanup")
    return cleanup_error


def _write_app_server_stderr_artifact(path: Path, *, stderr_text: str) -> None:
    path.write_text(stderr_text, encoding="utf-8")


def _append_app_server_stdout_artifact(path: Path, *, message: dict[str, object], lock: threading.Lock) -> None:
    with lock:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(message, sort_keys=True, separators=(",", ":"), default=str))
            handle.write("\n")


def _app_server_initial_input_payload(prepared: PreparedStepRun) -> list[dict[str, object]]:
    payload: list[dict[str, object]] = [{"type": "text", "text": prepared.prompt, "text_elements": []}]
    for image_path in prepared.image_paths:
        payload.append({"type": "localImage", "path": str(image_path)})
    return payload


def _load_active_steering_candidates(
    settings: Settings,
    *,
    prepared: PreparedStepRun,
    persistent: PreparedPersistentSpecialistStep,
) -> tuple[OrderedInputEvent, ...]:
    with session_scope(settings) as db:
        run = db.get(AIRun, prepared.run_id)
        if run is None:
            return ()
        context = load_ticket_context(db, prepared.ticket_id)
        unseen_events = load_strictly_unseen_input_events(
            db,
            context=context,
            run=run,
            conversation_id=persistent.conversation_id,
            include_turn_summaries=False,
            max_attachment_bytes=settings.max_image_bytes,
        )
        receipt_dedupe_keys = _existing_steer_dedupe_keys(db, turn_id=persistent.turn_id)
    return tuple(
        event
        for event in unseen_events
        if event.source_kind == "ticket_message"
        and event.source_id is not None
        and event.dedupe_key not in receipt_dedupe_keys
    )


def _load_active_steering_change_token(
    settings: Settings,
    *,
    ticket_id: uuid.UUID,
) -> ActiveSteeringChangeToken | None:
    with session_scope(settings) as db:
        row = db.execute(
            select(
                Ticket.updated_at,
                Ticket.status,
                Ticket.requeue_requested,
                Ticket.requeue_trigger,
                Ticket.requeue_source_message_id,
            )
            .where(Ticket.id == ticket_id)
            .limit(1)
        ).one_or_none()
    if row is None:
        return None
    updated_at, status, requeue_requested, requeue_trigger, requeue_source_message_id = row
    return ActiveSteeringChangeToken(
        updated_at=updated_at,
        status=str(status),
        requeue_requested=bool(requeue_requested),
        requeue_trigger=str(requeue_trigger) if requeue_trigger is not None else None,
        requeue_source_message_id=requeue_source_message_id,
    )


def _poll_active_steering_if_changed(
    settings: Settings,
    *,
    prepared: PreparedStepRun,
    persistent: PreparedPersistentSpecialistStep,
    client: CodexAppServerClient,
    thread_id: str,
    native_turn_id: str,
    deadline: float,
    state: ActiveSteeringPollState,
) -> int:
    change_token = _load_active_steering_change_token(settings, ticket_id=prepared.ticket_id)
    if change_token is None:
        return 0
    if state.initialized and state.change_token == change_token:
        return 0

    # Retain the token observed before scanning. If ticket content changes while
    # the scan is in progress, the next cheap poll observes a different token
    # and performs another full scan rather than treating that content as seen.
    state.initialized = True
    state.change_token = change_token
    return _attempt_active_steering_once(
        settings,
        prepared=prepared,
        persistent=persistent,
        client=client,
        thread_id=thread_id,
        native_turn_id=native_turn_id,
        deadline=deadline,
    )


def _attempt_active_steering_once(
    settings: Settings,
    *,
    prepared: PreparedStepRun,
    persistent: PreparedPersistentSpecialistStep,
    client: CodexAppServerClient,
    thread_id: str,
    native_turn_id: str,
    deadline: float,
) -> int:
    if not settings.codex_active_turn_steering_enabled:
        return 0
    if _remaining_seconds(deadline) < _ACTIVE_STEERING_MIN_REMAINING_SECONDS:
        return 0
    steered_count = 0
    for event in _load_active_steering_candidates(settings, prepared=prepared, persistent=persistent):
        if _remaining_seconds(deadline) < _ACTIVE_STEERING_MIN_REMAINING_SECONDS:
            break
        try:
            input_payload = app_server_input_for_events(
                (event,),
                trusted_attachment_root=settings.uploads_dir,
                max_attachment_bytes=settings.max_image_bytes,
            )
            payload_bytes = len(json.dumps(input_payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8"))
            if payload_bytes > _ACTIVE_STEERING_MAX_PAYLOAD_BYTES:
                raise UnsupportedInputBundleError("steering payload exceeds active-turn size limit")
            receipt_id = _prepare_steering_receipt(
                settings,
                prepared=prepared,
                persistent=persistent,
                event=event,
                input_payload=input_payload,
                thread_id=thread_id,
                native_turn_id=native_turn_id,
                deadline=deadline,
            )
            try:
                receipt = client.steer_turn(
                    thread_id=thread_id,
                    expected_turn_id=native_turn_id,
                    input_payload=input_payload,
                    timeout_seconds=_remaining_seconds(deadline),
                )
            except CodexAppServerRejectedError as exc:
                _mark_steering_receipt_terminal(
                    settings,
                    receipt_id=receipt_id,
                    status="rejected",
                    error_code=exc.error_code or "turn_steer_rejected",
                    error_text=str(exc),
                )
                continue
            except CodexAppServerError as exc:
                _mark_steering_receipt_terminal(
                    settings,
                    receipt_id=receipt_id,
                    status="ambiguous",
                    error_code=exc.error_code or "turn_steer_ambiguous",
                    error_text=str(exc),
                )
                raise
            try:
                _accept_steering_receipt(
                    settings,
                    prepared=prepared,
                    persistent=persistent,
                    receipt_id=receipt_id,
                    event=event,
                    rpc_request_id=receipt.rpc_request_id,
                    thread_id=thread_id,
                    native_turn_id=native_turn_id,
                    deadline=deadline,
                )
            except RunOwnershipLost:
                _mark_steering_receipt_terminal(
                    settings,
                    receipt_id=receipt_id,
                    status="ambiguous",
                    error_code="lease_lost_before_ack_commit",
                    error_text="Conversation lease was lost before steering acknowledgement commit.",
                )
                raise
            except CodexAppServerError as exc:
                _mark_steering_receipt_terminal(
                    settings,
                    receipt_id=receipt_id,
                    status="ambiguous",
                    error_code=exc.error_code or "ack_commit_ambiguous",
                    error_text=str(exc),
                )
                raise
            steered_count += 1
        except UnsupportedInputBundleError as exc:
            _reject_steering_event(
                settings,
                prepared=prepared,
                persistent=persistent,
                event=event,
                thread_id=thread_id,
                native_turn_id=native_turn_id,
                deadline=deadline,
                error_code="unsupported_bundle",
                error_text=str(exc),
            )
        except CodexAppServerRejectedError:
            break
    return steered_count


def _execute_app_server_persistent_specialist_step(
    settings: Settings,
    *,
    prepared: PreparedStepRun,
    persistent: PreparedPersistentSpecialistStep,
) -> StepRunResult:
    persistence = CodexAppServerTurnPersistence(
        settings=settings,
        prepared=prepared,
        turn_id=persistent.turn_id,
        session_id=persistent.session_id,
        conversation_id=persistent.conversation_id,
    )
    stdout_lock = threading.Lock()
    accepted = {"value": False}
    thread_id_state = {"value": persistent.stored_thread_id}

    def persist_protocol_item(message: dict[str, object]) -> None:
        _append_app_server_stdout_artifact(prepared.paths.stdout_jsonl_path, message=message, lock=stdout_lock)
        persistence.persist_protocol_item(message)

    def persist_thread_id(thread_id: str) -> None:
        thread_id_state["value"] = thread_id
        persistence.persist_thread_id(thread_id)

    def persist_turn_id(native_turn_id: str) -> None:
        accepted["value"] = True
        _mark_app_server_turn_accepted(
            settings,
            prepared=prepared,
            persistent=persistent,
            native_turn_id=native_turn_id,
            thread_id=thread_id_state["value"],
        )

    client = CodexAppServerClient(
        settings,
        command_spec=persistent.command_spec,
        cwd=settings.triage_workspace_dir,
        response_timeout_seconds=min(30.0, float(prepared.timeout_seconds)),
        on_protocol_item=persist_protocol_item,
        on_thread_id=persist_thread_id,
        on_turn_id=persist_turn_id,
    )
    output_payload: dict[str, object] | None = None
    turn_status = "failed"
    step_status = "failed"
    error_text: str | None = None
    completed_message: dict[str, object] | None = None
    try:
        with client:
            deadline = time.monotonic() + float(prepared.timeout_seconds)
            client.initialize(timeout_seconds=_remaining_seconds(deadline))
            _append_app_server_lifecycle_outcome(
                settings,
                prepared=prepared,
                persistent=persistent,
                event_type="app_server_initialize",
                payload_json={"transport_kind": "app_server"},
            )
            _refresh_owned_persistent_session_lease(settings, prepared=prepared, persistent=persistent)
            thread = client.start_or_resume_thread(
                stored_thread_id=persistent.stored_thread_id,
                prepared=prepared,
                timeout_seconds=_remaining_seconds(deadline),
            )
            thread_id_state["value"] = thread.thread_id
            _append_app_server_lifecycle_outcome(
                settings,
                prepared=prepared,
                persistent=persistent,
                event_type="thread/resume" if thread.resumed else "thread/start",
                payload_json={
                    "thread_id": thread.thread_id,
                    "resumed": thread.resumed,
                },
            )
            _refresh_owned_persistent_session_lease(settings, prepared=prepared, persistent=persistent)
            turn = client.start_turn(
                thread_id=thread.thread_id,
                input_payload=_app_server_initial_input_payload(prepared),
                prepared=prepared,
                timeout_seconds=_remaining_seconds(deadline),
            )
            persist_turn_id(turn.turn_id)
            lease_refresh_interval = max(
                1.0,
                min(
                    float(settings.worker_heartbeat_seconds),
                    float(settings.ai_run_stale_timeout_seconds) / 3.0,
                ),
            )
            next_lease_refresh_at = time.monotonic() + lease_refresh_interval
            steering_poll_state = ActiveSteeringPollState()

            def supervise_poll() -> None:
                nonlocal next_lease_refresh_at
                now = time.monotonic()
                if now >= next_lease_refresh_at:
                    _refresh_owned_persistent_session_lease(
                        settings,
                        prepared=prepared,
                        persistent=persistent,
                    )
                    next_lease_refresh_at = now + lease_refresh_interval
                if settings.codex_active_turn_steering_enabled:
                    _poll_active_steering_if_changed(
                        settings,
                        prepared=prepared,
                        persistent=persistent,
                        client=client,
                        thread_id=thread.thread_id,
                        native_turn_id=turn.turn_id,
                        deadline=deadline,
                        state=steering_poll_state,
                    )

            completed_message = client.supervise_until_completed(
                thread_id=thread.thread_id,
                turn_id=turn.turn_id,
                deadline=deadline,
                on_poll=supervise_poll,
                poll_interval_seconds=_ACTIVE_STEERING_POLL_SECONDS,
            )
            ambiguous_receipts = _commit_app_server_completion_fence(
                settings,
                prepared=prepared,
                persistent=persistent,
                completed_message=completed_message,
                expected_thread_id=thread.thread_id,
                expected_native_turn_id=turn.turn_id,
            )
            if ambiguous_receipts:
                raise CodexAppServerAmbiguousError(
                    f"turn/completed arrived with {ambiguous_receipts} unresolved steering receipt(s).",
                    error_code="turn_completed_with_unresolved_steer",
                    stderr_text=client.stderr_text,
                )
            _refresh_owned_persistent_session_lease(settings, prepared=prepared, persistent=persistent)
            output_payload = _load_validated_persistent_output(
                prepared=prepared,
                completed_message=completed_message,
            )
            turn_status = "completed"
            step_status = "succeeded"
    except RunOwnershipLost:
        _mark_unresolved_steering_receipts_ambiguous(
            settings,
            turn_id=persistent.turn_id,
            reason="lease_lost",
        )
        raise
    except Exception as exc:
        _mark_unresolved_steering_receipts_ambiguous(
            settings,
            turn_id=persistent.turn_id,
            reason="app_server_failure",
        )
        classification = classify_app_server_failure(exc, stderr_text=client.stderr_text)
        turn_status = classification.status if classification.status in {"ambiguous", "timed_out", "interrupted"} else "failed"
        error_text = classification.message
        _finalize_persistent_step(
            settings,
            prepared=prepared,
            persistent=persistent,
            output_payload=None,
            turn_status=turn_status,
            step_status="failed",
            error_text=error_text,
            usage=None,
            returncode=client.process.poll() if client.process is not None else None,
            retire_session=accepted["value"] or turn_status in {"ambiguous", "timed_out", "interrupted"},
        )
        _write_app_server_stderr_artifact(prepared.paths.stderr_path, stderr_text=classification.stderr_text)
        if isinstance(exc, StepRunError):
            raise
        raise StepRunError(error_text) from exc
    else:
        _finalize_persistent_step(
            settings,
            prepared=prepared,
            persistent=persistent,
            output_payload=output_payload,
            turn_status=turn_status,
            step_status=step_status,
            error_text=None,
            usage=None,
            returncode=client.process.poll() if client.process is not None else None,
        )
        _write_app_server_stderr_artifact(prepared.paths.stderr_path, stderr_text=client.stderr_text)
        return StepRunResult(step_id=persistent.step_id, prepared=prepared, output_payload=output_payload)


def execute_persistent_specialist_step(
    settings: Settings,
    *,
    prepared: PreparedStepRun,
    prompt_state: PromptConversationState,
) -> StepRunResult:
    persistent = prepare_persistent_specialist_step(
        settings,
        prepared=prepared,
        prompt_state=prompt_state,
    )
    if persistent.transport_kind == "app_server":
        return _execute_app_server_persistent_specialist_step(
            settings,
            prepared=prepared,
            persistent=persistent,
        )
    state = PersistentStreamState()
    stderr_lines: list[str] = []
    output_payload: dict[str, object] | None = None
    error_text: str | None = None
    turn_status = "failed"
    step_status = "failed"
    returncode: int | None = None
    timed_out = False
    supervisor_error: Exception | None = None
    try:
        process = subprocess.Popen(
            persistent.command_spec.command,
            cwd=settings.triage_workspace_dir,
            env=persistent.command_spec.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
    except OSError as exc:
        error_text = f"Persistent specialist turn failed to launch Codex: {exc}"
        _finalize_persistent_step(
            settings,
            prepared=prepared,
            persistent=persistent,
            output_payload=None,
            turn_status="failed",
            step_status="failed",
            error_text=error_text,
            usage=None,
            returncode=None,
        )
        raise StepRunError(error_text) from exc
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    stdout_thread = threading.Thread(
        target=_stdout_pump,
        kwargs={
            "stdout": process.stdout,
            "output_path": prepared.paths.stdout_jsonl_path,
            "settings": settings,
            "prepared": prepared,
            "persistent": persistent,
            "state": state,
        },
        name=f"codex-stdout-{persistent.turn_id}",
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_stderr_pump,
        kwargs={
            "stderr": process.stderr,
            "output_path": prepared.paths.stderr_path,
            "sink": stderr_lines,
            "state": state,
        },
        name=f"codex-stderr-{persistent.turn_id}",
        daemon=True,
    )
    writer_state = PromptWriterState()
    writer_thread = threading.Thread(
        target=_write_prompt_to_stdin,
        kwargs={
            "stdin": process.stdin,
            "prompt": prepared.prompt,
            "state": writer_state,
        },
        name=f"codex-stdin-{persistent.turn_id}",
        daemon=True,
    )
    try:
        stdout_thread.start()
        stderr_thread.start()
        # The configured timeout is the wall-clock budget from prompt delivery
        # start through Codex process completion. Cleanup after expiry is
        # bounded separately so the worker cannot block on a stuck pipe.
        deadline = time.monotonic() + float(prepared.timeout_seconds)
        writer_thread.start()
        writer_thread.join(timeout=_remaining_seconds(deadline))
        if writer_thread.is_alive():
            timed_out = True
            returncode = _terminate_process_group(process)
        else:
            supervisor_error = writer_state.captured_error()
            try:
                returncode = process.wait(timeout=_remaining_seconds(deadline))
            except subprocess.TimeoutExpired:
                timed_out = True
                returncode = _terminate_process_group(process)
            except Exception as exc:  # pragma: no cover - defensive process boundary
                supervisor_error = supervisor_error or exc
                returncode = _terminate_process_group(process)
    finally:
        _close_pipe(process.stdin)
        writer_thread.join(timeout=_WRITER_CLEANUP_JOIN_SECONDS)
        cleanup_error = _cleanup_stream_pumps(
            process,
            state=state,
            stdout_thread=stdout_thread,
            stderr_thread=stderr_thread,
        )
        supervisor_error = supervisor_error or cleanup_error

    if state.persistence_error is not None:
        if isinstance(state.persistence_error, RunOwnershipLost):
            raise state.persistence_error
        error_text = str(state.persistence_error)
        _finalize_persistent_step(
            settings,
            prepared=prepared,
            persistent=persistent,
            output_payload=None,
            turn_status="ambiguous" if state.accepted else "failed",
            step_status="failed",
            error_text=error_text,
            usage=state.usage,
            returncode=returncode,
            retire_session=True,
        )
        if isinstance(state.persistence_error, Exception):
            raise StepRunError(error_text) from state.persistence_error
        raise StepRunError(error_text)

    protocol_error: str | None = None
    if timed_out:
        protocol_error = "Persistent specialist turn exceeded its execution timeout."
    elif supervisor_error is not None:
        protocol_error = f"Persistent specialist process supervision failed: {supervisor_error}"
    elif returncode != 0:
        protocol_error = f"Persistent specialist Codex process exited with status {returncode}."
    elif not state.completed:
        protocol_error = "Codex exited without a durable turn.completed event."
    elif not persistent.command_spec.resumed and not state.thread_id:
        protocol_error = "Codex completed an initial turn without a durable thread.started event."

    if protocol_error is None:
        try:
            output_payload = _load_validated_persistent_output(prepared=prepared)
            step_status = "succeeded"
            turn_status = "completed"
        except FileNotFoundError:
            output_payload = None
        except (json.JSONDecodeError, OSError, OutputContractError, StepRunError) as exc:
            error_text = str(exc)
            output_payload = None
    else:
        error_text = protocol_error

    stderr_text = "".join(stderr_lines)
    if output_payload is None:
        stored_thread_missing = (
            "thread_not_found" in stderr_text
            or "thread not found" in stderr_text.lower()
        )
        turn_status, default_error = classify_persistent_failure(
            accepted=state.accepted,
            timed_out=timed_out,
            stderr_text=stderr_text,
        )
        if timed_out or stored_thread_missing:
            error_text = default_error
        else:
            error_text = error_text or default_error
        step_status = "failed"
        _finalize_persistent_step(
            settings,
            prepared=prepared,
            persistent=persistent,
            output_payload=None,
            turn_status=turn_status,
            step_status=step_status,
            error_text=error_text,
            usage=state.usage,
            returncode=returncode,
            retire_session=(
                timed_out
                or state.accepted
                or supervisor_error is not None
                or (returncode == 0 and not state.completed)
                or stored_thread_missing
            ),
        )
        raise StepRunError(error_text)

    _finalize_persistent_step(
        settings,
        prepared=prepared,
        persistent=persistent,
        output_payload=output_payload,
        turn_status=turn_status,
        step_status=step_status,
        error_text=None,
        usage=state.usage,
        returncode=returncode,
    )
    return StepRunResult(step_id=persistent.step_id, prepared=prepared, output_payload=output_payload)
