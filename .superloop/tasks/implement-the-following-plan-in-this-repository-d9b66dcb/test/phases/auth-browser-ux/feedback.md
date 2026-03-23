# Test Author ↔ Test Auditor Feedback

- Task ID: implement-the-following-plan-in-this-repository-d9b66dcb
- Pair: test
- Phase ID: auth-browser-ux
- Phase Directory Key: auth-browser-ux
- Phase Title: Browser Auth UX
- Scope: phase-local authoritative verifier artifact

## Test additions

- Added auth/browser tests for missing login CSRF, invalid-post-login `next` fallback, and requester-route wrong-role browser `403`.
- Updated the phase strategy with an explicit AC-to-test coverage map and preserved-invariant checks.
- Current targeted run: `pytest tests/test_auth_requester.py tests/test_ops_workflow.py -q` fails only at `tests/test_auth_requester.py::test_login_route_rejects_missing_csrf_and_rotates_preauth`, exposing that missing login CSRF currently returns FastAPI `422` instead of the contract-required rotated login failure response.

## Audit result

- No blocking or non-blocking test-quality findings in phase scope.
