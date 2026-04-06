# Implementation Notes

- Task ID: route-target-registry-impl
- Pair: implement
- Phase ID: additive-migration-foundation
- Phase Directory Key: additive-migration-foundation
- Phase Title: Additive Migration and Compatibility Foundation
- Scope: phase-local producer artifact

## Files changed

- `shared/models.py`
- `shared/ticketing.py`
- `shared/migrations/versions/20260406_0005_route_target_compatibility.py`
- `worker/triage.py`
- `tests/test_foundation_persistence.py`
- `tests/test_ai_worker.py`
- `.autoloop/tasks/route-target-registry-impl/decisions.txt`

## Symbols touched

- `shared.models.Ticket.route_target_id`
- `shared.models.AI_RUN_STEP_KINDS`
- `shared.ticketing.apply_ai_route_target`
- `worker.triage._apply_success_result`
- `shared.migrations.versions.20260406_0005_route_target_compatibility.upgrade`

## Checklist mapping

- Additive/backfill migration(s) and model updates for `route_target_id` plus selector step kind: complete.
- Compatibility-safe ticketing write helper updates: complete.
- Migration and persistence regression tests for dual-write and selector persistence: complete.

## Assumptions

- The live compatibility-phase worker still preserves legacy `ai_confidence`, `impact_level`, and `development_needed` for ops detail rendering until the read-path cutover phase.
- During this phase, persisted `route_target_id` continues to mirror the currently persisted specialist-side `ticket_class` behavior so no runtime classification behavior changes before the later pipeline cutover.

## Preserved invariants

- Ops and runtime reads still use the pre-cutover `ticket_class` path.
- No DB check constraint was added for `route_target_id`.
- Non-legacy route targets remain blocked for new runs by both the disabled registry state and the compatibility dual-write helper.

## Intended behavior changes

- Tickets now have additive `route_target_id` storage with migration backfill from `ticket_class`.
- Compatibility writes now dual-write `route_target_id` and legacy `ticket_class` through one helper.
- `ai_run_steps.step_kind` now permits `selector` persistence.

## Known non-changes

- No selector runtime execution or publication-policy refactor.
- No ops/UI route-target read-path cutover.
- No cleanup migration removing `ticket_class`.

## Expected side effects

- Applying Alembic head will populate `tickets.route_target_id` for existing rows where `ticket_class` is present.
- Attempts to persist non-legacy route targets during the dual-write window now fail fast instead of drifting into the legacy DB constraint.

## Validation performed

- `python -m py_compile shared/models.py shared/ticketing.py worker/triage.py shared/migrations/versions/20260406_0005_route_target_compatibility.py tests/test_foundation_persistence.py tests/test_ai_worker.py`
- `.venv/bin/pytest -q tests/test_foundation_persistence.py tests/test_ai_worker.py tests/test_routing_registry.py`
  Result: `75 passed`

## Centralization / deduplication

- Compatibility-phase route-target persistence is centralized in `shared.ticketing.apply_ai_route_target`.
- The non-legacy dual-write guard is shared by runtime code and persistence tests instead of being reimplemented in multiple worker call sites.
