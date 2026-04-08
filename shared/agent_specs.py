from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
from string import Formatter


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AGENT_SPECS_DIR = PROJECT_ROOT / "agent_specs"
SHARED_AGENT_FRAGMENTS_DIR = AGENT_SPECS_DIR / "_shared"
SPECIALIST_SHARED_POLICY_TEMPLATE_PATH = SHARED_AGENT_FRAGMENTS_DIR / "specialist_shared_policy.md"
WORKSPACE_SKILLS_RELATIVE_DIR = Path(".agents/skills")

PIPELINE_VERSION = "agent-pipeline-v1"
LEGACY_PIPELINE_VERSION = "legacy-single-step-v1"
LEGACY_AGENT_SPEC_ID = "legacy-stage1-single-step"
LEGACY_AGENT_SPEC_VERSION = "pre-agent-specs"


class AgentSpecError(RuntimeError):
    """Raised when agent specs are missing or invalid."""


@dataclass(frozen=True)
class AgentSpec:
    id: str
    version: str
    kind: str
    description: str
    skill_id: str
    output_contract: str
    model_override: str | None
    timeout_seconds_override: int | None
    spec_dir: Path
    prompt_path: Path
    skill_path: Path
    prompt_template: str
    skill_text: str

    @property
    def workspace_skill_path(self) -> Path:
        return WORKSPACE_SKILLS_RELATIVE_DIR / self.skill_id / "SKILL.md"


def _extract_placeholders(template: str) -> tuple[str, ...]:
    placeholders: list[str] = []
    for _literal_text, field_name, _format_spec, _conversion in Formatter().parse(template):
        if field_name:
            placeholders.append(field_name)
    return tuple(placeholders)


def _load_manifest(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentSpecError(f"Invalid agent manifest: {path}") from exc
    if not isinstance(data, dict):
        raise AgentSpecError(f"Agent manifest must contain a JSON object: {path}")
    return data


def _read_required_text(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise AgentSpecError(f"Required agent spec file is unreadable: {path}") from exc
    if not text:
        raise AgentSpecError(f"Required agent spec file is empty: {path}")
    return text


@lru_cache(maxsize=1)
def load_specialist_shared_policy_template() -> str:
    template = _read_required_text(SPECIALIST_SHARED_POLICY_TEMPLATE_PATH)
    placeholders = set(_extract_placeholders(template))
    required = {"REQUESTER_ROLE", "REQUESTER_CAN_VIEW_INTERNAL_MESSAGES"}
    missing = sorted(required - placeholders)
    unknown = sorted(placeholders - required)
    if missing:
        raise AgentSpecError(
            "Shared specialist policy template is missing required placeholders: " + ", ".join(missing)
        )
    if unknown:
        raise AgentSpecError(
            "Shared specialist policy template has unsupported placeholders: " + ", ".join(unknown)
        )
    return template


def _validate_spec_dir(spec_dir: Path) -> AgentSpec:
    manifest_path = spec_dir / "manifest.json"
    prompt_path = spec_dir / "prompt.md"
    skill_path = spec_dir / "skill.md"
    if not manifest_path.is_file():
        raise AgentSpecError(f"Missing manifest.json for agent spec: {spec_dir}")
    if not prompt_path.is_file():
        raise AgentSpecError(f"Missing prompt.md for agent spec: {spec_dir}")
    if not skill_path.is_file():
        raise AgentSpecError(f"Missing skill.md for agent spec: {spec_dir}")

    manifest = _load_manifest(manifest_path)
    required_fields = (
        "id",
        "version",
        "kind",
        "description",
        "skill_id",
        "output_contract",
        "model_override",
        "timeout_seconds_override",
    )
    missing = [field for field in required_fields if field not in manifest]
    if missing:
        raise AgentSpecError(f"Agent spec {spec_dir.name} is missing required fields: {', '.join(missing)}")

    spec_id = str(manifest["id"]).strip()
    if not spec_id:
        raise AgentSpecError(f"Agent spec id must be non-empty: {spec_dir}")
    if spec_id != spec_dir.name:
        raise AgentSpecError(f"Agent spec id must match directory name: {spec_dir}")
    kind = str(manifest["kind"]).strip()
    if kind not in {"router", "selector", "specialist"}:
        raise AgentSpecError(f"Agent spec {spec_id} has invalid kind: {kind}")
    timeout_override = manifest["timeout_seconds_override"]
    if timeout_override is not None:
        if not isinstance(timeout_override, int) or timeout_override <= 0:
            raise AgentSpecError(f"Agent spec {spec_id} timeout_seconds_override must be a positive integer or null")

    prompt_template = _read_required_text(prompt_path)
    skill_text = _read_required_text(skill_path)
    shared_policy_marker_count = prompt_template.count("{SPECIALIST_SHARED_POLICY}")
    if kind == "specialist" and shared_policy_marker_count != 1:
        raise AgentSpecError(
            f"Specialist prompt {spec_id} must include {{SPECIALIST_SHARED_POLICY}} exactly once"
        )
    if kind != "specialist" and shared_policy_marker_count:
        raise AgentSpecError(
            f"Non-specialist prompt {spec_id} must not include {{SPECIALIST_SHARED_POLICY}}"
        )

    return AgentSpec(
        id=spec_id,
        version=str(manifest["version"]).strip(),
        kind=kind,
        description=str(manifest["description"]).strip(),
        skill_id=str(manifest["skill_id"]).strip(),
        output_contract=str(manifest["output_contract"]).strip(),
        model_override=str(manifest["model_override"]).strip() if manifest["model_override"] is not None else None,
        timeout_seconds_override=timeout_override,
        spec_dir=spec_dir,
        prompt_path=prompt_path,
        skill_path=skill_path,
        prompt_template=prompt_template,
        skill_text=skill_text,
    )


def load_all_agent_specs() -> tuple[AgentSpec, ...]:
    if not AGENT_SPECS_DIR.is_dir():
        raise AgentSpecError(f"Missing agent specs directory: {AGENT_SPECS_DIR}")
    specs = tuple(
        _validate_spec_dir(path)
        for path in sorted(AGENT_SPECS_DIR.iterdir())
        if path.is_dir() and not path.name.startswith((".", "_"))
    )
    if not specs:
        raise AgentSpecError("No agent specs were found")
    router_count = sum(1 for spec in specs if spec.kind == "router")
    if router_count != 1:
        raise AgentSpecError(f"Exactly one router spec is required; found {router_count}")
    load_specialist_shared_policy_template()
    return specs


def load_agent_spec(spec_id: str) -> AgentSpec:
    normalized = spec_id.strip()
    if not normalized:
        raise AgentSpecError("Agent spec id must be non-empty")
    for spec in load_all_agent_specs():
        if spec.id == normalized:
            return spec
    raise AgentSpecError(f"Unknown agent spec: {normalized}")


def load_router_spec() -> AgentSpec:
    for spec in load_all_agent_specs():
        if spec.kind == "router":
            return spec
    raise AgentSpecError("Router spec was not found")


def required_workspace_skill_paths() -> tuple[Path, ...]:
    return tuple(spec.workspace_skill_path for spec in load_all_agent_specs())
