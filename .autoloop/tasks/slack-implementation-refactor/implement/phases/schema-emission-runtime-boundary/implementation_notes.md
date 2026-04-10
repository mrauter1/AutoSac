# Implementation Notes

- Task ID: slack-implementation-refactor
- Pair: implement
- Phase ID: schema-emission-runtime-boundary
- Phase Directory Key: schema-emission-runtime-boundary
- Phase Title: Schema, Emission, and Runtime Boundary
- Scope: phase-local producer artifact

## Files Changed

- `shared/integrations.py`
- `shared/ticketing.py`
- `shared/models.py`
- `shared/migrations/versions/20260410_0011_slack_routing_runtime_refactor.py`
- `app/routes_requester.py`
- `app/routes_ops.py`
- `worker/triage.py`
- `worker/queue.py`
- `tests/test_slack_event_emission.py`
- `tests/test_foundation_persistence.py`
- `tests/test_auth_requester.py`
- `tests/test_ops_workflow.py`
- `tests/test_ai_worker.py`

## Symbols Touched

- `shared.integrations.SlackRuntimeContext`
- `shared.integrations.build_slack_runtime_context`
- `shared.integrations.record_ticket_created_event`
- `shared.integrations.record_ticket_public_message_added_event`
- `shared.integrations.record_ticket_status_changed_event`
- `shared.ticketing.record_status_change`
- `shared.ticketing.create_requester_ticket`
- `shared.ticketing.add_requester_reply`
- `shared.ticketing.publish_ai_public_reply`
- `shared.ticketing.create_ai_draft`
- `shared.ticketing.route_ticket_after_ai`
- `shared.ticketing.resolve_ticket_for_requester`
- `shared.ticketing.add_ops_public_reply`
- `shared.ticketing.set_ticket_status_for_ops`
- `shared.ticketing.request_manual_rerun`
- `shared.ticketing.publish_ai_draft_for_ops`
- `shared.models.IntegrationEvent`
- `shared.models.IntegrationEventTarget`

## Checklist Mapping

- Milestone 1 / schema: added routing snapshot columns and `claim_token` in `20260410_0011`.
- Milestone 1 / runtime boundary: removed Slack settings lookup from session state and threaded `SlackRuntimeContext` through request and worker emission call sites.
- Milestone 1 / emission persistence: new events now write routing snapshot columns and keep `payload_json` free of private routing metadata.
- Milestone 1 / duplicate handling: duplicate reuse now reads first-class routing columns plus existing target rows only.
- Deferred by phase scope: delivery claim-token ownership enforcement and single finalization boundary remain for the next phase.

## Assumptions

- Pre-launch Slack rows are disposable, so duplicate reuse does not read legacy payload metadata.
- Route-level full-stack validation is limited by missing local packages (`python-multipart`, `bleach`) in this environment.

## Preserved Invariants

- Slack payload shape in `payload_json` remains the Phase 1 business payload contract.
- Duplicate reuse never repairs or creates target rows.
- Existing request-path and worker flows still source `Settings` from their existing dependencies; only the Slack boundary changed.

## Intended Behavior Changes

- Slack emission now requires an explicit runtime context instead of silently resolving `Settings` from `Session.info`.
- Routing snapshots persist in first-class event columns and drive duplicate-observability outcomes.

## Known Non-Changes

- No claim-token worker ownership logic or delivery finalization refactor yet.
- No compatibility bridge for legacy `_integration_routing` payload metadata or `claim_token IS NULL` processing rows.

## Expected Side Effects

- Ticketing helpers that can emit Slack events now require `slack_runtime` at the call site.
- Suppressed Slack events still record integration rows, but their routing details now live on the event row.

## Validation Performed

- `pytest tests/test_slack_event_emission.py tests/test_foundation_persistence.py -q`
- `pytest tests/test_auth_requester.py -k 'create_requester_ticket or add_requester_reply or resolve_ticket_for_requester or record_status_change or slack_routing_runtime_refactor_migration' -q`
- `pytest tests/test_ops_workflow.py -k 'add_ops_public_reply or set_ticket_status_for_ops or request_manual_rerun or publish_ai_draft_for_ops' -q`
- `pytest tests/test_ai_worker.py -k 'apply_success_result or mark_failed or recover_stale_runs' -q`
- `python3 -m py_compile shared/integrations.py shared/ticketing.py shared/models.py worker/triage.py worker/queue.py app/routes_requester.py app/routes_ops.py tests/test_slack_event_emission.py tests/test_foundation_persistence.py tests/test_auth_requester.py tests/test_ops_workflow.py tests/test_ai_worker.py`

## Deduplication / Centralization Decisions

- Kept Slack emission ownership in `shared/integrations.py`; duplicate routing parsing and emission logging now read the same first-class routing fields.
- Used one `SlackRuntimeContext` construction per route or worker flow and passed it down rather than recreating ad hoc settings bundles inside helpers.
