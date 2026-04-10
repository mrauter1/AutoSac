Mode
Repo-grounded architecture assessment and implementation plan for Phase 1 Slack outbound notifications only.

Current state
Verified from the repo:

Stage 1 explicitly excludes Slack and external notifications today in Autosac_PRD.md.
Ticket mutations are centralized in ticketing.py.
Public/internal separation is enforced in ticket_messages.visibility and status history is persisted in ticket_status_history in models.py.
The current worker has one main AI polling loop plus a separate heartbeat thread in main.py, and it already uses row locking with FOR UPDATE SKIP LOCKED in queue.py.
There is no existing Slack config in config.py, no integration schema in the existing migrations under shared/migrations/versions, and no Slack-related env vars in .env.example.
Confirmed design direction
With your decisions applied, the safest Phase 1 design is:

integration_events as the canonical event table, using event_type
no hard coupling from the integration layer to tickets
multiple named delivery targets in the schema from day 1, but only one active Slack target in the first rollout
Slack as an asynchronous projection only; AutoSac remains the system of record
event emission inside the same DB transaction as the ticket mutation; Slack delivery outside that transaction
I would not reuse ai_runs as the notification queue. That queue is intentionally single-run-oriented and tied to Codex orchestration. Notification delivery is a separate runtime concern and should not wait behind long AI runs.

Must-not-break invariants
Ticket creation, replies, and status changes must succeed even if Slack is down.
Internal notes must never be emitted to Slack in this first cut.
tickets.updated_at, ticket_status_history, and current unread/view behavior must stay unchanged.
The one-active-ai_run invariant must remain untouched.
Delivery must be idempotent enough to tolerate retries and worker restarts.
Recommended schema
1. integration_events
Purpose: canonical business/integration fact.

Recommended fields:

id uuid pk
source_system text not null default 'autosac'
event_type text not null
aggregate_type text not null
aggregate_id uuid null
dedupe_key text not null unique
payload_json jsonb not null
created_at timestamptz not null default now()
Recommended initial event_type values:

ticket.created
ticket.public_message_added
ticket.status_changed
2. integration_event_links
Purpose: optional links to one or more domain records without forcing the event table to depend on one table.

Recommended fields:

id uuid pk
event_id uuid fk -> integration_events.id
entity_type text not null
entity_id uuid not null
relation_kind text not null
created_at timestamptz not null default now()
Initial examples:

ticket.created -> ticket as primary, initial ticket_message as message
ticket.public_message_added -> ticket as primary, ticket_message as message
ticket.status_changed -> ticket as primary, ticket_status_history as status_history
3. integration_event_targets
Purpose: per-destination delivery state.

Recommended fields:

id uuid pk
event_id uuid fk -> integration_events.id
target_name text not null
target_kind text not null
delivery_status text not null
attempt_count int not null default 0
next_attempt_at timestamptz not null default now()
locked_at timestamptz null
locked_by text null
last_error text null
sent_at timestamptz null
created_at timestamptz not null default now()
unique constraint on (event_id, target_name)
Initial target_kind can be slack_webhook.
Initial delivery_status set: pending, processing, sent, failed, dead_letter.

Emission points
Use only the existing central mutation paths in ticketing.py:

create_requester_ticket -> emit ticket.created
add_requester_reply -> emit ticket.public_message_added
publish_ai_public_reply -> emit ticket.public_message_added
add_ops_public_reply -> emit ticket.public_message_added
publish_ai_draft_for_ops -> emit ticket.public_message_added
record_status_change -> emit ticket.status_changed
Important rule:

keep ticket.created separate from ticket.status_changed; do not treat the initial new history row as a second operational notification unless you explicitly want both later
Do not emit Slack targets from:

publish_ai_internal_note
publish_ai_failure_note
add_ops_internal_note
draft creation/rejection paths
Payload contract
For Phase 1, keep payload minimal and Slack-safe.

Recommended event payload fields:

ticket_id
ticket_reference
ticket_title
ticket_status
ticket_url
occurred_at
message_id when applicable
message_author_type when applicable
message_source when applicable
message_preview when applicable
status_from and status_to for status events
Recommended exclusions for first cut:

internal message bodies
attachment file paths
raw full message body without truncation
any hidden routing or reviewer-only metadata
Target resolution and config
Extend config.py and .env.example for explicit outbound config.

Recommended first-cut config:

SLACK_ENABLED
SLACK_DEFAULT_TARGET_NAME
SLACK_TARGETS_JSON or equivalent structured env mapping target name -> webhook URL and enabled flag
SLACK_NOTIFY_TICKET_CREATED
SLACK_NOTIFY_PUBLIC_MESSAGE_ADDED
SLACK_NOTIFY_STATUS_CHANGED
SLACK_MESSAGE_PREVIEW_MAX_CHARS
SLACK_DELIVERY_BATCH_SIZE
SLACK_DELIVERY_MAX_ATTEMPTS
Because you want multiple named targets in schema, I would keep target definitions in config, not in DB, for Phase 1. The DB owns event state; environment config owns credentials/endpoints.

Delivery runtime
Recommended runtime approach for the current architecture:

keep autosac-worker as the only worker service in the first rollout
add a dedicated delivery thread beside the existing heartbeat thread in main.py
give it its own DB sessions via session_scope
claim integration_event_targets in small batches using FOR UPDATE SKIP LOCKED, matching the concurrency pattern already used for ai_runs
use httpx, which is already present in requirements.txt
I would not put Slack delivery into the request path, and I would not serialize it behind the main process_ai_run loop.

Retry and idempotency
Recommended semantics:

event idempotency via dedupe_key on integration_events
per-target idempotency via unique (event_id, target_name)
retries with bounded exponential backoff by updating next_attempt_at
move to dead_letter after a finite attempt limit
preserve last_error for operator visibility
For dedupe_key, use a deterministic key derived from the concrete domain fact, for example:

ticket.created:<ticket_id>
ticket.public_message_added:<message_id>
ticket.status_changed:<status_history_id>
That is stronger than using timestamps and avoids duplicate delivery state when the same transaction is retried.

Slack message format
Keep Slack as a notification surface, not a mirrored conversation.

Recommended first-cut formatting:

Novo ticket T-000123: <title>
T-000123 mudou de waiting_on_user para waiting_on_dev_ti
Nova mensagem pública em T-000123 por requester: <preview>
Always include:

ticket reference
short text
deep link back to AutoSac
no internal-only content
Tests to add
migration creates the three integration tables and indexes correctly
create_requester_ticket emits exactly one ticket.created
requester/AI/ops public replies emit ticket.public_message_added
internal notes emit no Slack target
record_status_change emits ticket.status_changed
initial new history row does not accidentally produce duplicate outward notifications if that policy is disabled
target claim logic skips locked rows safely
transient HTTP failure increments attempt_count and reschedules
terminal failure moves target to dead_letter
duplicate emission attempts collapse on dedupe_key
Rollout plan
Add migration and ORM models for the integration tables.
Add config parsing and validation, still with Slack disabled.
Add event-building helpers in a dedicated integration module rather than bloating ticketing.py.
Emit integration events from the central mutation paths.
Add delivery polling thread to autosac-worker.
Add Slack formatter and webhook sender.
Ship with SLACK_ENABLED=false first to verify event creation without external delivery.
Enable one named target in one environment.
Observe for duplicate events, dead letters, and accidental over-notification before adding more targets.
Risks and side effects
Highest risk: leaking internal content if visibility filtering is implemented in the wrong layer.
Second risk: duplicate Slack posts if dedupe_key is not tied to message/status-history identity.
Third risk: delivery starvation if the new thread shares DB/session logic poorly with the main worker loop.
Fourth risk: scope creep into inbound Slack or per-channel routing too early.
Verification
Verified from repo evidence:

the correct mutation boundaries are in ticketing.py
the current worker runtime shape is in main.py
safe locking precedent already exists in queue.py
Slack/external notifications are currently out of scope in Autosac_PRD.md
no current Slack config or integration schema exists
Unknowns not confirmable in Stage 1:

actual Slack workspace rate limits and governance
final webhook secret distribution method in the real environment
exact target naming convention you want across environments
At this point the architecture is sufficiently specified for implementation. The only remaining choices are operational, not structural: exact env variable shape for named targets, final preview length, and whether ticket.created should be enabled from day 1 or staged behind config.

