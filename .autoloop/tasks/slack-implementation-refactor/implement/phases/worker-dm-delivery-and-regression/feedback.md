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
