# Slack Implementation Refactor Intent

## Context

AutoSac already has a Phase 1 Slack outbound notification implementation covering:

- soft Slack configuration parsing and validation
- integration event persistence and dedupe behavior
- routing decisions for Slack targets
- asynchronous delivery with retries, stale-lock recovery, and dead-letter handling
- rollout documentation and regression coverage

The implementation is functionally strong, but its internals are not yet as elegant, explicit, and maintainable as they could be. The next step is a refactor-focused design that preserves behavior while improving architectural clarity.

## Objective

Produce a refactor PRD for the Slack implementation that preserves current user-visible and operational behavior while making the internal design cleaner, more explicit, and easier to evolve safely.

## Required Outcomes

The refactor PRD must define a target design that keeps functional equivalence for:

- Slack event types and emission rules
- dedupe behavior for previously persisted integration events
- routing outcomes and suppression behavior
- dedicated worker-thread delivery runtime
- retry scheduling, stale-lock recovery, and dead-letter behavior
- soft invalid-config handling that never blocks startup
- existing rollout posture and regression expectations

## Desired Design Improvements

The PRD should evaluate and, where justified, incorporate these improvements:

1. Replace implicit integration settings access via `Session.info["settings"]` with a more explicit boundary or context object.
2. Replace payload-embedded preserved routing metadata with a more explicit first-class persistence model if that can be done without unacceptable migration or compatibility risk.
3. Replace composite Slack delivery ownership checks based on `locked_by` plus `attempt_count` with a cleaner lease or claim-token design if it materially improves correctness and readability.
4. Refactor the Slack delivery path into a clearer workflow or state-machine shape with explicit delivery outcomes and a single coherent finalization boundary.

## Constraints

- Preserve current external behavior and operational semantics.
- Do not regress concurrency safety, dedupe guarantees, or crash recovery.
- Do not weaken the current Slack hardening and observability posture.
- Prefer explicit domain modeling over hidden coupling or metadata smuggling.
- Keep the design sympathetic to the current AutoSac codebase and deployment model.

## Non-Goals

- Adding new Slack product features or new event types.
- Changing user-facing copy, Slack message content, or business rules.
- Reworking unrelated AI-run worker architecture beyond what is needed for Slack refactor boundaries.
- Performing implementation in this document; this is a design/refactor PRD, not a patch plan only.

## Expected PRD Qualities

The final refined PRD should:

- describe the current pain points clearly
- present the target architecture and design decisions
- explain why the design is more elegant without sacrificing behavior
- specify migration and backward-compatibility strategy
- identify risks, rollback posture, and test strategy
- define implementation phases that can be delivered safely

