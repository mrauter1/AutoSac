# Implementation Notes

- Task ID: slack-implementation-refactor
- Pair: implement
- Phase ID: worker-dm-delivery-and-regression
- Phase Directory Key: worker-dm-delivery-and-regression
- Phase Title: Worker DM Delivery and Regression Completion
- Scope: phase-local producer artifact

## Files changed

- `worker/slack_delivery.py`
- `tests/test_slack_delivery.py`
- `tests/test_hardening_validation.py`
- `README.md`
- `.env.example`
- `docs_deployment.md`
- `docs/ubuntu_internal_server_setup.md`

## Symbols touched

- `worker.slack_delivery.ClaimedDeliveryTarget`
- `worker.slack_delivery.deliver_claimed_target`
- `worker.slack_delivery.classify_delivery_attempt`
- `worker.slack_delivery.restore_claimed_delivery_targets`
- `worker.slack_delivery.run_delivery_cycle_preflight`
- `worker.slack_delivery.run_delivery_cycle`
- `worker.slack_delivery.load_delivery_runtime`
- `worker.slack_delivery.load_delivery_recipient`
- `worker.slack_delivery.persist_delivery_health_snapshot`
- `worker.slack_delivery._build_invalid_config_suppression_from_response`
- `worker.slack_delivery._classify_slack_web_api_failure`
- `worker.slack_delivery._build_retryable_outcome`

## Checklist mapping

- `AC-1`: the worker now reloads DB-backed Slack runtime each cycle, resolves the stored bot token, runs `auth.test` before stale-lock recovery or claim work, persists auth health snapshots, and restores still-owned batch claims to their pre-claim state when a send-time auth or scope failure halts the cycle.
- `AC-2`: delivery now resolves the current recipient by `recipient_user_id`, dead-letters missing or inactive recipients without Slack calls, opens DMs with `conversations.open`, sends text with `chat.postMessage`, requires `ok=true` on both responses, and honors `Retry-After` as a floor on retry scheduling.
- `AC-3`: worker claim/result logs now include `recipient_user_id` and `recipient_reason`, and the rollout docs plus hardening checks now describe one DB-backed Slack DM contract with config-first rollback, tracked-doc references, and no backfill.

## Assumptions

- Only auth-level or scope-level `auth.test` failures suppress a whole delivery cycle; transport failures in the preflight remain non-suppressing so row-local retry semantics still own transient Slack outages.
- Earlier dry-run Slack integration rows are disposable pre-launch data per the DM PRD.

## Preserved invariants

- Ticket mutations still perform no Slack HTTP work in the request path.
- PostgreSQL remains the delivery-state source of truth; worker health snapshots in `system_state` are advisory only.
- Existing claim-token ownership rules and finalize-by-claim-token semantics remain intact.

## Intended behavior changes

- Webhook delivery is replaced with Slack Web API DM delivery.
- The worker now validates Slack credentials every cycle, persists healthy or invalid-config auth snapshots, and halts a cycle when send-time auth or scope failures are discovered after first restoring still-owned claims from the current batch back to their pre-claim state.
- Rollout-facing docs no longer describe `SLACK_*` env vars as the Slack runtime contract; Slack DM configuration is now described as UI and PostgreSQL-backed.

## Known non-changes

- No inbound Slack interactions, OAuth, or historical backfill were added.
- No request-path emission behavior changed in this phase beyond the worker-facing contract already established in earlier phases.

## Expected side effects

- Successful `auth.test` checks refresh the stored Slack delivery health snapshot with current workspace metadata.
- Send-time auth or scope failures restore any still-owned claims from the current batch to their pre-claim `pending` or `failed` state while leaving already-finalized rows unchanged.
- `.env.example` documents Slack rollout posture only; operators must use `/ops/integrations/slack` and `/ops/users` for Slack administration.

## Validation performed

- `python3 -m compileall worker/slack_delivery.py tests/test_slack_delivery.py`
- `pytest tests/test_slack_delivery.py tests/test_hardening_validation.py -q`
- `pytest tests/test_ai_worker.py -q`

## Deduplication / centralization

- Centralized Slack Web API failure parsing, auth or scope suppression, recipient dead-lettering, and `Retry-After` handling inside `worker/slack_delivery.py` instead of scattering it across the worker loop and tests.
- Reused helper boundaries for per-cycle runtime load, send-time recipient lookup, delivery-health persistence, and claim-token-based claim restoration so tests can pin the DB-backed contract without duplicating session plumbing.
