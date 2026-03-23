# Test Strategy

- Task ID: implement-the-following-plan-in-this-repository-d9b66dcb
- Pair: test
- Phase ID: docs-env-contract
- Phase Directory Key: docs-env-contract
- Phase Title: Docs And Env Contract
- Scope: phase-local producer artifact

## Behavior To Coverage Map
- AC-1 `.env.example` variable contract:
  assert every required env var from the request snapshot appears as a concrete assignment in `.env.example`.
- AC-2 README runbook contract:
  assert `README.md` documents migrations, workspace bootstrap, admin creation with `--if-missing`, web and worker smoke checks, readiness, test execution, and Stage 1 non-goals.

## Preserved Invariants Checked
- Docs coverage remains file-local and deterministic; it does not depend on environment variables, database setup, or subprocess execution.
- The existing contract check stays in `tests/test_hardening_validation.py` so docs/env validation remains colocated with the broader bootstrap hardening coverage.

## Edge Cases And Failure Paths
- Missing any single required env var now fails the contract test instead of allowing partial coverage to pass.
- README regressions on the migration-first runbook or Stage 1 boundary language fail through targeted substring assertions.

## Known Gaps
- The test validates required contract content by presence, not prose quality or ordering.
- It does not execute the runbook here; execution coverage remains in the existing bootstrap and smoke-check tests.
