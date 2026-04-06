# Test Author ↔ Test Auditor Feedback

- Task ID: route-target-registry-impl
- Pair: test
- Phase ID: registry-contract-foundation
- Phase Directory Key: registry-contract-foundation
- Phase Title: Registry and Contract Foundation
- Scope: phase-local authoritative verifier artifact

- Added targeted AC-1 coverage in `tests/test_routing_registry.py` for missing `selector_spec_id` on auto-selection registries, invalid publish-policy confidence values, and forbidden human-assist auto-publish configuration. Updated `test_strategy.md` with the explicit behavior-to-test map and kept the suite deterministic via registry temp-file mutations and focused pytest reruns.
- Added AC-2 regression guards in `tests/test_routing_registry.py` and `tests/test_ai_worker.py` so rendered router, selector, and specialist prompts now assert the absence of legacy class placeholders and ticket-class routing phrasing while still allowing the intentional compatibility-phase `ticket_class` schema note. Updated `test_strategy.md` to record that exact negative-assertion scope and the flake-avoidance rationale.
- TST-001 | blocking | `tests/test_routing_registry.py` prompt-rendering coverage still only asserts the presence of the new registry-driven catalog and route-target fields. AC-2 also requires router, selector, and specialist prompts to stay free of hardcoded business taxonomy and legacy class placeholders, but no current test fails if legacy prompt text such as `TARGET_TICKET_CLASS`, `ROUTER_TICKET_CLASS`, or equivalent ticket-class routing instructions is reintroduced alongside the new placeholders. That leaves a material silent-regression path in one of the main behaviors this phase changed. Minimal fix: extend the existing router/selector/specialist prompt tests to assert that rendered prompts do not contain legacy class placeholders or hardcoded ticket-class routing text while still asserting the new registry-driven content.
