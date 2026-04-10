# Implement ↔ Code Reviewer Feedback

- Task ID: prd-phase-1-slack-outbound-notifications-for-aut-75cb0f46
- Pair: implement
- Phase ID: transactional-event-emission
- Phase Directory Key: transactional-event-emission
- Phase Title: Transactional Event Emission
- Scope: phase-local authoritative verifier artifact

- IMP-001 `blocking` — [`shared/integrations.py`](/home/marcelo/code/AutoSac/shared/integrations.py): `_build_duplicate_result` recomputes the current routing outcome for reused zero-target events, so a duplicate emission after config changes can log `routing_result="created"` even though no `integration_event_targets` row exists and no `target_name` is present. Concrete failure: emit `ticket.created` while notifications are disabled, then emit the same dedupe key again after enabling Slack; storage correctly preserves zero targets, but the second log claims `created`, violating AC-3 and the emission-path log contract that `created` logs include `target_name`. Minimal fix: preserve the original routing outcome for the first persisted event (or reconstruct it from stored state without ever returning `created` for reused zero-target events), and add a regression test for duplicate zero-target reuse after config changes.
- IMP-001 `resolved` — Rechecked after the cycle-2 implementation update. The helper now persists the first routing outcome in event payload metadata, reused zero-target events replay the preserved suppressed outcome instead of falsely logging `created`, and the focused regression test covers the duplicate-after-config-change case.
