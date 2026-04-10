# PRD + ARD — Registry-Driven Route Targets, Specialist Selection, and Human-Assist Execution

Document status: Proposed, standalone, implementation-ready  
Version: 1.0  
Date: 2026-04-06  
Owner: Internal Engineering  
Audience: autonomous implementation agent, technical reviewer, system owner  
Supersedes: [Autosac_Agent_Migration_Roadmap_PRD_ARD.md](/home/marcelo/code/AutoSac/Autosac_Agent_Migration_Roadmap_PRD_ARD.md)

Normative language:
- MUST = mandatory
- MUST NOT = prohibited
- SHOULD = recommended
- MAY = optional

## 1. Executive Summary

AutoSac currently has a partially-modernized AI pipeline:
- prompts are file-backed under `agent_specs/`
- runs have step rows in `ai_run_steps`
- ops reads accepted AI output from `AIRun.final_output_json`

But the routing taxonomy is still hardcoded in multiple places:
- `ticket_class` is hardcoded in database constraints and Python literals
- specialist selection is hardcoded in `shared/agent_specs.py`
- router and specialist prompts still enumerate the class list in prompt text
- human escalation is not modeled as a first-class route target
- specialist output is much larger than necessary for reliable LLM performance

This document defines the next architecture step:

1. Replace `ticket_class` with `route_target_id`.
2. Make one canonical registry file the single source of truth for route targets, specialists, target behavior, and human-assist handling.
3. Remove hardcoded routing taxonomy from Python code, database enum constraints, and prompt text.
4. Keep routing classification in the router only.
5. Reduce specialist structured output to the minimum needed for safe publication policy.
6. Model human escalation as a route target kind in the same registry, optionally with AI assistance.
7. Keep business logic deterministic in Python and keep prompts/editable metadata in files.

The intended result is:
- no hardcoded routing taxonomy
- no specialist/router classification mismatch logic
- a smaller specialist schema that is easier for the model to satisfy
- a cleaner route from router decision to publication behavior

## 2. Verified Current State

The following current-state facts were verified against the codebase before writing this document:

1. Agent specs are loaded from `agent_specs/<spec-id>/` via [shared/agent_specs.py](/home/marcelo/code/AutoSac/shared/agent_specs.py).
2. Exactly one router spec is currently required in [shared/agent_specs.py](/home/marcelo/code/AutoSac/shared/agent_specs.py).
3. Specialist selection is hardcoded in `specialist_agent_map()` and `resolve_specialist_spec_id()` in [shared/agent_specs.py](/home/marcelo/code/AutoSac/shared/agent_specs.py).
4. The router contract is hardcoded as `ticket_class + confidence + routing_rationale` in [worker/output_contracts.py](/home/marcelo/code/AutoSac/worker/output_contracts.py).
5. The specialist contract is hardcoded as `TriageResult` in [worker/output_contracts.py](/home/marcelo/code/AutoSac/worker/output_contracts.py), and still includes `ticket_class`.
6. The pipeline currently forces human review when router `ticket_class` and specialist `ticket_class` disagree in [worker/pipeline.py](/home/marcelo/code/AutoSac/worker/pipeline.py).
7. The ticket taxonomy is duplicated in:
   - [shared/models.py](/home/marcelo/code/AutoSac/shared/models.py) as `TICKET_CLASSES`
   - the database `tickets.ticket_class` check constraint
   - `Literal[...]` definitions in [worker/output_contracts.py](/home/marcelo/code/AutoSac/worker/output_contracts.py)
   - `_OPS_FILTERABLE_CLASSES` in [app/routes_ops.py](/home/marcelo/code/AutoSac/app/routes_ops.py)
   - router and specialist prompt text under `agent_specs/*/prompt.md`
8. Ops filters and ticket presentation still use `ticket_class` in [app/routes_ops.py](/home/marcelo/code/AutoSac/app/routes_ops.py) and the related templates.
9. Workspace bootstrap already syncs all current spec `skill.md` files into the workspace via [shared/workspace.py](/home/marcelo/code/AutoSac/shared/workspace.py).
10. The current AI pipeline uses `ai_runs` and `ai_run_steps`, and the DB is already authoritative for accepted AI output.

Confirmed implications:
- the next architectural bottleneck is routing taxonomy and output-contract shape, not prompt storage
- the current system still has hidden hardcoded classification surfaces
- the specialist contract can be simplified substantially because routing no longer needs to happen twice

## 3. Goals

The implementation MUST achieve all of the following:

1. Make routing taxonomy fully configurable from one canonical registry file.
2. Replace `ticket_class` with `route_target_id` across runtime, persistence, UI, prompts, and tests.
3. Model human escalation as a first-class route target using the same registry as direct AI targets.
4. Keep the router as the only classifier of route targets.
5. Reduce specialist structured output to the minimum needed for:
   - requester-language handling
   - generated reply content
   - internal handoff content
   - confidence-based automation policy
   - risk-based automation policy
6. Remove hardcoded route-target literals and DB enum constraints tied to business taxonomy.
7. Preserve deterministic, auditable worker behavior.
8. Keep KISS and DRY principles explicit:
   - registry as single source of truth
   - no duplicated taxonomy in prompts and code
   - no generic workflow engine

## 4. Non-Goals

The implementation MUST NOT attempt to solve the following:

1. A generic plugin platform.
2. Arbitrary DAG orchestration.
3. Parallel multi-agent execution.
4. Database-authored prompts.
5. User-editable routing rules inside the web UI.
6. Runtime evaluation of arbitrary expressions from the registry.
7. Replacement of the existing `agent_specs/<spec-id>/` folder model.
8. Automatic internet access, repo mutation, or any expansion of Stage 1 AI permissions.

## 5. Core Design Decisions

These decisions are locked for this design:

1. `route_target_id` replaces `ticket_class`.
2. The canonical taxonomy lives in one file: `agent_specs/registry.json`.
3. Router is the only classifier of route targets.
4. Specialists do not emit a competing route target.
5. Human escalation is modeled as a `route_target` kind, not as an after-the-fact downgrade hack.
6. Publication behavior is decided by deterministic code plus route-target policy, not by the model alone.
7. Specialist output uses a small schema with ordered fields and named enums.
8. The registry may choose a fixed specialist, no specialist, or an AI-selected specialist, depending on the route target.
9. If a target needs AI selection among candidate specialists, that selection happens through a dedicated selector step and contract, not by overloading the main router.

## 6. Terminology

### Route target

A workflow destination selected by the router. Examples:
- `support`
- `bug`
- `manual_review`
- `security_review`

A route target is not the same thing as a specialist. A route target is a workflow destination and policy container.

### Specialist

An AI execution unit defined by one `agent_specs/<spec-id>/` folder. A specialist may serve a direct AI target or assist a human-owned target.

### Human-assist target

A route target whose end state is human ownership. The AI may optionally prepare a draft reply and/or internal note, but MUST NOT auto-publish to the requester for such a target.

### Direct-AI target

A route target whose goal is an AI-authored requester response, subject to policy gating.

### Selector step

An optional AI step that chooses one specialist from a candidate set for a route target.

## 7. Product Requirements

### PR-1 Single source of truth

All route targets, target labels, routing descriptions, target kinds, specialist registrations, and specialist-selection rules MUST come from `agent_specs/registry.json`.

### PR-2 No hardcoded taxonomy

The system MUST NOT hardcode route-target IDs in:
- `Literal[...]` output-contract enums
- database check constraints
- ops filter tuples
- router prompt text
- specialist prompt text

### PR-3 Router-only classification

Only the router step MUST classify the ticket into a `route_target_id`. Specialists MUST NOT reclassify.

### PR-4 Human escalation as a route target

Human escalation MUST be represented as one or more route targets in the registry. It MUST be possible to configure whether such a target uses:
- no AI specialist
- a fixed specialist
- an AI-selected specialist from a candidate list
- an AI-selected specialist from all human-assist-eligible specialists

### PR-5 Minimal specialist contract

The specialist structured output MUST only contain the following fields, in this logical order:

1. `requester_language`
2. `public_reply_markdown`
3. `internal_note_markdown`
4. `response_confidence`
5. `risk_level`
6. `risk_reason`
7. `summary_internal`
8. `publish_mode_recommendation`

### PR-6 Deterministic publish gating

The system MUST use deterministic code and route-target policy to decide whether a specialist output is:
- auto-published
- drafted for human review
- held for manual-only handling

### PR-7 Ops visibility

Ops MUST be able to:
- filter by `route_target_id`
- inspect route target labels from the registry
- inspect step-level artifacts
- inspect whether a target was direct AI or human assist

### PR-8 Historical continuity

Historical tickets and runs MUST remain visible after migration. The implementation MAY use a bounded compatibility adapter in the ops presentation layer, keyed by `final_output_contract` or `pipeline_version`, but MUST NOT add new runtime worker fallback paths for legacy schema handling.

### PR-9 Accepted runs always have structured output

Every accepted run (`succeeded` or `human_review`) MUST persist a terminal `AIRun.final_output_json` and `AIRun.final_output_contract`, even when a `human_assist` target completes without running a specialist.

## 8. Target Architecture Overview

The target runtime flow is:

1. Load registry and validate it at startup.
2. Run the main router.
3. Router emits `route_target_id`.
4. Resolve the selected route target from the registry.
5. If the target requires specialist selection by AI, run a selector step.
6. If the target requires a specialist, run the specialist step.
7. Apply deterministic publication policy based on:
   - route-target kind
   - route-target publish policy
   - specialist `publish_mode_recommendation`
   - specialist `response_confidence`
   - specialist `risk_level`
8. Persist final structured output to `AIRun.final_output_json`.
9. Persist `route_target_id` to the `Ticket`.

Important invariant:
- accepted runs MUST always end with a terminal structured output payload
- there is no readiness or ops exception for `human_assist + specialist_selection.mode=none`

There are only three step kinds in the target system:
- `router`
- `selector`
- `specialist`

There are only two route-target kinds:
- `direct_ai`
- `human_assist`

This closed set keeps the orchestration simple.

## 9. Canonical Registry

### 9.1 File location

The canonical registry file MUST be:

`agent_specs/registry.json`

No second registry file is allowed.

### 9.2 Registry responsibilities

The registry is the source of truth for:
- route target definitions
- route target labels
- route target descriptions used by the router
- route target kind
- target visibility in ops
- target publication policy
- target specialist-selection behavior
- specialist registrations
- specialist display names
- specialist human-assist eligibility
- IDs of the router and selector specs

The registry MUST NOT contain executable code or arbitrary expressions.

### 9.3 Required top-level structure

The registry MUST contain exactly these top-level keys:

- `version`
- `router_spec_id`
- `selector_spec_id`
- `route_targets`
- `specialists`

`selector_spec_id` MAY be `null` only when no route target uses AI specialist selection.

### 9.4 Registry JSON shape

The required JSON shape is:

```json
{
  "version": 1,
  "router_spec_id": "router",
  "selector_spec_id": "specialist-selector",
  "route_targets": [
    {
      "id": "support",
      "label": "Support",
      "kind": "direct_ai",
      "enabled": true,
      "ops_visible": true,
      "router_description": "How-to help, usage help, low-risk troubleshooting, and straightforward product guidance.",
      "handler": {
        "specialist_selection": {
          "mode": "fixed",
          "specialist_id": "support"
        }
      },
      "publish_policy": {
        "allow_auto_publish": true,
        "min_response_confidence_for_auto_publish": "high",
        "max_risk_level_for_auto_publish": "low",
        "allow_draft_for_human": true,
        "allow_manual_only": true
      }
    },
    {
      "id": "manual_review",
      "label": "Manual Review",
      "kind": "human_assist",
      "enabled": true,
      "ops_visible": true,
      "router_description": "Use when the case is ambiguous, high risk, or better handled by a human operator.",
      "handler": {
        "human_queue_status": "waiting_on_dev_ti",
        "specialist_selection": {
          "mode": "auto"
        }
      },
      "publish_policy": {
        "allow_auto_publish": false,
        "min_response_confidence_for_auto_publish": "very_high",
        "max_risk_level_for_auto_publish": "none",
        "allow_draft_for_human": true,
        "allow_manual_only": true
      }
    }
  ],
  "specialists": [
    {
      "id": "support",
      "display_name": "Support Specialist",
      "spec_id": "support",
      "enabled": true,
      "can_assist_human": true
    },
    {
      "id": "bug",
      "display_name": "Bug Specialist",
      "spec_id": "bug",
      "enabled": true,
      "can_assist_human": true
    }
  ]
}
```

### 9.5 Route target rules

Each route target MUST satisfy:

1. `id` uses `snake_case`.
2. `id` is immutable once in use in production data.
3. `kind` is one of:
   - `direct_ai`
   - `human_assist`
4. `enabled=false` means the target remains loadable for historical display but MUST NOT be available to the router or selector for new runs.
5. `router_description` is the canonical taxonomy description used in router prompt rendering.
6. `ops_visible=true` means the target appears in ops filter lists and pills.

### 9.6 Specialist-selection rules

Each route target has one `handler.specialist_selection` object with:

- `mode`
- optional `specialist_id`
- optional `candidate_specialist_ids`

Allowed `mode` values:
- `fixed`
- `auto`
- `none`

Rules:

1. `direct_ai` targets MUST NOT use `mode=none`.
2. `fixed` requires `specialist_id`.
3. `fixed` MUST NOT include `candidate_specialist_ids`.
4. `auto` MAY include `candidate_specialist_ids`.
5. For `direct_ai` targets, `auto` MUST include `candidate_specialist_ids`.
   Reason: direct AI routing must not implicitly search all specialists.
6. For `human_assist` targets, `auto` with no `candidate_specialist_ids` means:
   - all enabled specialists where `can_assist_human=true`
7. `none` is valid only for `human_assist`.
8. `none` MUST NOT include `specialist_id` or `candidate_specialist_ids`.

### 9.7 Publish-policy rules

Each route target MUST define:

- `allow_auto_publish`
- `min_response_confidence_for_auto_publish`
- `max_risk_level_for_auto_publish`
- `allow_draft_for_human`
- `allow_manual_only`

Rules:

1. `allow_auto_publish=false` is REQUIRED for `human_assist` targets.
2. At least one human fallback MUST be allowed:
   - `allow_draft_for_human=true`
   - or `allow_manual_only=true`
3. `min_response_confidence_for_auto_publish` uses the same named enum as `specialist_result.response_confidence`.
4. `max_risk_level_for_auto_publish` uses the same named enum as `specialist_result.risk_level`.

### 9.8 Specialist rules

Each specialist entry MUST satisfy:

1. `id` is unique and stable.
2. `spec_id` points to an `agent_specs/<spec-id>/` folder.
3. `spec_id` MUST resolve to a manifest whose `id` is the same as the registry `spec_id`.
4. `enabled=false` means the specialist remains known for historical display but MUST NOT be selected for new runs.
5. `can_assist_human` defaults to `true` when omitted.

### 9.9 Registry validation module

Add a new module:

`shared/routing_registry.py`

This module MUST:
- load `agent_specs/registry.json`
- validate structure and cross-references
- load referenced agent specs
- expose typed accessors for:
  - route targets by id
  - specialists by id
  - ops-visible route targets
  - enabled route targets
  - router spec
  - selector spec
  - candidate specialists for a route target

Do not overload [shared/agent_specs.py](/home/marcelo/code/AutoSac/shared/agent_specs.py) with registry logic beyond spec loading.

## 10. Agent Specs

### 10.1 Folder model

Keep the existing folder shape:

`agent_specs/<spec-id>/`

Each spec folder MUST contain:
- `manifest.json`
- `prompt.md`
- `skill.md`

### 10.2 Spec manifest responsibilities

Per-spec `manifest.json` remains the source of:
- `id`
- `version`
- `kind`
- `description`
- `skill_id`
- `output_contract`
- optional `model_override`
- optional `timeout_seconds_override`

Allowed `kind` values in the new system:
- `router`
- `selector`
- `specialist`

### 10.3 Registry vs manifest ownership

Ownership split:

- Registry owns workflow taxonomy and routing policy.
- Spec manifest owns execution metadata for one spec.

The manifest MUST NOT declare route-target IDs.
The registry MUST NOT duplicate prompt or skill contents.

## 11. Output Contracts

Output contracts remain defined in Python and schema-generated from code.

Replace the current hardcoded `Literal[...]` taxonomy model in [worker/output_contracts.py](/home/marcelo/code/AutoSac/worker/output_contracts.py) with:
- static shape validation for shared enums and text fields
- runtime registry validation for `route_target_id` and `specialist_id`

### 11.1 Shared enums

Use these exact named enums:

`response_confidence`:
- `very_low`
- `low`
- `medium`
- `high`
- `very_high`

`risk_level`:
- `none`
- `low`
- `medium`
- `high`
- `critical`

`publish_mode_recommendation`:
- `auto_publish`
- `draft_for_human`
- `manual_only`

These enums MAY be hardcoded because they are system semantics, not business taxonomy.

### 11.2 router_result

Replace the current router contract with:

```json
{
  "route_target_id": "support",
  "routing_rationale": "The requester is asking for low-risk usage guidance."
}
```

Required fields:
- `route_target_id: string`
- `routing_rationale: string`

Validation rules:
- `route_target_id` MUST exist in the registry
- `route_target_id` MUST be enabled for new runs
- no extra fields

Rationale:
- remove hardcoded route-target enums
- keep router schema minimal
- remove router confidence until there is a concrete product use for it

### 11.3 specialist_selector_result

Add a new contract for selector steps:

```json
{
  "specialist_id": "bug",
  "selection_rationale": "This ticket likely needs debugging-oriented response drafting."
}
```

Required fields:
- `specialist_id: string`
- `selection_rationale: string`

Validation rules:
- `specialist_id` MUST exist in the registry
- `specialist_id` MUST be in the candidate set for the route target
- no extra fields

### 11.4 specialist_result

Replace the current `TriageResult` with one minimal contract.

Required field order in the Python model MUST be:

1. `requester_language`
2. `public_reply_markdown`
3. `internal_note_markdown`
4. `response_confidence`
5. `risk_level`
6. `risk_reason`
7. `summary_internal`
8. `publish_mode_recommendation`

Required shape:

```json
{
  "requester_language": "en",
  "public_reply_markdown": "Here is the answer for the requester...",
  "internal_note_markdown": "Optional operator note.",
  "response_confidence": "high",
  "risk_level": "low",
  "risk_reason": "The guidance is low-risk and based on known product behavior.",
  "summary_internal": "The requester needs configuration guidance for X.",
  "publish_mode_recommendation": "auto_publish"
}
```

Validation rules:

1. `requester_language` MUST be a non-empty language tag or language label.
2. `public_reply_markdown` MUST be required for:
   - `auto_publish`
   - `draft_for_human`
3. `public_reply_markdown` MAY be empty for `manual_only`.
4. `internal_note_markdown` MAY be empty except:
   - it MUST be non-empty for `manual_only`
5. `risk_reason` MUST be non-empty for every result.
6. `summary_internal` MUST be non-empty for every result.
7. No extra fields are allowed.

Important semantic rule:
- The specialist MUST NOT emit `route_target_id` or any other reclassification field.

### 11.5 human_handoff_result

Add one code-generated terminal contract for `human_assist` targets that do not run a specialist.

This contract is not emitted by an LLM. It is synthesized by deterministic worker code from router output and route-target handling.

Required shape:

```json
{
  "route_target_id": "manual_review",
  "handoff_reason": "The case is ambiguous and should be handled by a human operator.",
  "summary_internal": "Short internal handoff summary.",
  "internal_note_markdown": "Operator-facing handoff note.",
  "public_reply_markdown": "",
  "assistant_used": false,
  "assistant_specialist_id": null
}
```

Required fields:
- `route_target_id: string`
- `handoff_reason: string`
- `summary_internal: string`
- `internal_note_markdown: string`
- `public_reply_markdown: string`
- `assistant_used: boolean`
- `assistant_specialist_id: string | null`

Validation rules:
- `route_target_id` MUST exist in the registry
- `handoff_reason` MUST be non-empty
- `summary_internal` MUST be non-empty
- `internal_note_markdown` MUST be non-empty
- `assistant_used=false` requires `assistant_specialist_id=null`
- `assistant_used=true` requires a non-empty `assistant_specialist_id`

Persistence rules:
- use `AIRun.final_output_contract = "human_handoff_result"`
- populate `AIRun.final_output_json` for every accepted `human_assist + none` run
- set `AIRun.final_agent_spec_id = null` when the terminal payload was synthesized by code rather than produced by a specialist

### 11.6 Historical contracts

Keep existing `triage_result` contract definitions available only as legacy read models if needed for ops presentation. They MUST NOT be used for new step execution after migration.

## 12. Prompt Rendering

### 12.1 Router prompt

The router prompt MUST no longer hardcode the taxonomy in prompt text.

Instead, render a generated catalog placeholder such as:

`{ROUTE_TARGET_CATALOG}`

The generated catalog MUST list only enabled route targets and MUST include:
- target id
- label
- kind
- router description

### 12.2 Selector prompt

Add a selector spec, for example:

`agent_specs/specialist-selector/`

The selector prompt MUST receive:
- current ticket context
- chosen `route_target_id`
- target label
- target kind
- target router description
- candidate specialist catalog

Use a generated placeholder such as:

`{SPECIALIST_CANDIDATE_CATALOG}`

### 12.3 Specialist prompt

Specialist prompts MUST no longer hardcode class lists.

Each specialist prompt SHOULD receive:
- `ROUTE_TARGET_ID`
- `ROUTE_TARGET_LABEL`
- `ROUTE_TARGET_KIND`
- `ROUTE_TARGET_ROUTER_DESCRIPTION`
- `ROUTER_RATIONALE`
- `REQUESTER_ROLE`
- `REQUESTER_CAN_VIEW_INTERNAL_MESSAGES`
- message history

The specialist prompt MUST instruct the model to stay within the selected route target and produce only the `specialist_result` schema.

### 12.4 Prompt-renderer changes

Refactor [worker/prompt_renderer.py](/home/marcelo/code/AutoSac/worker/prompt_renderer.py) to support:
- router prompt values
- selector prompt values
- specialist prompt values
- generated catalogs from the registry

Do not keep `TARGET_TICKET_CLASS` or `ROUTER_TICKET_CLASS` placeholders.

## 12.5 Workspace contract

Update [shared/contracts.py](/home/marcelo/code/AutoSac/shared/contracts.py) so `WORKSPACE_AGENTS_CONTENT` becomes taxonomy-agnostic and contains only Stage 1 global guardrails.

The workspace contract MUST NOT hardcode:
- route-target or class lists
- old large-triage schema fields such as `impact_level` and `development_needed`
- clarifying-question workflow rules from the legacy `triage_result` contract

The workspace contract SHOULD continue to define:
- read-only execution
- prohibited data sources and side effects
- internal/public information-handling rules
- general Stage 1 evidence and safety guardrails

Route-target catalogs and selection instructions belong in router and selector prompts, not in `AGENTS.md`.

This migration MUST bump `WORKSPACE_BOOTSTRAP_VERSION`.

## 13. Runtime Flow

### 13.1 Direct-AI target

Flow:

1. Run router.
2. Resolve route target.
3. If specialist selection is `fixed`, use that specialist.
4. If specialist selection is `auto`, run selector, then use the chosen specialist.
5. Run specialist.
6. Evaluate deterministic publication policy.
7. Persist `route_target_id` to the ticket.
8. Auto-publish, create a draft, or hold for manual-only handling.

### 13.2 Human-assist target

Flow:

1. Run router.
2. Resolve route target.
3. If specialist selection is `none`, do not run a specialist and synthesize a `human_handoff_result`.
4. If specialist selection is `fixed`, run that specialist.
5. If specialist selection is `auto`, run selector, then the chosen specialist.
6. Never auto-publish to the requester.
7. If a public reply draft exists, create a draft.
8. Route the ticket to the configured human queue status.
9. Persist `route_target_id` to the ticket.
10. Persist a terminal structured output for the run in all cases.

### 13.3 No more mismatch logic

Delete the current router/specialist mismatch concept from [worker/pipeline.py](/home/marcelo/code/AutoSac/worker/pipeline.py).

Reason:
- specialists no longer classify
- the mismatch path becomes impossible by design

## 14. Deterministic Publication Policy

### 14.1 Ordering

Use these ordinal rankings in code:

`response_confidence` ascending:
- `very_low`
- `low`
- `medium`
- `high`
- `very_high`

`risk_level` ascending:
- `none`
- `low`
- `medium`
- `high`
- `critical`

### 14.2 Policy engine

Add a dedicated helper module, for example:

`worker/publication_policy.py`

This module MUST:
- compare specialist result against route-target publish policy
- resolve the effective publication mode
- keep the logic small and deterministic

`requester_can_view_internal_messages` MAY be available to prompts as context, but MUST NOT influence the publication-policy decision.

### 14.3 Effective publication mode algorithm

For `direct_ai` targets:

1. Start with `specialist_result.publish_mode_recommendation`.
2. If the recommendation is `auto_publish`, downgrade to `draft_for_human` when any of these are true:
   - `allow_auto_publish=false`
   - `response_confidence` is below `min_response_confidence_for_auto_publish`
   - `risk_level` is above `max_risk_level_for_auto_publish`
   - `public_reply_markdown` is empty
3. If the effective mode is `draft_for_human` but `allow_draft_for_human=false`, downgrade to `manual_only`.
4. If the effective mode is `manual_only` but `allow_manual_only=false`, that is a configuration error and the run MUST fail.

For `human_assist` targets:

1. Effective mode is always human review.
2. `auto_publish` MUST never happen.
3. `public_reply_markdown`, when present, is treated as a draft only.

Internal-requester policy:

1. `requester_can_view_internal_messages` is prompt/context input only.
2. It MUST NOT change route-target selection.
3. It MUST NOT change publication policy.
4. It MUST NOT convert a human-review or manual-only outcome into auto-publish.
5. The old internal-requester shortcut behavior is intentionally removed.

### 14.4 Outcome mapping

Direct AI:

- `auto_publish`
  - publish `public_reply_markdown`
  - optionally publish or store `internal_note_markdown`
  - set ticket status to `waiting_on_user`
  - set `AIRun.status` to `succeeded`

- `draft_for_human`
  - create an `AIDraft` from `public_reply_markdown`
  - write internal note if present, else synthesize one from `summary_internal`, `risk_level`, and `risk_reason`
  - keep ticket in `ai_triage`
  - set `AIRun.status` to `human_review`

- `manual_only`
  - if `public_reply_markdown` is present, create a draft
  - write internal note
  - keep ticket in `ai_triage`
  - set `AIRun.status` to `human_review`

Human assist:

- always set ticket status to `handler.human_queue_status`
- if `public_reply_markdown` is present, create a draft
- write internal note if present, else synthesize one from `summary_internal`, `risk_level`, `risk_reason`, and router rationale
- set `AIRun.status` to `human_review`

## 15. Persistence Changes

### 15.1 Ticket model

In [shared/models.py](/home/marcelo/code/AutoSac/shared/models.py):

1. Replace `ticket_class` with `route_target_id`.
2. Remove the hardcoded `TICKET_CLASSES` constant and the DB check constraint on ticket class.
3. Keep `requester_language`.
4. `ai_confidence`, `impact_level`, and `development_needed` are no longer written by new code.

Recommended approach:
- keep obsolete columns for one migration if needed for data safety
- stop writing them immediately
- remove them in a follow-up migration after code cutover

Do not block this architecture on backfilling named confidence levels onto the ticket row.

### 15.2 AIRun model

Keep:
- `pipeline_version`
- `final_step_id`
- `final_agent_spec_id`
- `final_output_contract`
- `final_output_json`

No new run-level taxonomy column is required if `Ticket.route_target_id` is updated and final output retains route-target context implicitly via the router step.

Accepted-run invariant:
- every `succeeded` or `human_review` run MUST have non-null `final_output_contract` and `final_output_json`

### 15.3 AIRunStep model

Extend `AI_RUN_STEP_KINDS` in [shared/models.py](/home/marcelo/code/AutoSac/shared/models.py) to:
- `router`
- `selector`
- `specialist`

### 15.4 Historical data compatibility

Historical runs may keep legacy `triage_result` payloads in `final_output_json`.

The worker MUST NOT attempt to rewrite historical outputs at runtime.
If historical presentation compatibility is needed, confine it to a small ops presentation adapter.

## 15.5 Manifests and artifacts

Update [worker/artifacts.py](/home/marcelo/code/AutoSac/worker/artifacts.py) and related step-writing code so manifests remain authoritative under the new routing model.

`run_manifest.json` MUST include:
- `route_target_id`
- `route_target_label`
- `route_target_kind`
- `selected_specialist_id`
- `effective_publication_mode`
- `final_output_contract`
- `final_step_id`
- `final_agent_spec_id`

Step-manifest requirements:

- router step manifest MUST include:
  - chosen `route_target_id`
  - `routing_rationale`
- selector step manifest MUST include:
  - candidate specialist ids
  - chosen `specialist_id`
  - `selection_rationale`
- specialist step manifest MUST include:
  - `publish_mode_recommendation`
  - `response_confidence`
  - `risk_level`

`human_assist + specialist_selection.mode=none` MUST still produce a finalized `run_manifest.json` with `final_output_contract = "human_handoff_result"`.

## 16. Worker and Shared-Code Changes

### 16.1 New modules

Add:

- `shared/routing_registry.py`
- `worker/publication_policy.py`

Optional but recommended:

- `app/ai_run_presenters.py`

### 16.2 Existing modules to modify

Modify:

- [shared/agent_specs.py](/home/marcelo/code/AutoSac/shared/agent_specs.py)
- [shared/contracts.py](/home/marcelo/code/AutoSac/shared/contracts.py)
- [shared/models.py](/home/marcelo/code/AutoSac/shared/models.py)
- [shared/ticketing.py](/home/marcelo/code/AutoSac/shared/ticketing.py)
- [shared/workspace.py](/home/marcelo/code/AutoSac/shared/workspace.py)
- [worker/artifacts.py](/home/marcelo/code/AutoSac/worker/artifacts.py)
- [worker/output_contracts.py](/home/marcelo/code/AutoSac/worker/output_contracts.py)
- [worker/prompt_renderer.py](/home/marcelo/code/AutoSac/worker/prompt_renderer.py)
- [worker/pipeline.py](/home/marcelo/code/AutoSac/worker/pipeline.py)
- [worker/triage.py](/home/marcelo/code/AutoSac/worker/triage.py)
- [worker/step_runner.py](/home/marcelo/code/AutoSac/worker/step_runner.py)
- [app/routes_ops.py](/home/marcelo/code/AutoSac/app/routes_ops.py)
- ops templates using `ticket_class`
- tests covering worker, persistence, ops, and readiness

### 16.3 Ticketing helper replacement

Replace `apply_ai_classification()` in [shared/ticketing.py](/home/marcelo/code/AutoSac/shared/ticketing.py) with a smaller helper such as:

`apply_ai_route_target(ticket, route_target_id, requester_language)`

Do not keep stale classification fields in the core write path.

## 17. Ops and UI Changes

### 17.1 Filters

Replace `ticket_class` filters with `route_target_id` filters.

Filter options MUST be generated from registry entries where:
- `ops_visible=true`

### 17.2 Display

Replace class pills and labels with registry-derived route-target labels.

If a stored `route_target_id` is not found in the current registry:
- display the raw ID
- do not crash

### 17.3 Historical run presentation

If needed, add one presentation adapter keyed by `final_output_contract` or `pipeline_version` so ops detail can render:
- legacy `triage_result`
- new `specialist_result`

This compatibility belongs only in the presentation layer.

## 18. Database Migration Plan

### 18.1 Migration strategy

Use an additive-then-cutover migration strategy:

1. Add `tickets.route_target_id`.
2. Add `selector` to the `ai_run_steps.step_kind` constraint.
3. Backfill `route_target_id = ticket_class` for existing tickets where possible.
4. Dual-write `ticket_class` and `route_target_id` for one compatibility phase.
5. Switch reads, UI filters, and presentation to `route_target_id`.
6. Stop writing `ticket_class`.
7. Drop the `ticket_class` constraint.
8. Drop the `ticket_class` column in a cleanup migration after the code and tests are green.

This sequence is mandatory. Do not choose between rename strategies at implementation time.

### 18.2 Route-target DB constraints

Do not add a DB enum/check constraint for route-target IDs.

Reason:
- the taxonomy is registry-driven
- hardcoded DB constraints would recreate the duplication problem

### 18.3 Step-kind migration

Add `selector` to the `ai_run_steps.step_kind` constraint.

## 19. Implementation Order

The autonomous implementation agent SHOULD implement in this order:

1. Add `shared/routing_registry.py` and registry validation tests.
2. Add `agent_specs/registry.json`.
3. Add `agent_specs/specialist-selector/`.
4. Extend spec loading to allow `kind=selector`.
5. Update [shared/contracts.py](/home/marcelo/code/AutoSac/shared/contracts.py) and bump `WORKSPACE_BOOTSTRAP_VERSION`.
6. Add Alembic migration for `route_target_id` and step-kind changes.
7. Backfill `route_target_id` and introduce temporary dual-write behavior.
8. Replace output contracts with:
   - `router_result`
   - `specialist_selector_result`
   - `specialist_result`
   - `human_handoff_result`
9. Refactor prompt rendering to use registry catalogs and `route_target_id`.
10. Refactor pipeline flow:
   - router
   - optional selector
   - optional specialist
11. Add publication-policy engine.
12. Refactor `worker/triage.py` to apply route-target policy, simplified specialist results, and `human_handoff_result`.
13. Update manifests and step artifacts for route-target and selector/publication metadata.
14. Switch reads and UI from `ticket_class` to `route_target_id`.
15. Stop writing `ticket_class`.
16. Add bounded legacy presentation adapter if needed.
17. Remove obsolete code paths:
   - `specialist_agent_map()`
   - `resolve_specialist_spec_id(ticket_class)`
   - mismatch logic
   - hardcoded taxonomy literals
18. Drop the old `ticket_class` constraint and column in cleanup migrations.

Keep the repo green after each major phase where practical.

## 20. Testing Plan

### 20.1 Registry tests

Add tests for:
- duplicate route-target IDs
- duplicate specialist IDs
- missing referenced specs
- invalid `fixed` selection config
- invalid `auto` config for `direct_ai`
- invalid `none` config for `direct_ai`
- disabled route target behavior
- disabled specialist behavior
- candidate resolution for human-assist auto mode

### 20.2 Contract tests

Add tests for:
- `router_result` with valid and invalid `route_target_id`
- `specialist_selector_result` with valid and invalid candidate selections
- `specialist_result` required-field rules
- `human_handoff_result` required-field rules
- `manual_only` requiring `internal_note_markdown`
- `auto_publish` requiring `public_reply_markdown`

### 20.3 Prompt-rendering tests

Add tests for:
- router prompt catalog generation
- selector prompt candidate catalog generation
- specialist prompt rendering with route-target placeholders
- missing-placeholder failures

### 20.4 Pipeline tests

Add tests for:
- direct-AI target with fixed specialist and auto publish
- direct-AI target with fixed specialist and downgrade to draft
- direct-AI target with fixed specialist and manual-only
- human-assist target with `none` producing valid `human_handoff_result`
- human-assist target with fixed specialist
- human-assist target with auto specialist selection
- selector choosing a candidate from the allowed list
- selector attempting to choose an invalid candidate
- `requester_can_view_internal_messages=true` not changing publication outcome

### 20.5 Persistence and migration tests

Add tests for:
- backfilling `route_target_id` from `ticket_class`
- `selector` step persistence
- temporary dual-write phase for `ticket_class` and `route_target_id`
- removal of ticket-class read/write paths
- no DB constraint on route-target taxonomy

### 20.6 Ops tests

Add tests for:
- dynamic route-target filters from the registry
- route-target labels rendered in ticket list/detail
- fallback display for unknown stored route-target IDs
- historical run presentation where needed
- accepted `human_review` runs with `human_handoff_result` rendering correctly

### 20.7 Readiness and checks

Update readiness and smoke checks so they validate:
- registry file exists and loads
- referenced spec folders exist
- selector spec exists if any route target uses `mode=auto`
- workspace bootstrap includes all active spec skills
- accepted runs still always have `final_output_json`

## 21. Acceptance Criteria

The implementation is complete only when all of the following are true:

1. `agent_specs/registry.json` exists and is the only workflow-taxonomy registry.
2. No business taxonomy is hardcoded in `Literal[...]`, DB check constraints, ops filter tuples, or prompt text.
3. `ticket_class` is no longer part of the new runtime execution path.
4. The router outputs only `route_target_id` and rationale.
5. Specialists no longer emit a route target.
6. Human escalation is represented by route targets in the registry.
7. Human-assist targets can be configured with `none`, `fixed`, or `auto` specialist selection.
8. The specialist contract contains only the minimal agreed fields.
9. Accepted runs always have terminal structured output, including `human_assist + none`.
10. Publication behavior is gated by deterministic code plus route-target policy.
11. `requester_can_view_internal_messages` no longer overrides automation outcomes.
12. Ops uses `route_target_id` and registry labels.
13. The workspace bootstrap contract is generic and no longer hardcodes legacy taxonomy or schema fields.
14. Run and step manifests carry route-target and selector/publication metadata.
15. The current regression suite is updated and green.
16. `run_web.py --check` and `run_worker.py --check` validate registry integrity.

## 22. Explicitly Rejected Alternatives

These alternatives were considered and rejected:

1. Keep `ticket_class` hardcoded and only move specialist mapping into a JSON file.
   Rejected because it still duplicates taxonomy across code, DB, and prompts.

2. Let specialists continue to classify and compare against the router.
   Rejected because it duplicates responsibility and creates avoidable mismatch handling.

3. Put routing logic in YAML/JSON conditions.
   Rejected because it increases complexity and hides workflow code in configuration.

4. Keep the large `TriageResult` schema.
   Rejected because it asks the model to satisfy too many structured fields and weakens output reliability.

5. Model human escalation outside the routing taxonomy.
   Rejected because it keeps the workflow split across ad hoc code paths instead of one registry.

## 23. Summary for the Implementation Agent

If you are implementing this document from the current codebase, the essential moves are:

1. Introduce `agent_specs/registry.json` and a typed loader in `shared/routing_registry.py`.
2. Replace `ticket_class` with `route_target_id` everywhere.
3. Replace the current output contracts with:
   - minimal `router_result`
   - `specialist_selector_result`
   - minimal `specialist_result`
4. Remove specialist reclassification and mismatch logic.
5. Add selector-step support for registry-driven AI specialist selection.
6. Add a deterministic publish-policy engine.
7. Add `human_handoff_result` so accepted runs always have structured output.
8. Update workspace bootstrap, UI, migrations, and tests to be registry-driven.

Do not reintroduce hardcoded taxonomy while implementing this design.
