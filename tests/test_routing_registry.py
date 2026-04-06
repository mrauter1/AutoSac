from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from shared.agent_specs import load_agent_spec, required_workspace_skill_paths
from shared.config import Settings
from shared.routing_registry import RoutingRegistryError, load_routing_registry
from worker.output_contracts import OutputContractError, RouterResult, validate_contract_output
from worker.prompt_renderer import PromptRenderError, render_agent_prompt


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = PROJECT_ROOT / "agent_specs" / "registry.json"


def _registry_payload() -> dict[str, object]:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def _write_registry(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _make_settings(tmp_path: Path) -> Settings:
    workspace_dir = tmp_path / "workspace"
    return Settings(
        app_base_url="http://localhost:8000",
        app_secret_key="test-secret",
        database_url="postgresql+psycopg://triage:triage@localhost:5432/triage",
        uploads_dir=tmp_path / "uploads",
        triage_workspace_dir=workspace_dir,
        repo_mount_dir=workspace_dir / "app",
        manuals_mount_dir=workspace_dir / "manuals",
        codex_bin="codex",
        codex_api_key="test-key",
        codex_model="",
        codex_timeout_seconds=3600,
        worker_poll_seconds=10,
        auto_support_reply_min_confidence=0.85,
        auto_confirm_intent_min_confidence=0.90,
        max_images_per_message=3,
        max_image_bytes=5 * 1024 * 1024,
        session_default_hours=12,
        session_remember_days=30,
    )


def _make_context():
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        reference="T-000001",
        title="Need help with routing",
        status="new",
        urgent=False,
    )
    public_message = SimpleNamespace(
        author_type="requester",
        source="ticket_create",
        created_at=SimpleNamespace(isoformat=lambda: "2026-04-06T00:00:00+00:00"),
        body_text="I need help with this workflow.",
    )
    internal_message = SimpleNamespace(
        author_type="dev_ti",
        source="human_internal_note",
        created_at=SimpleNamespace(isoformat=lambda: "2026-04-06T00:01:00+00:00"),
        body_text="Check the manuals before escalating.",
    )
    return SimpleNamespace(
        ticket=ticket,
        requester_role="requester",
        requester_can_view_internal_messages=False,
        public_messages=[public_message],
        internal_messages=[internal_message],
        public_attachments=[],
    )


def test_load_routing_registry_reads_current_registry() -> None:
    registry = load_routing_registry(REGISTRY_PATH)

    assert registry.router_spec.id == "router"
    assert registry.selector_spec is not None
    assert registry.selector_spec.id == "specialist-selector"
    assert registry.require_enabled_route_target("support").label == "Support"
    assert registry.require_route_target("manual_review").enabled is False


def test_load_routing_registry_rejects_duplicate_route_target_ids(tmp_path: Path) -> None:
    payload = _registry_payload()
    payload["route_targets"].append(dict(payload["route_targets"][0]))

    with pytest.raises(RoutingRegistryError, match="duplicate route target ids"):
        load_routing_registry(_write_registry(tmp_path, payload))


def test_load_routing_registry_rejects_duplicate_specialist_ids(tmp_path: Path) -> None:
    payload = _registry_payload()
    payload["specialists"].append(dict(payload["specialists"][0]))

    with pytest.raises(RoutingRegistryError, match="duplicate specialist ids"):
        load_routing_registry(_write_registry(tmp_path, payload))


def test_load_routing_registry_rejects_missing_spec_reference(tmp_path: Path) -> None:
    payload = _registry_payload()
    payload["specialists"][0]["spec_id"] = "missing-spec"

    with pytest.raises(RoutingRegistryError, match="references missing spec"):
        load_routing_registry(_write_registry(tmp_path, payload))


def test_load_routing_registry_rejects_invalid_fixed_selection_config(tmp_path: Path) -> None:
    payload = _registry_payload()
    del payload["route_targets"][0]["handler"]["specialist_selection"]["specialist_id"]

    with pytest.raises(RoutingRegistryError, match="mode=fixed must define specialist_id"):
        load_routing_registry(_write_registry(tmp_path, payload))


def test_load_routing_registry_rejects_invalid_auto_config_for_direct_ai(tmp_path: Path) -> None:
    payload = _registry_payload()
    payload["route_targets"][0]["handler"]["specialist_selection"] = {"mode": "auto"}

    with pytest.raises(RoutingRegistryError, match="must define candidate_specialist_ids"):
        load_routing_registry(_write_registry(tmp_path, payload))


def test_load_routing_registry_rejects_missing_selector_spec_id_for_auto_targets(tmp_path: Path) -> None:
    payload = _registry_payload()
    payload["selector_spec_id"] = None

    with pytest.raises(RoutingRegistryError, match="selector_spec_id is required"):
        load_routing_registry(_write_registry(tmp_path, payload))


def test_load_routing_registry_rejects_invalid_none_config_for_direct_ai(tmp_path: Path) -> None:
    payload = _registry_payload()
    payload["route_targets"][0]["handler"]["specialist_selection"] = {"mode": "none"}

    with pytest.raises(RoutingRegistryError, match="must use kind=human_assist"):
        load_routing_registry(_write_registry(tmp_path, payload))


def test_load_routing_registry_rejects_invalid_publish_policy_confidence(tmp_path: Path) -> None:
    payload = _registry_payload()
    payload["route_targets"][0]["publish_policy"]["min_response_confidence_for_auto_publish"] = "sometimes"

    with pytest.raises(RoutingRegistryError, match="invalid min_response_confidence_for_auto_publish"):
        load_routing_registry(_write_registry(tmp_path, payload))


def test_load_routing_registry_rejects_human_assist_auto_publish(tmp_path: Path) -> None:
    payload = _registry_payload()
    for route_target in payload["route_targets"]:
        if route_target["id"] == "manual_review":
            route_target["publish_policy"]["allow_auto_publish"] = True
            break

    with pytest.raises(RoutingRegistryError, match="must not allow auto publish"):
        load_routing_registry(_write_registry(tmp_path, payload))


def test_load_routing_registry_rejects_enabled_target_with_disabled_specialist(tmp_path: Path) -> None:
    payload = _registry_payload()
    payload["specialists"][0]["enabled"] = False

    with pytest.raises(RoutingRegistryError, match="references disabled specialist"):
        load_routing_registry(_write_registry(tmp_path, payload))


def test_load_routing_registry_resolves_human_assist_auto_candidates(tmp_path: Path) -> None:
    payload = _registry_payload()
    for route_target in payload["route_targets"]:
        if route_target["id"] == "manual_review":
            route_target["enabled"] = True
            break

    registry = load_routing_registry(_write_registry(tmp_path, payload))

    assert [specialist.id for specialist in registry.candidate_specialists_for_target("manual_review")] == [
        "support",
        "access-config",
        "data-ops",
        "bug",
        "feature",
    ]


def test_validate_contract_output_rejects_disabled_router_target() -> None:
    with pytest.raises(OutputContractError, match="disabled for new runs"):
        validate_contract_output(
            "router_result",
            {
                "route_target_id": "manual_review",
                "routing_rationale": "Needs a human operator.",
            },
        )


def test_validate_contract_output_rejects_selector_choice_outside_candidates() -> None:
    with pytest.raises(OutputContractError, match="outside the allowed candidate set"):
        validate_contract_output(
            "specialist_selector_result",
            {
                "specialist_id": "bug",
                "selection_rationale": "Debugging seems likely.",
            },
            route_target_id="support",
            candidate_specialist_ids=("support", "feature"),
        )


def test_validate_contract_output_rejects_specialist_result_without_public_reply() -> None:
    with pytest.raises(OutputContractError, match="public_reply_markdown"):
        validate_contract_output(
            "specialist_result",
            {
                "requester_language": "en",
                "public_reply_markdown": "",
                "internal_note_markdown": "",
                "response_confidence": "high",
                "risk_level": "low",
                "risk_reason": "Low-risk answer.",
                "summary_internal": "Requester needs guidance.",
                "publish_mode_recommendation": "auto_publish",
            },
        )


def test_validate_contract_output_rejects_handoff_assistant_mismatch() -> None:
    with pytest.raises(OutputContractError, match="assistant_specialist_id"):
        validate_contract_output(
            "human_handoff_result",
            {
                "route_target_id": "manual_review",
                "handoff_reason": "Needs a human.",
                "summary_internal": "Escalate to the queue.",
                "internal_note_markdown": "Operator review required.",
                "public_reply_markdown": "",
                "assistant_used": True,
                "assistant_specialist_id": None,
            },
        )


def test_render_router_prompt_includes_generated_route_target_catalog() -> None:
    prompt = render_agent_prompt(load_agent_spec("router"), context=_make_context())

    assert "Enabled route targets:" in prompt
    assert "- id: support" in prompt
    assert "- id: access_config" in prompt
    assert "manual_review" not in prompt


def test_render_selector_prompt_includes_candidate_catalog() -> None:
    prompt = render_agent_prompt(
        load_agent_spec("specialist-selector"),
        context=_make_context(),
        target_route_target_id="support",
        candidate_specialist_ids=("support", "bug"),
    )

    assert "Route target ID: support" in prompt
    assert "Candidate specialists:" in prompt
    assert "- id: support" in prompt
    assert "- id: bug" in prompt


def test_render_specialist_prompt_includes_route_target_context() -> None:
    prompt = render_agent_prompt(
        load_agent_spec("support"),
        context=_make_context(),
        router_result=RouterResult.model_validate(
            {
                "route_target_id": "support",
                "routing_rationale": "The requester needs low-risk usage guidance.",
            }
        ),
        target_route_target_id="support",
    )

    assert "Route target ID: support" in prompt
    assert "Route target label: Support" in prompt
    assert "Route target kind: direct_ai" in prompt
    assert "Route target description:" in prompt
    assert "If the provided schema includes `ticket_class`, set it exactly to the selected route target ID." in prompt


def test_render_agent_prompt_raises_for_unknown_placeholder() -> None:
    fake_spec = SimpleNamespace(
        id="broken-spec",
        kind="router",
        skill_id="broken-skill",
        prompt_template="Missing placeholder: {DOES_NOT_EXIST}",
    )

    with pytest.raises(PromptRenderError, match="Missing prompt placeholder value: DOES_NOT_EXIST"):
        render_agent_prompt(fake_spec, context=_make_context())


def test_verify_workspace_contract_paths_propagates_registry_failure(monkeypatch, tmp_path: Path) -> None:
    from shared.workspace import verify_workspace_contract_paths

    settings = _make_settings(tmp_path)
    settings.triage_workspace_dir.mkdir(parents=True)
    settings.runs_dir.mkdir(parents=True)
    settings.repo_mount_dir.mkdir(parents=True)
    settings.manuals_mount_dir.mkdir(parents=True)
    settings.workspace_agents_path.write_text("agents", encoding="utf-8")
    for relative_path in required_workspace_skill_paths():
        skill_path = settings.triage_workspace_dir / relative_path
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text("skill", encoding="utf-8")

    monkeypatch.setattr("shared.workspace.load_routing_registry", lambda: (_ for _ in ()).throw(RuntimeError("bad registry")))

    with pytest.raises(RuntimeError, match="bad registry"):
        verify_workspace_contract_paths(settings)
