# Implementation Notes

- Task ID: implement-the-following-plan-in-this-repository-d9b66dcb
- Pair: implement
- Phase ID: bootstrap-system-state
- Phase Directory Key: bootstrap-system-state
- Phase Title: Bootstrap And System State
- Scope: phase-local producer artifact

## Files Changed
- `scripts/bootstrap_workspace.py`
- `scripts/create_admin.py`
- `shared/models.py`
- `shared/ticketing.py`
- `worker/main.py`
- `tests/test_ai_worker.py`
- `tests/test_hardening_validation.py`
- `.superloop/tasks/implement-the-following-plan-in-this-repository-d9b66dcb/decisions.txt`

## Symbols Touched
- `scripts.bootstrap_workspace.main`
- `scripts.create_admin.main`
- `shared.models.SystemState.value_json`
- `shared.ticketing.ensure_system_state_defaults`
- `worker.main.main`

## Checklist Mapping
- Workstream E1: called `ensure_system_state_defaults()` from bootstrap script and worker startup before polling.
- Workstream E2: added idempotent `create_admin.py --if-missing` path and encoded the deterministic migration-first smoke/bootstrap path in tests.
- Workstream G: added coverage for bootstrap defaults, worker startup ordering, and idempotent admin bootstrap.

## Intended Behavior Changes
- `python scripts/bootstrap_workspace.py` now ensures `system_state.bootstrap_version` and `system_state.worker_heartbeat` exist.
- Worker startup seeds missing `system_state` defaults before heartbeat thread startup and before the first queue claim.
- `python scripts/create_admin.py --if-missing ...` succeeds without creating a duplicate when the admin already exists.

## Preserved Invariants
- `shared.workspace.bootstrap_workspace()` remains filesystem/workspace-only.
- Worker queue claim/processing semantics remain unchanged after startup seeding.
- Existing users are not mutated by `--if-missing`; conflicting non-admin accounts still fail closed.
- Runtime and CLI paths do not perform opportunistic schema creation outside the migration flow.

## Known Non-Changes
- README / `.env.example` contract updates remain deferred to the later docs/env phase.
- Web startup was left unchanged; bootstrap correctness does not depend on the web app initializing state.

## Side Effects / Centralization
- `SystemState.value_json` now uses a generic JSON type with PostgreSQL JSONB preserved via dialect variant, keeping PostgreSQL behavior while allowing SQLite smoke tests.
- The initial Alembic migration contains the SQLite-compatible adjustments needed for the explicit smoke/bootstrap runbook instead of pushing schema creation into runtime code.

## Validation Performed
- `pytest tests/test_ai_worker.py -q`
- `pytest tests/test_foundation_persistence.py -q`
- `pytest tests/test_hardening_validation.py -q -k 'not env_example_and_readme_capture_acceptance_contract'`
