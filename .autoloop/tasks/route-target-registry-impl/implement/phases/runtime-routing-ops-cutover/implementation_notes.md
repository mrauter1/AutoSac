# Implementation Notes

- Task ID: route-target-registry-impl
- Pair: implement
- Phase ID: runtime-routing-ops-cutover
- Phase Directory Key: runtime-routing-ops-cutover
- Phase Title: Runtime Routing, Policy, and Ops Cutover
- Scope: phase-local producer artifact

## Files changed

- `agent_specs/registry.json`
- `agent_specs/{support,access-config,data-ops,bug,feature,unknown}/{manifest.json,prompt.md}`
- `app/{ai_run_presenters.py,routes_ops.py}`
- `app/templates/{ops_filters.html,ops_ticket_rows.html,ops_board_columns.html,ops_ticket_detail.html}`
- `shared/{agent_specs.py,ticketing.py}`
- `worker/{artifacts.py,output_contracts.py,pipeline.py,prompt_renderer.py,publication_policy.py,step_runner.py,triage.py}`
- `tests/{test_ai_worker.py,test_foundation_persistence.py,test_ops_workflow.py,test_routing_registry.py}`
- `.autoloop/tasks/route-target-registry-impl/decisions.txt`

## Symbols touched

- `shared.ticketing.apply_ai_route_target`
- `shared.ticketing.create_ai_draft`
- `worker.publication_policy.resolve_effective_publication_mode`
- `worker.pipeline.execute_triage_pipeline`
- `worker.triage._apply_success_result`
- `worker.step_runner.write_run_manifest_snapshot`
- `app.ai_run_presenters.present_ai_run_output`
- `app.routes_ops._ops_filter_context`
- `app.routes_ops._ticket_detail_context`

## Checklist mapping

- Worker router/selector/specialist refactor and publication policy: complete.
- Route-target persistence cutover plus terminal output invariant: complete.
- Manifest metadata and synthesized human handoff handling: complete.
- Ops filter/detail cutover with bounded legacy presenter: complete.
- Runtime and ops regression coverage for fixed/auto/none paths and historical continuity: complete.

## Assumptions

- The cleanup migration dropping `tickets.ticket_class` and its constraint remains out of phase for this turn.
- Historical tickets may still rely on `ticket_class` or legacy `triage_result` only for presentation fallback, never for new worker execution.

## Preserved invariants

- Accepted `succeeded` and `human_review` runs always persist non-null `final_output_contract` and `final_output_json`.
- No new worker runtime fallback path was added for legacy payloads; compatibility is confined to ops presentation.
- Registry validation remains authoritative for enabled route targets and selectable specialists.

## Intended behavior changes

- The live worker now executes `router -> optional selector -> optional specialist` from the registry and no longer uses specialist reclassification or mismatch downgrades.
- `manual_review` is enabled for new routing, `unknown` is historical-only for new runs, and new runtime writes stop touching `ticket_class`.
- Direct-AI outcomes use deterministic publication policy; human-assist outcomes always stay on human review, including synthesized `human_handoff_result` when no specialist runs.
- Ops filters, pills, and ticket detail now read `route_target_id` and registry metadata, with a bounded adapter for legacy `triage_result`.

## Known non-changes

- The `ticket_class` column and DB constraint still exist until the later cleanup phase.
- Legacy backfill scripts and historical `triage_result` payloads remain unchanged.

## Expected side effects

- New specialist specs now emit `specialist_result`, so accepted runs surface response confidence/risk/publication recommendation in manifests and ops detail.
- Direct-AI human-review outcomes now stay in `ai_triage` instead of using the old route-to-Dev/TI shortcut.
- Human-assist routes with public drafts move tickets to the configured human queue status while keeping requester publication manual.

## Validation performed

- `python -m py_compile worker/triage.py worker/pipeline.py worker/publication_policy.py worker/step_runner.py worker/prompt_renderer.py worker/artifacts.py worker/output_contracts.py shared/ticketing.py shared/agent_specs.py app/ai_run_presenters.py app/routes_ops.py`
- `python -m py_compile tests/test_ai_worker.py tests/test_routing_registry.py tests/test_ops_workflow.py tests/test_foundation_persistence.py`
- `.venv/bin/pytest -q tests/test_ai_worker.py tests/test_routing_registry.py tests/test_ops_workflow.py tests/test_foundation_persistence.py`
  Result: `105 passed`

## Centralization / deduplication

- Publication gating now lives in `worker/publication_policy.py` instead of being spread across triage outcome branches.
- Ops contract adaptation is centralized in `app/ai_run_presenters.py` instead of templates inspecting raw legacy/new payload shapes directly.
- Run and step manifest enrichment is centralized in `worker.step_runner`/`worker.artifacts` so route-target metadata is generated once per snapshot path.
