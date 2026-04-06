# Test Strategy

- Task ID: route-target-registry-impl
- Pair: test
- Phase ID: runtime-routing-ops-cutover
- Phase Directory Key: runtime-routing-ops-cutover
- Phase Title: Runtime Routing, Policy, and Ops Cutover
- Scope: phase-local producer artifact

## Behavior-to-test coverage map

- Registry-driven runtime flow:
  `tests/test_ai_worker.py`
  Covers direct-AI fixed routing, direct-AI auto specialist selection, human-assist `none`, human-assist fixed, and human-assist auto so AC-1 is exercised across router, selector, and specialist orchestration.
- Deterministic publication policy:
  `tests/test_ai_worker.py`
  Covers auto-publish happy path, human-assist never-auto-publish behavior, direct-AI draft path, direct-AI `manual_only` downgrade without draft creation, and human-assist `manual_only` downgrade without draft creation.
- Terminal output persistence invariant:
  `tests/test_ai_worker.py`
  Checks accepted `human_review` and synthesized handoff paths keep `final_output_contract` and `final_output_json` populated; downgrade tests also assert the persisted structured output retains specialist reply text even when side effects are suppressed.
- Manifest metadata and historical continuity:
  `tests/test_ai_worker.py`
  Covers route-target/selector/publication metadata in `run_manifest.json` and verifies rerun or failure snapshots prefer current router output over stale ticket state.
- Ops presentation and bounded legacy compatibility:
  `tests/test_ops_workflow.py`
  Covers registry-driven filter options, route-target presentation, and legacy/new output rendering continuity.
- Persistence and migration boundaries:
  `tests/test_foundation_persistence.py`
  Covers route-target backfill, selector-step persistence, legacy-column non-write cutover helpers, and absence of DB route-target constraints.
- Registry and contract validation:
  `tests/test_routing_registry.py`
  Covers enabled/historical route-target semantics, selector candidate enforcement, `manual_review`/`unknown` taxonomy, prompt catalogs, and contract validation failures.

## Preserved invariants checked

- Accepted `succeeded` and `human_review` runs always persist terminal structured output.
- `manual_only` suppresses draft/public side effects without mutating persisted specialist output.
- Historical continuity is presentation-only; worker/runtime tests do not normalize a legacy fallback execution path.

## Edge cases and failure paths

- Draft-disabled publication downgrade for both direct-AI and human-assist targets.
- Synthesized `human_handoff_result` for `human_assist + none`.
- Selector output outside the candidate set and disabled/historical route-target validation failures.
- Rerun/failure manifest snapshots with stale prior ticket route targets.

## Flake controls

- Tests use local fakes, monkeypatching, and deterministic payloads only; no network, clock waiting, or ordering-sensitive shared state.

## Known gaps

- No browser-level UI automation was added in this phase; ops continuity remains covered at the route/context and template-data level.
