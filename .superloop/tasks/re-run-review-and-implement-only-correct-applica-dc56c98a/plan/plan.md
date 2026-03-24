# Plan

## Scope
- Re-run the prior feedback fixes and implement only the items that are correct and applicable.
- Keep behavior stable outside the requested auth, worker-validation, and bootstrap-default surfaces.

## Milestone
### Single slice: auth hardening, worker allowlist enforcement, and bootstrap default seeding
- Update `app/routes_auth.py` so missing or mismatched preauth login challenge state is treated as forbidden (`403`) rather than bad request (`400`).
- Rotate the preauth login challenge on failed credential attempts so the response re-renders with a fresh CSRF token and cookie instead of reusing the previous challenge.
- Update `worker/triage.py` validation so automatic public actions remain limited to `support` and `access_config` ticket classes unless a future explicit allowlist expansion is introduced.
- Update `scripts/bootstrap_workspace.py` to seed `system_state` defaults, including `bootstrap_version`, through the existing shared default initializer before the script exits.
- Extend focused tests for auth route status/cookie rotation, worker triage allowlist enforcement, and bootstrap script/system-state initialization.

## Interfaces And Invariants
- `POST /login`
  - Missing/expired preauth challenge returns a reissued login page with HTTP `403`.
  - Mismatched login CSRF returns a reissued login page with HTTP `403`.
  - Invalid credentials return the login page with HTTP `400`, but the preauth cookie and hidden CSRF token must be rotated.
  - Successful login behavior, redirect target handling, remember-me cookie handling, and preauth cleanup remain unchanged.
- Worker triage result validation
  - `auto_public_reply` and `auto_confirm_and_route` must be rejected unless `ticket_class` is `support` or `access_config`.
  - Existing confidence, evidence, clarification, and `unknown` safeguards remain enforced.
- Workspace bootstrap script
  - `scripts/bootstrap_workspace.py` must continue to bootstrap files and print the workspace snapshot.
  - The script must also initialize missing `system_state` defaults via the shared helper so `worker_heartbeat` and `bootstrap_version` exist after bootstrap.

## Compatibility Notes
- Intentional behavior change: invalid or missing login challenge state becomes `403 Forbidden` instead of `400 Bad Request`.
- No route shapes, cookie names, config keys, schema contracts, or bootstrap JSON fields should change.
- `system_state` seeding is additive and idempotent; existing rows must not be overwritten unless current shared behavior already does so.

## Regression Risks
- Login challenge rotation must not break successful login, preserved safe `next` redirects, or remember-me cookie issuance.
- Reissuing the login challenge on invalid credentials must not reuse stale preauth CSRF values or leave the previous preauth cookie active.
- Worker allowlist enforcement must only narrow automatic public actions for unsupported classes and must not block supported `support` / `access_config` flows.
- Bootstrap script seeding must reuse `ensure_system_state_defaults` to avoid drift from worker/admin initialization behavior.

## Validation
- Update auth route tests to assert:
  - invalid and missing preauth challenge failures now return `403`;
  - invalid credentials mint a new preauth cookie and CSRF token;
  - successful login still clears the preauth cookie and preserves remember-me behavior.
- Update worker tests to assert automatic public actions are rejected for classes outside `support` and `access_config`, while supported classes still validate.
- Update bootstrap/persistence or hardening tests to assert `scripts/bootstrap_workspace.py` initializes `system_state` defaults including `bootstrap_version`.
- Run the targeted test files covering auth, worker, and bootstrap behavior.

## Rollback
- Revert the login route status/rotation changes if they break browser login flow or cookie lifecycle expectations.
- Revert the triage allowlist check if it blocks an already-explicitly-supported automatic public class; no such expansion is currently authorized.
- Revert the bootstrap script DB seeding only if it introduces startup coupling beyond the existing script/database contract.
