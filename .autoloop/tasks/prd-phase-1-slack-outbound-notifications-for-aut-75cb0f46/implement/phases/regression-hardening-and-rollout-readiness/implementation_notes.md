# Implementation Notes

- Task ID: prd-phase-1-slack-outbound-notifications-for-aut-75cb0f46
- Pair: implement
- Phase ID: regression-hardening-and-rollout-readiness
- Phase Directory Key: regression-hardening-and-rollout-readiness
- Phase Title: Regression Hardening and Rollout Readiness
- Scope: phase-local producer artifact

## Files changed
- `README.md`
- `docs_deployment.md`
- `tests/test_hardening_validation.py`

## Symbols touched
- `tests.test_hardening_validation._require_python_multipart`
- `tests.test_hardening_validation._load_web_stack`
- `tests.test_hardening_validation.test_slack_docs_capture_phase1_rollout_posture`
- `tests.test_hardening_validation.test_bootstrap_web_and_worker_scripts_validate_end_to_end`
- `tests.test_hardening_validation.test_script_checks_fail_before_workspace_bootstrap`
- `tests.test_hardening_validation.test_script_checks_fail_when_backfill_is_pending`

## Checklist mapping
- Milestone 4 / AC-1: kept the Slack-focused persistence, config, emission, delivery, and migration suites green and made the broader hardening suite deterministic in this runner by skipping full-app health/script checks when `python-multipart` is unavailable.
- Milestone 4 / AC-2: revalidated adjacent Stage 1 requester, ops, and worker invariants with targeted regression tests after the Slack integration hooks landed.
- Milestone 4 / AC-3: finalized operator-facing rollout/rollback notes by documenting the `SLACK_ENABLED=false` rollback posture and the non-backfill contract in `README.md` and `docs_deployment.md`.

## Assumptions
- This runner does not have `python-multipart` installed, so full FastAPI app imports that bind `Form(...)` parameters are environment-sensitive rather than authoritative signals about Slack regressions.

## Preserved invariants
- No Slack persistence, routing, rendering, delivery, or retry logic changed in this phase.
- No requester-visible or Dev/TI-visible ticket workflow behavior changed.
- Existing Stage 1 ticket, AI-run, and worker semantics remain covered by targeted tests outside the environment-sensitive web smoke checks.

## Intended behavior changes
- Hardening tests now skip full-app readiness/script checks when the form-parser dependency is absent instead of failing the Slack regression phase on unrelated environment setup.
- Operator docs now state that disabling Slack is the Phase 1 rollback lever and that later re-enable still does not backfill historical ticket activity.

## Known non-changes
- No application source files under `shared/`, `worker/`, or `app/` changed in this phase.
- No new migration, schema, or runtime config behavior was added.

## Expected side effects
- `tests/test_hardening_validation.py` remains useful in lightweight runners that lack `python-multipart`.
- Operators have explicit rollout and rollback guidance for shipping with `SLACK_ENABLED=false` first and enabling targets later without historical replay.

## Validation performed
- `pytest tests/test_hardening_validation.py tests/test_slack_event_emission.py tests/test_slack_delivery.py tests/test_foundation_persistence.py`
- `pytest tests/test_hardening_validation.py -k 'slack_docs_capture_phase1_rollout_posture or readyz or healthz or bootstrap_web_and_worker_scripts_validate_end_to_end or script_checks_fail'`
- `pytest tests/test_ai_worker.py -k 'heartbeat_loop_emits_while_stop_event_controls_exit or emit_worker_heartbeat_initializes_system_state_defaults or emit_worker_heartbeat_updates_active_run_last_heartbeat'`
- `pytest tests/test_auth_requester.py -k 'create_requester_ticket_creates_initial_records or add_requester_reply_reopens_and_requeues'`
- `pytest tests/test_ops_workflow.py -k 'add_ops_public_reply_records_status_history_and_view or add_ops_internal_note_keeps_status_and_adds_internal_message or publish_ai_draft_for_ops_creates_ai_message_and_status_change'`
- `python3 -m compileall tests/test_hardening_validation.py`

## Deduplication / centralization decisions
- Centralized the environment check in one `_require_python_multipart()` helper so every full-app health/script validation path applies the same skip rule.
