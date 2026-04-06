# Autoloop Raw Phase Log


---
run_id=run-20260406T173414Z-02e24904 | entry=run_state
---
workspace=/home/marcelo/code/AutoSac
pairs=plan,implement,test
request_file=/home/marcelo/code/AutoSac/.autoloop/tasks/route-target-registry-impl/runs/run-20260406T173414Z-02e24904/request.md
session_mode=persistent


---
run_id=run-20260406T173439Z-51f2b380 | entry=run_state
---
workspace=/home/marcelo/code/AutoSac
pairs=plan,implement,test
request_file=/home/marcelo/code/AutoSac/.autoloop/tasks/route-target-registry-impl/runs/run-20260406T173439Z-51f2b380/request.md
session_mode=persistent


---
run_id=run-20260406T173439Z-51f2b380 | entry=session_turn | pair=plan | phase=producer | cycle=1 | attempt=1 | thread_id=019d63dc-5d79-7db3-b4da-0fb84071cab9
---
provider=codex
mode=start
template=/home/marcelo/code/AutoSac/.venv/lib/python3.10/site-packages/autoloop/templates/plan_producer.md
session_id=019d63dc-5d79-7db3-b4da-0fb84071cab9


---
run_id=run-20260406T173439Z-51f2b380 | entry=phase_output | pair=plan | phase=producer | process=codex-agent | cycle=1 | attempt=1 | thread_id=019d63dc-5d79-7db3-b4da-0fb84071cab9
---
I’m treating the PRD/ARD as the implementation contract and will first verify whether the raw log adds any clarifications, then map the spec onto the current code so the plan stays implementation-ready and scoped.

I’ve confirmed there are no later clarifications in the raw log. Next I’m tracing the exact code surfaces the spec calls out so the plan covers migration, compatibility, and regression containment rather than just the obvious schema swaps.

The spec goes beyond a simple rename: it introduces a registry, a selector step, a synthesized human handoff contract, a bounded dual-write migration, and an ops compatibility adapter. I’m reading the current worker and ops paths against those requirements now so the phases line up with actual dependency edges.

I’ve confirmed the current worker still routes through the legacy `triage` action model, including the internal-requester auto-publish shortcut the spec explicitly removes. I’m reading the remaining persistence and bootstrap code now so the plan can call out exactly where the migration boundary and compatibility shim belong.

I’ve confirmed the migration will touch more than the worker: the workspace bootstrap still hardcodes the old taxonomy and the ops routes/templates still read `ticket_class` directly. I’m checking the remaining integration points now, especially manifests, readiness checks, and any presentation helpers, so the plan doesn’t miss a late-stage breakage surface.

The ops detail page currently assumes legacy fields like `summary_short`, `ai_confidence`, `impact_level`, and `development_needed`; that confirms the compatibility adapter is not optional if we want historical runs to stay readable during cutover. I’m checking the check scripts and the rest of the ops presenter logic now so the plan can pin that adapter to one place.

I have enough codebase context to draft the plan structure, but I’m checking one internal precedent first so the artifact format matches the repository’s existing autoloop conventions instead of inventing a new layout.

Plan shape is clear now. I’m writing it as four ordered slices: registry and contracts first, runtime and publication policy second, additive migration plus UI cutover third, and cleanup/verification last so the mandatory dual-write and historical-compatibility boundaries stay explicit.

One more dependency edge surfaced while drafting: the current specialist folders use hyphenated spec IDs like `access-config` and `data-ops`, while the existing ticket taxonomy is underscore-based. I’m making that mapping explicit in the plan so the registry design does not accidentally create a second alias layer or break the required backfill path.

I’m updating the autoloop artifacts now: `plan.md` will become the single implementation contract for milestones, interfaces, compatibility, and risks; `phase_plan.yaml` will mirror that ordering for runtime; `feedback.md` and `decisions.txt` will only capture the non-obvious planning deltas from this turn.

The plan artifacts are written. I’m doing a consistency pass now against the spec and the planner rules, mainly to verify the ordered phases, rollback boundaries, and the decisions block stayed append-only under the current turn header.

I found one sequencing issue in the first draft: I had placed additive migration after the runtime refactor, but the spec’s mandatory cutover path requires `route_target_id` and selector persistence support before the new worker path can write against them. I’m correcting the milestone order now so migration/backfill/dual-write precede the runtime write-path switch.

The sequencing fix is in. I’m doing one final read for internal consistency, especially that the milestone text, phase dependencies, and the rollout note all now agree on migration preceding runtime cutover.

Updated the planner artifacts in [plan.md](/home/marcelo/code/AutoSac/.autoloop/tasks/route-target-registry-impl/plan/plan.md), [phase_plan.yaml](/home/marcelo/code/AutoSac/.autoloop/tasks/route-target-registry-impl/plan/phase_plan.yaml), [feedback.md](/home/marcelo/code/AutoSac/.autoloop/tasks/route-target-registry-impl/plan/feedback.md), and [decisions.txt](/home/marcelo/code/AutoSac/.autoloop/tasks/route-target-registry-impl/decisions.txt). I did not modify [criteria.md](/home/marcelo/code/AutoSac/.autoloop/tasks/route-target-registry-impl/plan/criteria.md).

The plan is now implementation-ready and aligned to the spec’s required sequencing: `registry/contracts` -> `additive migration + backfill + dual-write` -> `runtime routing/publication + ops cutover` -> `cleanup`. It explicitly locks the key non-obvious choices needed to avoid drift: preserve the current persisted class IDs as the initial direct-AI `route_target_id` set for lossless backfill, keep specialist IDs aligned to existing spec IDs, and confine legacy `triage_result` compatibility to ops presentation only.

I re-read the written artifacts for consistency after fixing the migration/runtime ordering issue. No clarifying question was necessary from the current authoritative inputs.


---
run_id=run-20260406T173439Z-51f2b380 | entry=session_turn | pair=plan | phase=verifier | cycle=1 | attempt=1 | thread_id=019d63dc-5d79-7db3-b4da-0fb84071cab9
---
provider=codex
mode=resume
template=/home/marcelo/code/AutoSac/.venv/lib/python3.10/site-packages/autoloop/templates/plan_verifier.md
session_id=019d63dc-5d79-7db3-b4da-0fb84071cab9


---
run_id=run-20260406T173439Z-51f2b380 | entry=phase_output | pair=plan | phase=verifier | process=codex-agent | cycle=1 | attempt=1 | thread_id=019d63dc-5d79-7db3-b4da-0fb84071cab9
---
I’m verifying the generated plan against the authoritative spec, raw log, and shared decisions first, then I’ll update the criteria and append only the findings that materially affect implementability or safety.

I found two material issues: one is a behavioral-safety ambiguity in the proposed initial taxonomy, and the other is an unresolved compatibility failure in the dual-write phase for new route-target IDs. I’m recording those as blocking findings and updating the verification checklist accordingly.

The findings and checklist are recorded. I’m doing a quick final read of the verifier-owned files now, then I’ll return the required summary plus the canonical loop-control status.

Scope considered

- Verified the active planning artifacts against the authoritative request, raw log, spec, and shared decisions.
- Broadened review only far enough to confirm the plan’s migration and behavior assumptions against the spec’s route-target, human-assist, and dual-write requirements.

What I analyzed

- The authoritative spec sections covering registry shape, human-assist behavior, publication policy, persistence changes, migration order, ops compatibility, and acceptance criteria.
- The produced [plan.md](/home/marcelo/code/AutoSac/.autoloop/tasks/route-target-registry-impl/plan/plan.md), [phase_plan.yaml](/home/marcelo/code/AutoSac/.autoloop/tasks/route-target-registry-impl/plan/phase_plan.yaml), [feedback.md](/home/marcelo/code/AutoSac/.autoloop/tasks/route-target-registry-impl/plan/feedback.md), [criteria.md](/home/marcelo/code/AutoSac/.autoloop/tasks/route-target-registry-impl/plan/criteria.md), and [decisions.txt](/home/marcelo/code/AutoSac/.autoloop/tasks/route-target-registry-impl/decisions.txt).

What I reviewed

- Phase ordering, dependency coherence, rollout/rollback coverage, and migration safety.
- Whether the plan silently changed product behavior or left compatibility gaps unresolved.
- Whether the shared decisions conflict with or silently extend the spec in risky ways.

Key findings / decisions

- Updated [criteria.md](/home/marcelo/code/AutoSac/.autoloop/tasks/route-target-registry-impl/plan/criteria.md) with all boxes left unchecked because there are blocking issues.
- Appended two blocking findings to [feedback.md](/home/marcelo/code/AutoSac/.autoloop/tasks/route-target-registry-impl/plan/feedback.md):
- `PLAN-001`: the plan locks `unknown` as an enabled new-run `direct_ai` target while also introducing `manual_review` for ambiguous/high-risk human escalation, creating overlapping taxonomy semantics and a material behavioral-safety risk.
- `PLAN-002`: the plan requires the mandatory dual-write phase but does not define how new route-target IDs like `manual_review` can be written to legacy `ticket_class` while the old DB constraint still exists.

Open issues / next step

- The producer needs to revise the plan so the initial enabled taxonomy does not silently preserve unsafe overlap between `unknown` and human-assist routing.
- The producer also needs to add an explicit compatibility strategy for dual-writing non-legacy route-target IDs during the constrained `ticket_class` phase.
- Verification remains incomplete until those blocking issues are resolved in the planning artifacts.

<loop-control>
{"schema":"docloop.loop_control/v1","kind":"promise","promise":"INCOMPLETE"}
</loop-control>


---
run_id=run-20260406T173439Z-51f2b380 | entry=session_turn | pair=plan | phase=producer | cycle=2 | attempt=1 | thread_id=019d63dc-5d79-7db3-b4da-0fb84071cab9
---
provider=codex
mode=resume
template=/home/marcelo/code/AutoSac/.venv/lib/python3.10/site-packages/autoloop/templates/plan_producer.md
session_id=019d63dc-5d79-7db3-b4da-0fb84071cab9


---
run_id=run-20260406T173439Z-51f2b380 | entry=phase_output | pair=plan | phase=producer | process=codex-agent | cycle=2 | attempt=1 | thread_id=019d63dc-5d79-7db3-b4da-0fb84071cab9
---
I’m addressing the verifier’s two blocking findings directly: first by removing the unsafe `unknown`/`manual_review` overlap from the final-state taxonomy plan, and second by making the dual-write compatibility window explicit so new route-target IDs do not hit the legacy `ticket_class` constraint.

I have a safe way to close both findings without changing user intent: keep the legacy class-aligned targets only for the compatibility window, prevent any non-legacy route-target ID from being emitted while `ticket_class` dual-write is still constrained, then flip to the final-state taxonomy by enabling `manual_review` and disabling `unknown` for new runs after the read/write cutover.

The revised plan now distinguishes compatibility-state from final-state taxonomy and makes the constrained dual-write rule explicit. I’m reading the updated artifacts once more to ensure the producer fix is internally consistent before I hand it back to the verifier.

Updated [plan.md](/home/marcelo/code/AutoSac/.autoloop/tasks/route-target-registry-impl/plan/plan.md), [phase_plan.yaml](/home/marcelo/code/AutoSac/.autoloop/tasks/route-target-registry-impl/plan/phase_plan.yaml), [feedback.md](/home/marcelo/code/AutoSac/.autoloop/tasks/route-target-registry-impl/plan/feedback.md), and the current turn block in [decisions.txt](/home/marcelo/code/AutoSac/.autoloop/tasks/route-target-registry-impl/decisions.txt). I did not modify [criteria.md](/home/marcelo/code/AutoSac/.autoloop/tasks/route-target-registry-impl/plan/criteria.md).

The revision resolves the verifier’s two blocking issues by making the taxonomy stateful across the migration: during dual-write, only route targets with a valid `ticket_class` shadow stay enabled for new runs, so `manual_review` remains registry-defined but disabled; after `route_target_id` becomes the active read path and legacy writes stop, `manual_review` becomes the human-assist target and `unknown` is retired from new-run routing but kept loadable for historical display. The plan and machine-readable phases now both spell out that compatibility rule, the cutover point, and the final no-overlap taxonomy state.


---
run_id=run-20260406T173439Z-51f2b380 | entry=session_turn | pair=plan | phase=verifier | cycle=2 | attempt=1 | thread_id=019d63dc-5d79-7db3-b4da-0fb84071cab9
---
provider=codex
mode=resume
template=/home/marcelo/code/AutoSac/.venv/lib/python3.10/site-packages/autoloop/templates/plan_verifier.md
session_id=019d63dc-5d79-7db3-b4da-0fb84071cab9


---
run_id=run-20260406T173439Z-51f2b380 | entry=phase_output | pair=plan | phase=verifier | process=codex-agent | cycle=2 | attempt=1 | thread_id=019d63dc-5d79-7db3-b4da-0fb84071cab9
---
I’m re-checking the revised planning artifacts against the same verifier concerns, with focus on whether the compatibility-window taxonomy and dual-write rules now close the earlier safety and migration gaps.

The revised plan now closes the two earlier gaps: it prevents constrained dual-write failures and removes the final-state overlap between `unknown` and `manual_review`. I’m marking the criteria accordingly and appending a closure note so the feedback history shows why this verifier pass is complete.

The verifier-owned files are updated. I’m doing one last read to confirm the checklist is fully checked and the feedback history now records both the original blockers and their closure in this pass.

Scope considered

- Re-verified the active planning artifacts against the authoritative request, raw log, spec, and shared decisions.
- Focused on the revised areas that previously caused blocking findings: taxonomy overlap and dual-write compatibility.

What I analyzed

- The updated [plan.md](/home/marcelo/code/AutoSac/.autoloop/tasks/route-target-registry-impl/plan/plan.md) sections covering initial registry state, compatibility boundaries, milestones, rollout, regression controls, and risk register.
- The updated [phase_plan.yaml](/home/marcelo/code/AutoSac/.autoloop/tasks/route-target-registry-impl/plan/phase_plan.yaml) phase boundaries, dependencies, acceptance criteria, and rollback notes.
- The continuity between the revised plan and [decisions.txt](/home/marcelo/code/AutoSac/.autoloop/tasks/route-target-registry-impl/decisions.txt).

What I reviewed

- Whether the plan now prevents `unknown`/`manual_review` overlap in the final enabled taxonomy.
- Whether the dual-write phase now has an explicit safe rule for non-legacy route-target IDs while the old `ticket_class` constraint still exists.
- Whether criteria and feedback history accurately reflect closure of the earlier blocking issues.

Key findings / decisions

- Updated [criteria.md](/home/marcelo/code/AutoSac/.autoloop/tasks/route-target-registry-impl/plan/criteria.md) so all verification checkboxes are now checked.
- Appended a non-blocking closure note to [feedback.md](/home/marcelo/code/AutoSac/.autoloop/tasks/route-target-registry-impl/plan/feedback.md) confirming that the prior blocking findings are addressed.
- The revised plan now explicitly separates compatibility-phase versus final-state enablement, keeps `manual_review` disabled during constrained dual-write, and retires `unknown` from new-run routing after the `route_target_id` cutover while preserving it for historical display.
- No remaining blocking findings were identified in this verifier pass.

Open issues / next step

- No blocking issues remain in the planning artifacts.
- The plan pair is ready to advance from verification.

<loop-control>
{"schema":"docloop.loop_control/v1","kind":"promise","promise":"COMPLETE"}
</loop-control>


---
run_id=run-20260406T173439Z-51f2b380 | entry=phase_scope_resolved
---
phase_mode=single
phase_ids=registry-contract-foundation,additive-migration-foundation,runtime-routing-ops-cutover,cleanup-verification
explicit=True
current_phase_index=0


---
run_id=run-20260406T173439Z-51f2b380 | entry=session_turn | pair=implement | phase=producer | cycle=1 | attempt=1 | thread_id=019d63e5-b1c5-7942-a40b-1d7f1d8cb09d
---
provider=codex
mode=start
template=/home/marcelo/code/AutoSac/.venv/lib/python3.10/site-packages/autoloop/templates/implement_producer.md
session_id=019d63e5-b1c5-7942-a40b-1d7f1d8cb09d


---
run_id=run-20260406T173439Z-51f2b380 | entry=phase_output | pair=implement | phase=producer | process=codex-agent | cycle=1 | attempt=1 | thread_id=019d63e5-b1c5-7942-a40b-1d7f1d8cb09d
---
I’m pulling the phase contract, spec, and current routing/prompt/bootstrap code first so I can map the required changes before editing. Then I’ll implement only the registry-contract foundation scope and verify the affected checks and tests.

I’ve confirmed the planning context and phase boundaries. Next I’m tracing the current registry-adjacent code paths and the already-added untracked files so I can implement this phase on top of the existing branch state instead of fighting it.

The current branch already has the agent-spec and worker pipeline scaffolding in place, but the taxonomy is still hardcoded exactly where the phase says it should stop being hardcoded. I’m reading the prompt templates, workspace contract text, and validation entry points now so the edits line up with the existing partial refactor.

The code confirms the current mismatch: prompt text, workspace instructions, and runtime schemas still embed the legacy taxonomy. I’m moving to the contract section of the spec now to mirror the exact registry shape and validation rules instead of inventing them from the plan summary.

I have the exact registry and contract rules now. I’m pulling the later persistence and readiness sections plus the current tests so the implementation lands on the right interfaces and I can add only the phase-local coverage the spec expects.

I’m checking the remaining runtime touch points before editing. The key question is how far to move the live pipeline in this phase without violating the explicit “no persistence behavior change yet” boundary, so I’m reading the current triage code and test expectations around contract usage.

I’ve resolved the likely blast radius. The safest foundation pass is: make router and prompt rendering registry-driven now, add selector support and validations, keep the current specialist persistence path intact for this phase, and wire registry integrity into bootstrap/readiness so startup fails early on bad topology.

Plan for this phase:
1. Add the registry foundation: `agent_specs/registry.json`, selector spec folder, and `shared/routing_registry.py` with strict topology/spec validation and typed accessors.
2. Update shared/runtime scaffolding: allow selector manifests, wire registry validation into workspace/bootstrap/readiness, and add contract models plus validation hooks for registry-driven IDs.
3. Refactor prompt rendering and prompt templates to use generated route-target/specialist catalogs and route-target placeholders without hardcoded taxonomy text.
4. Adjust the current pipeline just enough to consume the new router contract through the registry while keeping the legacy specialist persistence path unchanged for this phase.
5. Add targeted tests for registry validation, prompt rendering, startup/workspace integrity, and the minimal router-path compatibility surface, then update implementation notes and decisions.

I’m making the foundation edits now: adding the registry and selector spec, then replacing the taxonomy-bound prompt/skill text with registry-driven placeholders while keeping the current specialist write-path in place for this phase.

The registry assets are in. I’m wiring the typed loader and the shared scaffolding now so prompts, startup checks, and router-path validation all resolve through one canonical registry instead of literals spread across modules.

I found two schema-shape issues while wiring the loader: some registry fields are optional by spec, and the validator needs to allow them without weakening the fail-fast behavior. I’m tightening that now before I hook the runtime-side contract validation on top of it.

I’m replacing the contract module next. The new models will validate registry-backed `route_target_id` and `specialist_id` values, but I’m keeping `triage_result` available as a legacy model so this phase doesn’t force the later publication/persistence refactor in the same diff.

I’m on the prompt/runtime bridge now. This is the part that makes the registry matter operationally in this phase: generated catalogs, route-target handoff placeholders, and the current router path resolving specialists through the registry instead of the hardcoded map.

The main plumbing is in place. I’m doing a read pass on the new loader and prompt formatter now to catch any schema/typing mistakes before I move on to tests and the remaining contract updates.

The core code compiles. I’m updating the targeted tests now: first the existing worker prompt/router helpers so they match the new router contract, then I’ll add dedicated registry and prompt-rendering coverage for the new phase behavior.

I’m adding the phase-local coverage now: strict registry validation cases, prompt rendering off the generated catalogs, contract checks for the new registry-backed IDs, and a readiness-path test that proves a broken registry stops startup.

I’ve reached verification. I’m running the new registry/prompt tests plus the existing worker/hardening slices they touch so I can catch any contract drift before updating the phase artifacts.

The first test pass surfaced a few failures rather than a broad breakage, which is good. I’m pulling the exact failing assertions now and will patch those instead of widening the change set.

One failure is real code-path wrapping, one is a prompt assertion mismatch, and the rest are environmental: the web stack can’t import because `python-multipart` isn’t installed here. I’m checking whether that package is supposed to be part of the repo before I decide between a repo fix and a test-only skip.

`python-multipart` is already declared in `requirements.txt`, so the remaining web-script failures are just this environment missing a required runtime dependency. I’m installing that declared package rather than weakening the tests.

The remaining repo-side fixes are small: wrap registry validation errors as contract errors, align the selector prompt wording, and update the bootstrap-version assertions to the new workspace contract version. After that I’ll rerun the same slice under the repo venv so the subprocess checks inherit the declared dependencies.

I’ve patched the real failures. I’m rerunning the same slice under the repo venv now so the script subprocesses use the declared dependency set instead of the host Python.

The rerun is clean so far. I’m waiting for the final test exit and then I’ll update the phase notes and decision ledger with the exact scope, non-changes, and validation performed.

I’ve finished code and validation. I’m writing the phase artifacts now: concise implementation notes for the next turn, plus the few non-obvious decisions that would be easy to drift from if they were left implicit.

I’m doing one last diff scan for just the files touched in this phase so the close-out reflects the actual blast radius and I don’t accidentally summarize unrelated branch work.

Scope considered

- Phase-local foundation only: registry seed, selector spec support, registry validation, registry-driven prompt scaffolding, and workspace/readiness checks.
- I kept selector runtime orchestration, publication policy, `route_target_id` persistence migration, and ops/UI cutover out of scope.

What I analyzed

- The contract in [Autosac_Route_Target_Registry_PRD_ARD.md](/home/marcelo/code/AutoSac/Autosac_Route_Target_Registry_PRD_ARD.md), the active phase artifacts, and the current agent-spec, prompt, workspace, and worker paths.
- The existing router/specialist execution flow in [worker/pipeline.py](/home/marcelo/code/AutoSac/worker/pipeline.py), prompt rendering in [worker/prompt_renderer.py](/home/marcelo/code/AutoSac/worker/prompt_renderer.py), contract validation in [worker/output_contracts.py](/home/marcelo/code/AutoSac/worker/output_contracts.py), and bootstrap/readiness checks in [shared/workspace.py](/home/marcelo/code/AutoSac/shared/workspace.py).

What I changed

- Added the canonical registry in [agent_specs/registry.json](/home/marcelo/code/AutoSac/agent_specs/registry.json) and the selector spec in [agent_specs/specialist-selector/manifest.json](/home/marcelo/code/AutoSac/agent_specs/specialist-selector/manifest.json).
- Added typed registry loading/validation in [shared/routing_registry.py](/home/marcelo/code/AutoSac/shared/routing_registry.py) and extended [shared/agent_specs.py](/home/marcelo/code/AutoSac/shared/agent_specs.py) to allow `kind="selector"`.
- Made router/selector/specialist prompt rendering registry-driven in [worker/prompt_renderer.py](/home/marcelo/code/AutoSac/worker/prompt_renderer.py) and updated the prompt/skill text under [agent_specs/router/prompt.md](/home/marcelo/code/AutoSac/agent_specs/router/prompt.md) and [agent_specs/support/prompt.md](/home/marcelo/code/AutoSac/agent_specs/support/prompt.md).
- Replaced router/selector/foundation contract definitions and registry-backed validation in [worker/output_contracts.py](/home/marcelo/code/AutoSac/worker/output_contracts.py), and wired the current compatibility router path through the registry in [worker/pipeline.py](/home/marcelo/code/AutoSac/worker/pipeline.py) and [worker/step_runner.py](/home/marcelo/code/AutoSac/worker/step_runner.py).
- Bumped the workspace contract to `stage1-v3` in [shared/contracts.py](/home/marcelo/code/AutoSac/shared/contracts.py), made [shared/workspace.py](/home/marcelo/code/AutoSac/shared/workspace.py) fail fast on bad registry state, and updated the phase artifacts in [implementation_notes.md](/home/marcelo/code/AutoSac/.autoloop/tasks/route-target-registry-impl/implement/phases/registry-contract-foundation/implementation_notes.md) and [decisions.txt](/home/marcelo/code/AutoSac/.autoloop/tasks/route-target-registry-impl/decisions.txt).
- Added targeted coverage in [tests/test_routing_registry.py](/home/marcelo/code/AutoSac/tests/test_routing_registry.py) and updated affected expectations in [tests/test_ai_worker.py](/home/marcelo/code/AutoSac/tests/test_ai_worker.py), [tests/test_foundation_persistence.py](/home/marcelo/code/AutoSac/tests/test_foundation_persistence.py), and [tests/test_hardening_validation.py](/home/marcelo/code/AutoSac/tests/test_hardening_validation.py).

Key findings / decisions

- I intentionally kept specialist runtime execution on legacy `triage_result` in this phase so persistence/publication behavior does not move ahead of the later runtime-cutover phase.
- The seed registry includes `manual_review` but keeps it disabled for new runs, and marks `unknown` as not human-assist-eligible for future auto-candidate expansion.
- Validation passed with `.venv/bin/pytest -q tests/test_routing_registry.py tests/test_ai_worker.py tests/test_hardening_validation.py tests/test_foundation_persistence.py` with `81 passed`.

Open issues / next step

- The next phase can build the additive migration and dual-write foundation on top of this registry layer, then move runtime selector/human-assist execution and publication policy onto the new contracts.
