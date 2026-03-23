# Implementation Notes

- Task ID: implement-the-following-plan-in-this-repository-d9b66dcb
- Pair: implement
- Phase ID: auth-browser-ux
- Phase Directory Key: auth-browser-ux
- Phase Title: Browser Auth UX
- Scope: phase-local producer artifact

## Files changed
- `app/auth.py`
- `app/main.py`
- `app/routes_auth.py`
- `app/routes_ops.py`
- `app/routes_requester.py`
- `app/templates/login.html`
- `shared/contracts.py`
- `shared/models.py`
- `shared/sessions.py`
- `shared/migrations/versions/20260323_0002_preauth_sessions.py`
- `tests/test_auth_requester.py`
- `tests/test_ops_workflow.py`

## Symbols touched
- `app.auth.BrowserRedirectRequired`
- `app.auth.sanitize_next_path`
- `app.auth.login_redirect_path`
- `app.auth.next_path_from_request`
- `app.auth.resolve_post_login_redirect`
- `app.auth.get_optional_preauth_session`
- `app.auth.get_required_browser_auth_session`
- `app.auth.require_browser_user`
- `app.auth.require_browser_requester_user`
- `app.auth.require_browser_ops_user`
- `app.auth.issue_login_preauth_session`
- `app.auth.end_login_preauth_session`
- `app.routes_auth.login_page`
- `app.routes_auth.login_action`
- `app.routes_auth.logout_action`
- `shared.models.PreAuthSessionRecord`
- `shared.sessions.create_preauth_session`
- `shared.sessions.refresh_preauth_session`
- `shared.sessions.get_valid_preauth_session_by_token`
- `shared.sessions.invalidate_preauth_session`

## Checklist mapping
- Workstream B1: implemented dedicated `preauth_sessions` persistence, preauth cookie lifecycle, login CSRF validation, failure rotation, and preauth cleanup on successful login.
- Workstream B2: implemented browser-only redirect helpers for protected HTML GET routes, safe internal `next` sanitization/consumption, and preserved wrong-role browser `403` behavior.
- Workstream G auth/browser tests: added coverage for login preauth creation, invalid-CSRF rotation, safe-next redirects, unauthenticated browser redirects, wrong-role browser access, and updated route tests to use the browser-only dependencies.

## Assumptions
- Login preauth expiry reuses the existing non-remembered session expiry window instead of introducing a new setting.
- The preauth cookie remains session-scoped; server-side expiry still governs stale-token rejection.

## Preserved invariants
- Existing authenticated session cookies, authenticated form CSRF checks, and logout semantics remain in place.
- Non-browser/API-style auth semantics remain unchanged; only protected HTML GET routes use the new redirecting browser guards.
- Wrong-role authenticated browser access still returns `403` instead of cross-area redirects.

## Intended behavior changes
- `GET /login` now issues or refreshes a dedicated preauth session and embeds a login CSRF token.
- `POST /login` now requires a valid login CSRF token, rotates/reissues preauth state on failure, consumes sanitized internal `next`, and clears preauth on success.
- Unauthenticated browser requests to protected requester and ops HTML pages now redirect to `/login?next=...` with sanitized internal targets.

## Known non-changes
- No `SessionMiddleware`, OAuth/SSO, or authenticated POST-form CSRF redesign.
- No changes to JSON/health endpoint status semantics.
- No role-based validation of safe internal `next` targets beyond the existing destination route guards.

## Expected side effects
- Failed login attempts now commit once to persist the refreshed preauth CSRF state.
- Successful login responses emit both the auth session cookie and a preauth cookie deletion header.

## Validation performed
- `pytest tests/test_auth_requester.py tests/test_ops_workflow.py` ✅
- `pytest` ⚠️ blocked during collection by pre-existing `superloop` import-path issues (`loop_control` / `superloop` module resolution), unrelated to this phase’s code changes.

## Deduplication / centralization
- Centralized safe-next handling, browser redirect production, and login preauth cookie issuance in `app/auth.py` so requester, ops, and login routes share one behavior path.
