# Implementation Notes

- Task ID: route-target-registry-impl
- Pair: implement
- Phase ID: registry-contract-foundation
- Phase Directory Key: registry-contract-foundation
- Phase Title: Registry and Contract Foundation
- Scope: phase-local producer artifact

## Files changed

- `agent_specs/registry.json`
- `agent_specs/specialist-selector/{manifest.json,prompt.md,skill.md}`
- `agent_specs/router/{prompt.md,skill.md}`
- `agent_specs/{support,bug,feature,access-config,data-ops,unknown}/{prompt.md,skill.md}`
- `shared/{agent_specs.py,contracts.py,routing_registry.py,workspace.py}`
- `worker/{output_contracts.py,pipeline.py,prompt_renderer.py,step_runner.py}`
- `tests/{test_ai_worker.py,test_foundation_persistence.py,test_hardening_validation.py,test_routing_registry.py}`

## Symbols touched

- `shared.routing_registry.load_routing_registry`
- `shared.routing_registry.RoutingRegistry`
- `shared.agent_specs._validate_spec_dir`
- `shared.contracts.WORKSPACE_BOOTSTRAP_VERSION`
- `shared.workspace.verify_workspace_contract_paths`
- `worker.output_contracts.RouterResult`
- `worker.output_contracts.validate_contract_output`
- `worker.prompt_renderer.render_agent_prompt`
- `worker.step_runner.prepare_step_run`
- `worker.pipeline.execute_triage_pipeline`

## Checklist mapping

- Registry seed + selector spec: complete.
- Typed registry loading/validation: complete.
- Registry-backed prompt/catalog scaffolding: complete.
- Bootstrap/readiness registry validation: complete.
- Phase-local tests for registry/prompt/bootstrap foundation: complete.

## Assumptions

- This phase keeps the current specialist persistence/write path on legacy `triage_result`; selector execution, publication policy, and `route_target_id` persistence cutover remain later-phase work.
- Enabled direct-AI route targets stay aligned to the current persisted `ticket_class` IDs during the compatibility phase.

## Preserved invariants

- No database schema or ticket persistence behavior was changed in this phase.
- The live pipeline still only supports the compatibility-phase `direct_ai + fixed specialist` execution path.
- Readiness/smoke checks still flow through existing workspace verification entry points.

## Intended behavior changes

- Startup/bootstrap/workspace verification now fails fast on invalid registry topology or missing selector/spec references.
- Router prompt rendering and router output validation are registry-driven.
- Selector manifests, selector prompt rendering, and registry-backed selector contract validation are available for later runtime phases.
- Specialist prompts now receive route-target context placeholders instead of legacy class placeholders.
- The compatibility-phase specialist prompts now include one centralized instruction from `worker.prompt_renderer` that aligns any still-required legacy `ticket_class` field to the selected route target while the live specialist path remains on `triage_result`.

## Known non-changes

- No runtime selector orchestration.
- No publication-policy engine or `human_handoff_result` runtime synthesis.
- No `route_target_id` migration, dual-write path, or ops read-path cutover.

## Expected side effects

- Workspace bootstrap now syncs the selector skill and reports bootstrap version `stage1-v3`.
- The compatibility seed registry keeps `manual_review` registered but disabled for new runs.

## Validation performed

- `python -m py_compile shared/routing_registry.py worker/prompt_renderer.py worker/output_contracts.py worker/pipeline.py worker/step_runner.py shared/workspace.py shared/agent_specs.py shared/contracts.py tests/test_routing_registry.py tests/test_ai_worker.py tests/test_foundation_persistence.py tests/test_hardening_validation.py`
- `.venv/bin/pytest -q tests/test_routing_registry.py tests/test_ai_worker.py tests/test_hardening_validation.py tests/test_foundation_persistence.py`
  Result: `81 passed`

## Centralization / deduplication

- Route-target taxonomy, specialist registrations, and publish-policy metadata now live in `agent_specs/registry.json`.
- Generated router and selector catalogs come from `shared.routing_registry` through `worker.prompt_renderer` instead of duplicating taxonomy text in prompts or Python literals.
- The compatibility-phase legacy `ticket_class` alignment sentence is centralized in `worker.prompt_renderer` and reused across all live specialist prompts.
