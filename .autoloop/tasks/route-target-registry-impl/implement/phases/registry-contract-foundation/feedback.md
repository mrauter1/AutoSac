# Implement ↔ Code Reviewer Feedback

- Task ID: route-target-registry-impl
- Pair: implement
- Phase ID: registry-contract-foundation
- Phase Directory Key: registry-contract-foundation
- Phase Title: Registry and Contract Foundation
- Scope: phase-local authoritative verifier artifact

- IMP-001 | blocking | `agent_specs/{support,bug,feature,access-config,data-ops,unknown}/prompt.md` and `agent_specs/*/manifest.json`: the compatibility-phase worker still executes specialist specs with `output_contract="triage_result"`, but the rewritten specialist prompts removed the explicit `ticket_class`/classification guidance that the still-live contract requires. The current runtime therefore depends on the model inferring a required legacy field only from the JSON schema and selected route-target context, which can cause contract-validation failures or router/specialist mismatch downgrades even though this phase explicitly preserves current persistence behavior. Minimal fix: keep a centralized prompt instruction for the compatibility-phase specialist path that any required `ticket_class` field must match the selected route target, or cut specialist execution over to `specialist_result` in the same phase instead of leaving prompts and manifests out of sync.
