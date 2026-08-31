# Persistent Codex conversation plan

Status: evidence-backed implementation contract  
Scope: AutoSac ticket processing and ticket UI  
Decision date: 2026-08-24  
Repository baseline: `22af53a`

## 1. Decision

AutoSac will ship the smallest safe persistent slice first:

- one logical AI conversation per ticket;
- one native Codex thread segment per logical conversation under normal operation;
- specialist turns only become persistent first;
- router and selector remain internal `AIRunStep` executions and do not append to the native ticket thread;
- every attempted, accepted, completed, failed, interrupted, timed-out, rejected, edited, unpublished, superseded, and published specialist turn remains in durable AutoSac history;
- PostgreSQL remains authoritative for ticket state, review, publication, and exact requester-visible content;
- requester routes continue to project only explicitly published `TicketMessage` rows;
- the persistent path remains disabled by default until rollout blockers are implemented and validated together.

Ordinary conversation turns are not forked. Native session replacement is a recovery path, not a publication-control mechanism.

## 2. Repository evidence summary

The current repository is still an ephemeral pipeline:

- `worker/step_runner.py` builds `codex exec --ephemeral` commands for every router, selector, and specialist step.
- `worker/step_runner.py` executes Codex through `subprocess.run(..., capture_output=True)`, then writes `stdout.jsonl` and `stderr.txt` only after the process exits.
- `worker/queue.py` claims a pending `AIRun` with `FOR UPDATE SKIP LOCKED`, but the lock exists only for the claim transaction; it does not fence the external Codex process for the full call.
- `worker/main.py` and `worker/queue.py` implement heartbeat-based stale-run recovery that can mark a run failed and enqueue a replacement while an orphaned Codex process could still be alive.
- `worker/triage.py` decides staleness and supersession from `build_requester_visible_fingerprint(...)`, which currently hashes requester-visible messages, ticket title/urgent/status, and public attachments only.
- `worker/ticket_loader.py` and `worker/prompt_renderer.py` still render prompts from current public and internal ticket messages plus public attachments, not from an ordered durable turn/outcome ledger.
- `shared/ticketing.py` persists drafts, draft rejection, supersession, internal notes, public publication, and deferred requeue, but those records are not yet replayed into a later Codex turn as ordered events.
- `app/routes_requester.py` loads only `TicketMessage.visibility == "public"`, so requester visibility is currently guarded by the existing publication path.

These facts drive the contract below.

## 3. Codex CLI 0.148.0 capability baseline

Validation date: 2026-08-24  
Validation source: installed local CLI help plus version output

Commands run:

```bash
codex --version
codex --help
codex exec --help
codex exec resume --help
```

Observed support in `codex-cli 0.148.0`:

- `codex exec resume [SESSION_ID] [PROMPT]` accepts an explicit session/thread identifier.
- `codex exec resume` also exposes `--last`, which AutoSac must not use.
- `codex exec` exposes `--sandbox read-only|workspace-write|danger-full-access`.
- `codex exec resume` does not expose `--sandbox`.
- both `codex exec` and `codex exec resume` expose `--json`, `--output-schema`, and `--output-last-message`.
- both commands accept `-c/--config`, so resume-time policy overrides must go through supported config keys, not a resume-specific sandbox flag.
- top-level `codex --help` exposes `--search`, but `codex exec --help` and `codex exec resume --help` do not expose dedicated web-search flags.

Contract implication:

- explicit resume by stored thread/session ID is supported;
- explicit resume-time sandbox enforcement is not exposed as a first-class `exec resume` flag in 0.148.0;
- the persistent path must either validate a supported resume-time config override for read-only sandbox and disabled web search, or stay disabled.

Official OpenAI documentation search on 2026-08-24 did not surface a page enumerating the `codex exec resume` flag surface more precisely than the local help output, so the local CLI is the authoritative evidence for this phase.

General Codex references:

- [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Codex app-server](https://learn.chatgpt.com/docs/app-server)

## 4. Issue classification matrix

| # | Issue | Classification | Evidence | Contract consequence |
| --- | --- | --- | --- | --- |
| 1 | Short DB row lock does not fence the native thread for full external execution | Confirmed | `worker/queue.py` only locks while claiming a pending `AIRun`; `worker/step_runner.py` runs Codex outside that transaction; `worker/queue.py` stale recovery can enqueue a replacement after heartbeat expiry | Add full-duration conversation ownership/lease tied to worker ownership and recovery |
| 2 | Thread ID must be durably persisted as soon as `thread.started` is emitted | Confirmed | `worker/step_runner.py` uses `subprocess.run(..., capture_output=True)` and writes JSONL only after exit; no current code parses streaming events | Replace capture-only execution with line-oriented streaming ingestion and early thread persistence |
| 3 | Rollback to the current aggregated ephemeral path can omit rejected, unpublished, failed, or superseded turns | Confirmed | `worker/ticket_loader.py` and `worker/prompt_renderer.py` rebuild prompts from current ticket messages and attachments, not from stored AI turns/outcomes; rejected drafts live only in `AIDraft` state | Persistent feature cannot be enabled without replay/fallback that re-injects retained turn/outcome history |
| 4 | Persistent resume must not ship before minimal append-only publication/review/supersession outcomes can be injected into the next turn | Partially confirmed | `shared/ticketing.py` persists `AIDraft` states and published `TicketMessage` rows, but there is no append-only turn outcome model and no next-turn outcome injection path | Reuse existing draft/publication objects, but add append-only persistent turn outcomes before enablement |
| 5 | Current fingerprint and enqueue behavior miss ordered input events such as operator notes, review feedback, route transitions, and relevant state changes | Confirmed | `worker/triage.py:build_requester_visible_fingerprint` hashes public messages, attachments, title, urgent flag, and current status only | Replace requester-visible fingerprint gating with ordered, idempotent input-event consumption |
| 6 | Resume sandboxing should be explicit through supported configuration rather than inherited session state | Confirmed | Local `codex exec --help` exposes `--sandbox`; `codex exec resume --help` does not. Current repo has no resume implementation to enforce the policy another way | Use validated supported config overrides for resumed turns or keep the persistent path disabled |
| 7 | Conversation cardinality and the meaning of closed/new logical conversations must be unambiguous and DB-enforced | Confirmed | Current schema has `AIRun` and `AIDraft` only; there is no logical conversation/session model | Add conversation/session tables and constraints; normal resolves/reopens stay in the same logical conversation |
| 8 | Raw `codex_turn_items` must not become an alternate requester publication channel | Partially confirmed | Current requester routes only render `TicketMessage.visibility == "public"`, so the app does not currently leak raw step artifacts; future raw turn-item storage would create a second surface unless constrained | Keep raw turn items ops-internal only and never source requester output from them |
| 9 | Retention, permissions, backup, and coordinated deletion must exist before persistent sensitive storage is rolled out beyond tests | Confirmed | Current settings cover `codex_bin`, API key, model, timeout, and worker heartbeat only; there is no persistent `CODEX_HOME`, retention, backup, or coordinated delete contract | Do not enable persistent storage beyond tests until these operational controls exist |
| 10 | Rerouting policy, recovery transcript limits, and attempted/accepted/ambiguous/completed/published semantics must be explicit | Partially confirmed | Manual rerun and stale-run recovery already exist in `shared/ticketing.py` and `worker/queue.py`, but there is no durable state model separating attempted acceptance, ambiguous submission, completion, and publication | Define these semantics explicitly in schema and worker behavior before rollout |

## 5. First shipped slice

The first shipped slice is:

- specialist-only persistent turns;
- router and selector stay on the existing internal ephemeral `AIRunStep` path;
- the persistent specialist turn becomes the only native Codex conversation surface for the ticket;
- the feature flag is `CODEX_CONVERSATIONS_ENABLED`, default `false`;
- the flag stays `false` until phases 2 through 4 are complete and validated together.

Not in the first shipped slice:

- router/selector prompts or outputs appended into the native ticket thread;
- live UI event streaming;
- app-server adoption unless the CLI cannot satisfy a rollout-blocking invariant;
- broad requester UI redesign;
- any fallback that silently discards rejected, unpublished, failed, or superseded persistent turns.

## 6. Conversation and lifecycle contract

### 6.1 Logical conversation cardinality

- Each ticket gets at most one active logical AI conversation.
- The conversation is created lazily when the first persistent specialist turn is created.
- A resolved ticket does not create a new logical conversation when it reopens.
- Manual reruns, review feedback, operator notes, and route transitions remain part of the same logical conversation.
- A new logical conversation is exceptional and requires an explicit future product action or a coordinated governance action.

### 6.2 Native session cardinality

- Normally one logical conversation maps to one native Codex thread.
- Replacement native sessions are allowed only for technical recovery, corruption, missing native state, or retention/deletion workflows.
- Rejection, edited publication, supersession, reroute, or stale input do not create a new native conversation.

### 6.3 Closed versus active

- `active`: normal ticket conversation, including resolved tickets that may later reopen.
- `recovery_required`: the logical conversation continues, but the current native session cannot safely resume.
- `unavailable`: persistent execution is intentionally disabled or operational prerequisites are missing.
- `closed`: only for explicit future new-conversation/governance flows, not for normal ticket resolution.

## 7. Data authority and visibility

### 7.1 Authority

- PostgreSQL is authoritative for ticket messages, draft review state, publication state, requester-visible bodies, and authorization.
- Native Codex sessions are authoritative only for model-side conversational memory while they remain available.
- AutoSac must mirror enough turn/session data to recover, audit, and render the lifecycle without treating Codex rollout files as the product source of truth.

### 7.2 Requester-safe projection

- Requester routes continue to render only published `TicketMessage` rows.
- Raw `codex_turn_items`, JSONL events, reasoning summaries, commands, tool outputs, and internal notes are operations-internal.
- A requester-visible assistant reply must always point back to a stored persistent turn plus an explicitly published `TicketMessage`.
- Raw turn items may support ops inspection, but they may not be a publication source.

## 8. Required persistent schema additions

Minimum additions for the persistent slice:

- `codex_conversations`: one logical conversation row per ticket in the first rollout, which keeps closed/new-cardinality unambiguous until a future governance flow exists.
- `codex_sessions`: one or more native session segments per logical conversation, with unique active segment enforcement.
- `codex_turns`: one persistent specialist turn per triggering `AIRun`, with monotonic `turn_index` and one active prepared/running turn per conversation.
- `codex_turn_inputs`: ordered, idempotent turn-to-input links using a per-turn `input_index` plus a per-turn `dedupe_key`, rather than timestamp inference.
- `codex_turn_outcomes`: append-only lifecycle and publication outcomes.
- `codex_turn_items`: optional normalized event projection, but requester-inaccessible.

Required constraints:

- one active logical conversation per ticket;
- one active session segment per conversation;
- one `prepared` or `running` turn per conversation;
- unique `(conversation_id, turn_index)`;
- raw turn items constrained to ops-internal visibility;
- requester publication still flows only through `TicketMessage`.

## 9. Turn state and outcome semantics

The implementation must distinguish at least these states:

- `attempted`: AutoSac launched a specialist turn and persisted the prompt/intent.
- `accepted`: Codex accepted the turn and, for new sessions, emitted a durable thread ID.
- `ambiguous`: AutoSac cannot prove whether Codex accepted or completed the turn and must not silently resubmit the same prompt.
- `completed`: Codex produced a schema-valid final result.
- `published`: a requester-visible `TicketMessage` was explicitly created from that result.

Minimum append-only outcomes for the first safe rollout:

- attempted submission;
- accepted by Codex;
- completed structured result;
- auto-published;
- draft created;
- draft rejected;
- published with edits;
- superseded before publication;
- internal-only retention;
- failed / interrupted / timed out / ambiguous.

`AIDraft` remains the pending-review workflow object. It should link back to the generating persistent turn rather than replace the turn outcome history.

## 10. Input-event contract

The persistent path must consume ordered durable input events, not a requester-only fingerprint.

Eligible inputs for the next specialist turn:

- public requester messages;
- eligible public ops replies;
- eligible internal ops notes that should influence future reasoning;
- review feedback and rejection metadata;
- publication edits and supersession markers;
- route transitions and manual rerun instructions;
- relevant ticket state changes needed for reasoning or publication safety.

Non-goal:

- impersonating all of the above as requester speech.

The next-turn envelope must preserve event type, author type, source, ordering, and any relation to a prior persistent turn.

Phase-4 implementation note:

- ordered input consumption is built from a durable event ledger that includes a ticket-state snapshot, eligible public ticket messages, eligible internal ops notes, ticket status history, manual-rerun or recovery trigger metadata when present, and one latest-outcome summary per prior persistent turn;
- `codex_turn_inputs` stores the exact ordered event slice injected into each persistent turn, keyed by per-turn `input_index` plus `dedupe_key`;
- each run freezes one input envelope inside a short ticket-row-lock transaction; the router receives the full frozen context and the specialist receives the delta/replay projection derived from that same snapshot; no database lock is held during Codex execution;
- ordinary requester-triggered runs do not gain synthetic `AIRun` input events unless the trigger materially changes reasoning, such as manual rerun, forced reroute, or recovery;
- review outcomes, rejected drafts, superseded drafts, published bodies, and edited publications flow back into the next turn through append-only turn outcomes plus prior-turn summary events, not through raw turn items.

## 11. Replay and rollback contract

Rollback or feature disablement must preserve logical conversation continuity.

Rules:

- if `CODEX_CONVERSATIONS_ENABLED=false`, existing persistent turn history remains durable;
- fallback prompts for any later recovery path must replay stored persistent turns and outcomes, including rejected, unpublished, failed, ambiguous, interrupted, and superseded attempts;
- disabling the feature must not silently revert to the current requester-visible aggregation if that would drop retained context;
- if replay is not implemented, the feature stays disabled.

Phase-4 implementation note:

- when persistent history exists and the feature flag is disabled, specialist execution still switches to a replay-backed prompt instead of the old requester-only aggregate;
- replay prompts include each stored persistent turn, generated structured result fields when available, linked draft or publication state, and every append-only outcome so rejected, unpublished, failed, ambiguous, interrupted, timed-out, and superseded attempts remain visible to the next run;
- if the active native session is missing or marked `recovery_required`, AutoSac replaces the `codex_sessions` segment, writes an explicit recovery boundary into the next prompt, and continues the same logical conversation with a fresh native session rather than silently forking a new ticket conversation.

## 12. Transport and fencing contract

### 12.1 Fencing

`AIRun` remains the queue and heartbeat unit, but it is not sufficient as the native-thread fence.

The persistent implementation must add a full-duration ownership mechanism that:

- spans the entire external Codex execution;
- survives worker crash and stale recovery;
- prevents overlapping turns against the same logical conversation;
- remains operationally comprehensible;
- cooperates with existing heartbeat and stale-run recovery instead of bypassing them.

Phase-3 implementation note:

- the fence is stored on the active `codex_sessions` row as `lease_owner_run_id`, `lease_worker_instance_id`, `lease_acquired_at`, `lease_heartbeat_at`, and `lease_expires_at`;
- worker heartbeats refresh that lease alongside `AIRun.last_heartbeat_at`;
- stale recovery retains the persistent turn as terminal history and never creates a replacement `AIRun` from the stale run's original trigger, frozen prompt, or native thread state;
- if the ticket already has a later durable deferred requester/operator event, existing deferred-requeue processing may consume that event exactly once after the stale attempt is terminalized, creating a distinct `AIRun` and distinct logical turn with the deferred trigger, requesting user, forced route target, and forced specialist preserved;
- deferred requeue fields are cleared only when that replacement run is created under the existing active-run uniqueness rules; if creation loses a race, the durable deferred event remains queued for a later sweep;
- if no later deferred work exists, stale persistent recovery does not leave the ticket indefinitely in `ai_triage` with no active run; it uses the existing internal failure-note and Dev/TI status-transition seams without requester-visible publication;
- stale persistent recovery conservatively retires the native session segment and marks the logical conversation `recovery_required`, even when `turn.started` was never durably observed, because a missing acceptance row does not prove the CLI never received stdin;
- `turn.started`, not a pre-existing native thread ID, is the durable acceptance evidence for the specific attempt; accepted stale attempts become ambiguous, unaccepted stale attempts remain interrupted, and the next genuine event starts a replacement native session with an explicit recovery boundary rather than resuming the uncertain session;
- stale recovery records accepted input counts, steering receipt status counts, recovery markers, and the rule that late output from a retired native session may be retained as a raw item but is never publishable;
- lease expiry and worker-instance ownership are the cross-host fencing boundary; host-local PID checks are intentionally not used.

### 12.2 Transport

Persistent specialist execution must:

- stop using capture-only `subprocess.run` for the persistent path;
- ingest JSONL incrementally;
- persist `thread.started.thread_id` immediately on first-session creation;
- persist acceptance evidence before process exit;
- store stdout/stderr artifacts without waiting for a successful terminal state;
- validate structured output on every initial and resumed specialist turn.

Phase-3 implementation note:

- parsed JSONL lines are appended to `codex_turn_items` as ops-internal records while `stdout.jsonl` and `stderr.txt` are written incrementally;
- `thread.started` persists the native thread ID immediately, but only `turn.started` records acceptance of the current attempt;
- `turn.completed` usage is retained for the terminal `completed` outcome payload;
- timeout or non-terminal crash after acceptance is retained as an `ambiguous` turn instead of being silently re-submitted.
- success requires all of: exit status zero, durable `turn.completed`, a valid schema-conforming final output, and no stream/persistence error; a valid-looking stale `final.json` cannot override a transport failure.
- for the persistent path, the configured Codex timeout is interpreted as the monotonic wall-clock budget from prompt delivery start through Codex process completion;
- prompt writing runs on a bounded writer thread, `BrokenPipeError`/`OSError` from stdin delivery are classified through the strict failure path, and process waiting uses only the remaining execution budget;
- when the execution budget expires, AutoSac terminates the Codex process group with the existing graceful-then-forceful policy; the termination grace and subsequent local cleanup are bounded outside the configured execution budget so timeout handling itself cannot block indefinitely;
- finalization waits for stdout/stderr pumps to close or explicitly fails after bounded cleanup; if a leader exits while descendants hold pipes open, AutoSac terminates the remaining process group, closes parent pipe handles, and does not release ownership or finalize while stream threads can still write durable events.

### 12.3 Resume command rules

AutoSac must:

- resume only with the stored explicit thread/session ID;
- never use `--last`;
- enforce read-only sandbox and disabled web search on resumed turns through options supported by `codex-cli 0.148.0`;
- keep the persistent path disabled if that enforcement cannot be proven.

Phase-3 implementation note:

- initial persistent specialist turns use `codex exec --sandbox read-only`;
- resumed persistent specialist turns use `codex exec resume <stored-thread-id>`;
- both paths add `--strict-config`, `-c 'sandbox_mode="read-only"'`, `-c 'web_search="disabled"'`, `-c 'tools.web_search=false'`, `--disable web_search_request`, and `--disable standalone_web_search`.

Active-turn steering transport update:

- `CODEX_APP_SERVER_SPECIALIST_TRANSPORT_ENABLED=true` switches only persistent specialist turns to a run-scoped `codex app-server --stdio` process;
- router and selector steps remain on the existing `execute_step` / `codex exec --ephemeral` path;
- app-server specialist turns start or resume the stored thread ID, persist native thread and turn IDs as soon as they are returned, wait for `turn/completed`, and record accepted initial inputs only after `turn/start` acceptance;
- the setting is default-off so the earlier persistent `codex exec` transport remains the rollback path.

## 13. Retention, permissions, backup, and deletion

Persistent conversation storage is blocked on the following minimum controls:

- one persistent AutoSac `CODEX_HOME`, defaulting to `~/autosac/codex`, shared by every AutoSac ticket and native session without per-session suffix directories;
- worker readiness must validate `CODEX_HOME` existence and writability and verify `codex login status` when no API key is configured, while web readiness only validates configuration shape;
- no direct web-process requirement for raw Codex session files;
- durable backup coverage for database rows plus required turn artifacts plus native session storage, or an explicit decision to keep the feature disabled;
- coordinated deletion workflow for ticket data, persistent turn data, artifacts, and native session storage;
- requester-safe authorization for any new ops-only inspection views;
- documented minimum retention periods for persistent conversation data.

Until those controls exist, persistent native session storage may be used in tests, but not enabled for production traffic.

## 14. Deliberate deferrals

Deferred by design from the first shipped slice:

- live JSONL streaming in the requester UI;
- removing the persistent-specialist `codex exec` transport before an app-server soak period;
- routing every ordinary follow-up through the router;
- multiple requester-visible publication channels;
- a broad redesign of the ticket UI.

The first UI requirement is only minimal operations visibility plus requester-safe projection needed to operate and verify the lifecycle.

## 15. Phased implementation contract

### Phase 1: Evidence and contract

- classify the issues against repo and CLI evidence;
- update this architecture note;
- keep the persistent path disabled.

### Phase 2: Schema and config foundation

- add conversation/session/turn/outcome schema behind `CODEX_CONVERSATIONS_ENABLED=false`;
- add `CODEX_CONVERSATIONS_ENABLED` and `CODEX_HOME` configuration with web/worker readiness validation;
- keep existing ticket behavior unchanged when the flag is off.

### Phase 3: Persistent transport and fencing

- add full-duration conversation ownership;
- stream JSONL and persist thread ID immediately;
- keep router and selector on the current ephemeral path;
- prove command construction for initial and resumed specialist turns;
- retain stale, timed-out, and crash-after-acceptance specialist attempts as durable terminal turn history without automatically resubmitting the same prompt;
- process a later durable deferred requester/operator event, when present, only as a distinct new `AIRun` and logical turn after stale recovery; replace uncertain native sessions at an explicit recovery boundary.

### Phase 4: Input ordering, outcomes, and replay

- add ordered input-event consumption;
- add append-only outcomes;
- link existing drafts/publications/rejections;
- implement replay fallback and missing-session recovery.

### Phase 5: UI safe projection

- add minimal ops-only lifecycle visibility;
- keep persistent turn history out of the ticket activity ledger and expose it as a closed-by-default disclosure below **More analysis** in the ops ticket-detail AI Analysis panel;
- expose ops-only steering receipt history, delivery states, native thread/turn IDs, effective input hashes, completion fences, ambiguous blockers, and recovery markers;
- keep requester routes limited to published `TicketMessage` content and optional generic run-state indicators.

## 16. Rollout contract

`CODEX_CONVERSATIONS_ENABLED` defaults to `false`.

In the delivered phase-2 foundation:

- `CODEX_HOME` defaults to the worker user's `~/autosac/codex` and is used exactly for all AutoSac Codex calls;
- web startup does not require the native session filesystem to be mounted;
- worker startup requires `CODEX_HOME` to exist and be writable and verifies native Codex authentication when an API key is not configured;
- the schema is deployed but remains unused while the flag is `false`.

It may switch to `true` only after all of the following are true:

- full-call conversation fencing is implemented and tested;
- initial thread ID durability is immediate and crash-safe;
- minimal append-only outcomes exist and feed the next turn;
- rollback replay preserves logical conversation context;
- resumed turns explicitly enforce read-only sandbox and disabled web search using supported `codex-cli 0.148.0` behavior;
- requester-safe visibility is preserved with no alternate publication channel.

If any one of those conditions is not satisfied, the feature remains disabled and the repository keeps the updated contract as the source of truth for later phases.

Validation status on 2026-08-24:

- the application test suite passed with `.venv/bin/python -m pytest -q tests` after broad compatibility fixes for legacy fake DB results used outside the persistent path;
- the broader repository suite still hits unrelated `superloop/tests` collection failures because those tests import `loop_control` and `superloop` without the package path configured in the default test invocation;
- `codex-cli 0.148.0` local help still shows `--sandbox` on `codex exec`, no dedicated `--sandbox` flag on `codex exec resume`, explicit session-id resume support, and `-c/--config` plus `--disable` support on both commands;
- the persistent conversation feature therefore remains default-off for production rollout, not because of remaining lifecycle correctness gaps in the shipped slice, but because the operational controls in section 13 remain required before sensitive persistent session storage is enabled beyond tests.

Rollback posture on 2026-08-24:

- keep `CODEX_CONVERSATIONS_ENABLED=false` if any deployment-specific validation fails;
- preserve `codex_conversations`, `codex_sessions`, `codex_turns`, `codex_turn_inputs`, `codex_turn_outcomes`, and `codex_turn_items` rows for audit and replay context;
- continue using replay-backed prompt reconstruction when persistent history exists, rather than falling back to the older requester-visible aggregate that omits rejected, unpublished, failed, ambiguous, interrupted, timed-out, or superseded turns;
- treat schema downgrade as pre-rollout only, before operators rely on stored persistent conversation history.
