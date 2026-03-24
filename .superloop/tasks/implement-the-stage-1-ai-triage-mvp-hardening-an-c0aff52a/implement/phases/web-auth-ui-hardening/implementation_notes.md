# Implementation Notes

- Task ID: implement-the-stage-1-ai-triage-mvp-hardening-an-c0aff52a
- Pair: implement
- Phase ID: web-auth-ui-hardening
- Phase Directory Key: web-auth-ui-hardening
- Phase Title: Web Auth And UI Hardening
- Scope: phase-local producer artifact
- Files changed:
  app/auth.py
  app/main.py
  app/routes_auth.py
  app/routes_ops.py
  app/templates/base.html
  app/templates/login.html
  app/templates/ops_filters.html
  app/templates/ops_ticket_rows.html
  app/templates/ops_board_columns.html
  app/ui.py
  shared/contracts.py
  shared/models.py
  shared/preauth_login.py
  shared/migrations/versions/20260324_0002_preauth_login_sessions.py
  app/static/htmx.min.js
  tests/test_auth_requester.py
  tests/test_ops_workflow.py
- Symbols touched:
  `PreauthLoginSession`
  `create_preauth_login_session()`
  `get_valid_preauth_login_session()`
  `invalidate_preauth_login_session()`
  `sanitize_next_path()`
  `login_redirect_path()`
  `request_next_path()`
  `is_html_navigation_request()`
  `login_page()`
  `login_action()`
  `ops_ticket_list()`
  `ops_board()`
- Checklist mapping:
  Milestone 1 / module-relative paths: `app/ui.py`, `app/main.py`, path regression test added.
  Milestone 1 / browser redirect + safe next: `app/auth.py`, `app/main.py`, `app/routes_auth.py`, auth tests added.
  Milestone 1 / preauth login CSRF + additive persistence: `shared/models.py`, `shared/preauth_login.py`, migration `20260324_0002`, login tests added.
  Milestone 1 / local HTMX + fragment filtering: `app/static/htmx.min.js`, `base.html`, ops templates/routes, ops tests updated.
- Assumptions:
  Preauth login TTL stays hard-coded at 10 minutes for this phase because no settings knob was requested.
  Safe `next` accepts only relative in-app paths and rejects exact `/login` recursion; other invalid targets fall back to role-default post-login redirects.
- Preserved invariants:
  Authenticated session cookie name/format and role model are unchanged.
  Wrong-role requests still return `403`.
  Ops list, board, and HTMX filter refreshes still do not touch `ticket_views`; detail pages still do.
- Intended behavior changes:
  Unauthenticated browser GET/HEAD navigation to `/app` and `/ops` HTML pages now redirects to `/login?next=...`.
  `/login` now uses a dedicated server-side preauth challenge with a short-lived `/login`-scoped cookie and hidden CSRF token.
  `/ops` now returns `ops_ticket_rows.html` for HTMX requests, matching the existing board fragment pattern.
- Known non-changes:
  No SPA/client-side routing work.
  No drag-and-drop board behavior.
  No authenticated session schema or cookie contract changes.
- Expected side effects:
  Visiting `/login` rotates any existing preauth challenge to keep `next_path` and CSRF aligned with the latest navigation.
  Secure preauth cookies require HTTPS, matching `APP_BASE_URL`.
- Validation performed:
  `pytest -q tests/test_auth_requester.py tests/test_ops_workflow.py`
  `pytest -q tests/test_foundation_persistence.py tests/test_auth_requester.py tests/test_ops_workflow.py`
- Deduplication / centralization:
  Shared path and request classification helpers were centralized in `app/ui.py`.
  HTMX full-page vs fragment selection remains route-local via `_template_or_partial_response()`.
