# Test Strategy

- Task ID: re-run-review-and-implement-only-correct-applica-dc56c98a
- Pair: test
- Phase ID: auth-worker-bootstrap-fixes
- Phase Directory Key: auth-worker-bootstrap-fixes
- Phase Title: Auth challenge, triage allowlist, and bootstrap defaults
- Scope: phase-local producer artifact

## Behavior To Coverage Map
- AC-1 login preauth challenge rejection and reissue:
  `tests/test_auth_requester.py::test_login_post_rejects_missing_or_invalid_preauth_csrf_before_auth`
  Covers mismatched token and missing preauth state returning `403`, reissued preauth cookie, fresh hidden CSRF token, and no auth lookup on challenge failure.
- AC-2 failed credential rotation with preserved success behavior:
  `tests/test_auth_requester.py::test_login_post_failed_credentials_rotates_preauth_challenge`
  `tests/test_auth_requester.py::test_login_route_sets_remember_me_cookie`
  Covers failed-login `400` response with rotated cookie/token, preserved `next_path`, no session start on invalid credentials, and unchanged successful login remember-me/preauth cleanup behavior.
- AC-3 worker automatic public-action allowlist:
  `tests/test_ai_worker.py::test_validate_triage_result_rejects_bug_auto_public_action`
  `tests/test_ai_worker.py::test_validate_triage_result_allows_access_config_auto_confirm`
  `tests/test_ai_worker.py::test_validate_triage_result_rejects_unknown_auto_confirm`
  Covers unsupported class rejection, allowed `access_config` automatic action, and preserved unknown-ticket failure path.
- AC-4 bootstrap script shared default seeding:
  `tests/test_foundation_persistence.py::test_bootstrap_workspace_script_seeds_system_state_defaults`
  `tests/test_hardening_validation.py::test_bootstrap_web_and_worker_scripts_validate_end_to_end`
  Covers direct use of `ensure_system_state_defaults(..., WORKSPACE_BOOTSTRAP_VERSION)` and end-to-end creation of `system_state.bootstrap_version` plus `worker_heartbeat`.
- AC-5 focused regression validation:
  `pytest tests/test_auth_requester.py tests/test_ai_worker.py tests/test_foundation_persistence.py tests/test_hardening_validation.py`

## Preserved Invariants Checked
- Successful login still redirects, honors remember-me, and clears the preauth cookie.
- Challenge failures still re-render the login page rather than authenticating.
- Worker non-automatic-action validation remains unchanged outside the requested allowlist narrowing.
- Bootstrap script still emits the workspace snapshot and remains compatible with the existing smoke-check flow.

## Edge Cases And Failure Paths
- Missing preauth cookie state on `POST /login`
- Mismatched login CSRF token on `POST /login`
- Invalid credentials with valid preauth challenge
- Unsupported `bug` class attempting `auto_confirm_and_route`
- `unknown` class preserved automatic-action rejection
- SQLite-backed bootstrap test path with only the required `system_state` table present

## Flake Risk / Stabilization
- No timing or network dependencies were introduced.
- Script tests use temp directories and a local SQLite DB for deterministic state.
- Auth and worker tests use fake stores / monkeypatching to avoid external services and nondeterministic DB behavior.

## Known Gaps
- No new broad auth/session or migration coverage was added because those areas are out of scope for this phase.
