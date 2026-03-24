# Implementation Notes

- Task ID: implement-the-stage-1-ai-triage-mvp-hardening-an-c0aff52a
- Pair: implement
- Phase ID: docs-and-release-validation
- Phase Directory Key: docs-and-release-validation
- Phase Title: Docs And Release Validation
- Scope: phase-local producer artifact

## Files changed

- `README.md`
- `.env.example`
- `tests/test_auth_requester.py`
- `tests/test_ai_worker.py`
- `tests/test_hardening_validation.py`

## Symbols touched

- `test_protected_htmx_get_keeps_401_instead_of_redirect`
- `test_validate_triage_result_rejects_clarification_with_auto_public_flag`
- `test_env_example_and_readme_capture_acceptance_contract`

## Checklist mapping

- Plan milestone 3 / docs: rewrote `README.md` around the actual Stage 1 runtime, install path, migration step, deterministic bootstrap order, smoke checks, and user-management CLI flow.
- Plan milestone 3 / env contract: completed `.env.example` with every `Settings` environment variable plus comments for required values, defaults, and mount prerequisites.
- Plan milestone 3 / regression coverage: added assertions for HTMX unauthenticated 401 behavior, an additional AI triage validation contradiction, and stronger docs-contract checks.

## Assumptions

- Local development continues to use `requirements.txt` and `alembic upgrade head` as the supported setup path because those are the concrete repo-level entry points present in this workspace.

## Preserved invariants

- Existing auth, HTMX, bootstrap, and worker behavior were not changed in code; this phase only aligned documentation and expanded regression coverage.
- Browser HTML redirects remain limited to normal navigations; HTMX requests keep non-redirect 401 behavior.
- The deterministic admin bootstrap semantics from the earlier phase remain the documented contract.

## Intended behavior changes

- None at runtime. This phase changes operator-facing documentation and test coverage only.

## Known non-changes

- No product copy repositioning, operational tooling additions, or architecture changes.
- No edits to bootstrap, auth, worker, route, or persistence implementation modules.

## Expected side effects

- README and `.env.example` now describe the shipped workflow more explicitly, including migration/setup prerequisites and local user-management scripts.
- Docs regressions should now fail tests if required env vars or documented CLI steps drift.

## Validation performed

- `pytest tests/test_auth_requester.py tests/test_ai_worker.py tests/test_hardening_validation.py`
- `pytest tests/test_ops_workflow.py tests/test_foundation_persistence.py`
- `pytest tests/test_auth_requester.py tests/test_ops_workflow.py tests/test_ai_worker.py tests/test_foundation_persistence.py tests/test_hardening_validation.py`

## Deduplication / centralization

- Strengthened the existing docs-contract test instead of creating a separate docs-only suite, so README and `.env.example` drift remains enforced in the same hardening validation surface.
