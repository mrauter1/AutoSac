# Plan ↔ Plan Verifier Feedback

- Added a single-phase implementation plan because both regressions are handled at the same browser-auth exception boundary; captured the exact invariants to keep API/non-browser behavior unchanged while adding focused auth/ops regression tests.
- PLAN-001 non-blocking: No blocking findings. Verified that the plan covers HTMX-aware browser redirects, browser-only wrong-role 403 rendering, the new `403.html` template, focused auth/ops test updates, and the invariant that API/non-browser `HTTPException` behavior remains unchanged.
