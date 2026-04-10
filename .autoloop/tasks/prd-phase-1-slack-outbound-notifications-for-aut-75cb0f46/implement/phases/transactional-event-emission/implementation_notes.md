# Implementation Notes

- Task ID: prd-phase-1-slack-outbound-notifications-for-aut-75cb0f46
- Pair: implement
- Phase ID: transactional-event-emission
- Phase Directory Key: transactional-event-emission
- Phase Title: Transactional Event Emission
- Scope: phase-local producer artifact

## Files changed
- `shared/db.py`
- `shared/integrations.py`
- `shared/ticketing.py`
- `tests/test_slack_event_emission.py`

## Symbols touched
- `shared.db.get_session_factory`
- `shared.integrations.resolve_integration_settings`
- `shared.integrations.build_ticket_url`
- `shared.integrations.build_message_preview`
- `shared.integrations.resolve_routing_decision`
- `shared.integrations.record_ticket_created_event`
- `shared.integrations.record_ticket_public_message_added_event`
- `shared.integrations.record_ticket_status_changed_event`
- `shared.integrations._with_preserved_routing_metadata`
- `shared.integrations._extract_preserved_routing`
- `shared.integrations._safe_zero_target_duplicate_routing`
- `shared.ticketing.record_status_change`
- `shared.ticketing.create_requester_ticket`
- `shared.ticketing.add_requester_reply`
- `shared.ticketing.publish_ai_public_reply`
- `shared.ticketing.add_ops_public_reply`
- `shared.ticketing.publish_ai_draft_for_ops`

## Checklist mapping
- Milestone 2 / AC-1: added `shared/integrations.py` for payload building, routing outcomes, dedupe-safe persistence, link creation, optional target-row creation, and emission-path structured logging.
- Milestone 2 / AC-1: hooked ticket creation, requester replies, ops public replies, AI public publication, AI draft publication, and all status-history mutations through the shared helper.
- Milestone 2 / AC-2: preserved the initial `ticket_create` and initial `null -> new` history exclusions by only emitting `ticket.created` during ticket creation and by skipping `ticket.status_changed` for null/no-op transitions.
- Milestone 2 / AC-3: duplicate dedupe reuse now returns the prior event row, leaves any previously stored target state untouched, and reuses the original routing/logging outcome for zero-target events after later config changes.

## Assumptions
- Real request-path and worker-path sessions may resolve Slack/runtime config from `Session.info["settings"]`; existing lightweight fake sessions remain valid because the integration helper skips emission when settings are absent unless a test injects them.
- This phase owns only transactional persistence and emission-path logging; Slack HTTP delivery, retries, stale-lock recovery, and rendered Slack text stay deferred to later phases.

## Preserved invariants
- Internal notes, AI internal notes, AI failure notes, and draft rejection still create no `ticket.public_message_added` rows.
- `ticket.created` remains the only event emitted during initial ticket creation; the initial public `ticket_create` message and initial `null -> new` status-history row do not emit extra Phase 1 events.
- Duplicate emission attempts never repair or add `integration_event_targets` rows for an already-persisted event.
- Existing ticket, message, status-history, unread/view, and AI-run mutation semantics were left in place outside the new integration rows/logs.

## Intended behavior changes
- Eligible ticket mutations now persist Phase 1 `integration_events`, required `integration_event_links`, and optional `integration_event_targets` inside the same transaction as the source mutation.
- Status-history rows now get explicit UUIDs before flush so `ticket.status_changed` dedupe keys and links are stable within the outer transaction.
- Emission-path structured logs now include `routing_result`, target/config context, and duplicate-reuse markers for persisted integration events.
- New integration events now persist their initial routing/logging outcome in payload metadata so later duplicate emissions cannot falsely log `created` after preserved zero-target suppression.

## Known non-changes
- No worker Slack delivery loop, HTTP sending, retry scheduling, or dead-letter behavior was added.
- No Slack text-rendering helper was added in this phase.
- No UI or requester-visible behavior changed.

## Expected side effects
- Request-path and worker-path ticket mutations with resolved settings will now populate the integration tables immediately when they create eligible events.
- Existing unit tests that use lightweight fake sessions continue to work without broad rewrites because the helper tolerates missing query/savepoint APIs.

## Validation performed
- `python3 -m compileall shared/integrations.py shared/ticketing.py shared/db.py tests/test_slack_event_emission.py`
- `pytest tests/test_slack_event_emission.py`
- `pytest tests/test_auth_requester.py -k 'create_requester_ticket_creates_initial_records or add_requester_reply_reopens_and_requeues'`
- `pytest tests/test_ops_workflow.py -k 'add_ops_public_reply_records_status_history_and_view or add_ops_internal_note_keeps_status_and_adds_internal_message or publish_ai_draft_for_ops_creates_ai_message_and_status_change'`

## Deduplication / centralization decisions
- Centralized all Phase 1 event payload/routing/persistence logic in `shared/integrations.py` rather than duplicating event-row assembly across ticketing helpers and worker flows.
- Reused `record_status_change` as the single status-event seam so AI failure, AI internal route-only, draft-creation, manual status changes, and requester reopen flows inherit the same no-op/initial-transition rules automatically.
- Stored duplicate-log preservation metadata alongside the immutable event payload instead of widening the schema during this phase, keeping the reviewer fix local to the shared integration helper.
