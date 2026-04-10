# Implementation Notes

- Task ID: prd-phase-1-slack-outbound-notifications-for-aut-75cb0f46
- Pair: implement
- Phase ID: async-delivery-runtime
- Phase Directory Key: async-delivery-runtime
- Phase Title: Async Delivery Runtime
- Scope: phase-local producer artifact

## Files changed
- `worker/main.py`
- `worker/slack_delivery.py`
- `tests/test_slack_delivery.py`

## Symbols touched
- `worker.main.start_slack_delivery_thread`
- `worker.main.main`
- `worker.slack_delivery.resolve_delivery_suppression`
- `worker.slack_delivery.recover_stale_delivery_targets`
- `worker.slack_delivery.claim_delivery_targets`
- `worker.slack_delivery.render_slack_message`
- `worker.slack_delivery.send_slack_webhook`
- `worker.slack_delivery._post_slack_webhook_async`
- `worker.slack_delivery._sanitize_operator_summary`
- `worker.slack_delivery.deliver_claimed_target`
- `worker.slack_delivery.run_delivery_cycle`
- `worker.slack_delivery.delivery_loop`

## Checklist mapping
- Milestone 3 / AC-1: added a dedicated Slack delivery module plus `worker/main.py` thread startup separate from the AI-run polling path.
- Milestone 3 / AC-2: implemented stale-lock recovery, batched claims with `FOR UPDATE SKIP LOCKED`, payload-only rendering, hard-deadline HTTP send, retry scheduling, success writes, and dead-letter transitions.
- Milestone 3 / AC-3: runtime suppression now skips claim/send/stale-lock recovery whenever Slack config is globally invalid, and the normal disabled posture leaves stored rows untouched without per-poll log spam.

## Assumptions
- Delivery uses the worker process' resolved `Settings` snapshot for target definitions and tunables; operational config changes still follow the existing process-restart model for environment-driven settings.
- Delivery-state tests stay at helper level with fake sessions because the ORM models use PostgreSQL-specific types and the phase contract focuses on worker/runtime semantics.

## Preserved invariants
- Slack delivery continues to render from `integration_events.payload_json` plus `event_type` only; it does not rebuild messages from current ticket tables.
- No delivery path mutates ticket domain rows, AI-run rows, or existing integration target rows outside the PRD-defined state transitions.
- Global suppression when Slack is disabled or invalid leaves pending/failed/processing target rows unchanged and unclaimed.

## Intended behavior changes
- Worker startup now launches a dedicated Slack delivery thread that polls on the existing worker cadence.
- Claimed target rows now transition through `processing`, `sent`, `failed`, and `dead_letter` with PRD-aligned lock handling, attempt counting, retry backoff, pre-send terminal validation, and an actual total webhook deadline around each outbound attempt.
- Delivery runtime structured logs now cover row claims, successful sends, retry scheduling, dead-letter transitions, and invalid-config suppression.
- Operator-facing delivery errors now redact absolute URLs and Slack-hook-shaped fragments before storage/logging.

## Known non-changes
- No multi-target fan-out, inbound Slack features, or requester-visible UI behavior was added.
- No dynamic settings reload mechanism was introduced beyond the repository's existing environment-driven startup behavior.

## Expected side effects
- Eligible `integration_event_targets` rows are now processed asynchronously by the worker without blocking AI-run claim/process transactions.
- Stored `processing` rows from crashed delivery attempts become recoverable on later poll cycles once Slack delivery is not globally suppressed.

## Validation performed
- `python3 -m compileall worker/slack_delivery.py worker/main.py tests/test_slack_delivery.py`
- `pytest tests/test_slack_delivery.py`
- `pytest tests/test_ai_worker.py -k 'heartbeat_loop_emits_while_stop_event_controls_exit or emit_worker_heartbeat_initializes_system_state_defaults or emit_worker_heartbeat_updates_active_run_last_heartbeat'`
- `pytest tests/test_slack_event_emission.py tests/test_slack_delivery.py`

## Deduplication / centralization decisions
- Centralized all Phase 1 Slack runtime behavior in `worker/slack_delivery.py` rather than scattering claim/retry/send logic across `worker/main.py` and AI-run modules.
- Reused the worker's existing thread/session pattern in `worker/main.py` so Slack delivery stays operationally separate without inventing a second worker entrypoint.
