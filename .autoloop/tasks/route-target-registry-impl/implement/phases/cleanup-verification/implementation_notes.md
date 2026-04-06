# Implementation Notes

- Task ID: route-target-registry-impl
- Pair: implement
- Phase ID: cleanup-verification
- Phase Directory Key: cleanup-verification
- Phase Title: Cleanup and Verification
- Scope: phase-local producer artifact

## Files changed

- `shared/models.py`
- `shared/migrations/versions/20260406_0006_drop_ticket_class.py`
- `worker/output_contracts.py`
- `app/ai_run_presenters.py`
- `app/templates/ops_ticket_detail.html`
- `tests/test_foundation_persistence.py`
- `tests/test_ai_worker.py`
- `.autoloop/tasks/route-target-registry-impl/decisions.txt`

## Symbols touched

- `shared.models.Ticket`
- `worker.output_contracts.TriageResult`
- `app.ai_run_presenters.present_ai_run_output`
- `shared.migrations.versions.20260406_0006_drop_ticket_class.upgrade`
- `shared.migrations.versions.20260406_0006_drop_ticket_class.downgrade`

## Checklist mapping

- Legacy code-path removal and cleanup migration: complete.
- Updated regression coverage for route-target cleanup invariants: complete.
- Readiness/smoke verification for registry integrity and structured-output invariants: complete.

## Assumptions

- Historical `triage_result` payloads remain readable for backfill/ops presentation, but they are never used for new step execution.
- `ai_confidence`, `impact_level`, and `development_needed` stay schema-readable for now; this phase only removes `ticket_class` storage mandated by the cleanup migration.

## Preserved invariants

- New runtime writes still only persist `route_target_id` plus requester-language data through `apply_ai_route_target`.
- Accepted `succeeded` and `human_review` runs still require terminal structured output.
- Ops historical continuity remains presentation-only; worker runtime behavior does not branch on legacy payload shape.

## Intended behavior changes

- The head `Ticket` model no longer exposes `ticket_class` or its hardcoded DB taxonomy constraint.
- The cleanup migration drops the legacy `ticket_class` constraint/column and only reconstructs overlapping legacy IDs on downgrade.
- Ops detail reads legacy confidence/impact/development-needed values from historical `triage_result` payloads instead of ticket-row columns.
- The legacy `triage_result` read model no longer hardcodes route taxonomy literals.

## Known non-changes

- Historical migrations and backfill code still mention `ticket_class` where required for additive backfill and legacy run hydration.
- `app.ai_run_presenters.present_ticket_route_target` still tolerates historical objects that may carry `ticket_class` as a fallback attribute.

## Expected side effects

- Any code importing `Ticket.ticket_class` now fails fast at import/test time instead of silently depending on dropped schema.
- A stale triage workspace missing the selector skill fails readiness until `bootstrap_workspace()` is rerun; this turn re-bootstrapped the local workspace before re-running smoke checks.

## Validation performed

- `python -m py_compile shared/models.py worker/output_contracts.py app/ai_run_presenters.py tests/test_foundation_persistence.py tests/test_ai_worker.py shared/migrations/versions/20260406_0006_drop_ticket_class.py`
- `.venv/bin/pytest -q tests/test_foundation_persistence.py tests/test_ai_worker.py tests/test_ops_workflow.py tests/test_routing_registry.py`
  Result: `110 passed`
- `python scripts/run_web.py --check`
  Result: `ok` after re-running `bootstrap_workspace()` to refresh the selector skill file in `/home/marcelo/autosac_workspace`
- `python scripts/run_worker.py --check`
  Result: `ok`

## Centralization / deduplication

- Legacy ops-detail compatibility for `triage_result` now lives in `app.ai_run_presenters` instead of reading ticket-row fallback fields directly in the template.
- The only live route-taxonomy authority remains the registry plus `route_target_id`; the removed `ticket_class` schema/model surfaces no longer duplicate it at head.
