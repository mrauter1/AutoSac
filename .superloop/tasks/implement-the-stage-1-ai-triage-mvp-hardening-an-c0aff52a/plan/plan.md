# Stage 1 AI Triage MVP Hardening And PRD Alignment Plan

## Scope And Guardrails
- Preserve the existing FastAPI + Jinja2 + server-side session + worker architecture.
- Keep current ticket workflow, view-tracking semantics, and Codex `final.json` output contract unless the request explicitly changes them.
- Prefer small local changes in existing modules; add only the minimum new persistence needed for server-side preauth login CSRF.
- Out of scope: SPA behavior, drag-and-drop board work, non-local HTMX/CDN assets, interactive Codex login, signed-cookie auth sessions, or changes to ticket state definitions.

## Milestones

### 1. Web/Auth/UI Hardening
- Make static and template roots module-relative so app startup is independent of the process working directory.
- Add browser-aware auth handling:
  - unauthenticated browser navigation to protected HTML routes redirects to `/login` with a sanitized relative `next` path
  - authenticated users with the wrong role continue to receive `403`
  - unsafe, absolute, empty, or recursive `next` targets are ignored
- Add server-side preauth login CSRF for `GET /login` and `POST /login` without changing authenticated session behavior.
  - use a dedicated additive preauth-login store keyed by a short-lived opaque cookie distinct from the authenticated session cookie
  - store `token_hash`, `csrf_token`, sanitized `next_path`, `created_at`, `expires_at`, and optional request metadata already used elsewhere (`user_agent`, `ip_address`)
  - keep the browser token opaque-only, `HttpOnly`, `SameSite=Lax`, `Secure` when HTTPS, and scoped to `/login`
  - expire preauth state aggressively (minutes, not hours/days), invalidate it on successful login, and treat stale/missing state as a safe login-form failure that issues a fresh challenge
- Vendor a local HTMX asset and wire `/ops` and `/ops/board` filters to request fragment responses while preserving full-page SSR fallback.
- Keep `ticket_views` behavior unchanged: list pages, board pages, and HTMX filter refreshes never mark tickets read.

### 2. Worker/Bootstrap/Validation Hardening
- Harden password verification so malformed or unsupported hashes fail closed without raising login-time 500s.
- Change Codex prompt transport so the full prompt is still written to run artifacts but is not passed as a raw argv payload.
- Strengthen `validate_triage_result()` cross-field checks so unsupported or contradictory action combinations fail deterministically before publication.
- Initialize `system_state` defaults, including `bootstrap_version`, during bootstrap and worker startup before heartbeat/run processing.
- Make workspace bootstrap/admin setup deterministic and idempotent, with explicit handling for an already-existing admin account.

### 3. Docs And Regression Coverage
- Complete `.env.example` so all required runtime knobs and defaults are present and aligned with the current `Settings` contract.
- Rewrite `README.md` around the actual Stage 1 product, deterministic bootstrap sequence, smoke checks, and local CLI workflow.
- Add or update targeted tests for auth redirects, login CSRF, safe `next`, malformed password hashes, HTMX partial responses, module-relative paths, prompt transport, validation matrix, system-state defaults, and bootstrap/admin idempotency.

## Interfaces And File-Level Plan

### Auth And Browser Flow
- `app/auth.py`
  - keep authenticated session creation/deletion semantics intact
  - add browser-facing helpers for redirect-vs-HTTP-error decisions
- `app/routes_auth.py`
  - `GET /login`: create/load a preauth server record, expose CSRF + sanitized `next`
  - `POST /login`: validate preauth CSRF, authenticate, then redirect to safe `next` or role default
  - on stale or invalid preauth state, re-render the login form with a fresh preauth challenge instead of authenticating
- `app/ui.py`
  - centralize module-relative template/static roots and safe-`next` helpers used by auth/routes/templates
- `app/main.py`
  - mount static assets from a module-relative path
  - add browser-aware unauthorized handling if that is the smallest way to preserve existing dependency wiring
- Persistence
  - add the smallest dedicated preauth-login store for anonymous login CSRF because `sessions.user_id` is non-null and signed-cookie fallback is out of contract
  - record contract: opaque cookie in the browser, hashed token plus CSRF and sanitized next-path server-side, short TTL, success-time invalidation, and opportunistic expiry cleanup during login reads/writes
  - migration must be additive and must not alter the authenticated `sessions` schema or cookie name

### Ops HTMX Filtering
- `app/templates/base.html`
  - load the vendored local HTMX asset
- `app/templates/ops_filters.html`
  - add page-specific HTMX attributes so list and board filters submit to their existing routes and target only the result container
- `app/routes_ops.py`
  - `/ops`: return `ops_ticket_rows.html` on HTMX requests, full page otherwise
  - `/ops/board`: keep fragment behavior via `ops_board_columns.html`
  - preserve current filter parsing and view-tracking behavior

### Worker And Bootstrap
- `worker/codex_runner.py`
  - keep `prompt.txt`, `schema.json`, `final.json`, stdout/stderr artifacts
  - update the execute path so prompt content is transported via stdin or file-based input rather than a full argv argument
- `worker/triage.py`
  - tighten action validation without changing valid Stage 1 action meanings
- `shared/security.py`
  - treat invalid hashes as authentication failures, not exceptions
- `shared/ticketing.py`
  - reuse `ensure_system_state_defaults()` from bootstrap/worker startup instead of duplicating bootstrap-version writes
- `scripts/bootstrap_workspace.py`, `scripts/run_worker.py`, `worker/main.py`, `scripts/create_admin.py`
  - enforce deterministic bootstrap ordering and idempotent admin creation behavior

## Compatibility, Migration, Rollout, Rollback
- Compatibility
  - non-HTMX browser behavior remains full-page SSR
  - authenticated session cookie contract remains opaque-token-only and unchanged
  - the new preauth login cookie is separate, short-lived, and limited to the `/login` flow only
  - Codex `final.json` remains the canonical parsed result; JSONL remains diagnostics only
- Migration
  - add only additive persistence for preauth login CSRF
  - initialize missing `system_state` keys lazily and idempotently so existing databases do not require manual backfill
- Rollout
  - land additive preauth-login persistence together with auth/browser hardening before enabling HTMX filters so redirect and CSRF behavior are stable first
  - verify bootstrap flow from a clean temp workspace/database and from an already-bootstrapped workspace/database
- Rollback
  - auth/UI rollback is safe if preauth-login reads/writes are feature-local and additive and stale preauth rows are harmless after code rollback
  - HTMX rollback is limited to removing fragment behavior while keeping the same routes and templates
  - worker rollback is safe if prompt artifacts and final-output parsing remain backward-compatible

## Regression Surfaces And Invariants
- Unauthenticated browser requests must go to `/login`; authenticated wrong-role requests must not silently redirect across roles.
- Safe `next` handling must never allow external URLs, protocol-relative URLs, or login-loop recursion.
- Preauth login state must remain opaque in the browser, server-expiring, and one-flow scoped; expired or missing preauth state must fail closed and issue a fresh challenge.
- List/board/filter refreshes must never call `upsert_ticket_view()`.
- Ticket detail GETs and ticket-mutating POSTs must keep the existing view-update behavior.
- `verify_password()` must never raise for bad stored data.
- `validate_triage_result()` must reject contradictory payloads before any ticket publication side effects.
- Bootstrap and worker startup must be repeatable without duplicate `system_state` rows or duplicate admin creation.

## Validation Plan
- Extend auth route tests for:
  - redirect-to-login with preserved safe `next`
  - authenticated login-page redirect behavior
  - wrong-role `403`
  - preauth login CSRF success/failure paths
  - malformed password-hash rejection
- Extend ops tests for:
  - HTMX list fragment vs full-page response
  - HTMX board fragment vs full-page response
  - unchanged view-tracking semantics under HTMX filter refresh
- Extend worker/bootstrap tests for:
  - prompt artifact creation and non-argv transport
  - expanded triage validation matrix
  - system-state default initialization on bootstrap and worker startup
  - idempotent admin/bootstrap flow and README/.env contract assertions

## Risk Register
- Open redirect or redirect loop risk.
  - Mitigation: one shared safe-path helper, explicit `/login` recursion guard, direct tests for external and malformed targets.
- HTMX fragment drift risk.
  - Mitigation: keep route-local partial selection, reuse existing templates, add fragment/full-page tests on both `/ops` and `/ops/board`.
- Over-tight triage validation risk.
  - Mitigation: codify only PRD-supported contradictions, add positive tests for each valid action and negative tests for invalid combinations.
- Codex CLI transport compatibility risk.
  - Mitigation: preserve existing run artifacts, document the transport contract, and test the command/execution boundary instead of only string contents.
- Bootstrap idempotency risk.
  - Mitigation: reuse one default-initialization path, explicitly define “create if missing / no-op if matching / fail if conflicting” behavior for the admin bootstrap step.
