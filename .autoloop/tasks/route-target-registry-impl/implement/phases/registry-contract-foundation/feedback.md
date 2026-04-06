# Implement ↔ Code Reviewer Feedback

- Task ID: route-target-registry-impl
- Pair: implement
- Phase ID: registry-contract-foundation
- Phase Directory Key: registry-contract-foundation
- Phase Title: Registry and Contract Foundation
- Scope: phase-local authoritative verifier artifact

- IMP-001 | blocking | `agent_specs/{support,bug,feature,access-config,data-ops,unknown}/prompt.md` and `agent_specs/*/manifest.json`: the compatibility-phase worker still executes specialist specs with `output_contract="triage_result"`, but the rewritten specialist prompts removed the explicit `ticket_class`/classification guidance that the still-live contract requires. The current runtime therefore depends on the model inferring a required legacy field only from the JSON schema and selected route-target context, which can cause contract-validation failures or router/specialist mismatch downgrades even though this phase explicitly preserves current persistence behavior. Minimal fix: keep a centralized prompt instruction for the compatibility-phase specialist path that any required `ticket_class` field must match the selected route target, or cut specialist execution over to `specialist_result` in the same phase instead of leaving prompts and manifests out of sync.
- IMP-001 follow-up | non-blocking | Verified resolved in producer cycle 2. `worker.prompt_renderer.render_agent_prompt` now injects one centralized compatibility-phase instruction that any still-required `ticket_class` field must equal the selected route target ID, the live specialist prompts render that instruction via `{LEGACY_TICKET_CLASS_ALIGNMENT_RULE}`, the prompt coverage tests assert it, and `.venv/bin/pytest -q tests/test_routing_registry.py tests/test_ai_worker.py tests/test_hardening_validation.py tests/test_foundation_persistence.py` passed with `81 passed`.
