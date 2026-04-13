# Test Strategy

- Task ID: slack-implementation-refactor
- Pair: test
- Phase ID: emission-dm-recipient-routing
- Phase Directory Key: emission-dm-recipient-routing
- Phase Title: Emission-Time DM Recipient Routing
- Scope: phase-local producer artifact

## Behaviors covered

- Emission creates `slack_dm` target rows from post-mutation ticket state for requester-only, assignee-only, and requester-plus-assignee cases.
- Requester and assignee collapse to one row with `recipient_reason = requester_assignee` and `target_name = user:<recipient_user_id>` when they are the same AutoSac user.
- Actor identity is not used as a suppression rule; an assignee still receives a DM target when they performed the public reply themselves.
- Valid enabled events with no eligible recipients persist the event and link rows, emit `suppressed_no_recipients`, and log `recipient_target_count = 0`.
- Fresh created emissions log `recipient_target_count` and do not log webhook-era `target_name`.

## Preserved invariants checked

- Canonical payload fields and message preview behavior remain unchanged.
- `routing_target_name` stays null for DM routing outcomes.
- Internal notes still create no integration rows.
- Duplicate reuse stays read-only and preserves existing target row state.

## Edge cases

- Requester without a Slack ID is treated as ineligible while an eligible assignee still receives the only target row.
- Inactive requester is treated as ineligible while an eligible assignee still receives the only target row.
- Zero-target duplicate reuse preserves stored `suppressed_no_recipients` or `suppressed_invalid_config` routing snapshots.
- Stale or missing zero-target duplicate snapshots that claim `created` still degrade to `suppressed_notify_disabled`.

## Failure / regression paths

- Later Slack enablement does not backfill target rows for an event originally stored as `suppressed_slack_disabled`.
- Later Slack ID changes do not backfill target rows for an event originally stored with zero recipients.
- Later assignment changes do not append missing assignee rows to an already stored event.

## Stabilization notes

- Tests use the in-memory fake session and monkeypatched query helpers to keep recipient resolution deterministic and free of database ordering or network variance.
- Logging assertions compare full emitted payloads so webhook-era field regressions are caught directly.

## Known gaps

- Worker send-time recipient lookup and Slack Web API transport remain intentionally out of scope for this phase.
