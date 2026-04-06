# Test Author ↔ Test Auditor Feedback

- Task ID: route-target-registry-impl
- Pair: test
- Phase ID: registry-contract-foundation
- Phase Directory Key: registry-contract-foundation
- Phase Title: Registry and Contract Foundation
- Scope: phase-local authoritative verifier artifact

- Added targeted AC-1 coverage in `tests/test_routing_registry.py` for missing `selector_spec_id` on auto-selection registries, invalid publish-policy confidence values, and forbidden human-assist auto-publish configuration. Updated `test_strategy.md` with the explicit behavior-to-test map and kept the suite deterministic via registry temp-file mutations and focused pytest reruns.
