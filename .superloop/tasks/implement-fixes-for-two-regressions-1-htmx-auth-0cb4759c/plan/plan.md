# Plan

## Scope
- Fix browser auth redirect handling so HTMX requests to protected browser pages trigger full-page navigation to login instead of swapping login HTML into the target.
- Fix wrong-role browser page access so browser HTML routes render a dedicated 403 page instead of falling back to API-style JSON 403 responses.
- Keep API and other non-browser auth/permission behavior unchanged.

## Affected Areas
- `app/auth.py`
  - Browser auth dependencies and browser-only exception types.
- `app/main.py`
  - Central exception handlers for browser redirect and browser forbidden cases.
- `app/templates/403.html`
  - New browser 403 template.
- `tests/test_ops_workflow.py`
  - HTMX unauthenticated `/ops` and `/ops/board` redirect assertions.
  - Browser wrong-role ops access assertions.
- `tests/test_auth_requester.py`
  - Browser wrong-role requester access assertions.

## Intended Implementation
- Keep `BrowserRedirectRequired` as the browser unauthenticated control path, but make its handler branch on `HX-Request`.
  - Non-HTMX browser requests: preserve existing `303 Location` redirect behavior.
  - HTMX requests: return a non-redirect response with `HX-Redirect: <login-url>` so htmx performs a full navigation.
- Introduce a browser-only forbidden exception for wrong-role browser pages instead of raising plain `HTTPException(403)` from `require_browser_requester_user` and `require_browser_ops_user`.
- Register a matching exception handler in `app/main.py` that renders `403.html` with HTTP 403 for browser HTML routes.
- Reuse existing template plumbing (`templates` and `build_template_context`) rather than adding a separate rendering stack.
- Limit the new browser-forbidden path to the browser-specific role dependencies only; leave API/non-browser permission checks and unrelated 403s on their existing JSON/HTTPException behavior.

## Interface / Behavior Notes
- Browser unauthenticated access:
  - `/ops`, `/ops/board`, `/app`, and other routes using browser auth dependencies still send users to `/login?...`.
  - Only HTMX requests change transport shape by using `HX-Redirect` instead of a plain 303.
- Browser wrong-role access:
  - Status remains `403`.
  - Response becomes HTML template content for routes using `require_browser_requester_user` and `require_browser_ops_user`.
  - Response must not redirect.
- Non-browser/API behavior:
  - `require_requester_user`, `require_ops_user`, attachment 403s, CSRF 403s, and other plain `HTTPException` flows remain unchanged.

## Milestone
### Single implementation slice
- Add the browser-only forbidden exception and template.
- Update browser exception handlers for HTMX-aware redirect and HTML 403 rendering.
- Add focused regression tests for HTMX unauthenticated ops routes and wrong-role browser routes.
- Run the targeted auth/ops test modules.

## Regression Controls
- Keep browser-role changes local to `require_browser_requester_user` and `require_browser_ops_user`; do not broaden to all 403 handlers.
- Preserve existing safe-`next` login redirect generation by continuing to use `login_redirect_path(next_path_from_request(request))`.
- Ensure HTMX partial-success responses for authenticated `/ops` and `/ops/board` remain unchanged; only exception responses should differ.
- Ensure wrong-role browser requests return 403 HTML without `Location` headers so tests catch accidental redirects.

## Validation
- Update/add focused tests in:
  - `tests/test_ops_workflow.py`
  - `tests/test_auth_requester.py`
- Validate:
  - Unauthenticated HTMX `GET /ops?status=new` returns HTMX redirect semantics to login.
  - Unauthenticated HTMX `GET /ops/board?status=new` returns HTMX redirect semantics to login.
  - Wrong-role browser access to ops pages returns 403 HTML template content and no redirect.
  - Wrong-role browser access to requester pages returns 403 HTML template content and no redirect.
  - Existing non-HTMX browser redirect tests continue to assert 303 `Location`.

## Compatibility / Rollback
- No public API, config, or persistence changes.
- Rollback is limited to removing the new browser-forbidden exception/template and restoring the previous exception handlers if regressions appear.

## Risk Register
- Risk: Returning `HX-Redirect` on a 3xx response would not reliably fix the htmx swap behavior.
  - Control: Explicitly require a non-3xx HTMX-aware response shape in the handler and test the header contract.
- Risk: A global 403 handler could accidentally convert API JSON errors into HTML.
  - Control: Use a distinct browser-only exception type raised only by browser role dependencies.
- Risk: Rendering `403.html` without existing base-template context could break nav/logout rendering.
  - Control: Reuse `build_template_context` and pass the available browser user/session context through the browser-forbidden exception path.
