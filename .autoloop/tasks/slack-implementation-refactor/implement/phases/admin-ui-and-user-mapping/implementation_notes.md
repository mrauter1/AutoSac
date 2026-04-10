# Implementation Notes

- Task ID: slack-implementation-refactor
- Pair: implement
- Phase ID: admin-ui-and-user-mapping
- Phase Directory Key: admin-ui-and-user-mapping
- Phase Title: Admin UI and User Slack Mapping
- Scope: phase-local producer artifact

## Files changed

- `app/auth.py`
- `app/i18n.py`
- `app/routes_ops.py`
- `app/templates/base.html`
- `app/templates/ops_slack_integration.html`
- `app/templates/ops_users.html`
- `app/ui.py`
- `shared/permissions.py`
- `shared/user_admin.py`
- `tests/test_auth_requester.py`
- `tests/test_foundation_persistence.py`
- `tests/test_ops_workflow.py`
- `tests/test_ui_i18n.py`

## Symbols touched

- `app.auth.require_admin_user`
- `app.i18n.translate_error_text`
- `app.routes_ops.ops_slack_integration`
- `app.routes_ops.ops_save_slack_integration`
- `app.routes_ops.ops_disconnect_slack_integration`
- `app.routes_ops._render_slack_integration_page`
- `app.routes_ops._slack_integration_page_extra`
- `app.routes_ops._resolve_requested_slack_user_id`
- `shared.permissions.is_admin_user`
- `shared.user_admin.validate_slack_user_id`
- `shared.user_admin.get_user_by_slack_user_id`
- `shared.user_admin.create_user`
- `shared.user_admin.update_user`

## Checklist mapping

- `AC-1`: added the admin-only `/ops/integrations/slack` GET/save/disconnect routes, Slack integration template, nav entry, `auth.test` validation for new tokens, blank-token preservation, disconnect behavior, no-token-echo error rendering, and localized tuning-validation errors for the Slack settings form.
- `AC-2`: extended `/ops/users` create or update flows and `shared/user_admin.py` with optional `slack_user_id` trim, clear, whitespace-only rejection, uniqueness, and explicit non-admin rejection when the field is manually submitted.
- `AC-3`: surfaced stored-token presence, workspace metadata, updater metadata, runtime config validity, and last-known delivery health on the Slack integration page.

## Assumptions

- The DB-backed Slack DM foundation from the prior phase is already present and authoritative.
- Local route/UI verification in this workspace requires user-local installs of `bleach`, `python-multipart`, and `Pillow`; those were installed to run the focused suites.

## Preserved invariants

- Slack settings remain DB-backed; no `SLACK_*` env fallback was reintroduced.
- The Slack bot token remains write-only in the UI and is never re-rendered after save errors.
- Dev/TI users still retain the existing `/ops/users` capabilities, but they do not gain Slack mapping controls or mutate paths.

## Intended behavior changes

- Admins now have an `/ops/integrations/slack` screen for enablement, token management, notify flags, tuning values, and delivery-health visibility.
- The ops user-management surface now includes admin-only Slack ID inputs and an admin-only Slack nav entry.
- `Admin access required` is now a first-class translated auth error for admin-only routes and route-layer Slack-ID mutation rejections.

## Known non-changes

- No recipient-routing inserts for business events were added in this phase.
- No worker DM send-path or retry-classification logic was changed in this phase.
- No requester-facing UI exposes `slack_user_id`.

## Expected side effects

- Admin-only Slack save errors preserve non-secret submitted values while keeping the bot token input blank.
- Slack settings validation failures for delivery tuning values now localize through the shared UI error translation path instead of falling back to raw English helper messages.
- Admin user pages now show Slack IDs for operators; non-admin ops pages keep the prior table layout without the Slack column or inputs.
- Route-level tests now rely on the newly installed local Python packages listed above when run in this workspace.

## Validation performed

- `python3 -m compileall app shared tests scripts`
- `python3 -m pytest tests/test_ops_workflow.py tests/test_ui_i18n.py tests/test_auth_requester.py tests/test_foundation_persistence.py -q`
- `python3 -m pytest tests/test_ui_i18n.py tests/test_ops_workflow.py -q`

## Deduplication / centralization

- Centralized admin-only Slack page rendering through `_slack_integration_page_extra` and `_render_slack_integration_page`.
- Centralized Slack ID permission handling in `_resolve_requested_slack_user_id` so create and update flows reject non-admin crafted submissions the same way.
- Kept Slack ID normalization and uniqueness in `shared.user_admin.py` so route handlers do not duplicate trim or duplicate-check logic.
- Kept Slack settings validation strings stable in `shared.slack_dm.py` and localized them in `app.i18n.py`, preserving a single UI-facing error translation path instead of branching validation by locale.
