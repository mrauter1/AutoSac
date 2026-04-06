# Test Strategy

- Task ID: route-target-registry-impl
- Pair: test
- Phase ID: cleanup-verification
- Phase Directory Key: cleanup-verification
- Phase Title: Cleanup and Verification
- Scope: phase-local producer artifact

## Behavior-to-coverage map

- Head cleanup migration and model shape:
  Covered by `tests/test_foundation_persistence.py`
  Checks source-level migration intent, sqlite-backed column-drop preservation, and that `Ticket` no longer exposes `ticket_class` or a route-taxonomy DB constraint at head.
- Runtime route-target write path after cleanup:
  Covered by `tests/test_foundation_persistence.py` and `tests/test_ai_worker.py`
  Checks `apply_ai_route_target()` only writes `route_target_id`/`requester_language` and that `_apply_success_result()` keeps publishing behavior and final structured output intact without reintroducing legacy taxonomy writes.
- Legacy contract compatibility without hardcoded taxonomy literals:
  Covered by `tests/test_routing_registry.py`
  Checks `triage_result` validation accepts historical non-registry `ticket_class` strings so the legacy read model does not reintroduce business-taxonomy `Literal[...]` constraints.
- Ops historical presentation after `ticket_class` drop:
  Covered by `tests/test_ops_workflow.py`
  Checks `present_ticket_route_target()` still falls back to historical `ticket_class`, `present_ai_run_output()` surfaces legacy confidence/impact/development-needed from `triage_result`, and `_ticket_detail_context()` carries those values into ops detail state.
- Readiness / smoke coverage:
  Covered by direct command execution
  Checks `python scripts/run_web.py --check` and `python scripts/run_worker.py --check` both succeed against the registry-driven workspace state.

## Preserved invariants checked

- Accepted analysis runs still expose terminal structured output for ops detail rendering.
- Route-target labels remain registry-derived or raw-ID fallback only; no hardcoded ops filter tuple was reintroduced.
- Cleanup keeps historical compatibility in presentation/backfill paths only, not in new worker runtime execution.

## Edge cases and failure paths

- Historical `triage_result` payloads with non-registry `ticket_class` values remain readable.
- Historical tickets with no `route_target_id` but a legacy `ticket_class` still render a route-target presentation fallback.
- Sqlite-backed schema recreation test guards against losing `route_target_id`/`requester_language` data while dropping `ticket_class`.

## Flake risk / stabilization

- Tests use pure-unit or sqlite-backed setups with deterministic payloads and no timing-sensitive assertions.
- Smoke checks use deterministic `--check` entry points and the already-bootstrapped local workspace, avoiding live worker loops or network-dependent flows.

## Known gaps

- No new full Alembic integration harness was added; migration behavior remains covered by source assertions plus sqlite schema-transition simulations, consistent with earlier phase test strategy.
