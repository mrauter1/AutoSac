# Implement ↔ Code Reviewer Feedback

- Task ID: route-target-registry-impl
- Pair: implement
- Phase ID: additive-migration-foundation
- Phase Directory Key: additive-migration-foundation
- Phase Title: Additive Migration and Compatibility Foundation
- Scope: phase-local authoritative verifier artifact

- IMP-001 | blocking | [tests/test_foundation_persistence.py](/home/marcelo/code/AutoSac/tests/test_foundation_persistence.py): AC-3 requires migration and persistence coverage for `route_target_id` backfill and `selector` step persistence, but the new tests only inspect migration source text and SQLAlchemy metadata. No test actually exercises the additive migration/backfill path or persists an `AIRunStep` with `step_kind="selector"`, so a broken constraint change or ineffective backfill could still ship undetected. Minimal fix: add a persistence-oriented test in this suite (or the existing migration harness) that applies the additive schema/backfill behavior and proves both `route_target_id` backfill and `selector` step insertion succeed.
