# Test Strategy

- Task ID: prd-phase-1-slack-outbound-notifications-for-aut-75cb0f46
- Pair: test
- Phase ID: regression-hardening-and-rollout-readiness
- Phase Directory Key: regression-hardening-and-rollout-readiness
- Phase Title: Regression Hardening and Rollout Readiness
- Scope: phase-local producer artifact

## Behavior-to-test coverage map

- Schema and persistence contract:
  - `tests/test_foundation_persistence.py`
  - Covers migration source for `integration_events`, `integration_event_links`, `integration_event_targets`, required indexes, and delivery-state DB constraints.
- Slack config and operator docs:
  - `tests/test_hardening_validation.py`
  - Covers `.env.example` Slack knobs, README/deployment rollout notes, rollback/no-backfill wording, valid config parsing, and invalid-config soft suppression cases.
- Event emission rules and payload snapshots:
  - `tests/test_slack_event_emission.py`
  - Covers `ticket.created`, `ticket.public_message_added`, `ticket.status_changed`, duplicate reuse, routing outcomes, author/source mapping, ticket URL normalization, and no-event actions.
- Delivery runtime behavior:
  - `tests/test_slack_delivery.py`
  - Covers render sanitization, claim ordering, stale-lock recovery, success, retryable failures, terminal dead-letter paths, retry exhaustion, and suppression behavior.
- Preserved Stage 1 invariants:
  - `tests/test_ai_worker.py`
  - `tests/test_auth_requester.py`
  - `tests/test_ops_workflow.py`
  - Covers heartbeat/state defaults plus requester and ops flows that must remain unchanged after Slack hooks landed.

## Edge cases and failure paths

- Duplicate emission with preserved zero-target state after later routing changes.
- Invalid Slack config cases that must suppress target creation and delivery without blocking startup.
- Missing/disabled target at send time, malformed payloads, retry exhaustion, and stale `processing` recovery.
- Environment-sensitive full-app readiness/smoke checks skip when FastAPI's multipart dependency guard fails in the runner.

## Stabilization approach

- Keep delivery tests helper-level with fake sessions and fake senders to avoid network, timing, and DB nondeterminism.
- Use source assertions for migration/docs coverage where runtime dependencies are unnecessary.
- Gate full FastAPI app health/script tests behind FastAPI's own multipart availability check so skips reflect framework reality instead of brittle module probing.

## Known gaps

- Full web/script smoke checks remain skipped in runners without `python-multipart`; targeted requester/ops/worker regressions cover the preserved Stage 1 semantics in those environments.
- No production enablement or historical backfill tooling is exercised in this phase because both are out of scope.
