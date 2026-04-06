# Test Strategy

- Task ID: route-target-registry-impl
- Pair: test
- Phase ID: additive-migration-foundation
- Phase Directory Key: additive-migration-foundation
- Phase Title: Additive Migration and Compatibility Foundation
- Scope: phase-local producer artifact

## Behaviors covered

- Additive schema coverage in `tests/test_foundation_persistence.py`:
  - migration source contains `tickets.route_target_id`, backfill SQL, widened `selector` step-kind constraint, and no `route_target_id` taxonomy constraint
  - sqlite-backed persistence transition backfills stored `ticket_class` values into `route_target_id`
  - sqlite-backed persistence transition accepts `AIRunStep(step_kind="selector")`
- Compatibility helper coverage in `tests/test_foundation_persistence.py`:
  - `apply_ai_route_target()` dual-writes `route_target_id`, legacy `ticket_class`, and `requester_language`
  - non-legacy targets such as `manual_review` are rejected without partial mutation
- Runtime write-path coverage in `tests/test_ai_worker.py`:
  - `_apply_success_result()` invokes the real compatibility write path and mutates the ticket with `route_target_id`, `ticket_class`, `requester_language`, and compatibility-only confidence fields

## Preserved invariants checked

- `route_target_id` storage remains unconstrained by DB taxonomy in model/migration assertions
- legacy ops-path compatibility fields remain populated on successful worker completion
- disabled non-legacy targets stay blocked during the dual-write window

## Edge cases and failure paths

- tickets with `ticket_class IS NULL` remain `route_target_id IS NULL` after backfill
- helper rejection path preserves pre-call ticket state for unsupported route targets
- widened step-kind persistence preserves an existing router step before inserting a selector step

## Stabilization

- all persistence checks are deterministic and local: temp sqlite database, fixed row values, no network, no wall-clock assertions, explicit ordering in result queries

## Known gaps

- no full Alembic-on-Postgres integration test in this phase
- selector runtime orchestration, ops/UI route-target reads, and `ticket_class` cleanup remain out of scope for this phase
