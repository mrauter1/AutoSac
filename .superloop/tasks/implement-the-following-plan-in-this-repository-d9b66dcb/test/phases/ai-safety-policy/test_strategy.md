# Test Strategy

- Task ID: implement-the-following-plan-in-this-repository-d9b66dcb
- Pair: test
- Phase ID: ai-safety-policy
- Phase Directory Key: ai-safety-policy
- Phase Title: AI Action Validation
- Scope: phase-local producer artifact

## Behavior-To-Test Coverage Map

- AC-1 `auto_public_reply` class gate:
  covered by `test_validate_triage_result_allows_auto_public_reply_only_for_safe_classes`
  and `test_validate_triage_result_rejects_auto_public_reply_for_unsafe_classes`.
- AC-1 `auto_public_reply` required fields:
  covered by `test_validate_triage_result_enforces_auto_reply_threshold`
  and the invalid-matrix cases for `auto_public_reply_allowed=false`, `evidence_found=false`, blank reply, and contradictory clarification metadata.
- AC-2 `ask_clarification` invariants:
  covered by `test_validate_triage_result_allows_ask_clarification_with_reply`
  and invalid-matrix cases for `needs_clarification=false` and blank reply.
- AC-2 `auto_confirm_and_route` invariants:
  covered by `test_validate_triage_result_allows_auto_confirm_and_route_when_threshold_met`
  and invalid-matrix cases for missing permission flag, blank reply, low confidence, and contradictory clarification metadata.
- AC-2 `draft_public_reply` invariants:
  covered by `test_validate_triage_result_allows_draft_public_reply_with_manual_send_only`
  and invalid-matrix cases for `auto_public_reply_allowed=true`, blank reply, and contradictory clarification metadata.
- AC-2 `route_dev_ti` invariants:
  covered by `test_validate_triage_result_allows_route_dev_ti_without_public_reply`
  and invalid-matrix cases for contradictory clarification metadata.
- AC-2 clarifying-question length guard:
  covered by `test_validate_triage_result_rejects_more_than_three_clarifying_questions`.
- AC-3 preserved override:
  covered by `test_effective_next_action_preserves_two_round_clarification_override`.

## Preserved Invariants Checked

- The changed validation remains localized to `validate_triage_result()` and does not require publication or lifecycle integration changes to prove correctness.
- The existing after-two-clarification-rounds override still converts `ask_clarification` into `route_dev_ti`.
- `route_dev_ti` still accepts an empty `public_reply_markdown`.

## Edge Cases And Failure Paths

- Unsafe auto-reply classes: `data_ops`, `bug`, `feature`, `unknown`.
- Contradictory clarification state on non-clarification actions.
- Blank public replies where the chosen action requires one.
- Threshold boundary for `auto_confirm_and_route` at `0.90`.
- Schema-level rejection when more than three clarifying questions are returned.

## Stabilization Approach

- Use direct unit tests with local `Settings` fixtures and synthetic payloads to avoid database, subprocess, or timing dependencies.
- Keep assertions at the validator boundary so failures point to the contract violation instead of downstream worker side effects.

## Known Gaps

- No integration test was added for publication behavior here because this phase intentionally does not change publication ordering or run lifecycle; existing worker tests already cover those adjacent paths separately.
