# Implement ↔ Code Reviewer Feedback

- Task ID: slack-implementation-refactor
- Pair: implement
- Phase ID: dm-persistence-runtime-foundation
- Phase Directory Key: dm-persistence-runtime-foundation
- Phase Title: DM Persistence and Runtime Foundation
- Scope: phase-local authoritative verifier artifact

## Findings

- IMP-001 `blocking` — [shared/migrations/versions/20260410_0012_slack_dm_persistence_runtime_foundation.py](/home/marcelo/code/AutoSac/shared/migrations/versions/20260410_0012_slack_dm_persistence_runtime_foundation.py#L19), [shared/models.py](/home/marcelo/code/AutoSac/shared/models.py#L315), [tests/test_slack_dm_foundation.py](/home/marcelo/code/AutoSac/tests/test_slack_dm_foundation.py#L51): the migration deletes all pre-launch Slack integration rows and then keeps `integration_event_targets.target_kind` permissive (`slack_webhook`, `slack_dm`). That contradicts the accepted phase contract and shared decision block_seq `6`, which explicitly uses the disposable-row cleanup to enforce `target_kind=slack_dm` without a compatibility bridge. As written, any remaining emitter path can still create fresh webhook-era rows after the migration, so AC-1 is not actually met and later phases inherit avoidable compatibility debt. Minimal fix: tighten the migration/model/test contract to DM-only target kinds now that the pre-launch rows are being purged.

- IMP-002 `blocking` — [shared/integrations.py](/home/marcelo/code/AutoSac/shared/integrations.py#L259), [shared/integrations.py](/home/marcelo/code/AutoSac/shared/integrations.py#L303): the request-path runtime now loads Slack config from PostgreSQL, but `resolve_routing_decision()` still requires webhook-era `default_target_name` / `targets`, and `_record_integration_event()` still inserts `target_kind=\"slack_webhook\"`. A structurally valid `slack_dm_settings` row therefore gets recorded as `suppressed_invalid_config`, and no request-path event can route correctly under the new DB-backed DM contract. This is not just later-phase recipient work: the foundation now misclassifies valid DM config and preserves webhook semantics in the runtime path it just switched to. Minimal fix: centralize DM-aware routing gating in `shared/integrations.py` so DB-backed enablement/validity/notify flags no longer depend on webhook target fields, and remove webhook target-row creation from the DB-backed request-path flow.

## Cycle 2 Resolution Check

- No new findings.
- IMP-001 resolved: the migration, model enum, and foundation test now enforce `target_kind = 'slack_dm'` after the pre-launch row purge.
- IMP-002 resolved: DB-loaded Slack settings now run in explicit DM mode, valid persisted config no longer depends on webhook target lookup, and the request-path runtime records `suppressed_no_recipients` instead of misclassifying valid DM config as invalid.
