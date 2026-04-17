from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid

from sqlalchemy import select

from shared.agent_specs import AgentSpec
from shared.config import Settings
from shared.db import session_scope
from shared.logging import log_worker_event
from shared.models import AIRun, AIRunStep, Ticket
from shared.routing_registry import RoutingRegistryError, load_routing_registry
from shared.security import utc_now
from worker.artifacts import StepArtifactPaths, build_run_dir, build_step_artifact_paths, write_run_manifest, write_step_manifest
from worker.output_contracts import OutputContractError, RouterResult, SpecialistResult, SpecialistSelectorResult, validate_contract_output, schema_json_for_contract
from worker.prompt_renderer import PromptAttachment, render_agent_prompt
from worker.run_ownership import RunOwnershipLost, load_owned_running_run
from worker.ticket_loader import LoadedTicketContext


class StepRunError(RuntimeError):
    """Raised when a step fails to execute or validate."""


@dataclass(frozen=True)
class PreparedStepRun:
    run_id: uuid.UUID
    ticket_id: uuid.UUID
    worker_instance_id: str
    step_index: int
    step_kind: str
    spec: AgentSpec
    paths: StepArtifactPaths
    prompt: str
    schema_json: str
    attachment_root: Path
    public_attachments: tuple[PromptAttachment, ...]
    image_paths: list[Path]
    model_name: str | None
    timeout_seconds: int
    requester_role: str
    route_target_id: str | None
    selected_specialist_id: str | None
    candidate_specialist_ids: tuple[str, ...] | None


@dataclass(frozen=True)
class StepRunResult:
    step_id: uuid.UUID
    prepared: PreparedStepRun
    output_payload: dict[str, object]


def _is_image_attachment(attachment) -> bool:
    return getattr(attachment, "width", None) is not None and getattr(attachment, "height", None) is not None


def _sanitize_workspace_attachment_stem(original_filename: str) -> str:
    stem = Path(original_filename).stem.strip()
    if not stem:
        return "attachment"
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    return sanitized[:64] or "attachment"


def _safe_workspace_attachment_filename(*, attachment_id, original_filename: str, source_path: Path) -> str:
    extension = source_path.suffix
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,16}", extension):
        extension = ""
    stem = _sanitize_workspace_attachment_stem(original_filename)
    return f"{attachment_id}__{stem}{extension}"


def _path_within_workspace(settings: Settings, path: Path) -> bool:
    try:
        return path.resolve(strict=False).is_relative_to(settings.triage_workspace_dir.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return False


def _workspace_relative_path(settings: Settings, path: Path) -> str:
    return str(path.resolve(strict=False).relative_to(settings.triage_workspace_dir.resolve(strict=False)))


def _materialize_public_attachments(
    settings: Settings,
    *,
    run_dir: Path,
    ticket_id,
    public_attachments,
) -> tuple[Path, tuple[PromptAttachment, ...]]:
    attachment_root = run_dir / "attachments"
    if not public_attachments:
        return attachment_root, ()
    attachment_root.mkdir(parents=True, exist_ok=True)
    materialized: list[PromptAttachment] = []
    for attachment in public_attachments:
        source_path = Path(attachment.stored_path)
        if not source_path.is_file():
            log_worker_event(
                "attachment_file_missing",
                level="warning",
                ticket_id=str(ticket_id),
                attachment_id=str(attachment.id),
                original_filename=attachment.original_filename,
                stored_path=str(source_path),
            )
            continue
        target_path = attachment_root / _safe_workspace_attachment_filename(
            attachment_id=attachment.id,
            original_filename=attachment.original_filename,
            source_path=source_path,
        )
        if not _path_within_workspace(settings, target_path):
            raise StepRunError(f"Materialized attachment path escaped the workspace: {target_path}")
        if source_path.resolve(strict=False) != target_path.resolve(strict=False) and not target_path.exists():
            shutil.copy2(source_path, target_path)
        materialized.append(
            PromptAttachment(
                attachment_id=str(attachment.id),
                original_filename=attachment.original_filename,
                mime_type=attachment.mime_type,
                size_bytes=attachment.size_bytes,
                sha256=attachment.sha256,
                workspace_path=_workspace_relative_path(settings, target_path),
                absolute_path=str(target_path.resolve(strict=False)),
                is_image=_is_image_attachment(attachment),
            )
        )
    return attachment_root, tuple(materialized)


def prepare_step_run(
    settings: Settings,
    *,
    run_id,
    ticket_id,
    worker_instance_id: str,
    step_index: int,
    step_kind: str,
    spec: AgentSpec,
    context: LoadedTicketContext,
    router_result=None,
    target_route_target_id: str | None = None,
    selected_specialist_id: str | None = None,
    candidate_specialist_ids: tuple[str, ...] | None = None,
) -> PreparedStepRun:
    resolved_route_target_id = target_route_target_id
    paths = build_step_artifact_paths(settings, ticket_id=ticket_id, run_id=run_id, step_index=step_index, spec=spec)
    attachment_root, public_attachments = _materialize_public_attachments(
        settings,
        run_dir=paths.run_dir,
        ticket_id=ticket_id,
        public_attachments=context.public_attachments,
    )
    prompt = render_agent_prompt(
        spec,
        context=context,
        public_attachments=public_attachments,
        attachments_root=_workspace_relative_path(settings, attachment_root) if public_attachments else "(none)",
        router_result=router_result,
        target_route_target_id=resolved_route_target_id,
        candidate_specialist_ids=candidate_specialist_ids,
    )
    schema_json = schema_json_for_contract(spec.output_contract)
    paths.prompt_path.write_text(prompt, encoding="utf-8")
    paths.schema_path.write_text(schema_json, encoding="utf-8")
    image_paths = [Path(attachment.absolute_path) for attachment in public_attachments if attachment.is_image]
    model_name = spec.model_override or settings.codex_model or None
    timeout_seconds = spec.timeout_seconds_override or settings.codex_timeout_seconds
    return PreparedStepRun(
        run_id=run_id,
        ticket_id=ticket_id,
        worker_instance_id=worker_instance_id,
        step_index=step_index,
        step_kind=step_kind,
        spec=spec,
        paths=paths,
        prompt=prompt,
        schema_json=schema_json,
        attachment_root=attachment_root,
        public_attachments=public_attachments,
        image_paths=image_paths,
        model_name=model_name,
        timeout_seconds=timeout_seconds,
        requester_role=context.requester_role,
        route_target_id=resolved_route_target_id,
        selected_specialist_id=selected_specialist_id,
        candidate_specialist_ids=candidate_specialist_ids,
    )


def _ownership_lost_error(*, run_id, worker_instance_id: str, phase: str) -> RunOwnershipLost:
    return RunOwnershipLost(
        f"Run {run_id} is no longer running for worker {worker_instance_id} during {phase}."
    )


def build_codex_command(settings: Settings, *, prepared: PreparedStepRun) -> tuple[list[str], dict[str, str]]:
    command = [
        settings.codex_bin,
        "--ask-for-approval",
        "never",
        "exec",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--json",
        "--output-schema",
        str(prepared.paths.schema_path),
        "--output-last-message",
        str(prepared.paths.final_output_path),
        "-c",
        'web_search="disabled"',
    ]
    if prepared.model_name:
        command.extend(["--model", prepared.model_name])
    for image_path in prepared.image_paths:
        command.extend(["--image", str(image_path)])
    command.append("-")
    env = os.environ.copy()
    if settings.codex_api_key:
        env["CODEX_API_KEY"] = settings.codex_api_key
    else:
        env.pop("CODEX_API_KEY", None)
    return command, env


def _normalize_stream_contents(contents: str | bytes | None) -> str:
    if contents is None:
        return ""
    if isinstance(contents, bytes):
        return contents.decode("utf-8", errors="replace")
    return contents


def _write_stream(path: Path, contents: str | bytes | None) -> None:
    path.write_text(_normalize_stream_contents(contents), encoding="utf-8")


def _load_final_output(final_output_path: Path) -> dict[str, object]:
    if not final_output_path.is_file():
        raise StepRunError("Codex did not write final.json")
    try:
        payload = json.loads(final_output_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StepRunError("Codex final.json was missing or invalid JSON") from exc
    if not isinstance(payload, dict):
        raise StepRunError("Codex final.json must contain a JSON object")
    return payload


def _write_final_output(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _create_running_step_row(settings: Settings, prepared: PreparedStepRun) -> uuid.UUID:
    with session_scope(settings) as db:
        run = load_owned_running_run(
            db,
            run_id=prepared.run_id,
            worker_instance_id=prepared.worker_instance_id,
        )
        if run is None:
            raise _ownership_lost_error(
                run_id=prepared.run_id,
                worker_instance_id=prepared.worker_instance_id,
                phase="step start",
            )
        started_at = utc_now()
        run.last_heartbeat_at = started_at
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
            started_at=started_at,
        )
        db.add(step)
        db.flush()
        step_id = step.id
    write_step_manifest(
        prepared.paths,
        step_id=step_id,
        run_id=prepared.run_id,
        ticket_id=prepared.ticket_id,
        step_index=prepared.step_index,
        step_kind=prepared.step_kind,
        spec=prepared.spec,
        status="running",
        model_name=prepared.model_name,
        output_contract=prepared.spec.output_contract,
        metadata=_step_manifest_metadata(prepared, output_payload=None),
    )
    return step_id


def _step_manifest_metadata(
    prepared: PreparedStepRun,
    *,
    output_payload: dict[str, object] | None,
) -> dict[str, object]:
    metadata: dict[str, object] = {}
    if prepared.route_target_id is not None:
        metadata["route_target_id"] = prepared.route_target_id
    if prepared.selected_specialist_id is not None:
        metadata["selected_specialist_id"] = prepared.selected_specialist_id
    if prepared.candidate_specialist_ids is not None:
        metadata["candidate_specialist_ids"] = list(prepared.candidate_specialist_ids)
    metadata["attachments_root"] = _normalize_stream_contents(str(prepared.attachment_root))
    metadata["public_attachments"] = [attachment.as_payload() for attachment in prepared.public_attachments]
    if output_payload is None:
        return metadata
    if prepared.step_kind == "router":
        router_result = RouterResult.model_validate(output_payload)
        metadata["route_target_id"] = router_result.route_target_id
        metadata["routing_rationale"] = router_result.routing_rationale
    elif prepared.step_kind == "selector":
        selector_result = SpecialistSelectorResult.model_validate(output_payload)
        metadata["selected_specialist_id"] = selector_result.specialist_id
        metadata["selection_rationale"] = selector_result.selection_rationale
    elif prepared.step_kind == "specialist":
        specialist_result = SpecialistResult.model_validate(output_payload)
        metadata["publish_mode_recommendation"] = specialist_result.publish_mode_recommendation
        metadata["response_confidence"] = specialist_result.response_confidence
        metadata["risk_level"] = specialist_result.risk_level
    return metadata


def _update_step_row(
    *,
    settings: Settings,
    run_id,
    worker_instance_id: str,
    step_id,
    status: str,
    output_payload: dict[str, object] | None,
    error_text: str | None,
) -> None:
    with session_scope(settings) as db:
        run = load_owned_running_run(
            db,
            run_id=run_id,
            worker_instance_id=worker_instance_id,
        )
        if run is None:
            raise _ownership_lost_error(
                run_id=run_id,
                worker_instance_id=worker_instance_id,
                phase="step finish",
            )
        step = db.get(AIRunStep, step_id)
        if step is None:
            raise StepRunError(f"Missing AIRunStep row {step_id} for run {run_id}")
        if step.ai_run_id != run.id:
            raise StepRunError(f"AIRunStep row {step_id} belongs to a different run")
        completed_at = utc_now()
        step.status = status
        step.output_json = output_payload
        step.error_text = error_text
        step.ended_at = completed_at
        run.last_heartbeat_at = completed_at


def execute_step(settings: Settings, *, prepared: PreparedStepRun) -> StepRunResult:
    step_id = _create_running_step_row(settings, prepared)
    command, env = build_codex_command(settings, prepared=prepared)
    output_payload: dict[str, object] | None = None
    error_text: str | None = None
    status = "failed"
    try:
        completed = subprocess.run(
            command,
            cwd=settings.triage_workspace_dir,
            env=env,
            capture_output=True,
            text=True,
            input=prepared.prompt,
            timeout=prepared.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        _write_stream(prepared.paths.stdout_jsonl_path, exc.stdout)
        _write_stream(prepared.paths.stderr_path, exc.stderr)
        error_text = f"Codex timed out after {prepared.timeout_seconds} seconds"
        _update_step_row(
            settings=settings,
            run_id=prepared.run_id,
            worker_instance_id=prepared.worker_instance_id,
            step_id=step_id,
            status=status,
            output_payload=None,
            error_text=error_text,
        )
        write_step_manifest(
            prepared.paths,
            step_id=step_id,
            run_id=prepared.run_id,
            ticket_id=prepared.ticket_id,
            step_index=prepared.step_index,
            step_kind=prepared.step_kind,
            spec=prepared.spec,
            status=status,
            model_name=prepared.model_name,
            output_contract=prepared.spec.output_contract,
            error_text=error_text,
            metadata=_step_manifest_metadata(prepared, output_payload=None),
        )
        raise StepRunError(error_text) from exc

    _write_stream(prepared.paths.stdout_jsonl_path, completed.stdout)
    _write_stream(prepared.paths.stderr_path, completed.stderr)
    if completed.returncode != 0:
        error_text = f"Codex exited with status {completed.returncode}"
        _update_step_row(
            settings=settings,
            run_id=prepared.run_id,
            worker_instance_id=prepared.worker_instance_id,
            step_id=step_id,
            status=status,
            output_payload=None,
            error_text=error_text,
        )
        write_step_manifest(
            prepared.paths,
            step_id=step_id,
            run_id=prepared.run_id,
            ticket_id=prepared.ticket_id,
            step_index=prepared.step_index,
            step_kind=prepared.step_kind,
            spec=prepared.spec,
            status=status,
            model_name=prepared.model_name,
            output_contract=prepared.spec.output_contract,
            error_text=error_text,
            metadata=_step_manifest_metadata(prepared, output_payload=None),
        )
        raise StepRunError(error_text)

    payload = _load_final_output(prepared.paths.final_output_path)
    try:
        validate_contract_output(
            prepared.spec.output_contract,
            payload,
            route_target_id=prepared.route_target_id,
            candidate_specialist_ids=prepared.candidate_specialist_ids,
            requester_role=prepared.requester_role,
        )
    except OutputContractError as exc:
        error_text = str(exc)
        _update_step_row(
            settings=settings,
            run_id=prepared.run_id,
            worker_instance_id=prepared.worker_instance_id,
            step_id=step_id,
            status=status,
            output_payload=None,
            error_text=error_text,
        )
        write_step_manifest(
            prepared.paths,
            step_id=step_id,
            run_id=prepared.run_id,
            ticket_id=prepared.ticket_id,
            step_index=prepared.step_index,
            step_kind=prepared.step_kind,
            spec=prepared.spec,
            status=status,
            model_name=prepared.model_name,
            output_contract=prepared.spec.output_contract,
            error_text=error_text,
            metadata=_step_manifest_metadata(prepared, output_payload=None),
        )
        raise StepRunError(error_text) from exc

    output_payload = payload
    status = "succeeded"
    _update_step_row(
        settings=settings,
        run_id=prepared.run_id,
        worker_instance_id=prepared.worker_instance_id,
        step_id=step_id,
        status=status,
        output_payload=output_payload,
        error_text=None,
    )
    write_step_manifest(
        prepared.paths,
        step_id=step_id,
        run_id=prepared.run_id,
        ticket_id=prepared.ticket_id,
        step_index=prepared.step_index,
        step_kind=prepared.step_kind,
        spec=prepared.spec,
        status=status,
        model_name=prepared.model_name,
        output_contract=prepared.spec.output_contract,
        error_text=None,
        metadata=_step_manifest_metadata(prepared, output_payload=output_payload),
    )
    return StepRunResult(step_id=step_id, prepared=prepared, output_payload=output_payload)


def record_synthetic_step_success(
    settings: Settings,
    *,
    run_id,
    ticket_id,
    worker_instance_id: str,
    step_index: int,
    step_kind: str,
    spec: AgentSpec,
    output_payload: dict[str, object],
    prompt_text: str,
    route_target_id: str | None = None,
    selected_specialist_id: str | None = None,
    candidate_specialist_ids: tuple[str, ...] | None = None,
    requester_role: str | None = None,
) -> StepRunResult:
    validate_contract_output(
        spec.output_contract,
        output_payload,
        route_target_id=route_target_id,
        candidate_specialist_ids=candidate_specialist_ids,
        requester_role=requester_role,
    )
    paths = build_step_artifact_paths(settings, ticket_id=ticket_id, run_id=run_id, step_index=step_index, spec=spec)
    schema_json = schema_json_for_contract(spec.output_contract)
    paths.prompt_path.write_text(prompt_text, encoding="utf-8")
    paths.schema_path.write_text(schema_json, encoding="utf-8")
    _write_final_output(paths.final_output_path, output_payload)
    paths.stdout_jsonl_path.write_text("", encoding="utf-8")
    paths.stderr_path.write_text("", encoding="utf-8")

    prepared = PreparedStepRun(
        run_id=run_id,
        ticket_id=ticket_id,
        worker_instance_id=worker_instance_id,
        step_index=step_index,
        step_kind=step_kind,
        spec=spec,
        paths=paths,
        prompt=prompt_text,
        schema_json=schema_json,
        attachment_root=paths.run_dir / "attachments",
        public_attachments=(),
        image_paths=[],
        model_name=None,
        timeout_seconds=0,
        requester_role=requester_role or "requester",
        route_target_id=route_target_id,
        selected_specialist_id=selected_specialist_id,
        candidate_specialist_ids=candidate_specialist_ids,
    )
    with session_scope(settings) as db:
        run = load_owned_running_run(
            db,
            run_id=run_id,
            worker_instance_id=worker_instance_id,
        )
        if run is None:
            raise _ownership_lost_error(
                run_id=run_id,
                worker_instance_id=worker_instance_id,
                phase="synthetic step",
            )
        completed_at = utc_now()
        run.last_heartbeat_at = completed_at
        step = AIRunStep(
            ai_run_id=run_id,
            step_index=step_index,
            step_kind=step_kind,
            agent_spec_id=spec.id,
            agent_spec_version=spec.version,
            output_contract=spec.output_contract,
            model_name=None,
            status="succeeded",
            prompt_path=str(paths.prompt_path),
            schema_path=str(paths.schema_path),
            final_output_path=str(paths.final_output_path),
            stdout_jsonl_path=str(paths.stdout_jsonl_path),
            stderr_path=str(paths.stderr_path),
            output_json=output_payload,
            error_text=None,
            started_at=completed_at,
            ended_at=completed_at,
        )
        db.add(step)
        db.flush()
        step_id = step.id
    write_step_manifest(
        paths,
        step_id=step_id,
        run_id=run_id,
        ticket_id=ticket_id,
        step_index=step_index,
        step_kind=step_kind,
        spec=spec,
        status="succeeded",
        model_name=None,
        output_contract=spec.output_contract,
        error_text=None,
        metadata=_step_manifest_metadata(prepared, output_payload=output_payload),
    )
    return StepRunResult(step_id=step_id, prepared=prepared, output_payload=output_payload)


def _step_payload_for_run_manifest(step: AIRunStep) -> dict[str, object]:
    payload = {
        "step_id": str(step.id),
        "step_index": step.step_index,
        "step_kind": step.step_kind,
        "agent_spec_id": step.agent_spec_id,
        "agent_spec_version": step.agent_spec_version,
        "output_contract": step.output_contract,
        "status": step.status,
        "model_name": step.model_name,
        "paths": {
            "prompt_path": step.prompt_path,
            "schema_path": step.schema_path,
            "final_output_path": step.final_output_path,
            "stdout_jsonl_path": step.stdout_jsonl_path,
            "stderr_path": step.stderr_path,
        },
    }
    if isinstance(step.output_json, dict):
        if step.step_kind == "router":
            payload["route_target_id"] = step.output_json.get("route_target_id")
            payload["routing_rationale"] = step.output_json.get("routing_rationale")
        elif step.step_kind == "selector":
            payload["selected_specialist_id"] = step.output_json.get("specialist_id")
            payload["selection_rationale"] = step.output_json.get("selection_rationale")
        elif step.step_kind == "specialist":
            payload["publish_mode_recommendation"] = step.output_json.get("publish_mode_recommendation")
            payload["response_confidence"] = step.output_json.get("response_confidence")
            payload["risk_level"] = step.output_json.get("risk_level")
    return payload


def _resolve_route_target_details(ticket: Ticket, steps: list[AIRunStep]) -> tuple[str | None, str | None, str | None]:
    route_target_id = None
    for step in steps:
        if step.step_kind == "router" and isinstance(step.output_json, dict):
            candidate_route_target_id = step.output_json.get("route_target_id")
            if isinstance(candidate_route_target_id, str) and candidate_route_target_id.strip():
                route_target_id = candidate_route_target_id
                break
    if route_target_id is None:
        route_target_id = ticket.route_target_id
    if not isinstance(route_target_id, str) or not route_target_id.strip():
        return None, None, None
    try:
        route_target = load_routing_registry().require_route_target(route_target_id)
    except (RoutingRegistryError, RuntimeError):
        return route_target_id, route_target_id, None
    return route_target.id, route_target.label, route_target.kind


def _resolve_selected_specialist_id(steps: list[AIRunStep], *, route_target, run: AIRun) -> str | None:
    for step in steps:
        if step.step_kind == "selector" and isinstance(step.output_json, dict):
            specialist_id = step.output_json.get("specialist_id")
            if isinstance(specialist_id, str) and specialist_id.strip():
                return specialist_id
    forced_specialist_id = getattr(run, "forced_specialist_id", None)
    if isinstance(forced_specialist_id, str) and forced_specialist_id.strip():
        return forced_specialist_id
    if route_target is None:
        return None
    selection = getattr(getattr(route_target, "handler", None), "specialist_selection", None)
    specialist_id = getattr(selection, "specialist_id", None)
    if getattr(selection, "mode", None) == "fixed" and isinstance(specialist_id, str) and specialist_id.strip():
        return specialist_id
    return None


def _resolve_effective_publication_mode(run: AIRun, ticket: Ticket) -> str | None:
    if run.status not in {"succeeded", "human_review"} or not isinstance(run.final_output_json, dict):
        return None
    last_ai_action = getattr(ticket, "last_ai_action", None)
    if last_ai_action == "auto_public_reply":
        return "auto_publish"
    if last_ai_action == "draft_public_reply":
        return "draft_for_human"
    if last_ai_action == "manual_only":
        return "manual_only"
    public_reply = run.final_output_json.get("public_reply_markdown")
    if isinstance(public_reply, str):
        return "draft_for_human" if public_reply.strip() else "manual_only"
    return None


def write_run_manifest_snapshot(settings: Settings, *, run_id) -> None:
    with session_scope(settings) as db:
        run = db.get(AIRun, run_id)
        if run is None:
            return
        ticket = db.get(Ticket, run.ticket_id)
        if ticket is None:
            return
        steps = list(
            db.execute(
                select(AIRunStep).where(AIRunStep.ai_run_id == run.id).order_by(AIRunStep.step_index.asc())
            ).scalars()
        )

    route_target_id, route_target_label, route_target_kind = _resolve_route_target_details(ticket, steps)
    route_target = None
    if isinstance(route_target_id, str) and route_target_id.strip():
        try:
            route_target = load_routing_registry().require_route_target(route_target_id)
        except (RoutingRegistryError, RuntimeError):
            route_target = None
    write_run_manifest(
        build_run_dir(settings, ticket_id=run.ticket_id, run_id=run.id),
        run_id=run.id,
        ticket_id=run.ticket_id,
        pipeline_version=run.pipeline_version,
        status=run.status,
        final_agent_spec_id=run.final_agent_spec_id,
        final_step_id=run.final_step_id,
        final_output_contract=run.final_output_contract,
        error_text=run.error_text,
        ended_at=run.ended_at.isoformat() if run.ended_at is not None else None,
        steps=[_step_payload_for_run_manifest(step) for step in steps],
        metadata={
            "route_target_id": route_target_id,
            "route_target_label": route_target_label,
            "route_target_kind": route_target_kind,
            "selected_specialist_id": _resolve_selected_specialist_id(steps, route_target=route_target, run=run),
            "effective_publication_mode": _resolve_effective_publication_mode(run, ticket),
            "forced_route_target_id": getattr(run, "forced_route_target_id", None),
            "forced_specialist_id": getattr(run, "forced_specialist_id", None),
            "worker_pid": getattr(run, "worker_pid", None),
            "worker_instance_id": getattr(run, "worker_instance_id", None),
            "started_at": getattr(run, "started_at", None).isoformat() if getattr(run, "started_at", None) is not None else None,
            "last_heartbeat_at": run.last_heartbeat_at.isoformat() if getattr(run, "last_heartbeat_at", None) is not None else None,
            "recovered_from_run_id": str(getattr(run, "recovered_from_run_id", None)) if getattr(run, "recovered_from_run_id", None) is not None else None,
            "recovery_attempt_count": getattr(run, "recovery_attempt_count", 0),
        },
    )
