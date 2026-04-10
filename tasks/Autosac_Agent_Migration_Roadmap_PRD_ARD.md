# PRD + ARD — Agent-Spec Migration And Multi-Step AI Pipeline Roadmap

Document status: Proposed, implementation-ready  
Version: 0.1  
Date: 2026-04-06  
Owner: Internal Engineering  
Audience: autonomous implementation agent, technical reviewer, system owner

Decision context:
- Breaking changes are allowed when justified.
- Implementation time is not a constraint.
- Regression risk is still a first-class concern.
- KISS, DRY, and low cyclomatic complexity remain mandatory.

Normative terms:
- MUST = mandatory
- MUST NOT = prohibited
- SHOULD = recommended
- MAY = optional

---

## 1. Executive Summary

AutoSac Stage 1 currently has a single hardcoded triage prompt, a single workspace skill, a single-step worker execution model, and run-level artifact fields that assume one prompt and one output per AI run. That shape does not fit the planned future where different ticket classes use different specialized prompts and agents.

This roadmap defines the direct end-state and the phased implementation plan to reach it:
- replace hardcoded prompt strings with file-backed `agent_specs/`
- replace the one-step AI run model with a deterministic `router -> specialist` pipeline
- replace filesystem-dependent web reads with database-backed final structured outputs
- replace run-level artifact assumptions with step-level persistence and artifacts
- preserve Stage 1 safety rules and the existing ticket outcome logic

This document intentionally chooses the target architecture instead of a temporary compatibility wrapper. The implementation sequence remains staged so that the system can be verified at each boundary and legacy assumptions can be removed only after backfill and test coverage are in place.

---

## 2. Verified Current State

The following current-state assumptions were verified against the repository before writing this roadmap:

1. The main triage prompt is hardcoded in `shared/contracts.py` as `TRIAGE_PROMPT_TEMPLATE`.
2. The worker renders that prompt directly in `worker/codex_runner.py` and writes one flat run directory containing `prompt.txt`, `schema.json`, `final.json`, `stdout.jsonl`, and `stderr.txt`.
3. The worker currently assumes exactly one prompt/schema/output set per `ai_run` and stores those paths directly on the `ai_runs` row.
4. The workspace bootstrap currently provisions exactly one skill file at `.agents/skills/stage1-triage/SKILL.md`.
5. The ops ticket detail route currently reads accepted AI output from `AIRun.final_output_path` on disk rather than from structured data stored in the database.
6. Existing regression coverage already touches the critical seams that this migration will affect:
   - worker prompt/artifact behavior in `tests/test_ai_worker.py`
   - ops artifact display and accepted-analysis loading in `tests/test_ops_workflow.py`
   - workspace bootstrap and contract persistence in `tests/test_foundation_persistence.py`

Confirmed implications:
- the current architecture is single-step by construction, not by accident
- prompt extraction alone is insufficient for future class-specialized agents
- the persistence model, bootstrap model, and ops read path all need to change together
- a safe migration requires both schema work and codepath work

---

## 3. Decision Summary

The confirmed plan is:

1. Introduce file-backed `agent_specs/` as the source of prompt and skill definitions.
2. Introduce a deterministic two-step worker pipeline:
   - step 1: `router`
   - step 2: specialist selected from router `ticket_class`
3. Introduce `ai_run_steps` so artifacts and outputs are stored per step instead of per run.
4. Store final validated structured output on `ai_runs` in the database and treat disk artifacts as debugging/audit support, not as the web app source of truth.
5. Keep the existing `TriageResult` business rules and outcome application logic as intact as possible to reduce behavior regression risk.
6. Avoid generic workflow engines, plugin systems, prompt inheritance DSLs, or dynamic routing logic.

This is the smallest architecture that actually matches the planned product direction.

---

## 4. Part I — Product Requirements Document

## 4.1 Problem Statement

The current Stage 1 worker can only run one generic triage agent because:
- prompt text is embedded in Python code
- workspace skill bootstrap assumes one skill
- `ai_runs` assumes one prompt and one result
- the web app reads AI result JSON from disk

This blocks the roadmap item for specialized prompts and agents by ticket class. It also creates unnecessary coupling between runtime behavior, repository constants, artifact layout, and UI behavior.

## 4.2 Goals

The migration MUST achieve all of the following:

1. Support class-specialized AI agents without reintroducing prompt strings in Python source.
2. Keep Stage 1 deterministic: a given ticket input must flow through a predictable orchestration path.
3. Make final AI result consumption database-backed for the web app.
4. Preserve operator observability by exposing step-level artifacts and structured step outputs.
5. Preserve historical runs through schema evolution and backfill.
6. Keep the worker implementation simple enough to reason about locally without a workflow framework.
7. Make adding a new specialist agent a bounded change.

## 4.3 Non-Goals

The migration MUST NOT attempt to solve the following:

1. A generic multi-agent platform.
2. Dynamic plugin discovery from arbitrary external packages.
3. Arbitrary DAG execution or branching pipelines.
4. Parallel step execution.
5. Prompt authoring in the database.
6. External web search, code patching, or any widening of Stage 1 AI privileges.
7. A full redesign of ticketing or workflow semantics outside the AI execution path.

## 4.4 Product Requirements

### PR-1 File-backed agent definitions

The system MUST store prompt and skill definitions as repository files under a dedicated top-level directory. These definitions MUST be human-reviewable and version-controlled.

### PR-2 Deterministic routing

Each AI run MUST use a deterministic routing step before specialist execution. The router MUST emit a validated structured result. The specialist MUST be selected by a simple code-level mapping from `ticket_class` to `agent_spec_id`.

### PR-3 Step-level observability

Operators MUST be able to inspect step-level artifacts and understand:
- which agent ran
- which version ran
- which output contract was expected
- which output was produced
- where failures occurred

### PR-4 Database-backed final AI output

The web layer MUST be able to render accepted AI analysis, summaries, and relevant paths without reading `final.json` from disk.

### PR-5 Historical continuity

Pre-migration `ai_runs` data MUST remain visible and auditable after the migration. Historical runs MUST NOT disappear from the ops interface solely because the schema changed.

### PR-6 Safe mismatch handling

If router output and specialist output disagree materially, the system MUST fail safe. It MUST NOT silently auto-publish from a contradictory pipeline.

### PR-7 Minimal specialist onboarding cost

Adding a new specialist MUST require only:
- a new `agent_specs/<agent-id>/` directory
- one deterministic routing mapping update
- contract or prompt tests

It MUST NOT require editing multiple hardcoded prompt strings spread across the codebase.

### PR-8 Safety invariants preserved

The migration MUST preserve Stage 1 guardrails:
- read-only Codex execution
- no web search
- no repo modification
- no DB/DDL/log inspection by the AI
- no disclosure of internal-only information in automatic public replies

## 4.5 Success Criteria

The migration is successful only when all of the following are true:

1. New runs create step rows and step artifacts.
2. Ops ticket detail renders AI summaries and relevant paths from database-backed structured output.
3. Existing business behavior for public replies, internal notes, drafts, routing, and human-review downgrade remains correct.
4. Historical runs remain visible after backfill.
5. A new specialist can be added without modifying shared Python prompt constants.
6. The regression suite covers router execution, specialist execution, failure paths, and backfilled historical data.

---

## 5. Part II — Architecture Requirements Document

## 5.1 Design Principles

1. Keep orchestration linear.
   The pipeline is `router -> specialist`. No DAG engine.

2. Keep business rules in Python.
   Prompt manifests MAY describe metadata. They MUST NOT contain approval logic, routing policies, or workflow code.

3. Keep prompts editable.
   Prompt text and skills belong in files under version control.

4. Keep structured validation typed.
   Output contracts MUST be defined in Python models and validated in code.

5. Keep the database authoritative for final UI-facing AI output.
   Files on disk are supplementary artifacts.

6. Keep failures safe.
   Ambiguity, malformed output, or router/specialist disagreement MUST downgrade to human review or failure, not silent automation.

## 5.2 Target Concepts

### Agent spec

An agent spec is a repository-defined execution unit containing:
- one manifest
- one prompt template
- one workspace skill file

An agent spec is versioned by file contents plus manifest version.

### AI run

An AI run represents one orchestration attempt for one ticket and one input fingerprint.

### AI run step

An AI run step represents one Codex invocation within an AI run.

### Final output

The final output for a run is the validated structured output of the final specialist step, persisted on the `ai_runs` row for easy web consumption.

## 5.3 Target Repository Layout

The repo MUST add:

```text
agent_specs/
  router/
    manifest.json
    prompt.md
    skill.md
  support/
    manifest.json
    prompt.md
    skill.md
  access-config/
    manifest.json
    prompt.md
    skill.md
  data-ops/
    manifest.json
    prompt.md
    skill.md
  bug/
    manifest.json
    prompt.md
    skill.md
  feature/
    manifest.json
    prompt.md
    skill.md
  unknown/
    manifest.json
    prompt.md
    skill.md
```

No prompt inheritance system is required for the first implementation. Shared rules SHOULD live in reusable skill text or shared Python rendering helpers, not in a custom prompt DSL.

## 5.4 Agent Spec Contract

Each `manifest.json` MUST contain:
- `id`
- `version`
- `kind`
- `description`
- `skill_id`
- `output_contract`
- `model_override` or `null`
- `timeout_seconds_override` or `null`

Example:

```json
{
  "id": "support",
  "version": "1",
  "kind": "specialist",
  "description": "Support and how-to specialist for Stage 1 triage.",
  "skill_id": "triage-support",
  "output_contract": "triage_result",
  "model_override": null,
  "timeout_seconds_override": null
}
```

Rules:
- `id` MUST be stable.
- `version` MUST change when the prompt or skill changes in a way that matters operationally.
- `kind` MUST be either `router` or `specialist`.
- `skill_id` determines the workspace `.agents/skills/<skill_id>/SKILL.md` destination.
- `output_contract` MUST map to a typed Python output contract.

Prompt rendering rules:
- `prompt.md` contains the prompt body template.
- the runtime MUST prepend the skill invocation line automatically based on `skill_id`
- placeholder substitution MUST be handled by Python code
- unresolved placeholders MUST fail fast before any Codex invocation

## 5.5 Output Contract Layer

The system MUST define typed output contracts in Python, not as large hand-authored JSON strings.

Required contracts:

1. `RouterResult`
   - `ticket_class`
   - `confidence`
   - `routing_rationale`

2. `TriageResult`
   - same business fields as the current Stage 1 triage output

Requirements:
- JSON schema MUST be generated from the typed models
- runtime validation MUST use the typed models
- schema files written to disk for Codex MUST be generated from the same typed models
- worker business validation for `TriageResult` MUST remain explicit in Python

## 5.6 Worker Module Responsibilities

The runtime SHOULD be decomposed into small modules with single responsibilities:

- `worker/agent_specs.py`
  Loads and validates agent specs from disk.

- `worker/output_contracts.py`
  Defines typed output contracts and schema generation.

- `worker/prompt_renderer.py`
  Renders prompt templates from agent specs and ticket context.

- `worker/artifacts.py`
  Creates run directories, step directories, and manifest files.

- `worker/step_runner.py`
  Executes one step, writes artifacts, validates output, persists the step row.

- `worker/pipeline.py`
  Orchestrates `router -> specialist`.

- `worker/triage.py`
  Retains outcome resolution and ticket mutation logic.

This separation keeps cyclomatic complexity low and makes unit testing straightforward.

## 5.7 Pipeline Definition

The pipeline MUST behave as follows:

1. Load the target `ai_run`.
2. Load ticket context.
3. Compute requester-visible input fingerprint.
4. If the fingerprint is unchanged and the trigger is not `manual_rerun`, mark the run `skipped` using the current semantics.
5. Ensure the ticket status is `ai_triage` before execution, preserving current behavior.
6. Start the router step.
7. Validate `RouterResult`.
8. Map router `ticket_class` to a specialist `agent_spec_id` using a simple constant mapping.
9. Start the specialist step.
10. Validate `TriageResult`.
11. If router and specialist disagree on `ticket_class`, downgrade to human review or fail safe. The first implementation MUST NOT auto-reroute recursively.
12. Persist the specialist output as the run final output on `ai_runs`.
13. Apply the existing ticket outcome logic.
14. Finalize run status and process deferred requeue rules.

Deterministic class-to-specialist mapping:
- `support -> support`
- `access_config -> access-config`
- `data_ops -> data-ops`
- `bug -> bug`
- `feature -> feature`
- `unknown -> unknown`

The mapping MUST live in Python code, not in the manifest layer.

## 5.8 Persistence Model

### `ai_runs`

`ai_runs` MUST remain the orchestration record and MUST contain:
- existing core identity and trigger fields
- `input_hash`
- `status`
- `error_text`
- `started_at`
- `ended_at`
- `created_at`
- `pipeline_version`
- `final_step_id`
- `final_agent_spec_id`
- `final_output_contract`
- `final_output_json`

The run row MUST stop being the storage location for individual step artifact paths.

### `ai_run_steps`

A new `ai_run_steps` table MUST be added with at least:
- `id`
- `ai_run_id`
- `step_index`
- `step_kind`
- `agent_spec_id`
- `agent_spec_version`
- `output_contract`
- `model_name`
- `status`
- `prompt_path`
- `schema_path`
- `final_output_path`
- `stdout_jsonl_path`
- `stderr_path`
- `output_json`
- `error_text`
- `started_at`
- `ended_at`
- `created_at`

Required constraints and indexes:
- primary key on `id`
- foreign key to `ai_runs.id`
- unique constraint on `(ai_run_id, step_index)`
- check constraint for `step_kind in ('router', 'specialist')`
- check constraint for valid step statuses
- index on `ai_run_id, step_index`

Relationship policy:
- `TicketMessage.ai_run_id` and `AIDraft.ai_run_id` SHOULD remain linked to the parent `ai_run`
- a new `ai_run_step_id` foreign key is not required in the first implementation
- the parent run and `final_step_id` are sufficient to identify the authoritative specialist result

### Legacy columns

The legacy run-level artifact columns on `ai_runs` SHOULD remain temporarily during migration and MUST be dropped only after:
- schema backfill is complete
- the web app no longer reads them
- the worker no longer writes them

## 5.9 Artifact Model

New runs MUST use step directories:

```text
runs/<ticket_id>/<run_id>/
  run_manifest.json
  01-router/
    prompt.txt
    schema.json
    final.json
    stdout.jsonl
    stderr.txt
    step_manifest.json
  02-support/
    prompt.txt
    schema.json
    final.json
    stdout.jsonl
    stderr.txt
    step_manifest.json
```

Rules:
- `run_manifest.json` is supplemental metadata, not the source of truth
- `step_manifest.json` SHOULD summarize the step contract, status, and artifact paths
- step artifact filenames SHOULD remain the same across all steps for operator familiarity

Historical runs MUST NOT be physically moved. Backfilled step rows MAY point to legacy flat file paths.

## 5.10 Workspace Bootstrap

The current single-skill bootstrap model MUST be replaced.

Required changes:
- replace singular `workspace_skill_path` with a skills-directory concept
- bootstrap all required skills from `agent_specs/*/skill.md` into workspace `.agents/skills/<skill_id>/SKILL.md`
- keep `AGENTS.md` generic and stable
- verify all required skill files exist during worker startup checks
- bump `WORKSPACE_BOOTSTRAP_VERSION`

Bootstrap and verification MUST remain deterministic and idempotent.

## 5.11 Web And Ops UI Requirements

The web app MUST stop reading accepted analysis from `final.json` on disk.

Required behavior:
- `latest_analysis_run` selection MUST operate on run status plus structured output availability
- accepted AI summary and relevant paths MUST come from `ai_runs.final_output_json`
- ops ticket detail MUST expose step-level artifacts and metadata by reading `ai_run_steps`
- historical backfilled runs with missing `output_json` MUST degrade gracefully rather than crash

The UI MAY continue showing artifact file paths for debugging, but those paths MUST be read from `ai_run_steps`, not `ai_runs`.

## 5.12 Data Migration And Backfill

The migration MUST be staged.

### Schema migration

An Alembic migration MUST:
- create `ai_run_steps`
- add new final-output columns to `ai_runs`
- preserve legacy artifact columns temporarily

### Backfill

Historical backfill SHOULD be performed by an idempotent application script, not by Alembic, because:
- filesystem artifact availability is an operational concern
- backfill MAY need to inspect existing `final.json` files
- resumability and reporting are important

The backfill script MUST:
- create one synthetic step row per historical `ai_run`
- set `step_kind='specialist'` for historical runs
- set `agent_spec_id='legacy-stage1-single-step'`
- set `agent_spec_version='pre-agent-specs'`
- copy legacy artifact paths onto the new step row
- attempt to parse historical `final_output_path` into `output_json`
- populate `ai_runs.final_output_json`, `final_output_contract`, `final_agent_spec_id`, and `final_step_id` when possible
- leave incomplete historical runs visible even if some files are missing

For historical runs, `ai_runs.final_agent_spec_id` SHOULD be set to `legacy-stage1-single-step` instead of pretending that a modern specialist produced the output.

No historical artifact files need to be relocated.

## 5.13 Failure Handling

The new pipeline MUST define explicit failure semantics:

1. Router timeout or invalid output:
   - run fails
   - ticket follows current failure routing rules

2. Specialist timeout or invalid output:
   - run fails
   - ticket follows current failure routing rules

3. Router/specialist class mismatch:
   - downgrade to human review or fail safe
   - no automatic public reply

4. Missing historical artifact files during backfill:
   - preserve run history
   - mark structured output unavailable
   - do not abort the backfill batch

## 5.14 Security And Safety Invariants

The migration MUST preserve all Stage 1 constraints:
- Codex remains read-only
- web search remains disabled
- prompts and skills MUST continue forbidding repo mutation, database inspection, DDL inspection, and log inspection
- internal-only information MUST remain protected by existing validation and outcome logic

## 5.15 Configuration Policy

No new runtime environment variables SHOULD be added unless strictly necessary.

The following SHOULD remain true:
- agent specs live in the repository
- workspace skill sync is derived from repository contents
- model selection defaults still come from existing settings unless a spec manifest explicitly overrides them

---

## 6. Implementation Roadmap

The recommended delivery order is six PR-sized phases. They are sequential by default, but phases 1 and 2 can overlap if ownership is clear.

## 6.1 Phase 0 — Freeze Expectations

Scope:
- add roadmap document
- add or tighten baseline tests around current prompt rendering, run artifact files, ops accepted-analysis behavior, and workspace bootstrap
- capture golden prompt snapshots from the current single prompt

Deliverables:
- approved roadmap
- golden prompt fixture for current triage behavior
- explicit tests that fail on prompt drift

Exit criteria:
- the baseline suite passes
- the exact current prompt is captured in tests

## 6.2 Phase 1 — Add Agent Specs And Contract Scaffolding

Scope:
- create `agent_specs/`
- move prompt content into `prompt.md` files
- add `skill.md` files
- add `manifest.json` files
- add `worker/agent_specs.py`
- add `worker/output_contracts.py`
- add schema generation from typed models

Rules:
- no runtime cutover yet
- no database changes yet
- prompt text for the default specialist MUST remain behaviorally equivalent to the current prompt

Deliverables:
- file-backed specs for router and all planned specialists
- typed `RouterResult` and `TriageResult`
- unit tests for manifest loading and prompt rendering

Exit criteria:
- loader tests pass
- golden prompt tests prove parity for the default specialist prompt

## 6.3 Phase 2 — Expand Persistence And Add Backfill

Scope:
- add `ai_run_steps`
- add final-output fields on `ai_runs`
- add ORM models and migrations
- add idempotent backfill script

Rules:
- legacy artifact columns remain for now
- no destructive schema removal in this phase

Deliverables:
- Alembic migration
- backfill script with dry-run and summary output
- tests for backfilling historical runs with and without readable `final.json`

Exit criteria:
- migration tests pass
- backfill is resumable and idempotent

## 6.4 Phase 3 — Bootstrap And Workspace Skill Sync

Scope:
- replace singular workspace skill assumptions with multi-skill sync
- update workspace verification logic
- bump bootstrap version

Deliverables:
- updated `shared/workspace.py`
- updated `shared/config.py`
- updated bootstrap script behavior
- bootstrap tests covering all required skill files

Exit criteria:
- worker startup checks pass with the new workspace layout
- bootstrap remains idempotent

## 6.5 Phase 4 — Worker Pipeline Cutover

Scope:
- add `worker/prompt_renderer.py`
- add `worker/artifacts.py`
- add `worker/step_runner.py`
- add `worker/pipeline.py`
- cut the runtime over from one step to `router -> specialist`
- keep ticket outcome logic in `worker/triage.py`

Rules:
- no generic workflow engine
- no recursive reroute logic
- class mismatch fails safe

Deliverables:
- new step-aware execution path
- nested step artifact directories for new runs
- run-level final structured output persistence

Exit criteria:
- end-to-end worker tests pass for:
  - router success
  - specialist success
  - router timeout
  - specialist timeout
  - malformed router output
  - malformed specialist output
  - class mismatch downgrade
  - unchanged-input skip path

## 6.6 Phase 5 — Web And Ops UI Cutover

Scope:
- stop reading `final.json` from disk in web handlers
- show final output from `ai_runs.final_output_json`
- show step-level artifact metadata from `ai_run_steps`

Deliverables:
- updated `app/routes_ops.py`
- updated templates
- updated ops workflow tests

Exit criteria:
- ops detail renders accepted summaries from DB
- backfilled historical runs render without exceptions

## 6.7 Phase 6 — Legacy Removal

Scope:
- stop writing legacy run-level artifact columns
- remove hardcoded prompt/schema constants
- drop unused legacy columns in a final migration
- remove disk-read fallback paths from the web layer

Deliverables:
- cleanup migration
- final simplified codepaths
- updated README and operator docs

Exit criteria:
- no production path depends on legacy columns or the hardcoded triage prompt constant
- all tests pass after legacy removal

---

## 7. Regression Prevention And Verification Strategy

The migration MUST be protected by targeted tests, not just broad smoke tests.

### 7.1 Golden Tests

- golden prompt render for router
- golden prompt render for default specialist
- generated schema snapshots for `RouterResult` and `TriageResult`

### 7.2 Worker Unit Tests

- agent spec loading
- prompt rendering with expected placeholders
- missing placeholder failure
- step artifact creation
- Codex command construction
- per-step output validation

### 7.3 Worker Integration Tests

- router-to-specialist happy path
- router output invalid
- specialist output invalid
- router timeout
- specialist timeout
- class mismatch
- unchanged-input skip
- stale-input supersede

### 7.4 Migration Tests

- alembic upgrade on a legacy schema
- backfill script on runs with readable artifacts
- backfill script on runs with missing artifacts
- idempotent backfill re-run

### 7.5 Web Tests

- ops ticket detail uses `final_output_json`
- step artifacts render correctly
- historical runs with no parsed output degrade gracefully

### 7.6 Bootstrap Tests

- all required skills are written to workspace
- worker contract path verification checks all required skills

### 7.7 Manual Verification

Before final cleanup:
- bootstrap a fresh workspace
- run `python scripts/run_worker.py --check`
- run `python scripts/run_web.py --check`
- exercise one full ticket from new run creation through ops detail display

---

## 8. Risks And Mitigations

### Risk 1: Router misclassification sends tickets to the wrong specialist

Mitigation:
- keep routing deterministic
- keep specialists safe
- treat router/specialist mismatch as human review or failure, not silent automation

### Risk 2: Backfill silently loses historical analysis data

Mitigation:
- use an idempotent backfill script
- do not delete legacy columns until backfill is validated
- keep historical artifact files in place

### Risk 3: Prompt migration changes behavior unintentionally

Mitigation:
- golden prompt tests
- schema snapshot tests
- preserve business logic in Python

### Risk 4: Workspace bootstrap becomes brittle

Mitigation:
- derive required workspace skills directly from repository agent specs
- keep bootstrap deterministic and idempotent

### Risk 5: Complexity creeps in through premature generalization

Mitigation:
- linear pipeline only
- code-level routing map
- no DAG engine
- no prompt inheritance DSL

---

## 9. Rollout And Rollback

## 9.1 Rollout Order

1. Merge phase 1 and phase 2 code.
2. Apply schema migration.
3. Run the backfill script and inspect its summary.
4. Merge phase 3 and re-bootstrap the workspace.
5. Deploy phase 4 worker cutover.
6. Deploy phase 5 web cutover.
7. Verify smoke checks and one end-to-end ticket.
8. Only after stability, merge phase 6 cleanup and drop legacy columns.

## 9.2 Rollback Policy

Rollback MUST remain possible until phase 6:
- additive schema changes may remain in place
- legacy columns stay available
- old application code can be redeployed if the new pipeline fails

Destructive schema removal MUST happen only after:
- new worker and web codepaths are stable
- backfill has been validated
- historical run rendering is confirmed

---

## 10. Final Decisions Confirmed

The following decisions are intentional and confirmed:

1. The end-state concept is `agent_specs`, not only “prompt templates”.
2. The correct execution model is `router -> specialist`, not a single generic prompt with conditionals.
3. The correct persistence boundary is step-level, not run-level artifact paths.
4. The database, not disk, must be the source of truth for final UI-facing AI output.
5. Historical artifact files should be referenced, not relocated.
6. A backfill script is preferable to a filesystem-aware Alembic migration.
7. The first specialist mismatch policy is fail-safe downgrade, not reroute recursion.
8. Simplicity is preserved by rejecting workflow engines, plugin systems, and prompt DSLs.

---

## 11. Final Acceptance Checklist

Implementation is not complete until all items below are true:

- `agent_specs/` exists with router and specialist specs
- typed output contracts replace hardcoded schema strings
- `ai_run_steps` exists and is populated for new runs
- historical `ai_runs` are backfilled into step rows
- workspace bootstrap syncs all required skills
- worker uses `router -> specialist`
- ops UI uses `final_output_json` and `ai_run_steps`
- legacy run-level artifact fields are no longer required by runtime code
- regression suite covers new execution and historical backfill
- README and operator docs reflect the new architecture
