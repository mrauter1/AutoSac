# Test Strategy

- Task ID: implement-the-stage-1-ai-triage-mvp-hardening-an-c0aff52a
- Pair: test
- Phase ID: worker-bootstrap-hardening
- Phase Directory Key: worker-bootstrap-hardening
- Phase Title: Worker Bootstrap And Validation Hardening
- Scope: phase-local producer artifact
- Behaviors covered:
  malformed or unsupported password hashes fail closed
  Codex prompt transport uses stdin while preserving prompt/schema/final artifacts
  triage validation rejects contradictory clarification/public-route payloads and preserves valid `route_dev_ti`
  worker heartbeat/startup seed missing `system_state` defaults, including `bootstrap_version`, under `autoflush=False`-like semantics
  admin bootstrap is create-if-missing, explicit conflict on mismatched user state, and explicit success on matching existing admin
- Preserved invariants checked:
  `final.json` remains the canonical parsed output
  worker heartbeat still records `status=alive`
  supported Stage 1 action names and happy-path publication flow remain unchanged
- Edge cases / failure paths:
  malformed Argon2 hashes
  `route_dev_ti` with an unexpected public reply
  `unknown` ticket class with automatic public confirmation
  pending `SystemState` rows invisible until flush
  matching admin rerun through `scripts/create_admin.py`
- Stabilization approach:
  use fake DB/session doubles and monkeypatched subprocess/script boundaries instead of real Codex or database processes
  keep worker state tests deterministic by modeling pending-vs-flushed rows explicitly
- Known gaps:
  no end-to-end PostgreSQL-backed bootstrap script integration test; current coverage stays unit-level and deterministic
