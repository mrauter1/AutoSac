# Implementation Notes

- Task ID: slack-implementation-refactor
- Pair: implement
- Phase ID: delivery-claim-outcome-finalization
- Phase Directory Key: delivery-claim-outcome-finalization
- Phase Title: Delivery Claim, Outcome, and Finalization Refactor
- Scope: phase-local producer artifact

## Files Changed

- `worker/slack_delivery.py`
- `worker/main.py`
- `tests/test_slack_delivery.py`

## Symbols Touched

- `worker.slack_delivery.build_worker_slack_runtime_context`
- `worker.slack_delivery.ClaimedDeliveryTarget`
- `worker.slack_delivery.DeliveryOutcome`
- `worker.slack_delivery.load_claimed_processing_target`
- `worker.slack_delivery.recover_stale_delivery_targets`
- `worker.slack_delivery.claim_delivery_targets`
- `worker.slack_delivery.classify_delivery_attempt`
- `worker.slack_delivery.finalize_delivery_claim`
- `worker.slack_delivery.run_delivery_cycle`
- `worker.slack_delivery.delivery_loop`
- `worker.main.start_slack_delivery_thread`

## Checklist Mapping

- Milestone 2 / claim token ownership: claim now writes a fresh `claim_token`, returns it in the claim handle, and finalization ownership is proven by `(id, delivery_status='processing', claim_token)`.
- Milestone 2 / typed outcomes: executor classification now returns explicit `sent`, `retryable_failure`, and `dead_letter_terminal` outcomes with pre-finalization retry exhaustion conversion.
- Milestone 2 / single finalization boundary: `finalize_delivery_claim` is now the sole claimed-row mutation entrypoint; stale-lock recovery reuses the same failed-state claim-clearing write set.
- Milestone 3 / regression completion: updated delivery tests now cover claim-token writes/clears, direct outcome classification, ownership-lost behavior, and repository-side non-recomputation.

## Assumptions

- Pre-launch Slack processing rows with `claim_token IS NULL` remain unsupported and disposable, per the refactor PRD.
- Existing worker log consumers tolerate additive `claim_token` context on claim, delivery, and ownership-lost events.

## Preserved Invariants

- Global Slack suppression still skips stale-lock recovery, claim, and send work.
- Render, timeout, retry-delay, and dead-letter `next_attempt_at` semantics remain unchanged from the Phase 1 contract.
- Stale-lock recovery still preserves `attempt_count` while making the row immediately eligible again.

## Intended Behavior Changes

- Delivery claim ownership now depends on `claim_token` instead of `(locked_by, attempt_count)`.
- Retry exhaustion is decided during executor classification before repository finalization.
- Slack delivery loop entrypoints now take an explicit `SlackRuntimeContext` built once at worker-thread startup.

## Known Non-Changes

- No schema cleanup or removal of legacy Slack columns.
- No mixed-version compatibility path for `claim_token IS NULL` processing rows.
- No change to Slack message wording, routing behavior, or non-delivery ticket-domain flows.

## Expected Side Effects

- Worker delivery logs now include `claim_token` on claim, outcome, and ownership-lost events.
- Direct delivery helpers in `worker/slack_delivery.py` now require `SlackRuntimeContext` rather than raw `Settings`.

## Validation Performed

- `python3 -m py_compile worker/slack_delivery.py worker/main.py tests/test_slack_delivery.py`
- `pytest tests/test_slack_delivery.py -q`

## Deduplication / Centralization Decisions

- Kept delivery repository and executor responsibilities in `worker/slack_delivery.py` rather than introducing a new package split.
- Centralized claimed-row write sets behind `_apply_sent_state`, `_apply_failed_state`, and `_apply_dead_letter_state`, with `finalize_delivery_claim` as the canonical caller for post-claim updates.
- Kept executor classification read-only with respect to persistence; it returns `DeliveryOutcome` objects that the repository applies without reinterpretation.
