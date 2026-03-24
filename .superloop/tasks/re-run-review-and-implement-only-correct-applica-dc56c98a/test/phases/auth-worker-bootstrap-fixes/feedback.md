# Test Author ↔ Test Auditor Feedback

- Task ID: re-run-review-and-implement-only-correct-applica-dc56c98a
- Pair: test
- Phase ID: auth-worker-bootstrap-fixes
- Phase Directory Key: auth-worker-bootstrap-fixes
- Phase Title: Auth challenge, triage allowlist, and bootstrap defaults
- Scope: phase-local authoritative verifier artifact

- Added focused regression coverage for `403` login challenge failures, failed-login preauth rotation, worker automatic public-action allowlist enforcement, and bootstrap default seeding. The bootstrap end-to-end script test now also asserts the actual `system_state` rows created by `scripts/bootstrap_workspace.py`. Focused validation passed: `67 passed`.
- TST-001 | non-blocking | Clean pass: the added tests map directly to AC-1 through AC-5, preserve the requested successful-login and unknown-ticket invariants, and use deterministic temp-dir / fake-store setup. Re-ran `pytest tests/test_auth_requester.py tests/test_ai_worker.py tests/test_foundation_persistence.py tests/test_hardening_validation.py` with `67 passed`.
