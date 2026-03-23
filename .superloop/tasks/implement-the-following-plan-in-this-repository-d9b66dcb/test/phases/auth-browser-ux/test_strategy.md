# Test Strategy

- Task ID: implement-the-following-plan-in-this-repository-d9b66dcb
- Pair: test
- Phase ID: auth-browser-ux
- Phase Directory Key: auth-browser-ux
- Phase Title: Browser Auth UX
- Scope: phase-local producer artifact

## Behavior-to-test coverage map

- AC-1 `GET /login` preauth issuance / refresh:
  - `tests/test_auth_requester.py::test_login_page_sets_preauth_cookie_and_embeds_csrf`
  - Verifies preauth cookie issuance, embedded login CSRF token, and unsafe `next` sanitization on the rendered form.
- AC-2 `POST /login` CSRF rejection, rotation, success redirect, and cleanup:
  - `tests/test_auth_requester.py::test_login_route_rejects_invalid_csrf_and_rotates_preauth`
  - `tests/test_auth_requester.py::test_login_route_rejects_missing_csrf_and_rotates_preauth`
  - `tests/test_auth_requester.py::test_login_route_redirects_to_sanitized_next_and_clears_preauth_cookie`
  - `tests/test_auth_requester.py::test_login_route_invalid_next_falls_back_to_role_redirect`
  - `tests/test_auth_requester.py::test_login_route_sets_remember_me_cookie`
  - `tests/test_auth_requester.py::test_login_route_returns_invalid_credentials_for_malformed_hash`
- AC-3 browser redirect / safe-next / wrong-role behavior:
  - `tests/test_auth_requester.py::test_logged_in_login_page_uses_safe_next_or_role_fallback`
  - `tests/test_auth_requester.py::test_unauthenticated_browser_requester_route_redirects_to_login_with_safe_next`
  - `tests/test_auth_requester.py::test_ops_user_cannot_access_requester_browser_routes`
  - `tests/test_ops_workflow.py::test_unauthenticated_browser_ops_route_redirects_to_login_with_safe_next`
  - `tests/test_ops_workflow.py::test_requester_cannot_access_ops_routes`
  - `tests/test_ops_workflow.py::test_requester_cannot_access_ops_board`
  - `tests/test_ops_workflow.py::test_requester_cannot_access_ops_ticket_detail`

## Preserved invariants checked

- Existing authenticated-form CSRF behavior remains unchanged via existing requester and ops route tests.
- Protected list and detail views keep their existing read-tracking expectations.
- Wrong-role browser access remains `403` rather than cross-area redirecting.

## Edge cases and failure paths

- Invalid external `next` values on GET and POST login flows.
- Missing login CSRF versus invalid login CSRF.
- Malformed stored password hash still returns login failure instead of a server error.
- Successful login clears preauth cookie and keeps auth cookie behavior.

## Stabilization approach

- Tests use deterministic dependency overrides and lightweight fake DB objects rather than real persistence.
- Cookie rotation and redirect assertions are checked directly on response headers to avoid timing or ordering flake.

## Known gaps

- The new missing-CSRF test currently fails because `/login` uses required form validation for `csrf_token`, yielding FastAPI `422` before the app can rotate or refresh preauth state. This is an implementation gap against AC-2, not a test flake.
