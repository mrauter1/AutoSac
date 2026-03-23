# Test Strategy

- Task ID: implement-the-following-plan-in-this-repository-d9b66dcb
- Pair: test
- Phase ID: bootstrap-system-state
- Phase Directory Key: bootstrap-system-state
- Phase Title: Bootstrap And System State
- Scope: phase-local producer artifact

## Behavior To Coverage Map
- AC-1 bootstrap seeding:
  - `tests/test_hardening_validation.py::test_bootstrap_workspace_script_seeds_required_system_state_defaults`
  - Verifies `bootstrap_version` and `worker_heartbeat` exist after the documented bootstrap step.
- AC-1 worker startup ordering:
  - `tests/test_ai_worker.py::test_worker_main_seeds_system_state_before_heartbeat_and_poll_loop`
  - Verifies the worker seeds defaults before heartbeat startup and before the first queue claim.
- AC-2 admin bootstrap idempotency:
  - `tests/test_hardening_validation.py::test_create_admin_if_missing_is_idempotent`
  - Verifies repeated `create_admin.py --if-missing` does not duplicate the admin row.
- AC-2 preserved safety on conflicting accounts:
  - `tests/test_hardening_validation.py::test_create_admin_if_missing_rejects_existing_non_admin`
  - Verifies `--if-missing` does not silently repurpose an existing non-admin account.
- AC-3 explicit bootstrap runbook:
  - `tests/test_hardening_validation.py::test_bootstrap_web_and_worker_scripts_validate_end_to_end`
  - Verifies the migration-first sequence `alembic upgrade head -> bootstrap_workspace.py -> create_admin.py --if-missing -> smoke checks`.
  - `tests/test_hardening_validation.py::test_run_web_check_works_outside_repo_root_after_bootstrap`
  - Verifies the same sequence still works when the web smoke check is run outside the repo root.

## Failure Paths / Edge Cases
- `tests/test_hardening_validation.py::test_bootstrap_workspace_script_requires_migrations_first`
  - Locks the runbook order by asserting bootstrap fails before schema setup.
- `tests/test_hardening_validation.py::test_script_checks_fail_before_workspace_bootstrap`
  - Preserves the pre-bootstrap smoke-check failure behavior.

## Preserved Invariants Checked
- Bootstrap seeding does not change worker queue semantics; worker ordering is verified at the `worker.main` unit boundary.
- The bootstrap contract remains migration-first; runtime/CLI paths are not expected to create schema tables opportunistically.

## Flake Risk / Stabilization
- Script-level tests use temp dirs, explicit env vars, and SQLite files under `tmp_path` to avoid shared state.
- The worker-startup test uses monkeypatched control flow instead of real threads or sleeps.

## Known Gaps
- README / `.env.example` contract coverage remains deferred to the later docs/env phase, matching the shared phase decisions.
