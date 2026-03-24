# Test Strategy

- Task ID: implement-the-stage-1-ai-triage-mvp-hardening-an-c0aff52a
- Pair: test
- Phase ID: web-auth-ui-hardening
- Phase Directory Key: web-auth-ui-hardening
- Phase Title: Web Auth And UI Hardening
- Scope: phase-local producer artifact

## Behavior → Coverage

- AC-1 browser redirect + safe next:
  `tests/test_auth_requester.py`
  `test_protected_html_get_redirects_to_login_with_safe_next`
  `test_wrong_role_still_returns_403_instead_of_redirect`
  `test_safe_next_path_rejects_external_empty_and_recursive_targets`
- AC-2 login preauth CSRF:
  `tests/test_auth_requester.py`
  `test_login_route_sets_remember_me_cookie`
  `test_login_get_issues_preauth_challenge_with_sanitized_next`
  `test_login_post_rejects_missing_or_invalid_preauth_csrf_before_auth`
- AC-3 additive preauth store contract + migration:
  `tests/test_foundation_persistence.py`
  `test_preauth_login_session_creation_hashes_browser_token_and_sets_short_expiry`
  `test_preauth_login_lookup_and_invalidation_use_hashed_tokens`
  `test_additive_preauth_migration_declares_store_and_expiry_index`
- AC-4 HTMX fragments + preserved view semantics:
  `tests/test_ops_workflow.py`
  `test_ops_list_route_does_not_mark_ticket_as_read`
  `test_ops_board_route_does_not_mark_ticket_as_read`
  `test_ops_detail_route_marks_ticket_as_read`
  `test_ops_routes_source_and_templates_keep_internal_and_public_lanes_separate`
- AC-5 module-relative static/template paths:
  `tests/test_auth_requester.py`
  `test_module_relative_paths_support_rendering_and_static_assets`

## Preserved Invariants Checked

- Wrong-role access remains `403` instead of redirecting across roles.
- Ops list, board, and HTMX filter refreshes do not mark tickets read.
- Ops detail views still mark tickets read.
- Login success still honors remember-me cookie behavior.

## Edge / Failure Paths

- Unsafe or recursive login `next` values are rejected.
- Missing or invalid login preauth state reissues a fresh challenge and blocks authentication.
- Preauth helper tests pin hashed-token behavior, short TTL, and cleanup/invalidation call paths.

## Stability Notes

- Login route tests use an HTTPS `TestClient` base URL so secure-cookie behavior matches the `APP_BASE_URL` contract deterministically.
- Preauth helper tests monkeypatch token/time generators to avoid nondeterministic values.

## Known Gaps

- No full end-to-end database-backed migration test was added in this phase; migration coverage is source-level plus helper-contract unit coverage.
