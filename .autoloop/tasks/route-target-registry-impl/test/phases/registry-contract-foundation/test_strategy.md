# Test Strategy

- Task ID: route-target-registry-impl
- Pair: test
- Phase ID: registry-contract-foundation
- Phase Directory Key: registry-contract-foundation
- Phase Title: Registry and Contract Foundation
- Scope: phase-local producer artifact

## Behavior-to-test coverage map

- Registry loading and validation:
  duplicate route-target IDs, duplicate specialist IDs, missing specialist spec references, missing selector spec when any target uses `mode=auto`, invalid `fixed`/`auto`/`none` specialist-selection configs, invalid publish-policy values, human-assist auto-publish rejection, disabled specialist misuse, and human-assist auto-candidate expansion.
- Contract validation:
  disabled router target rejection, selector candidate enforcement, specialist required-field validation, and human-handoff assistant metadata validation.
- Prompt rendering:
  router catalog generation from enabled registry targets, selector candidate catalog rendering, specialist route-target context rendering, centralized legacy `ticket_class` alignment instruction, and missing-placeholder failure handling.
- Workspace/bootstrap/readiness:
  workspace contract path verification propagates registry failures, bootstrap version remains `stage1-v3`, and workspace skill verification stays driven by loaded agent specs instead of hardcoded taxonomy.

## Preserved invariants checked

- Compatibility-phase runtime still validates against current legacy specialist persistence expectations while prompt text is registry-driven.
- Disabled route targets remain unavailable for new-run routing and contract validation.
- Selector support remains additive: manifest loading and prompt rendering exist without requiring runtime selector orchestration in this phase.

## Edge cases and failure paths

- Invalid registry topology is exercised through temp-file registry mutations rather than shared global fixture changes.
- Prompt failure coverage uses a minimal fake spec to isolate placeholder-resolution errors deterministically.
- Workspace verification tests use `tmp_path` and monkeypatching to avoid filesystem or service flake.

## Known gaps

- This phase does not add runtime selector orchestration, publication-policy execution, or `route_target_id` persistence tests because those behaviors are explicitly out of scope for `registry-contract-foundation`.
