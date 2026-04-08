from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any

from shared.agent_specs import AgentSpec


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


@dataclass(frozen=True)
class StepArtifactPaths:
    run_dir: Path
    step_dir: Path
    prompt_path: Path
    schema_path: Path
    final_output_path: Path
    stdout_jsonl_path: Path
    stderr_path: Path
    step_manifest_path: Path
    run_manifest_path: Path

    def as_payload(self) -> dict[str, str]:
        return {key: str(value) for key, value in asdict(self).items()}


def build_run_dir(settings, *, ticket_id, run_id) -> Path:
    run_dir = settings.runs_dir / str(ticket_id) / str(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def build_step_artifact_paths(settings, *, ticket_id, run_id, step_index: int, spec: AgentSpec) -> StepArtifactPaths:
    run_dir = build_run_dir(settings, ticket_id=ticket_id, run_id=run_id)
    step_dir = run_dir / f"{step_index:02d}-{spec.id}"
    step_dir.mkdir(parents=True, exist_ok=True)
    return StepArtifactPaths(
        run_dir=run_dir,
        step_dir=step_dir,
        prompt_path=step_dir / "prompt.txt",
        schema_path=step_dir / "schema.json",
        final_output_path=step_dir / "final.json",
        stdout_jsonl_path=step_dir / "stdout.jsonl",
        stderr_path=step_dir / "stderr.txt",
        step_manifest_path=step_dir / "step_manifest.json",
        run_manifest_path=run_dir / "run_manifest.json",
    )


def write_step_manifest(
    paths: StepArtifactPaths,
    *,
    step_id,
    run_id,
    ticket_id,
    step_index: int,
    step_kind: str,
    spec: AgentSpec,
    status: str,
    model_name: str | None,
    output_contract: str,
    error_text: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    payload = {
        "step_id": str(step_id),
        "run_id": str(run_id),
        "ticket_id": str(ticket_id),
        "step_index": step_index,
        "step_kind": step_kind,
        "agent_spec_id": spec.id,
        "agent_spec_version": spec.version,
        "skill_id": spec.skill_id,
        "output_contract": output_contract,
        "status": status,
        "model_name": model_name,
        "error_text": error_text,
        "paths": paths.as_payload(),
    }
    if metadata:
        payload.update(metadata)
    write_json(paths.step_manifest_path, payload)


def write_run_manifest(
    run_dir: Path,
    *,
    run_id,
    ticket_id,
    pipeline_version: str | None,
    status: str,
    final_agent_spec_id: str | None,
    final_step_id,
    final_output_contract: str | None,
    error_text: str | None,
    ended_at: str | None,
    steps: list[dict[str, Any]],
    metadata: dict[str, Any] | None = None,
) -> None:
    payload = {
        "run_id": str(run_id),
        "ticket_id": str(ticket_id),
        "pipeline_version": pipeline_version,
        "status": status,
        "final_agent_spec_id": final_agent_spec_id,
        "final_step_id": str(final_step_id) if final_step_id is not None else None,
        "final_output_contract": final_output_contract,
        "error_text": error_text,
        "ended_at": ended_at,
        "steps": steps,
    }
    if metadata:
        payload.update(metadata)
    write_json(run_dir / "run_manifest.json", payload)
