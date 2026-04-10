# Test Strategy

- Task ID: slack-implementation-refactor
- Pair: test
- Phase ID: schema-emission-runtime-boundary
- Phase Directory Key: schema-emission-runtime-boundary
- Phase Title: Schema, Emission, and Runtime Boundary
- Scope: phase-local producer artifact

## Behavior-to-Test Coverage Map

- AC-1 explicit runtime boundary
  - `tests/test_slack_event_emission.py`
    - explicit runtime is required for Slack emission entrypoints
    - emission no longer relies on `Session.info["settings"]`
  - `tests/test_auth_requester.py`
    - requester route and ticketing entrypoints propagate explicit Slack runtime from resolved settings
  - `tests/test_ops_workflow.py`
    - ops route and ticketing entrypoints propagate explicit Slack runtime from resolved settings
  - `tests/test_ai_worker.py`
    - worker status/publication paths pass explicit Slack runtime through changed helpers

- AC-2 first-class routing snapshot persistence
  - `tests/test_slack_event_emission.py`
    - created and suppressed events persist routing columns
    - `payload_json` remains free of `_integration_routing`
    - invalid-config logging still emits config details from first-class routing state
  - `tests/test_foundation_persistence.py`
    - additive migration adds routing snapshot columns and `claim_token`

- AC-3 duplicate reuse from stored routing columns and target rows
  - `tests/test_slack_event_emission.py`
    - existing target row forces duplicate observability to `created` without mutating target state
    - zero-target duplicates preserve stored non-created suppression outcomes
    - zero-target duplicates preserve stored `suppressed_target_disabled` target name
    - zero-target duplicates preserve stored `suppressed_invalid_config` error details
    - zero-target duplicates fall back to `suppressed_notify_disabled` for missing or stale `created` routing snapshots

## Preserved Invariants Checked

- Duplicate reuse never creates a repair target row.
- Duplicate reuse does not rewrite existing target delivery state.
- New routing persistence is additive and does not change event payload contract.

## Edge Cases / Failure Paths

- Missing runtime context raises at the call boundary instead of silently suppressing.
- Invalid-config suppression keeps config details in logs without inventing target-row state.
- Stale zero-target rows with missing or `created` snapshots degrade conservatively to `suppressed_notify_disabled`.

## Stability Notes

- Tests use fake sessions, fixed timestamps, and injected loggers to avoid timing and environment flake.
- Route coverage remains dependency-light by overriding FastAPI settings/session dependencies instead of hitting external services.

## Known Gaps

- This phase does not cover worker claim-token finalization semantics; those belong to the later delivery/finalization phase.
