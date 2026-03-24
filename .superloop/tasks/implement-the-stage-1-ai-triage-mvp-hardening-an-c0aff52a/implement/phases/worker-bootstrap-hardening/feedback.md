# Implement ↔ Code Reviewer Feedback

- Task ID: implement-the-stage-1-ai-triage-mvp-hardening-an-c0aff52a
- Pair: implement
- Phase ID: worker-bootstrap-hardening
- Phase Directory Key: worker-bootstrap-hardening
- Phase Title: Worker Bootstrap And Validation Hardening
- Scope: phase-local authoritative verifier artifact

- IMP-001 `blocking` [worker/main.py:25, shared/ticketing.py:152]:
  `update_worker_heartbeat()` now calls `ensure_worker_system_state()` and then immediately does `db.get(SystemState, "worker_heartbeat")`, but the repository session factory runs with `autoflush=False`. In real SQLAlchemy behavior, `Session.get()` does not see the just-added pending `SystemState(key="worker_heartbeat", ...)` row, so first-call paths that rely on `update_worker_heartbeat()` to seed defaults will add a second `worker_heartbeat` row with the same primary key and fail on commit. This breaks the new “initialize defaults here too” contract for the heartbeat helper and creates a latent first-startup regression if the separate `main()` pre-seed path is bypassed or refactored. Minimal fix: centralize the seeding in one place, or flush/reuse the pending row before `db.get()`, so `update_worker_heartbeat()` never inserts a duplicate key when defaults are missing.

- IMP-001 resolution check:
  fixed in cycle 2 by flushing after `ensure_worker_system_state()` and before `db.get(SystemState, "worker_heartbeat")`; the focused worker/bootstrap/auth regression suite now passes with a test double that keeps `SystemState` rows pending until `flush()`. No remaining blocking findings in this phase review.
