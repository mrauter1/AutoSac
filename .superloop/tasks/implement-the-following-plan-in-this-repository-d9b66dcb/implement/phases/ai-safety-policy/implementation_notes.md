# Implementation Notes

- Task ID: implement-the-following-plan-in-this-repository-d9b66dcb
- Pair: implement
- Phase ID: ai-safety-policy
- Phase Directory Key: ai-safety-policy
- Phase Title: AI Action Validation
- Scope: phase-local producer artifact

## Files Changed
- `worker/triage.py`
- `tests/test_ai_worker.py`
- `.superloop/tasks/implement-the-following-plan-in-this-repository-d9b66dcb/decisions.txt`

## Symbols Touched
- `worker.triage.validate_triage_result`
- `worker.triage._effective_next_action` coverage via tests
- `tests.test_ai_worker._valid_payload`

## Checklist Mapping
- Plan D1 / Phase AC-1: restricted `auto_public_reply` to `support` and `access_config`, while still requiring permission flag, evidence, reply text, and confidence threshold.
- Plan D1 / Phase AC-2: enforced non-clarification `needs_clarification=false`, rejected non-empty `clarifying_questions` outside `ask_clarification`, preserved draft/confirm/route reply requirements, and kept `route_dev_ti` compatible with an empty public reply.
- Plan D1 / Phase AC-3: added coverage proving the two-clarification-round override still forces `route_dev_ti`.
- Plan G: expanded the worker validation matrix tests in `tests/test_ai_worker.py`.

## Assumptions
- `data_ops` is not Stage 1-approved for `auto_public_reply`; no approval flag exists in the current contract, so it remains rejected.
- `clarifying_questions` are contradictory metadata for non-clarification actions and should fail validation even when other fields are otherwise valid.

## Preserved Invariants
- No changes to AI run preparation, execution, publication ordering, stale-run handling, or failure routing.
- `_effective_next_action()` retains the existing after-two-clarification-rounds override.
- `route_dev_ti` still allows an empty `public_reply_markdown`.

## Intended Behavior Changes
- Validation now rejects unsafe auto-public replies for `data_ops`, `bug`, `feature`, and `unknown`.
- Validation now rejects contradictory combinations where a non-clarification action still advertises clarification state/questions.

## Known Non-Changes
- No new worker actions, ticket classes, routing states, or schema fields.
- No changes to prompt transport, fingerprinting, or publication side effects.

## Expected Side Effects
- Invalid Codex outputs that previously slipped through as contradictory combinations now fail fast and route through the existing worker failure path.

## Validation Performed
- `pytest tests/test_ai_worker.py -q`

## Deduplication / Centralization
- Kept the stricter matrix localized in `validate_triage_result()` instead of spreading action guards across later publication code paths.
