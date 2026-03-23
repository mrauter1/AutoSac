# AutoSac Implementation Plan

## Objective
Align the Stage 1 application to the supplied contract without expanding scope: fix cwd-dependent web paths, harden password verification and Codex prompt transport, add login CSRF plus browser redirect behavior, complete HTMX list/board partials, tighten AI action validation, ensure deterministic bootstrap/system-state initialization, and bring README/.env/tests up to the same contract.

## Scope And Sequencing
1. Foundations
   - `app/main.py` and `app/ui.py`: resolve static/template directories from `Path(__file__).resolve()` so `create_app()` and `python scripts/run_web.py --check` work outside repo root while keeping `/static` and current template names unchanged.
   - `shared/security.py`: broaden `verify_password()` failure handling to return `False` for malformed or invalid argon2 hashes without changing the function signature or leaking details.
   - `worker/codex_runner.py`: keep `prompt.txt` and `final.json` canonical, preserve required Codex flags, and stop passing the full prompt as the final argv element; prefer stdin transport and fall back to a short fixed instruction that points Codex to `prompt.txt`.
2. Auth And Browser UX
   - Add a dedicated preauth login CSRF flow with a server-side preauth session table and cookie; do not use `SessionMiddleware`.
   - Keep auth-session behavior for logged-in POST forms unchanged, but separate login-page CSRF from authenticated session CSRF.
   - Add browser-only auth guards for protected HTML pages so unauthenticated requests redirect to `/login?next=...`, external/protocol `next` values are ignored, successful login consumes the sanitized internal `next` target when present, and authenticated wrong-role browser requests return `403` instead of redirecting.
3. HTMX Ops UI
   - Vendor HTMX locally under app static assets and load it from `base.html`.
   - Update `/ops` and `/ops/board` filter forms to use `hx-get`, `hx-target`, and `hx-push-url="true"` with stable fragment container ids.
   - Return the rows partial for `/ops` and board-columns partial for `/ops/board` only when `HX-Request: true`; preserve full-template responses and non-JS GET fallback.
   - Preserve read-tracking semantics: list and board routes must not call `upsert_ticket_view()`.
4. AI Safety Validation
   - Tighten `worker/triage.py::validate_triage_result()` so the allowed action matrix matches the user contract and explicitly rejects contradictory combinations.
   - Preserve the existing “after two clarification rounds, route to Dev/TI” override in `_effective_next_action()`.
5. Bootstrap And System State
   - Reuse `shared.ticketing.ensure_system_state_defaults()` instead of introducing new state helpers.
   - Call it from `scripts/bootstrap_workspace.py` and worker startup before the poll loop; web startup remains optional and must not be required for correctness.
   - Make admin bootstrap deterministic and idempotent by adding a non-interactive `--if-missing` path to `scripts/create_admin.py`.
6. Docs And Environment Contract
   - Expand `.env.example` to include the full required variable set from the request.
   - Rewrite `README.md` to match the actual Stage 1 scope, bootstrap sequence, migration/runbook steps, smoke checks, tests, and worker boundaries.
7. Tests And Verification
   - Extend route, worker, bootstrap, and contract tests to cover the new auth/HTMX/bootstrap behaviors and prevent regressions in view tracking, CLI contract, and malformed-hash handling.

## Interfaces And Data Changes
- New persisted auth data:
  - Add a dedicated `preauth_sessions` table/model for login CSRF. Fields should mirror the existing server-session pattern closely enough to support opaque cookie lookup, csrf token rotation, expiry, user-agent/ip capture, and invalidation on successful login.
  - Add a migration for the new table only; existing `sessions`, `ticket_views`, and `system_state` tables remain in place.
- Browser auth helpers:
  - Introduce browser-specific helpers in the auth layer rather than changing `get_current_user()` / `require_ops_user()` semantics globally. This keeps JSON/health/API-style behaviors unchanged.
  - Safe `next` handling must only allow internal absolute paths such as `/ops` or `/app/tickets/123`; reject empty, protocol-relative, absolute-URL, or scheme-prefixed values.
  - Successful login must redirect to the sanitized `next` target when it is present and valid; otherwise it falls back to `post_login_redirect_path(user)`. Logged-in visits to `/login` should follow the same sanitized-`next` rule to avoid discarding the browser return target.
- Ops list/board rendering:
  - Keep `ops_ticket_rows.html` and `ops_board_columns.html` as the canonical fragment templates.
  - Add stable DOM wrapper ids so filter HTMX swaps target only the rows/columns container, not the full page shell.
- Codex transport:
  - `prompt.txt` remains the persisted source of truth for the prompt and `final.json` remains the canonical result artifact.
  - Command-line flags already asserted in tests remain unchanged except for prompt delivery.

## Compatibility And Regression Notes
- No SPA, OAuth/SSO, Slack/email notifications, worker web search, repo-write access for Codex, OCR, non-image attachments, edit/delete ticket content, or Codex-generated patches.
- Existing routes, table names, status transitions, and worker state machine stay stable unless the request explicitly changes them.
- Browser redirects apply only to protected HTML pages. `/healthz`, `/readyz`, existing JSON responses, and non-HTML failures must retain their current status semantics.
- The new `next` parameter is a browser-only flow-control input, not a general redirect primitive. Invalid or unsafe values must be ignored, never echoed back as external redirects, and must fall back to the existing role-default post-login destination.
- Wrong-role authenticated access must stay a hard `403`; do not redirect a requester from `/ops` to `/app`, or vice versa.
- List and board filters must remain read-only with respect to `ticket_views.last_viewed_at`; only detail views and existing state-changing actions should move read markers.
- `verify_password()` must fail closed to `False` for malformed hashes so login cannot raise a server error on bad stored data.

## Validation Plan
- Route/auth tests:
  - login GET creates/refreshes a preauth session and emits login CSRF token
  - login POST rejects missing/invalid preauth CSRF, rotates/reissues preauth on failure, clears it on success
  - unauthenticated browser HTML requests redirect to `/login` with sanitized `next`
  - successful login redirects to the sanitized internal `next` value when present and otherwise falls back to `post_login_redirect_path(user)`
  - authenticated wrong-role browser requests return `403`
  - malformed password hashes do not produce login `500`
- HTMX tests:
  - normal GET `/ops` and `/ops/board` return full templates
  - HTMX GETs return only `ops_ticket_rows.html` or `ops_board_columns.html`
  - filter responses update URL semantics and do not touch `ticket_views`
- Worker/tests:
  - validation matrix for every `recommended_next_action`
  - unsafe `auto_public_reply` rejection for `bug`, `feature`, `unknown`, and `data_ops`
  - Codex command tests assert no full prompt argv tail while preserving `prompt.txt`, `final.json`, images, schema, and required flags
- Bootstrap/docs tests:
  - cwd-independent `create_app()` / `run_web.py --check`
  - `ensure_system_state_defaults()` exercised from bootstrap and worker startup
  - `.env.example` and `README.md` contain the documented contract
  - smoke checks pass after deterministic bootstrap sequence

## Milestones
- M1 Foundations complete: path resolution, malformed-hash handling, Codex prompt transport.
- M2 Browser auth complete: preauth login CSRF, sanitized browser redirects, wrong-role `403`.
- M3 Ops HTMX complete: vendored HTMX, fragment responses, stable targets, no read-tracking drift.
- M4 Worker safety complete: stricter triage validation without changing the two-clarification override.
- M5 Bootstrap/docs complete: system-state defaults invoked deterministically, admin bootstrap idempotent, docs/env aligned.
- M6 Verification complete: targeted tests cover each requested gap and preserve current boundaries.

## Risk Register
- Risk: redirect logic accidentally changes existing API/health semantics.
  - Control: isolate browser-only guards and keep existing dependency behavior for non-browser callers.
- Risk: `next` handling is sanitized on entry but ignored after login, breaking return-to-page UX or reintroducing unsafe redirect behavior.
  - Control: define one shared sanitizer/consumer path for login GET, unauthenticated browser redirects, and successful login fallback behavior, then lock it with route tests.
- Risk: login CSRF flow creates stale-cookie loops or mixes preauth/auth cookies.
  - Control: separate cookie names and invalidation paths, rotate preauth on failed login, clear preauth after successful auth-session creation.
- Risk: HTMX partial rendering silently changes template shells or read-tracking behavior.
  - Control: reuse existing partial templates, add focused fragment tests, and keep `upsert_ticket_view()` calls detail-only.
- Risk: Codex stdin support varies by CLI version.
  - Control: plan for preferred stdin transport with short fixed fallback instruction pointing to `prompt.txt`; do not reintroduce large argv prompt payloads.
- Risk: system-state default seeding becomes order-dependent.
  - Control: call the existing idempotent helper from bootstrap and worker startup, and verify the resulting rows in tests.

## Rollback Notes
- Revert browser-only auth helpers and login preauth wiring together if redirect/CSRF regressions appear; do not partially retain one without the other.
- Revert `next`-aware login redirects together with the sanitizer and route tests if they prove unstable; do not keep a `/login?next=...` producer without the matching success-path consumer.
- Revert HTMX behavior by keeping full-page GET rendering intact and removing fragment-only branches.
- Revert Codex prompt transport only as a full unit with its command tests if the target CLI cannot support stdin/fixed-instruction usage.
