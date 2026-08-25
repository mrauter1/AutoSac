from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
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

from shared.config import Settings
from shared.codex_turns import append_codex_turn_outcome
from shared.db import session_scope
from shared.models import AIRunStep, CodexConversation, CodexSession, CodexTurn, CodexTurnInput, CodexTurnItem, CodexTurnOutcome
from shared.security import utc_now
from worker.artifacts import write_step_manifest
from worker.codex_inputs import PromptConversationState
from worker.output_contracts import OutputContractError, validate_contract_output
from worker.run_ownership import RunOwnershipLost, load_owned_running_run
from worker.step_runner import PreparedStepRun, StepRunError, StepRunResult, _step_manifest_metadata


_STREAM_JOIN_SECONDS = 5.0
_PROCESS_TERMINATION_GRACE_SECONDS = 5.0
_PROCESS_KILL_WAIT_SECONDS = 5.0
_WRITER_CLEANUP_JOIN_SECONDS = 1.0


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

        command_spec = build_persistent_codex_command(
            settings,
            prepared=prepared,
            thread_id=session.thread_id,
        )
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
                "route_target_id": prepared.route_target_id,
                "selected_specialist_id": prepared.selected_specialist_id,
                "prompt_mode": prompt_state.prompt_mode,
                "recovery_required": prompt_state.recovery_required,
                "input_hash": prompt_state.input_hash,
                "prompt_sha256": hashlib.sha256(prepared.prompt.encode("utf-8")).hexdigest(),
            },
        )
        for input_index, event in enumerate(prompt_state.pending_events, start=1):
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
        persistent = PreparedPersistentSpecialistStep(
            step_id=step.id,
            turn_id=turn.id,
            conversation_id=conversation.id,
            session_id=session.id,
            command_spec=command_spec,
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
    _append_turn_outcome(
        db,
        turn_id=turn.id,
        outcome_kind="ambiguous" if accepted else "interrupted",
        payload_json={
            "reason": "stale_run_recovery",
            "stale_timeout_seconds": stale_timeout_seconds,
            "accepted": accepted,
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
            turn.accepted_at = now
            with state.lock:
                state.accepted = True
            _append_turn_outcome(
                db,
                turn_id=turn.id,
                outcome_kind="accepted",
                payload_json={
                    "event_type": item_kind,
                    "thread_id": session.thread_id,
                },
            )
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
            "usage": usage,
        },
    )


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
            payload = json.loads(prepared.paths.final_output_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise StepRunError("Codex final.json must contain a JSON object")
            validated_output = validate_contract_output(
                prepared.spec.output_contract,
                payload,
                route_target_id=prepared.route_target_id,
                candidate_specialist_ids=prepared.candidate_specialist_ids,
                requester_role=prepared.requester_role,
            )
            output_payload = validated_output.model_dump()
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
