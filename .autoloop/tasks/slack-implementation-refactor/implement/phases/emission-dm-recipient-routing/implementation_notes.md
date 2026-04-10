# Implementation Notes

- Task ID: slack-implementation-refactor
- Pair: implement
- Phase ID: emission-dm-recipient-routing
- Phase Directory Key: emission-dm-recipient-routing
- Phase Title: Emission-Time DM Recipient Routing
- Scope: phase-local producer artifact

## Files changed

- `shared/integrations.py`
- `tests/test_slack_event_emission.py`

## Symbols touched

- `shared.integrations.RoutingDecision`
- `shared.integrations.RecipientTarget`
- `shared.integrations.EmissionResult`
- `shared.integrations.resolve_routing_decision`
- `shared.integrations.load_user_by_id`
- `shared.integrations.resolve_dm_recipient_targets`
- `shared.integrations._record_integration_event`
- `shared.integrations._build_duplicate_result`
- `shared.integrations._log_emission`
- `tests.test_slack_event_emission._FakeSession`
- `tests.test_slack_event_emission._make_slack_settings`

## Checklist mapping

- `AC-1`: requester and current assignee are now resolved from the post-mutation ticket state, filtered to active users with nonblank `slack_user_id`, collapsed to one `requester_assignee` row when they are the same user, and never suppressed based on actor identity.
- `AC-2`: valid enabled events with zero eligible recipients still persist `integration_events` and link rows, create zero target rows, and record `suppressed_no_recipients` plus `recipient_target_count = 0`.
- `AC-3`: duplicate reuse stays read-only; existing target rows win for `created`, and zero-target duplicates preserve the stored routing snapshot instead of adding rows after later Slack ID or assignment changes.

## Assumptions

- The DM schema and DB-backed Slack settings from earlier phases are already authoritative.
- `users.slack_user_id` is the only recipient eligibility signal; no actor-based suppression or historical repair is allowed in this phase.

## Preserved invariants

- Canonical payload fields, dedupe keys, and internal-content exclusions remain unchanged.
- Slack HTTP calls still do not occur in the request path.
- Duplicate reuse still mutates no existing event or target rows.

## Intended behavior changes

- Emission no longer maps `created` to a single webhook target; it now means one or more `slack_dm` recipient rows were inserted.
- `integration_event_targets` rows now use `target_name = user:<recipient_user_id>`, `target_kind = slack_dm`, and the persisted `recipient_user_id` or `recipient_reason` DM fields.
- Emission logs now report `recipient_target_count`, and `routing_target_name` remains null for DM routing outcomes.

## Known non-changes

- No Slack Web API delivery transport or worker retry classification changes landed in this phase.
- No admin UI or user-management behavior changed in this phase.

## Expected side effects

- DM-mode request-path tests now need explicit `User` rows in the fake session to make requester or assignee routing eligible.
- Zero-target duplicate reuse falls back to `suppressed_notify_disabled` only for stale or missing stored routing snapshots that claim `created`.

## Validation performed

- `python3 -m pytest tests/test_slack_event_emission.py tests/test_slack_dm_foundation.py -q`
- `python3 -m pytest tests/test_foundation_persistence.py -q`
- `python3 -m compileall shared tests`

## Deduplication / centralization

- Kept all recipient eligibility, collapse, and DM target creation in `shared/integrations.py` so ticketing callers did not grow per-event routing branches.
- Reused the existing duplicate-result path and only changed its source-of-truth fields from target-name logging to recipient-count logging.
