# Test Strategy

- Task ID: slack-implementation-refactor
- Pair: test
- Phase ID: admin-ui-and-user-mapping
- Phase Directory Key: admin-ui-and-user-mapping
- Phase Title: Admin UI and User Slack Mapping
- Scope: phase-local producer artifact

## Behavior-to-test coverage map

- `AC-1 Slack admin page load/save/disconnect`: covered in `tests/test_ops_workflow.py` for admin load metadata/guidance, new-token `auth.test` success, blank-token preservation, save error token non-echo, and disconnect clearing behavior.
- `AC-1 locale-aware Slack admin errors`: covered in `tests/test_ui_i18n.py` for Slack admin error language-switch path preservation, Portuguese route rendering on invalid numeric settings, and direct translation coverage for all five Slack tuning validation messages.
- `AC-2 admin-only Slack ID management`: covered in `tests/test_ops_workflow.py` for create/update role matrices, explicit non-admin forged `slack_user_id` submission rejection on create/update, and preserved page context on validation failures.
- `AC-2 Slack ID normalization and uniqueness`: covered in `tests/test_foundation_persistence.py` for trim, whitespace-only rejection, duplicate rejection, and blank-input clear behavior in `shared.user_admin.py`.
- `AC-3 admin-only route and UI affordances`: covered in `tests/test_auth_requester.py` and `tests/test_ops_workflow.py` for `require_admin_user`, admin-only Slack route access, template/nav presence, and ops-users admin-vs-non-admin Slack column or input visibility.

## Preserved invariants checked

- Slack bot tokens are never echoed back into HTML after save failures.
- Blank token edits preserve an existing stored token.
- Non-admin ops users cannot mutate `slack_user_id` even via handcrafted form posts.
- Locale-switch links on error pages stay anchored to the Slack integration GET path.

## Edge cases and failure paths

- Invalid Slack `auth.test` responses.
- Invalid numeric Slack tuning values.
- Missing token while enabling Slack.
- Duplicate, blank, or whitespace-only Slack user IDs.
- Non-admin submission of forbidden Slack ID fields.

## Reliability / stabilization

- All Slack HTTP behavior is monkeypatched; no live network calls occur.
- Route tests use deterministic in-memory doubles for DB/session state.
- Translation regression coverage uses direct `translate_error_text()` assertions for exact error strings, avoiding template noise when the goal is i18n mapping completeness.

## Known gaps

- This phase does not test recipient routing or worker DM delivery behavior; those remain deferred by phase scope.
