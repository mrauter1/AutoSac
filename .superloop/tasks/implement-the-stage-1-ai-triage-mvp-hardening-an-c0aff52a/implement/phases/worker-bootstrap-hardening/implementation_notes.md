# Implementation Notes

- Task ID: implement-the-stage-1-ai-triage-mvp-hardening-an-c0aff52a
- Pair: implement
- Phase ID: worker-bootstrap-hardening
- Phase Directory Key: worker-bootstrap-hardening
- Phase Title: Worker Bootstrap And Validation Hardening
- Scope: phase-local producer artifact
- Files changed:
  `shared/security.py`, `worker/codex_runner.py`, `worker/triage.py`, `worker/main.py`, `shared/user_admin.py`, `scripts/create_admin.py`, `README.md`, `.env.example`, `tests/test_ai_worker.py`, `tests/test_foundation_persistence.py`
- Symbols touched:
  `verify_password`, `build_codex_command`, `execute_codex_run`, `validate_triage_result`, `ensure_worker_system_state`, `update_worker_heartbeat`, `ensure_admin_user`
- Checklist mapping:
  prompt transport hardening -> `worker/codex_runner.py`
  password hash hardening -> `shared/security.py`
  validation matrix hardening -> `worker/triage.py`
  worker/system_state initialization -> `worker/main.py`, `scripts/create_admin.py`
  deterministic admin bootstrap docs/flow -> `shared/user_admin.py`, `scripts/create_admin.py`, `README.md`, `.env.example`
  regression coverage -> `tests/test_ai_worker.py`, `tests/test_foundation_persistence.py`
- Assumptions:
  matching bootstrap admin state means same normalized email, active admin role, same stripped display name, and password verifying against the stored hash
- Preserved invariants:
  Codex artifacts still include `prompt.txt`, `schema.json`, `stdout.jsonl`, `stderr.txt`, and `final.json`
  valid Stage 1 action names and publication flows remain unchanged
  worker heartbeat still updates `system_state.worker_heartbeat` in place
- Intended behavior changes:
  malformed or unsupported stored password hashes now fail closed instead of surfacing login-time exceptions
  Codex prompt content is sent via stdin (`-`) instead of argv while keeping the same run artifacts
  contradictory triage payloads are rejected earlier, including clarification/action mismatches and route-only payloads carrying public replies
  worker startup and admin bootstrap both seed missing `system_state` defaults, including `bootstrap_version`
  worker heartbeat now flushes seeded `system_state` defaults before reading `worker_heartbeat`, which keeps first-call behavior safe under the repo's `autoflush=False` session factory
  `create_admin.py` is create-if-missing, explicit no-op on exact match, and explicit error on conflict
- Known non-changes:
  no ticket state definitions or AI action names changed
  no interactive Codex auth or web-search behavior changed
  no schema rewrite or migration shape changed in this phase
- Expected side effects:
  rerunning `create_admin.py` with identical inputs now prints an idempotent success message instead of failing on duplicate email
  direct `emit_worker_heartbeat()` calls also backfill missing `system_state` defaults
  the heartbeat helper now performs one explicit flush after seeding defaults before it re-reads `worker_heartbeat`
- Validation performed:
  `pytest tests/test_ai_worker.py tests/test_foundation_persistence.py tests/test_hardening_validation.py -q`
  `pytest tests/test_auth_requester.py -q`
  `python -m compileall shared worker scripts tests`
- Deduplication / centralization:
  worker default seeding reuses `shared.ticketing.ensure_system_state_defaults()` rather than duplicating `bootstrap_version` writes
  admin bootstrap matching is centralized in `shared.user_admin.ensure_admin_user()`
