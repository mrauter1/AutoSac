# Implement ↔ Code Reviewer Feedback

- Task ID: slack-implementation-refactor
- Pair: implement
- Phase ID: worker-dm-delivery-and-regression
- Phase Directory Key: worker-dm-delivery-and-regression
- Phase Title: Worker DM Delivery and Regression Completion
- Scope: phase-local authoritative verifier artifact

IMP-001 | blocking | worker/slack_delivery.py::_run_delivery_cycle_with_runtime, run_delivery_cycle_preflight
The cycle still batch-claims rows before any send-time scope or auth error can be discovered. `run_delivery_cycle_preflight()` only checks `auth.test`, then `_run_delivery_cycle_with_runtime()` runs stale-lock recovery, claims up to `delivery_batch_size` rows, and only afterwards calls `deliver_claimed_target()` for each claim. In the real PRD scenario where `auth.test` succeeds but the bot token is missing `conversations.open` or `chat.postMessage` scope, the first send returns `missing_scope`, the worker logs invalid-config suppression, and returns with every already-claimed row left mutated to `processing`. That contradicts the required runtime behavior and the shared decision log for this phase: auth or scope invalid config discovered at runtime must halt the cycle while leaving pending/failed/processing rows unchanged until config is usable again. Minimal fix: centralize the send-time invalid-config handling in the claim orchestration so the worker either claims one row at a time after preflight or explicitly reverts every unfinalized claim in the current batch back to its pre-claim state before returning. Add a regression test at the cycle level that exercises `auth.test` success followed by `missing_scope` from `conversations.open` or `chat.postMessage`.

IMP-002 | non-blocking | README.md:184
The README now points readers at `tasks/slack_dm_integration_PRD.md`, but that file is currently untracked in this worktree (`git ls-files --error-unmatch tasks/slack_dm_integration_PRD.md` fails). If the phase ships without separately adding that file, the new documentation path is broken. Minimal fix: either add the PRD file to the repository in the same rollout or change the README reference to a tracked document.

Follow-up review, cycle 2
IMP-001 resolved: `worker/slack_delivery.py` now restores still-owned claimed rows to their pre-claim state when send-time auth or scope invalid-config halts the cycle, and `tests/test_slack_delivery.py::test_run_delivery_cycle_restores_unfinalized_claims_when_send_hits_missing_scope` covers that path.
IMP-002 resolved: `README.md` no longer points at the untracked Slack DM PRD working file.

IMP-003 | blocking | worker/slack_delivery.py::_run_delivery_cycle_with_runtime, recover_stale_delivery_targets
The batch-claim regression is fixed, but the worker still mutates stale `processing` rows before it can discover send-time auth or scope invalid-config. `_run_delivery_cycle_with_runtime()` calls `recover_stale_delivery_targets()` immediately after `auth.test`, and only later reaches `conversations.open` or `chat.postMessage`, where `missing_scope` or similar global invalid-config failures are now detected. In the PRD scenario where `auth.test` succeeds but DM send scopes are missing, every stale `processing` row in that cycle is still rewritten to `failed` with a new `last_error`, `next_attempt_at`, and cleared lock state before the worker halts. That still violates the required runtime behavior and the shared decision intent that invalid-config suppression leave pending, failed, and processing rows unchanged for the affected cycle. Minimal fix: either move stale-lock recovery behind a capability check that can prove DM send is usable before any row mutation, or capture and restore stale-recovery mutations alongside claimed-row restoration when send-time auth or scope invalid-config is discovered. Add a regression test covering stale rows plus a send-time `missing_scope` failure.

Follow-up review, cycle 3
IMP-003 resolved: `_run_delivery_cycle_with_runtime()` now defers stale-lock recovery until after a claim/send pass completes without a send-time auth or scope halt, and `tests/test_slack_delivery.py::test_run_delivery_cycle_restores_unfinalized_claims_when_send_hits_missing_scope` now also proves stale recovery is skipped and stale `processing` rows remain unchanged in the `missing_scope` path.
No remaining findings in phase scope.
