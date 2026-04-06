# Route-Target Registry Implementation Plan

## Scope Considered

- Authoritative source: `Autosac_Route_Target_Registry_PRD_ARD.md`; no later clarifications were appended to the run raw log.
- Baseline: current Stage 1 pipeline with hardcoded `ticket_class`, router-to-specialist constant mapping, legacy `triage_result`, workspace taxonomy text, and ops screens that read ticket class directly.
- Out of scope unless explicitly required by the spec: generic plugin workflows, UI-authored routing rules, runtime legacy worker fallbacks, prompt storage redesign, or non-Stage-1 permission expansion.

## Baseline Delta Summary

- `ticket_class` is embedded in SQLAlchemy constraints, Alembic history, worker contracts, prompt placeholders, ops filters, and test fixtures.
- `shared/agent_specs.py` still owns routing taxonomy through `specialist_agent_map()` and only permits `router` and `specialist` spec kinds.
- `worker/pipeline.py` and `worker/triage.py` still implement the legacy two-classification flow, including router/specialist mismatch downgrade and the internal-requester auto-publish shortcut that the spec removes.
- Ops detail rendering assumes legacy output fields such as `summary_short`, `ai_confidence`, `impact_level`, and `development_needed`, so historical continuity requires a bounded presentation adapter at the UI layer.

## Locked Implementation Shape

### Initial registry seed and IDs

- Seed `agent_specs/registry.json` with the current persisted taxonomy IDs (`support`, `access_config`, `data_ops`, `bug`, `feature`, `unknown`) so backfill to `route_target_id` remains lossless, but treat that as compatibility state rather than the final enabled taxonomy.
- Add `manual_review` as the human-assist route target with `handler.human_queue_status = "waiting_on_dev_ti"`.
- During the compatibility phase while `ticket_class` is still dual-written and constrained, only route targets with a 1:1 legacy `ticket_class` representation may remain enabled for new runs; `manual_review` stays present in the registry but disabled for new-run routing until `route_target_id` is the active read path and legacy `ticket_class` writes stop.
- After route-target cutover, enable `manual_review` for new runs and set legacy-overlap target `unknown` to `enabled=false` so it remains loadable for historical display but is no longer available to the router or selector.
- Use current agent-spec IDs as registry specialist IDs where possible (`support`, `bug`, `feature`, `unknown`, `access-config`, `data-ops`) so the registry does not introduce a second alias namespace; fixed route-target handlers map underscore-based route-target IDs to those existing spec IDs.
- Keep `human_assist + specialist_selection.mode=none` as a supported runtime path even if the cutover registry exercises `manual_review` through `mode=auto`; tests must cover `none`, `fixed`, and `auto`.

### Interface definitions

- Add `shared/routing_registry.py` as the only registry loader/validator. It owns JSON parsing, cross-reference validation, enabled-vs-historical filtering, candidate-specialist resolution, and typed accessors for route targets, specialists, router spec, selector spec, and ops-visible targets.
- Extend `shared/agent_specs.py` only enough to allow `kind="selector"` and to keep per-spec manifest loading isolated from registry policy.
- Replace runtime execution contracts in `worker/output_contracts.py` with `router_result`, `specialist_selector_result`, `specialist_result`, and `human_handoff_result`; keep `triage_result` only as a legacy read model if the ops adapter needs it.
- Replace `apply_ai_classification()` with `apply_ai_route_target(ticket, route_target_id, requester_language)`; during the compatibility phase this helper dual-writes legacy `ticket_class`, but new runtime behavior stops writing `ai_confidence`, `impact_level`, and `development_needed`.
- Add `worker/publication_policy.py` to compute deterministic effective publication mode from route-target publish policy plus specialist output enums.
- Add an ops-only presenter/adapter module, preferably `app/ai_run_presenters.py`, that normalizes legacy `triage_result`, new `specialist_result`, and `human_handoff_result` into the ticket-detail fields the templates need.

### Prompt and workspace contract changes

- Router prompt rendering becomes catalog-driven via `{ROUTE_TARGET_CATALOG}` and lists enabled route targets only.
- Add `agent_specs/specialist-selector/` and support selector prompt rendering via route-target context plus `{SPECIALIST_CANDIDATE_CATALOG}`.
- Specialist prompts receive route-target context placeholders (`ROUTE_TARGET_ID`, `ROUTE_TARGET_LABEL`, `ROUTE_TARGET_KIND`, `ROUTE_TARGET_ROUTER_DESCRIPTION`, `ROUTER_RATIONALE`) and must not mention router confidence, specialist reclassification, or the legacy `TARGET_TICKET_CLASS` placeholder.
- Update `shared/contracts.py` and `shared/workspace.py` so `AGENTS.md` retains only Stage 1 guardrails, removes taxonomy/schema-specific instructions, and bumps `WORKSPACE_BOOTSTRAP_VERSION`.

### Persistence and compatibility boundaries

- Follow the spec’s mandatory additive-then-cutover sequence: add `tickets.route_target_id`, backfill from `ticket_class`, dual-write for one compatibility phase, switch reads/UI, then stop writing `ticket_class`, and only then drop the legacy constraint and column in cleanup migrations.
- Make the dual-write rule explicit: while the legacy `ticket_class` constraint still exists, the router MUST NOT emit any new-run `route_target_id` that lacks a valid 1:1 legacy `ticket_class` representation. Non-legacy targets such as `manual_review` stay registry-defined but disabled until route-target reads are active and legacy writes stop.
- Do not add any DB enum/check constraint for route-target IDs.
- Extend `AI_RUN_STEP_KINDS` and the step-kind DB constraint to include `selector`.
- Historical continuity is presentation-only: legacy runs stay readable via the ops adapter keyed by `final_output_contract` or `pipeline_version`; the worker must not gain new runtime fallback paths for legacy payloads.
- Accepted-run invariant remains strict for all new flows: every `succeeded` or `human_review` run persists non-null `final_output_contract` and `final_output_json`, including synthesized `human_handoff_result`.

## Milestones

### Milestone 1: Registry, selector spec, and contract foundation

- Add `agent_specs/registry.json` and `agent_specs/specialist-selector/`.
- Implement `shared/routing_registry.py` validation rules from the spec, including enabled/historical semantics, specialist-candidate resolution, and the compatibility distinction between historical-only versus new-run-enabled targets.
- Extend spec loading for `kind="selector"` without moving routing policy into `shared/agent_specs.py`.
- Replace contract definitions and prompt-rendering scaffolding so router, selector, and specialist prompts can render registry-driven catalogs and route-target placeholders.
- Update workspace bootstrap content/version and readiness/smoke checks so registry integrity is validated by `/readyz`, `scripts/run_web.py --check`, and `scripts/run_worker.py --check`.

Exit criteria:

- Registry load fails fast on malformed topology, missing specs, invalid selection modes, invalid publish policy, and disabled-target misuse.
- Prompt rendering no longer depends on hardcoded taxonomy literals or `TARGET_TICKET_CLASS`/`ROUTER_TICKET_CLASS`.
- Workspace bootstrap includes selector skills and no longer hardcodes the old taxonomy or legacy large-triage schema.

### Milestone 2: Additive migration, backfill, and compatibility write path

- Update models and Alembic migrations to add `route_target_id`, backfill from `ticket_class`, extend step-kind constraints for `selector`, and introduce bounded dual-write.
- Replace ticket-write helpers so new runtime code has a compatibility-safe place to write `route_target_id` while the legacy column still exists.
- Keep any route target without a valid legacy `ticket_class` shadow value disabled for new runs throughout the dual-write window so the existing DB constraint is never violated and legacy read paths are never mislabeled.
- Keep reads and UI on the pre-cutover path until the new worker behavior is ready, but ensure migration tests prove backfill and selector-step persistence before the runtime switch lands.

Exit criteria:

- `route_target_id` exists, is backfilled from `ticket_class`, and is written alongside the legacy column during the compatibility phase.
- `AI_RUN_STEP_KINDS` and the step-kind DB constraint admit `selector`.
- The repository has no DB constraint that hardcodes route-target taxonomy, and no non-legacy route target is emitted while the legacy `ticket_class` constraint still governs writes.

### Milestone 3: Runtime routing, publication policy, ops cutover, and bounded legacy presentation

- Refactor `worker/pipeline.py` into router -> optional selector -> optional specialist orchestration driven by the routing registry.
- Remove mismatch handling entirely; specialists no longer emit route-target data.
- Add `worker/publication_policy.py` and move publish/draft/manual-only resolution to named-enum comparison logic.
- Refactor `worker/triage.py` to apply route-target policy, synthesize `human_handoff_result` for `human_assist + none`, always finalize accepted runs with terminal structured output, and remove the internal-requester shortcut behavior.
- Update `worker/step_runner.py` and `worker/artifacts.py` so step manifests and `run_manifest.json` include route-target, selector, specialist, and effective-publication metadata.
- Switch ticket reads, ops filters, labels, and templates to `route_target_id` plus registry-derived labels/kinds.
- After route-target reads are active and `ticket_class` writes stop, enable `manual_review` for new runs and mark `unknown` historical-only (`enabled=false`) so ambiguous/high-risk cases no longer share overlapping new-run taxonomy.
- Add the ops presentation adapter so ticket detail renders both legacy `triage_result` and new contracts without touching runtime worker behavior.
- Ensure unknown historical route-target IDs display raw IDs instead of crashing.

Exit criteria:

- Direct-AI targets support `fixed` and `auto` specialist selection.
- Human-assist targets support `none`, `fixed`, and `auto`; `auto_publish` is impossible for all of them.
- New runtime reads come from `route_target_id`; `ticket_class` remains compatibility-only until cleanup.
- The final enabled new-run taxonomy has no overlap between human-escalation handling and legacy `unknown` routing; `unknown` remains available only for historical display/backfilled records.
- Historical tickets and accepted runs remain visible in ops after the cutover.

### Milestone 4: Cleanup, legacy write removal, and full verification

- Stop writing `ticket_class` and remove obsolete runtime helpers and literals: `specialist_agent_map()`, `resolve_specialist_spec_id(ticket_class)`, mismatch text, legacy prompt placeholders, and direct ops dependencies on `ticket_class`.
- Ship the cleanup migration that drops the legacy ticket-class constraint and column only after the code and tests are green on the route-target path.
- Update worker, persistence, ops, readiness, and migration tests to the new contracts and to the accepted-run invariant.

Exit criteria:

- `ticket_class` is no longer part of the new runtime execution path.
- No business taxonomy remains hardcoded in DB constraints, prompt text, output-contract literals, or ops filter tuples.
- The regression suite and smoke checks validate registry integrity, selector step persistence, route-target presentation, and structured-output invariants.

## Compatibility, Rollout, and Rollback

- Rollout order is fixed: registry/contracts first, additive migration/backfill/dual-write second, runtime flow and UI cutover third, cleanup last.
- Dual-write is temporary and must remain confined to the migration compatibility phase; no long-lived shared write path should depend on both fields.
- During the dual-write window, keep non-legacy route targets disabled for new runs; only after route-target reads are active and legacy writes stop may the plan enable `manual_review` and retire `unknown` from new-run routing.
- Before cleanup migration lands, rollback is code-only: revert to the previous branch while leaving additive schema changes in place.
- After the cleanup migration drops `ticket_class`, rollback should be handled as a forward fix, not by reintroducing runtime fallbacks or relying on a destructive DB downgrade.

## Regression Controls

- Validate registry topology at startup and in smoke checks so broken route-target/spec relationships fail before runtime execution.
- Keep all route-target decision logic deterministic in Python: selector candidate enforcement, publish-mode downgrades, human-assist no-auto-publish, and accepted-run final-output persistence.
- Bound legacy compatibility to `app/ai_run_presenters.py` or equivalent presentation code so worker code never branches on historical payload shapes.
- Preserve baseline taxonomy continuity during backfill by keeping current ticket-class IDs as initial direct-AI route targets.
- Prevent overlapping ambiguous-case routing by treating `unknown` as compatibility-only and historical-only after cutover, with `manual_review` taking over human-escalation behavior once legacy writes stop.
- Treat missing registry metadata for historical tickets as display-only fallback: show the stored ID/raw values and keep the page functional.

## Test and Verification Plan

- Registry tests: duplicate IDs, missing spec references, invalid selection configs, disabled target/specialist handling, human-assist auto candidate expansion.
- Contract tests: valid/invalid `route_target_id`, selector candidate enforcement, specialist required-field rules, human handoff synthesis rules, publish-mode gating.
- Prompt tests: router catalog generation, selector candidate catalog generation, specialist placeholder rendering, missing-placeholder failures.
- Pipeline tests: fixed and auto direct-AI targets, human-assist `none`/`fixed`/`auto`, invalid selector choice, publication downgrades, no internal-requester shortcut.
- Persistence/migration tests: `route_target_id` backfill, `selector` step persistence, dual-write behavior, removal of class writes, no DB route-target constraint.
- Ops/readiness tests: registry-driven filters, route-target labels/kinds, unknown-ID fallback, historical contract rendering, `/readyz` and script smoke checks validating registry integrity.

## Risk Register

- R1: Backfill mismatch between underscore route-target IDs and hyphenated specialist spec IDs could corrupt fixed-handler wiring.
  Mitigation: keep route-target IDs aligned to current ticket-class values, keep specialist IDs aligned to current spec IDs, and validate fixed references in `shared/routing_registry.py`.
- R2: Historical ops detail rendering could regress when `summary_short` and related legacy fields disappear from new outputs.
  Mitigation: centralize adaptation in a presenter module keyed by `final_output_contract`/`pipeline_version`; do not let templates inspect raw contracts directly.
- R3: Publication behavior could silently widen if old confidence shortcuts or requester-internal shortcuts survive the refactor.
  Mitigation: isolate publish resolution in `worker/publication_policy.py` and add downgrade-path tests for every named enum threshold.
- R4: Cleanup migration could strand operators if `ticket_class` is dropped before all reads and tests are on `route_target_id`.
  Mitigation: keep cleanup as the final milestone only after dual-write removal, green tests, and successful smoke/readiness checks.
- R5: New human-assist route targets could violate the legacy `ticket_class` constraint or mislabel tickets if they are enabled before the dual-write window ends.
  Mitigation: keep non-legacy targets such as `manual_review` disabled until `route_target_id` is the active read path and `ticket_class` writes have stopped; then enable `manual_review` and retire `unknown` from new-run routing.
