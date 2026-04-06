# Implement ↔ Code Reviewer Feedback

- Task ID: route-target-registry-impl
- Pair: implement
- Phase ID: runtime-routing-ops-cutover
- Phase Directory Key: runtime-routing-ops-cutover
- Phase Title: Runtime Routing, Policy, and Ops Cutover
- Scope: phase-local authoritative verifier artifact

## Findings

- IMP-001 | blocking | [worker/triage.py](/home/marcelo/code/AutoSac/worker/triage.py#L178) and [worker/triage.py](/home/marcelo/code/AutoSac/worker/triage.py#L298): `resolve_effective_publication_mode()` can downgrade a specialist result to `manual_only`, but `_resolve_specialist_outcome()` still carries the specialist's public reply and `_apply_success_result()` drafts whenever that string is non-empty. A route target with `allow_draft_for_human=false` and `allow_manual_only=true` will therefore create a public draft and record `draft_public_reply` instead of taking the manual-only path. Gate both the emitted `public_reply_markdown` and `last_ai_action` off `decision.effective_mode`, and add regression coverage for draft-disabled direct-AI and human-assist targets.
- IMP-002 | blocking | [worker/step_runner.py](/home/marcelo/code/AutoSac/worker/step_runner.py#L381): run-manifest route-target metadata prefers `ticket.route_target_id` over the current run's router step output. On reruns, or when a current run fails before `_apply_success_result()` updates the ticket row, `run_manifest.json` can advertise the previous route target instead of the one selected by the active run. Prefer the latest router-step output for the current run and fall back to the ticket field only when no router output exists; add a manifest regression test for rerun/failure snapshots.

## Re-review

- No remaining findings. IMP-001 is resolved in [worker/triage.py](/home/marcelo/code/AutoSac/worker/triage.py#L178) with explicit `manual_only` side-effect suppression and regression coverage in [tests/test_ai_worker.py](/home/marcelo/code/AutoSac/tests/test_ai_worker.py#L914).
- No remaining findings. IMP-002 is resolved in [worker/step_runner.py](/home/marcelo/code/AutoSac/worker/step_runner.py#L381) by preferring the current run's router output, with regression coverage in [tests/test_ai_worker.py](/home/marcelo/code/AutoSac/tests/test_ai_worker.py#L1309).
