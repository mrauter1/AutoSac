# Codex Active-Turn Steering Implementation Plan

Status: approved and implementation-ready  
Date: 2026-08-25

## 1. Objective

Make each AutoSac ticket correspond to one persistent Codex conversation. When
new ticket content arrives, AutoSac should append it to the exact active Codex
specialist turn when that is safe and workflow-authorized. Otherwise, the
content remains durable context for a later authorized turn.

The fundamental distinction is:

> Ticket content determines what Codex eventually sees. Ticket workflow
> determines whether Codex should run now.

The implementation must never fork the native Codex thread, discard a ticket
or Codex turn, or publish output based on input whose delivery is uncertain.

## 2. Core invariants

1. Every ticket has at most one active `AIRun` and one active persistent
   specialist turn.
2. Every persistent specialist turn belongs to the ticket's existing Codex
   thread.
3. All ticket content can become Codex context regardless of author,
   visibility, or message source.
4. Visibility remains metadata. Codex seeing an internal note never makes that
   note requester-visible.
5. Content accumulation and execution scheduling are independent.
6. Operator content outside AI Triage remains dormant context and does not
   create, requeue, or steer an `AIRun`.
7. A content item is model-seen only after it is part of an accepted
   `turn/start`, durably acknowledged by `turn/steer`, or already known through
   the Codex turn that causally generated it.
8. Writing to app-server stdin is not evidence of acceptance.
9. `turn/completed` is the authoritative terminal signal. Agent messages
   emitted before it are provisional.
10. Publication is prohibited when delivery is ambiguous or newer unconsumed
    authorized content makes the result stale.
11. Router, selector, publication, requester-visibility, and status policies
    remain unchanged unless this plan explicitly changes them.
12. The existing deferred path remains durable escrow until steering is
    accepted.

## 3. Triggering and context rules

### 3.1 Operator public replies

- `next_status=ai_triage`: preserve the current explicit AI-run behavior. If a
  compatible specialist turn is active and there is no forced route or
  specialist change, the reply may satisfy the request through steering.
- `next_status=waiting_on_user`: persist only; do not run or steer.
- `next_status=waiting_on_dev_ti`: persist only; do not run or steer.
- `next_status=resolved`: persist only; do not run or steer.

### 3.2 Operator internal notes

- If active-turn steering is enabled and a compatible AI Triage app-server
  specialist turn is active, create a durable `ticket_content` requeue request
  as escrow and attempt steering.
- If no compatible turn is active, preserve current behavior: persist the note
  as dormant future context without starting a run.
- If steering succeeds, clear only the matching content-driven escrow.
- If steering loses the completion race or becomes ambiguous, retain the
  escrow so a successor run processes the note.

### 3.3 Other existing triggers

Preserve current behavior for new tickets, manual reruns, and reopens.
Requester replies may steer only when a compatible AI Triage app-server turn
was already active. Replies that reopen a resolved ticket or move Waiting on
User or Waiting on Dev/TI back to AI Triage retain the existing successor-run
semantics. Manual reruns with forced route or specialist overrides are
workflow controls and cannot be satisfied by steering into the old specialist
turn.

### 3.4 Generic active-turn content escrow

Add the generic trigger `ticket_content` to `AI_RUN_TRIGGERS` and
`REQUEUE_TRIGGERS`. It means that human content arrived while compatible AI
processing was active. It is deliberately not specific to author, visibility,
or message subtype.

`ticket_content` may create a successor run only while the ticket remains in
AI Triage. If the ticket later moves to Waiting on User, Waiting on Dev/TI, or
Resolved, retire the content-driven execution request without consuming the
content. The content remains dormant for a later authorized run.

Manual rerun, reopen, and forced-routing requests retain their existing,
stronger control semantics and cannot be cleared by accepted steering.

## 4. Reuse the ordered-input architecture

Do not add a generic event platform or a numeric ticket event stream unless
the existing representation proves insufficient.

Reuse and extend:

- `OrderedInputEvent`
- Stable `dedupe_key` values
- Deterministic ordering
- `CodexTurnInput`
- Conversation-wide consumed-key discovery
- Pending-event construction in `worker/codex_inputs.py`

The effective content cursor is the set of dedupe keys already represented by
accepted `CodexTurnInput` rows or causally known Codex outcomes:

```text
current ordered ticket events
- events already known to the conversation
= unseen input delta
```

Add a strict helper for active steering, such as
`load_strictly_unseen_input_events`. It must return an empty tuple when nothing
is unseen. It must not inherit `build_prompt_conversation_state`'s existing
full-replay fallback:

```python
if not pending_events:
    pending_events = current_events
```

That fallback may remain for intentional recovery or manual replay, but active
steering must never resend the complete ticket history.

## 5. Canonical content projection

Project all ticket messages into ordered canonical events before any causal
filtering, including public requester/operator content, internal notes,
system-authored messages, and prior AI-authored messages. Centralized
causal-known logic decides whether an exact AI-origin duplicate is already
represented by the conversation; human edits and other non-identical messages
remain new context.

Represent every ticket message through one canonical envelope containing:

- Stable message ID and dedupe key
- Ticket ID
- Author identity and author type
- Visibility
- Message source
- Creation time
- Body text
- Attachment metadata and safe input representation
- Causal `ai_run_id` or `codex_turn_outcome_id`, when present

Attachments created with a message form one logical input bundle. Do not mark
the text accepted while silently omitting an attachment. Reuse the current
safe attachment projection for initial input and steering. When the bundle is
representable, send the canonical text envelope plus native `localImage` input
items for supported images and safe path metadata for non-image documents. If
the active-turn protocol cannot represent the complete bundle, leave the bundle
unaccepted for a later turn.

## 6. Causal exclusion

All ticket content can be visible to Codex, but some content is already known
because Codex generated it. Centralize this decision in a predicate such as
`event_is_known_to_conversation`.

Treat an event as known when:

- Its dedupe key exists in an accepted `CodexTurnInput`.
- It is an exact output causally linked to a turn in the conversation.
- It is represented by an authoritative prior-turn publication or review
  summary.
- It was accepted through a steering receipt committed with its
  `CodexTurnInput`.

Exclude content generated by the active turn from that turn's steering scan.
Human edits, review feedback, and content that differs from the original AI
result remain new context.

## 7. Persistent specialist transport

Replace only persistent specialist execution with a run-scoped
`codex app-server` process over stdio. Router and selector remain on the
current ephemeral `codex exec` path.

For each specialist execution:

1. Acquire the existing conversation/session lease.
2. Start app-server with AutoSac's shared `CODEX_HOME` at
   `~/autosac/codex/`.
3. Perform `initialize` followed by `initialized`.
4. Start or resume the stored native thread.
5. Persist any newly returned thread ID immediately.
6. Call `turn/start` with the unseen input delta and the existing model,
   working directory, sandbox, approval policy, and output schema.
7. Persist the returned native turn ID.
8. Supervise responses and notifications until the matching
   `turn/completed`.
9. Terminate the run-owned process through the existing bounded process-group
   and non-quiescent cleanup protections.

Do not use `thread/fork`. Do not introduce a daemon, WebSocket transport,
Redis, Kafka, a generic actor framework, or PostgreSQL `LISTEN/NOTIFY` in V1.

## 8. App-server client responsibilities

Add a focused client beside the persistent executor with:

- JSON-RPC request correlation
- Serialized stdin writes
- One stdout reader and bounded stderr capture
- Notification dispatch
- Safe refusal of unexpected interactive or approval requests
- Structured item persistence
- Turn interruption on timeout
- Bounded process-group cleanup and stream joining
- Protocol and transport failure classification

Persist meaningful native items in `CodexTurnItem`, including user input,
agent messages, reasoning summaries, commands, file changes, and completion
notifications. These artifacts remain ops-internal unless the existing
publication layer explicitly creates a public `TicketMessage`.

## 9. Minimal additive persistence

Use one forward additive migration. No database freeze or downtime is needed.

### 9.1 `codex_turns`

Add:

- `transport_kind`: `exec` or `app_server`
- `native_turn_id`
- `steering_closed_at`
- `effective_input_hash`

Add an appropriate uniqueness constraint for a non-null native turn within a
session.

### 9.2 `codex_turn_steers`

Add one narrow delivery-receipt table containing:

- `id`
- `turn_id`
- `event_kind`
- `source_kind`
- `source_id`
- `dedupe_key`
- `expected_native_turn_id`
- `rpc_request_id`
- `payload_json`
- `payload_hash`
- `status`
- `attempted_at`
- `acknowledged_at`
- `resolved_at`
- `error_code`
- `error_text`
- `created_at`

Allowed statuses are `prepared`, `sending`, `accepted`, `rejected`, and
`ambiguous`. Enforce uniqueness on `(turn_id, dedupe_key)`.

This table describes the database-to-app-server custody boundary. It is not a
second message queue.

### 9.3 Ticket requeue provenance

Add nullable `requeue_source_message_id`, or an equivalent stable source
reference, so accepted steering can clear only the content-driven request that
it actually satisfied. It must never clear a separate manual rerun, reopen,
forced route, forced specialist, or newer message request.

### 9.4 `codex_turn_inputs`

Keep its current meaning: content durably incorporated into the turn. Insert a
steered input only in the transaction that marks the corresponding receipt
accepted.

## 10. Steering loop

While the specialist turn is running:

1. Poll a lightweight ticket change token at a short bounded interval.
2. Load strict unseen ordered content on the first poll and whenever that token
   changes. Retain the token read before the scan so a concurrent update forces
   another scan on the next poll.
3. Exclude known conversation content, current-turn causal output, and events
   with prepared, sending, or accepted receipts.
4. Revalidate the ticket, `AIRun`, lease, native thread, native turn,
   `steering_closed_at`, deadline, size limits, and workflow status.
5. Send complete content bundles sequentially in deterministic order.

A compatible turn requires the same ticket conversation, current worker lease
ownership, the exact active native turn, an open steering fence, AI Triage
status, no forced route or specialist replacement, active-turn steering
enabled, app-server transport, and a representable complete content bundle.

Use one logical message bundle per steer in V1. Multiple messages may be sent
sequentially while the native turn remains active.

## 11. Delivery transaction

For each content bundle:

1. Lock and revalidate the turn and lease.
2. Insert a `prepared` receipt.
3. Commit `sending` before writing to stdin.
4. Call `turn/steer` using only documented fields: `threadId`, `input`, and
   `expectedTurnId`.
5. Require success with the expected native turn ID.
6. In one database transaction, revalidate ownership, mark the receipt
   accepted, insert `CodexTurnInput`, advance `effective_input_hash` only when
   the latest durably accepted frontier now matches the full accepted ticket
   snapshot, append an accepted outcome, and clear only a matching
   source-provenanced content-driven escrow when no older or newer unconsumed
   authorized content and no stronger control request remain.

JSON-RPC IDs are correlation identifiers, not idempotency guarantees. A stable
event marker may be embedded in the rendered input envelope for audit and
duplicate recognition, but do not send unsupported app-server parameters.

## 12. Rejection and ambiguity

Definitive errors such as no active turn, expected-turn mismatch, request
validation failure, or a closed steering fence mark the receipt `rejected`.
They do not create `CodexTurnInput`; the content stays available for later.

Process loss, worker death after possible transmission, malformed success,
lease loss before acknowledgement commit, or completion with an unresolved
request is `ambiguous`.

For ambiguity:

- Do not publish the current result.
- Do not blindly resend into the same native turn.
- Recovery-fence or retire the native session.
- Replay the content at the existing recovery boundary.
- Retain the original turn, receipt, items, and outcome.

At-least-once model visibility is acceptable during recovery; lost content and
uncertain publication are not.

## 13. Completion fence

On the matching `turn/completed` notification:

1. Stop admitting steering work immediately.
2. Lock the run, turn, session, and ticket.
3. Persist the completion notification.
4. Set `steering_closed_at`.
5. Reconcile every receipt.
6. Freeze `effective_input_hash`.
7. Identify any committed but unaccepted authorized content.
8. Commit the fence before selecting output.

Accepted receipts are authoritative inputs. Rejected receipts remain unseen.
Unresolved receipts become ambiguous and block publication.

Only after the fence may AutoSac select the last completed final-answer agent
message, parse it, validate it against the specialist contract, and apply the
existing publication policy. Earlier final-looking messages are never
publishable while the turn is active.

## 14. Status changes during a turn

If the ticket leaves AI Triage while a specialist turn is active:

- Stop steering.
- Do not steer subsequent operator content from a waiting or resolved state.
- Preserve that content as dormant context.
- Best-effort interrupt work made obsolete by the status change.
- Prevent the AI result from overriding the operator-selected status.
- Do not manufacture a successor run.

If the ticket later returns to AI Triage through an authorized action, the next
turn receives all unseen dormant content.

## 15. Staleness and publication scheduling

For app-server turns, finalization compares current AI-relevant input with
`CodexTurn.effective_input_hash`, not only the original `AIRun.input_hash`.

Correct the existing stale-input behavior: stale input alone must not synthesize
a requester-reply requeue. The rule is:

```text
stale result + authorized execution request
    -> supersede and create a successor run

stale result + no authorized execution request
    -> supersede only; preserve unseen content as dormant context
```

`process_deferred_requeue` may process `requester_reply` and `ticket_content`
only while the ticket is in AI Triage. Manual rerun and reopen retain their
explicit control semantics.

Under the final ticket lock, publication additionally requires successful
completion, a valid final result, current ownership, no ambiguous receipt, no
stronger unconsumed control request, no unseen authorized content, and a ticket
workflow state compatible with the proposed result.

Accepted steering does not permanently force human review. Preserve the
current publication policy after rollout validation.

## 16. Timeout and capacity

Steering does not reset the original monotonic specialist deadline. Apply
conservative count, byte, attachment, and remaining-time limits. Never partially
accept a message-and-attachment bundle.

When a limit prevents steering, retain the authorized escrow while the ticket
remains in AI Triage; otherwise leave the content dormant.

## 17. Recovery

Extend the existing persistent-session recovery rather than adding another
recovery subsystem:

- Failure before native acceptance follows ordinary failure handling.
- Possible acceptance creates an ambiguous recovery boundary.
- Accepted inputs remain in `CodexTurnInput`.
- Rejected and dormant inputs remain discoverable from ordered events.
- Ambiguous inputs replay with an explicit recovery boundary.
- Late output from a retired session is stored but cannot publish.
- Every original turn, item, output, and receipt remains retained.

## 18. UI and observability

Do not add a new message type. The existing public reply and internal note
composers remain authoritative.

Optional ops-only delivery states may show:

- Included in active AI turn
- Waiting for future AI context
- Queued for another AI run
- Delivery uncertain; recovery required

Track initialization, start/resume, steering disposition, commit-to-ack
latency, dormant content, completion-race fallback, supersession, avoided runs,
effective response latency, cleanup duration, and orphan processes. Persist a
bounded raw protocol artifact under existing access and retention controls.

## 19. Validation

### Protocol and lifecycle

- Initialization ordering, thread start/resume, structured turn start
- Successful and rejected steering
- Multiple sequential steers
- Early final-looking output followed by steering and later final output
- Final selection only after `turn/completed`
- Malformed protocol, timeout, interrupt, process loss, and cleanup

### Content and causality

- Public requester and operator messages
- Internal operator notes
- AI-authored messages and human-edited AI drafts
- Images and other supported attachments
- Strict unseen-event behavior with an empty result
- Causal exclusion and stable deduplication
- Dormant content included exactly once in the next authorized run

### Status regressions

For Waiting on User, Waiting on Dev/TI, and Resolved, prove that an operator
reply persists without creating a run, setting requeue, or steering, and that it
appears in the next authorized run.

For AI Triage, prove both paths:

```text
active turn -> internal note -> ticket_content escrow -> accepted steer
-> escrow cleared -> no successor run
```

```text
active turn -> internal note -> ticket_content escrow -> completion race
-> current result superseded -> successor run -> note included exactly once
```

### Race and recovery matrix

Test content before start, during reasoning, after early final output, immediately
around completion, and during publication. Inject worker death before send,
after possible send, after acknowledgement, and after completion. Test lease
expiry, status changes, resolution, forced routing, and publication races.

For every interleaving, content must be either model-seen or durably retained,
and requester-visible publication must remain exactly once.

Run all existing router, selector, queue, publication, authorization, requester
nondisclosure, persistent-session, cleanup, migration, and full-suite tests.

## 20. Rollout

1. Ship additive schema and protocol fixtures with behavior disabled.
2. Add app-server specialist transport behind a transport setting.
3. Verify app-server parity with steering disabled.
4. Run shadow steering classification and receipts without sending.
5. Enable real steering for a deterministic internal canary.
6. Use human review as a temporary canary safeguard only.
7. Require no lost content, duplicate public messages, unauthorized waiting or
   resolved runs, stale publication, or cleanup leaks.
8. Restore the normal publication policy for the canary.
9. Expand gradually.
10. Remove the old persistent-specialist exec transport only after a stable
    soak period.

Rollback affects future executions. Running app-server turns either finish or
enter conservative recovery, and all history remains retained.

## 21. Explicit non-goals

- Forked Codex threads
- Live requester-facing reasoning
- A persistent app-server daemon
- A generic event bus or actor framework
- Semantic message eligibility classification
- New public/internal message types
- Changed router or selector execution
- AI execution from operator replies in Waiting on User, Waiting on Dev/TI, or
  Resolved
- Deleting rejected or unpublished turns
- Exactly-once model input as a correctness assumption

## 22. Done criteria

The change is complete only when:

- Persistent specialist turns run through the app-server transport.
- Compatible AI Triage content is steered into the same native turn.
- Every active-turn human content update has durable fallback custody.
- Dormant content does not schedule AI but appears in the next authorized turn.
- Waiting and resolved operator replies retain their existing no-run behavior.
- Completion, delivery, lease, recovery, and publication races are covered.
- Existing behavior outside persistent specialist execution remains compatible.
- Documentation and the architecture visualization reflect the new flow.
- Targeted and full regression tests pass with recorded evidence.
