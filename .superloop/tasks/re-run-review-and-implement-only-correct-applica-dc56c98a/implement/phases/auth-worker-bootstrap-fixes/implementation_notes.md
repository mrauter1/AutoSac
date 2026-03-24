# Implementation Notes

- Task ID: re-run-review-and-implement-only-correct-applica-dc56c98a
- Pair: implement
- Phase ID: auth-worker-bootstrap-fixes
- Phase Directory Key: auth-worker-bootstrap-fixes
- Phase Title: Auth challenge, triage allowlist, and bootstrap defaults
- Scope: phase-local producer artifact

## Files Changed
- `app/routes_auth.py`
- `worker/triage.py`
- `scripts/bootstrap_workspace.py`
- `tests/test_auth_requester.py`
- `tests/test_ai_worker.py`
- `tests/test_foundation_persistence.py`
- `tests/test_hardening_validation.py`
- `.superloop/tasks/re-run-review-and-implement-only-correct-applica-dc56c98a/decisions.txt`

## Symbols Touched
- `app.routes_auth.login_action`
- `app.routes_auth._issue_login_challenge`
- `worker.triage.AUTO_PUBLIC_ACTION_ALLOWED_CLASSES`
- `worker.triage.validate_triage_result`
- `scripts.bootstrap_workspace.main`

## Checklist Mapping
- Login preauth `403` reissue: done in `app/routes_auth.py` and `tests/test_auth_requester.py`
- Failed-login preauth rotation: done in `app/routes_auth.py` and `tests/test_auth_requester.py`
- Worker automatic public-action allowlist: done in `worker/triage.py` and `tests/test_ai_worker.py`
- Bootstrap `system_state` default seeding: done in `scripts/bootstrap_workspace.py`, `tests/test_foundation_persistence.py`, and `tests/test_hardening_validation.py`
- Targeted regression coverage: done via the focused pytest run below

## Assumptions
- `scripts/bootstrap_workspace.py` runs after DB schema creation, consistent with the documented `alembic upgrade head` bootstrap order.

## Preserved Invariants
- Successful login still clears the preauth cookie, starts the user session, and preserves remember-me handling.
- Invalid credentials still return `400`; only invalid or missing preauth challenge state changed to `403`.
- Worker validation remains unchanged for non-automatic public actions outside the requested allowlist narrowing.
- Bootstrap script still creates workspace files and prints the same workspace snapshot shape.

## Intended Behavior Changes
- Missing or mismatched login preauth challenge state now returns `403` and always issues a fresh login challenge.
- Invalid credential submissions now rotate the preauth login cookie and hidden CSRF token by reissuing the challenge.
- `auto_public_reply` and `auto_confirm_and_route` are rejected unless `ticket_class` is `support` or `access_config`.
- `scripts/bootstrap_workspace.py` seeds `system_state` defaults with `WORKSPACE_BOOTSTRAP_VERSION` before exit.

## Known Non-Changes
- No logout or authenticated-session CSRF behavior changed.
- No allowlist expansion beyond `support` and `access_config`.
- No schema, migration, or configuration files were changed.

## Expected Side Effects
- Browser clients receive a new preauth cookie after failed credential attempts and challenge failures.
- Bootstrap script now touches the database in addition to workspace files.

## Validation Performed
- `pytest tests/test_auth_requester.py tests/test_ai_worker.py tests/test_foundation_persistence.py tests/test_hardening_validation.py`
- Result: `67 passed`

## Deduplication / Centralization Decisions
- Reused `_issue_login_challenge()` for failed-credential responses to keep challenge issuance, cookie rotation, and commit behavior on one path.
- Kept the worker allowlist check inside `validate_triage_result()` so both automatic public actions share the same enforcement point.
