from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from shared import agent_specs as agent_specs_module
from shared.agent_specs import load_agent_spec
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
    assert registry.require_enabled_route_target("business_analyst").label == "Business Analyst"
    assert registry.require_enabled_route_target("software_architect").label == "Software Architect"
    assert registry.require_enabled_route_target("manual_review").kind == "human_assist"
    assert registry.require_specialist("business-analyst").spec.id == "business-analyst"
    assert registry.require_specialist("software-architect").spec.id == "software-architect"
    assert registry.require_route_target("unknown").enabled is False


def test_load_all_agent_specs_ignores_shared_fragment_dirs() -> None:
    spec_ids = {spec.id for spec in agent_specs_module.load_all_agent_specs()}

    assert "_shared" not in spec_ids


def test_load_specialist_shared_policy_template_requires_expected_placeholders(monkeypatch, tmp_path: Path) -> None:
    template_path = tmp_path / "specialist_shared_policy.md"
    template_path.write_text("Role: {REQUESTER_ROLE}", encoding="utf-8")
    monkeypatch.setattr(agent_specs_module, "SPECIALIST_SHARED_POLICY_TEMPLATE_PATH", template_path)
    agent_specs_module.load_specialist_shared_policy_template.cache_clear()

    with pytest.raises(
        agent_specs_module.AgentSpecError,
        match="missing required placeholders: REQUESTER_CAN_VIEW_INTERNAL_MESSAGES",
    ):
        agent_specs_module.load_specialist_shared_policy_template()

    agent_specs_module.load_specialist_shared_policy_template.cache_clear()


def test_load_specialist_shared_policy_template_rejects_unsupported_placeholders(monkeypatch, tmp_path: Path) -> None:
    template_path = tmp_path / "specialist_shared_policy.md"
    template_path.write_text(
        "Role: {REQUESTER_ROLE}\nVisibility: {REQUESTER_CAN_VIEW_INTERNAL_MESSAGES}\nBad: {STATUS}",
        encoding="utf-8",
    )
    monkeypatch.setattr(agent_specs_module, "SPECIALIST_SHARED_POLICY_TEMPLATE_PATH", template_path)
    agent_specs_module.load_specialist_shared_policy_template.cache_clear()

    with pytest.raises(agent_specs_module.AgentSpecError, match="unsupported placeholders: STATUS"):
        agent_specs_module.load_specialist_shared_policy_template()

    agent_specs_module.load_specialist_shared_policy_template.cache_clear()


def test_validate_spec_dir_requires_specialist_shared_policy_placeholder(tmp_path: Path) -> None:
    spec_dir = tmp_path / "bad-specialist"
    spec_dir.mkdir()
    (spec_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": "bad-specialist",
                "version": "1",
                "kind": "specialist",
                "description": "Broken specialist",
                "skill_id": "triage-bad-specialist",
                "output_contract": "specialist_result",
                "model_override": None,
                "timeout_seconds_override": None,
            }
        ),
        encoding="utf-8",
    )
    (spec_dir / "prompt.md").write_text("Prompt without shared policy", encoding="utf-8")
    (spec_dir / "skill.md").write_text("skill", encoding="utf-8")

    with pytest.raises(
        agent_specs_module.AgentSpecError,
        match=r"must include \{SPECIALIST_SHARED_POLICY\} exactly once",
    ):
        agent_specs_module._validate_spec_dir(spec_dir)


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
        "business-analyst",
        "software-architect",
    ]


def test_load_routing_registry_exposes_manual_rerun_specialist_options() -> None:
    registry = load_routing_registry(REGISTRY_PATH)

    options = registry.manual_rerun_specialist_options()
    option_ids = [option.route_target_id for option in options]

    assert "support" in option_ids
    assert "business_analyst" in option_ids
    assert "software_architect" in option_ids
    assert "manual_review" not in option_ids
    assert "unknown" not in option_ids


def test_load_routing_registry_resolves_persisted_forced_manual_rerun_choice_even_if_disabled(tmp_path: Path) -> None:
    payload = _registry_payload()
    for route_target in payload["route_targets"]:
        if route_target["id"] == "software_architect":
            route_target["enabled"] = False
            break
    for specialist in payload["specialists"]:
        if specialist["id"] == "software-architect":
            specialist["enabled"] = False
            break

    registry = load_routing_registry(_write_registry(tmp_path, payload))

    choice = registry.resolve_forced_manual_rerun_choice(
        route_target_id="software_architect",
        specialist_id="software-architect",
    )

    assert choice.route_target_id == "software_architect"
    assert choice.specialist_id == "software-architect"


def test_validate_contract_output_rejects_disabled_router_target() -> None:
    with pytest.raises(OutputContractError, match="disabled for new runs"):
        validate_contract_output(
            "router_result",
            {
                "route_target_id": "unknown",
                "routing_rationale": "Historical-only target.",
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


def test_validate_contract_output_accepts_legacy_triage_result_without_hardcoded_ticket_taxonomy() -> None:
    result = validate_contract_output(
        "triage_result",
        {
            "ticket_class": "manual_review",
            "confidence": 0.9,
            "impact_level": "medium",
            "requester_language": "en",
            "summary_short": "Accepted analysis",
            "summary_internal": "Internal accepted analysis",
            "development_needed": False,
            "needs_clarification": False,
            "clarifying_questions": [],
            "incorrect_or_conflicting_details": [],
            "evidence_found": True,
            "relevant_paths": [{"path": "manuals/access.md", "reason": "Relevant doc"}],
            "answer_scope": "document_scoped",
            "evidence_status": "verified",
            "misuse_or_safety_risk": False,
            "human_review_reason": "",
            "recommended_next_action": "auto_public_reply",
            "auto_public_reply_allowed": True,
            "public_reply_markdown": "Reply",
            "internal_note_markdown": "Note",
        },
    )

    assert result.ticket_class == "manual_review"


def test_render_router_prompt_includes_generated_route_target_catalog() -> None:
    prompt = render_agent_prompt(load_agent_spec("router"), context=_make_context())

    assert "Enabled route targets:" in prompt
    assert "Repo-local code, migrations, DDL, and schema dumps under app/ are allowed when relevant." in prompt
    assert "technical repo and schema details are valid routing evidence." in prompt
    assert "- id: support" in prompt
    assert "- id: access_config" in prompt
    assert "- id: business_analyst" in prompt
    assert "- id: software_architect" in prompt
    assert "- id: manual_review" in prompt
    assert "unknown" not in prompt
    assert "TARGET_TICKET_CLASS" not in prompt
    assert "ROUTER_TICKET_CLASS" not in prompt
    assert "ticket_class" not in prompt
    assert "ticket class" not in prompt.lower()


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
    assert "TARGET_TICKET_CLASS" not in prompt
    assert "ROUTER_TICKET_CLASS" not in prompt
    assert "ticket_class" not in prompt
    assert "ticket class" not in prompt.lower()


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
    assert "Perform every Stage 1-safe probe you can before concluding." in prompt
    assert "When the exact fix or root cause cannot be confirmed" in prompt
    assert "Make it warm, respectful, empathetic, and concise." in prompt
    assert "request lacks enough context to understand the problem" in prompt
    assert "public_reply_markdown may include technical investigation details" in prompt
    assert "set publish_mode_recommendation to auto_publish" in prompt
    assert "Put clarifying questions in public_reply_markdown" in prompt
    assert "When asking clarifying questions, combine them with the best current understanding" in prompt
    assert "Reserve manual_only for cases where requester-facing guidance would be materially unsafe" in prompt
    assert "TARGET_TICKET_CLASS" not in prompt
    assert "ROUTER_TICKET_CLASS" not in prompt
    assert "ticket class" not in prompt.lower()
    assert "classified the ticket as" not in prompt


def test_render_software_architect_prompt_includes_expected_assessment_structure() -> None:
    prompt = render_agent_prompt(
        load_agent_spec("software-architect"),
        context=_make_context(),
        router_result=RouterResult.model_validate(
            {
                "route_target_id": "software_architect",
                "routing_rationale": "The requester needs a repository-grounded architecture assessment.",
            }
        ),
        target_route_target_id="software_architect",
    )

    assert "Analyze this internal ticket as the Stage 1 software architect specialist." in prompt
    assert "Mode, Current state, Assumptions, Analysis, Recommendation, Regression / side-effect risks, Plan, Verification, Open issues." in prompt
    assert "Never emit route_target_id or any reclassification field." in prompt
    assert "Route target ID: software_architect" in prompt
    assert "Perform every Stage 1-safe probe you can before concluding." in prompt
    assert "Make it warm, respectful, empathetic, and concise." in prompt
    assert "Repo-local code, migrations, DDL, and schema dumps under app/ are allowed when relevant." in prompt
    assert "set publish_mode_recommendation to auto_publish" in prompt


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
    for spec in agent_specs_module.load_all_agent_specs():
        skill_path = settings.workspace_skill_file_path(spec.skill_id)
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text("skill", encoding="utf-8")

    monkeypatch.setattr("shared.workspace.load_routing_registry", lambda: (_ for _ in ()).throw(RuntimeError("bad registry")))

    with pytest.raises(RuntimeError, match="bad registry"):
        verify_workspace_contract_paths(settings)
