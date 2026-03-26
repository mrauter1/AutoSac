# Role and user-management expansion plan

## Scope
- Preserve and verify ticket-opening access for `admin` and `dev_ti` through the existing requester ticket flow at `/app/tickets/new` and `POST /app/tickets`.
- Reuse the existing user-management surface at `/ops/users` and `POST /ops/users/create`; do not add a second admin page or new role types.
- Enforce the requested creation matrix exactly: `admin` can create `requester` and `dev_ti`; `dev_ti` can create `requester` only; `requester` cannot access user management.
- Add route-level and permission-focused tests so the behavior is locked in and future refactors do not silently regress it.

## Implementation milestone
1. Harden and validate the existing role gates and user-management page.
   - Keep route names, login behavior, and template locations stable unless tests expose a real mismatch with intent.
   - Tighten only the minimum code needed if current behavior differs from the requested matrix or if error handling loses page context.
   - Prefer existing helpers (`require_requester_user`, `require_ops_user`, `_allowed_new_user_roles`) over new abstractions.

## Interfaces
- `GET /app/tickets/new`
  Used by `requester`, `dev_ti`, and `admin` to open a new ticket via the existing requester-facing form.
- `POST /app/tickets`
  Must continue to create a ticket owned by the authenticated user without introducing new role-specific branching.
- `GET /ops/users`
  Must render for `admin` and `dev_ti`; must reject `requester`.
- `POST /ops/users/create`
  Must enforce allowed target roles from the authenticated actor and redisplay the page with context on validation errors.
- `shared.permissions`
  Remains the authority for role classification; no new role constants or schema changes.

## Compatibility notes
- No database migration is expected. Existing roles (`requester`, `dev_ti`, `admin`) and current paths remain unchanged.
- Keep the current ops navigation and login redirect behavior. Ops users may continue landing on `/ops` while still being able to open tickets through the existing `/app` routes.
- Preserve the current `/ops/users` page instead of introducing a duplicate route or alternate UI surface.

## Regression risks and controls
- Risk: changing requester-route guards could block `admin` or `dev_ti` from opening tickets.
  Control: add explicit GET/POST tests for both roles on the requester ticket creation flow.
- Risk: broadening ops access could accidentally expose `/ops/users` to `requester`.
  Control: add denial tests for requester access on both the page and create action.
- Risk: user-creation permissions could drift and let `dev_ti` create `dev_ti` or `admin`.
  Control: add POST tests that assert the exact allowed-role matrix and 403 on forbidden role submissions.
- Risk: validation failures on user creation could stop rendering the current user list or allowed role options.
  Control: cover duplicate/invalid create failures and assert the template still renders contextual data.

## Validation
- Extend web-route tests around auth, requester, and ops flows to cover:
  - `admin` and `dev_ti` access to new-ticket page and submission path.
  - `admin` and `dev_ti` access to `/ops/users`.
  - `admin` can create `dev_ti` and `requester`.
  - `dev_ti` can create `requester` and is denied for `dev_ti` or `admin`.
  - `requester` is denied from user-management routes.
- Run the targeted pytest modules covering requester/auth and ops workflows after implementation.

## Rollback
- Revert only the role-access and `/ops/users` changes if tests or manual verification show regression in existing requester or ops flows.
- Do not roll back unrelated auth, ticketing, or migration files.
