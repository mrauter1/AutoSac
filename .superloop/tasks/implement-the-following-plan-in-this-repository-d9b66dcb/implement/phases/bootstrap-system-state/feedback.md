# Implement ↔ Code Reviewer Feedback

- Task ID: implement-the-following-plan-in-this-repository-d9b66dcb
- Pair: implement
- Phase ID: bootstrap-system-state
- Phase Directory Key: bootstrap-system-state
- Phase Title: Bootstrap And System State
- Scope: phase-local authoritative verifier artifact

- IMP-001 `blocking` — [shared/ticketing.py](/workspace/AutoSac/shared/ticketing.py) `ensure_system_state_defaults()` and [scripts/create_admin.py](/workspace/AutoSac/scripts/create_admin.py) now create `system_state` / `users` tables opportunistically instead of relying on one explicit bootstrap order. That breaks the intended deterministic runbook: if an operator runs `bootstrap_workspace.py` or `create_admin.py` before migrations on a real database, later `alembic upgrade head` will fail on already-existing tables, and worker startup now implicitly requires DDL privileges because `ensure_system_state_defaults()` executes `CREATE TABLE` on boot. [tests/test_hardening_validation.py](/workspace/AutoSac/tests/test_hardening_validation.py) then blesses this divergent contract by removing migrations from the smoke/bootstrap sequence entirely. Minimal fix: keep runtime/CLI paths to data seeding only, restore a single explicit bootstrap sequence (`migrations -> bootstrap workspace -> create_admin --if-missing -> smoke checks`), and make the tests validate that sequence instead of hidden partial schema creation.
- Review update (cycle 2): IMP-001 is resolved. Runtime/CLI paths are back to data seeding only, the initial migration carries the SQLite smoke-path compatibility adjustments, and the bootstrap tests now validate the explicit migration-first sequence.
