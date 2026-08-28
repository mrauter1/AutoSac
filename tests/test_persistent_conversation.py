from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid

import pytest


class _FakeScalarResult:
    def __init__(self, values):
        self._values = list(values)

    def __iter__(self):
        return iter(self._values)

    def all(self):
        return list(self._values)

    def first(self):
        return self._values[0] if self._values else None

    def scalar_one(self):
        if not self._values:
            raise AssertionError("expected one scalar value")
        return self._values[0]

    def scalar_one_or_none(self):
        if not self._values:
            return None
        return self._values[0]

    def scalars(self):
        return _FakeScalarResult(self._values)


class _PromptStateDb:
    def __init__(self, *, conversation=None, session=None, turn_ids=()):
        self.conversation = conversation
        self.session = session
        self.turn_ids = list(turn_ids)

    def execute(self, statement):
        descriptions = statement.column_descriptions
        first_name = descriptions[0]["name"]
        first_type = descriptions[0].get("type")
        if first_type is not None and getattr(first_type, "__name__", "") == "CodexConversation":
            return _FakeScalarResult([self.conversation] if self.conversation is not None else [])
        if first_type is not None and getattr(first_type, "__name__", "") == "CodexSession":
            return _FakeScalarResult([self.session] if self.session is not None else [])
        if first_name == "id":
            return _FakeScalarResult(self.turn_ids)
        raise AssertionError(f"unexpected execute call: {first_name}")


class _InputEventsDb:
    def execute(self, statement):
        descriptions = statement.column_descriptions
        first_type = descriptions[0].get("type")
        if first_type is not None and getattr(first_type, "__name__", "") == "TicketStatusHistory":
            return _FakeScalarResult([])
        raise AssertionError("unexpected execute call")


class _KnownInputsDb:
    def __init__(self, *, consumed_rows=(), steer_rows=(), turn_rows=(), outcome_rows=()):
        self.consumed_rows = list(consumed_rows)
        self.steer_rows = list(steer_rows)
        self.turn_rows = list(turn_rows)
        self.outcome_rows = list(outcome_rows)
        self.execute_calls = 0

    def execute(self, statement):
        self.execute_calls += 1
        descriptions = statement.column_descriptions
        first_name = descriptions[0]["name"]
        if first_name == "dedupe_key" and len(descriptions) == 6:
            return _FakeScalarResult(self.consumed_rows)
        if first_name == "dedupe_key" and len(descriptions) == 4:
            return _FakeScalarResult(self.steer_rows)
        if first_name == "ai_run_id" and len(descriptions) == 1:
            return _FakeScalarResult(self.turn_rows)
        if first_name == "id" and len(descriptions) == 3:
            return _FakeScalarResult(self.outcome_rows)
        raise AssertionError(f"unexpected execute call: {first_name}")


class _ReplayDb:
    def __init__(self, *, outcomes, ai_run, draft=None, published_message=None):
        self.outcomes = outcomes
        self.ai_run = ai_run
        self.draft = draft
        self.published_message = published_message

    def execute(self, statement):
        descriptions = statement.column_descriptions
        first_type = descriptions[0].get("type")
        if first_type is not None and getattr(first_type, "__name__", "") == "CodexTurnOutcome":
            return _FakeScalarResult(self.outcomes)
        if first_type is not None and getattr(first_type, "__name__", "") == "AIDraft":
            return _FakeScalarResult([self.draft] if self.draft is not None else [])
        if first_type is not None and getattr(first_type, "__name__", "") == "TicketMessage":
            return _FakeScalarResult([self.published_message] if self.published_message is not None else [])
        raise AssertionError("unexpected execute call")

    def get(self, model, key):
        return self.ai_run


class _PrepareDb:
    def __init__(self, *, conversation, session, prior_turn_count: int):
        self.conversation = conversation
        self.session = session
        self.prior_turn_count = prior_turn_count
        self.added = []
        self.flush_calls = 0

    def add(self, item):
        self.added.append(item)
        if getattr(item, "id", None) is None:
            item.id = uuid.uuid4()
        if item.__class__.__name__ == "CodexSession" and item is not self.session:
            self.session = item

    def flush(self):
        self.flush_calls += 1

    def execute(self, statement):
        descriptions = statement.column_descriptions
        first_name = descriptions[0]["name"]
        first_type = descriptions[0].get("type")
        if first_name == "count" or "count" in first_name:
            return _FakeScalarResult([self.prior_turn_count if self.flush_calls < 3 else 2])
        if first_name == "coalesce":
            return _FakeScalarResult([0])
        if first_type is not None and getattr(first_type, "__name__", "") == "CodexSession":
            return _FakeScalarResult([self.session] if self.session is not None else [])
        if first_type is not None and getattr(first_type, "__name__", "") == "CodexTurn":
            turns = [item for item in self.added if item.__class__.__name__ == "CodexTurn"]
            return _FakeScalarResult(turns[-1:])
        raise AssertionError(f"unexpected execute call: {first_name}")


def _make_context(*, public_messages=None, internal_messages=None, public_attachments=()):
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        reference="T-000901",
        title="Persistent thread",
        status="ai_triage",
        urgent=False,
        route_target_id="support",
        requester_language="en",
        last_ai_action="draft_public_reply",
        clarification_rounds=1,
    )
    public_message = SimpleNamespace(
        id=uuid.uuid4(),
        author_type="requester",
        visibility="public",
        source="requester_reply",
        created_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
        body_text="Please continue.",
    )
    internal_message = SimpleNamespace(
        id=uuid.uuid4(),
        author_type="dev_ti",
        visibility="internal",
        source="human_internal_note",
        created_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
        body_text="Use the approved wording.",
    )
    return SimpleNamespace(
        ticket=ticket,
        requester_role="requester",
        requester_can_view_internal_messages=False,
        public_messages=tuple(public_messages or (public_message,)),
        internal_messages=tuple(internal_messages or (internal_message,)),
        public_attachments=tuple(public_attachments),
    )


def test_prompt_appendix_includes_review_feedback_and_reroute_transition():
    from worker.codex_inputs import OrderedInputEvent, _build_prompt_appendix

    pending_events = (
        OrderedInputEvent(
            event_kind="ticket_message",
            source_kind="ticket_message",
            source_id=uuid.uuid4(),
            dedupe_key="ticket-message:1",
            payload_json={
                "visibility": "public",
                "author_type": "requester",
                "source": "requester_reply",
                "created_at": "2026-08-24T10:00:00+00:00",
                "body_text": "Please continue.",
            },
            order_key=(1,),
        ),
        OrderedInputEvent(
            event_kind="ticket_status_changed",
            source_kind="ticket_status_history",
            source_id=uuid.uuid4(),
            dedupe_key="status:1",
            payload_json={
                "from_status": "waiting_on_dev_ti",
                "to_status": "ai_triage",
                "changed_by_type": "dev_ti",
                "note": "Retry with the support specialist.",
            },
            order_key=(2,),
        ),
        OrderedInputEvent(
            event_kind="run_trigger",
            source_kind="ai_run",
            source_id=uuid.uuid4(),
            dedupe_key="run:1",
            payload_json={
                "triggered_by": "manual_rerun",
                "forced_route_target_id": "support",
                "forced_specialist_id": "support",
                "recovered_from_run_id": None,
            },
            order_key=(3,),
        ),
        OrderedInputEvent(
            event_kind="prior_turn_summary",
            source_kind="ai_run",
            source_id=uuid.uuid4(),
            dedupe_key="turn-summary:1:5",
            payload_json={
                "turn_index": 2,
                "specialist_id": "support",
                "route_target_id": "support",
                "status": "completed",
                "latest_outcome_kind": "draft_rejected",
                "summary_internal": "Previous draft was rejected for tone.",
                "public_reply_markdown": "Old draft",
                "draft": {"status": "rejected"},
                "published_message": None,
            },
            order_key=(4,),
        ),
    )

    appendix = _build_prompt_appendix(
        db=None,
        prompt_mode="resume_delta",
        recovery_required=False,
        conversation_id=None,
        pending_events=pending_events,
    )

    assert "latest_outcome_kind=draft_rejected" in appendix
    assert "triggered_by=manual_rerun" in appendix
    assert "forced_route_target_id=support" in appendix
    assert "waiting_on_dev_ti -> ai_triage" in appendix


def test_format_replay_turn_includes_failed_and_rejected_history():
    from worker.codex_inputs import _format_replay_turn

    turn = SimpleNamespace(
        id=uuid.uuid4(),
        ai_run_id=uuid.uuid4(),
        turn_index=4,
        specialist_id="support",
        route_target_id="support",
        status="failed",
        accepted_at=datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 8, 24, 10, 5, tzinfo=timezone.utc),
    )
    ai_run = SimpleNamespace(
        final_output_json={
            "public_reply_markdown": "Draft that never shipped.",
            "internal_note_markdown": "Manual follow-up required.",
            "summary_internal": "Requester needed human review.",
        }
    )
    outcomes = [
        SimpleNamespace(outcome_index=1, outcome_kind="attempted", payload_json={"resumed": True}, created_at=datetime.now(timezone.utc), id=uuid.uuid4()),
        SimpleNamespace(outcome_index=2, outcome_kind="failed", payload_json={"error": "stored thread was not found"}, created_at=datetime.now(timezone.utc), id=uuid.uuid4()),
        SimpleNamespace(outcome_index=3, outcome_kind="draft_rejected", payload_json={"draft_id": "draft-1"}, created_at=datetime.now(timezone.utc), id=uuid.uuid4()),
    ]
    draft = SimpleNamespace(status="rejected", body_markdown="Rejected draft body.")
    db = _ReplayDb(outcomes=outcomes, ai_run=ai_run, draft=draft, published_message=None)

    replay = _format_replay_turn(db, turn=turn)

    assert "status=failed" in replay
    assert "draft_status=rejected" in replay
    assert "outcome_kind=failed" in replay
    assert "outcome_kind=draft_rejected" in replay


def test_build_prompt_conversation_state_uses_fallback_replay_when_feature_disabled(monkeypatch):
    from worker.codex_inputs import KnownConversationInputs, OrderedInputEvent, build_prompt_conversation_state

    conversation = SimpleNamespace(id=uuid.uuid4(), ticket_id=uuid.uuid4(), status="active")
    session = SimpleNamespace(id=uuid.uuid4(), thread_id="thread-123", ended_at=None)
    db = _PromptStateDb(conversation=conversation, session=session, turn_ids=[uuid.uuid4()])
    context = _make_context()
    run = SimpleNamespace(
        id=uuid.uuid4(),
        triggered_by="requester_reply",
        requested_by_user_id=None,
        forced_route_target_id=None,
        forced_specialist_id=None,
        recovered_from_run_id=None,
        recovery_attempt_count=0,
    )
    event = OrderedInputEvent(
        event_kind="ticket_message",
        source_kind="ticket_message",
        source_id=context.public_messages[0].id,
        dedupe_key="ticket-message:1",
        payload_json={"visibility": "public", "body_text": "Please continue."},
        order_key=(1,),
    )

    monkeypatch.setattr("worker.codex_inputs.build_ordered_input_events", lambda *args, **kwargs: (event,))
    monkeypatch.setattr(
        "worker.codex_inputs._load_conversation_known_inputs",
        lambda *args, **kwargs: KnownConversationInputs(
            dedupe_keys=frozenset({event.dedupe_key}),
            causal_ai_run_ids=frozenset(),
            causal_outcome_payloads={},
            outcome_ai_run_ids={},
        ),
    )
    monkeypatch.setattr("worker.codex_inputs._build_prompt_appendix", lambda *args, **kwargs: "Replay appendix")

    prompt_state = build_prompt_conversation_state(
        db,
        context=context,
        run=run,
        feature_enabled=False,
    )

    assert prompt_state.prompt_mode == "fallback_replay"
    assert prompt_state.prompt_appendix == "Replay appendix"
    assert prompt_state.pending_events == (event,)


def test_build_prompt_conversation_state_resume_delta_excludes_exact_causally_known_ai_output(monkeypatch):
    from worker.codex_inputs import KnownConversationInputs, OrderedInputEvent, build_prompt_conversation_state

    conversation = SimpleNamespace(id=uuid.uuid4(), ticket_id=uuid.uuid4(), status="active")
    session = SimpleNamespace(id=uuid.uuid4(), thread_id="thread-123", ended_at=None)
    db = _PromptStateDb(conversation=conversation, session=session, turn_ids=[uuid.uuid4()])
    context = _make_context()
    prior_run_id = uuid.uuid4()
    outcome_id = uuid.uuid4()
    run = SimpleNamespace(
        id=uuid.uuid4(),
        triggered_by="requester_reply",
        requested_by_user_id=None,
        forced_route_target_id=None,
        forced_specialist_id=None,
        recovered_from_run_id=None,
        recovery_attempt_count=0,
    )
    event = OrderedInputEvent(
        event_kind="ticket_message",
        source_kind="ticket_message",
        source_id=uuid.uuid4(),
        dedupe_key="ticket-message:causal-ai-output",
        payload_json={
            "author_type": "ai",
            "source": "ai_auto_public",
            "body_text": "Already generated in the prior turn.",
            "ai_run_id": str(prior_run_id),
            "codex_turn_outcome_id": str(outcome_id),
        },
        order_key=(1,),
    )
    known_inputs = KnownConversationInputs(
        dedupe_keys=frozenset(),
        causal_ai_run_ids=frozenset({prior_run_id}),
        causal_outcome_payloads={outcome_id: {"body_markdown": "Already generated in the prior turn."}},
        outcome_ai_run_ids={outcome_id: prior_run_id},
    )
    monkeypatch.setattr("worker.codex_inputs.build_ordered_input_events", lambda *args, **kwargs: (event,))
    monkeypatch.setattr("worker.codex_inputs._load_conversation_known_inputs", lambda *args, **kwargs: known_inputs)

    prompt_state = build_prompt_conversation_state(db, context=context, run=run, feature_enabled=True)

    assert prompt_state.prompt_mode == "resume_delta"
    assert prompt_state.current_events == (event,)
    assert prompt_state.pending_events == ()


def test_strict_unseen_input_events_returns_empty_when_every_event_is_known(monkeypatch):
    from worker.codex_inputs import (
        KnownConversationInputs,
        OrderedInputEvent,
        load_strictly_unseen_input_events,
    )

    conversation_id = uuid.uuid4()
    context = _make_context()
    run = SimpleNamespace(id=uuid.uuid4())
    event = OrderedInputEvent(
        event_kind="ticket_message",
        source_kind="ticket_message",
        source_id=context.public_messages[0].id,
        dedupe_key=f"ticket-message:{context.public_messages[0].id}",
        payload_json={
            "author_type": "requester",
            "visibility": "public",
            "source": "requester_reply",
            "body_text": "Please continue.",
        },
        order_key=(1,),
    )
    monkeypatch.setattr("worker.codex_inputs.build_ordered_input_events", lambda *args, **kwargs: (event,))
    monkeypatch.setattr(
        "worker.codex_inputs._load_conversation_known_inputs",
        lambda *args, **kwargs: KnownConversationInputs(
            dedupe_keys=frozenset({event.dedupe_key}),
            causal_ai_run_ids=frozenset(),
            causal_outcome_payloads={},
            outcome_ai_run_ids={},
        ),
    )

    unseen = load_strictly_unseen_input_events(
        object(),
        context=context,
        run=run,
        conversation_id=conversation_id,
    )

    assert unseen == ()


def test_conversation_knowledge_projects_accepted_ids_and_reuses_causal_predicate_without_message_query():
    from shared.codex_knowledge import causal_message_is_known_to_conversation, load_conversation_known_inputs

    conversation_id = uuid.uuid4()
    prior_run_id = uuid.uuid4()
    accepted_message_id = uuid.uuid4()
    internal_message_id = uuid.uuid4()
    edited_message_id = uuid.uuid4()
    internal_outcome_id = uuid.uuid4()
    edited_outcome_id = uuid.uuid4()
    internal_payload = {"internal_note_markdown": "Exact internal note."}
    edited_payload = {
        "edited": True,
        "published_body_markdown": "Edited by an operator.",
        "original_draft_body_markdown": "Original draft.",
    }
    db = _KnownInputsDb(
        consumed_rows=[
            (
                f"ticket-message:{accepted_message_id}",
                "ticket_message",
                "ticket_message",
                accepted_message_id,
                {"body_text": "Accepted human message."},
                prior_run_id,
            )
        ],
        turn_rows=[(prior_run_id,)],
        outcome_rows=[
            (internal_outcome_id, internal_payload, prior_run_id),
            (edited_outcome_id, edited_payload, prior_run_id),
        ],
    )

    known = load_conversation_known_inputs(db, conversation_id=conversation_id)

    assert accepted_message_id in known.ticket_message_ids
    assert db.execute_calls == 4
    assert causal_message_is_known_to_conversation(
        known_inputs=known,
        author_type="ai",
        source="ai_internal_note",
        body_candidates=("Exact internal note.",),
        ai_run_id=prior_run_id,
        outcome_id=internal_outcome_id,
    )
    assert not causal_message_is_known_to_conversation(
        known_inputs=known,
        author_type="ai",
        source="ai_draft_published",
        body_candidates=("Edited by an operator.",),
        ai_run_id=prior_run_id,
        outcome_id=edited_outcome_id,
    )
    assert internal_message_id not in known.ticket_message_ids
    assert edited_message_id not in known.ticket_message_ids


def test_causal_known_predicate_requires_consistent_run_ownership_and_body_evidence():
    from shared.codex_knowledge import KnownConversationInputs, causal_message_is_known_to_conversation

    outcome_run_id = uuid.uuid4()
    mismatched_run_id = uuid.uuid4()
    outcome_id = uuid.uuid4()
    bodyless_draft_outcome_id = uuid.uuid4()
    known = KnownConversationInputs(
        dedupe_keys=frozenset(),
        causal_ai_run_ids=frozenset({outcome_run_id, mismatched_run_id}),
        causal_outcome_payloads={
            outcome_id: {"body_markdown": "Exact AI output."},
            bodyless_draft_outcome_id: {"edited": False},
        },
        outcome_ai_run_ids={
            outcome_id: outcome_run_id,
            bodyless_draft_outcome_id: outcome_run_id,
        },
    )

    assert causal_message_is_known_to_conversation(
        known_inputs=known,
        author_type="ai",
        source="ai_auto_public",
        body_candidates=("Exact AI output.",),
        ai_run_id=outcome_run_id,
        outcome_id=outcome_id,
    )
    assert not causal_message_is_known_to_conversation(
        known_inputs=known,
        author_type="ai",
        source="ai_auto_public",
        body_candidates=("Exact AI output.",),
        ai_run_id=mismatched_run_id,
        outcome_id=outcome_id,
    )
    assert not causal_message_is_known_to_conversation(
        known_inputs=known,
        author_type="ai",
        source="ai_draft_published",
        body_candidates=("Body unavailable from the outcome.",),
        ai_run_id=outcome_run_id,
        outcome_id=bodyless_draft_outcome_id,
    )


def test_strict_unseen_counts_active_turn_accepted_start_inputs_as_known(monkeypatch):
    from worker.codex_inputs import OrderedInputEvent, load_strictly_unseen_input_events

    conversation_id = uuid.uuid4()
    context = _make_context()
    run = SimpleNamespace(id=uuid.uuid4())
    message_id = context.public_messages[0].id
    event = OrderedInputEvent(
        event_kind="ticket_message",
        source_kind="ticket_message",
        source_id=message_id,
        dedupe_key=f"ticket-message:{message_id}",
        payload_json={
            "author_type": "requester",
            "visibility": "public",
            "source": "requester_reply",
            "body_text": "Please continue.",
        },
        order_key=(1,),
    )
    db = _KnownInputsDb(
        consumed_rows=[
            (
                event.dedupe_key,
                event.event_kind,
                event.source_kind,
                event.source_id,
                event.payload_json,
                run.id,
            )
        ],
        turn_rows=[(run.id,)],
    )
    monkeypatch.setattr("worker.codex_inputs.build_ordered_input_events", lambda *args, **kwargs: (event,))

    unseen = load_strictly_unseen_input_events(
        db,
        context=context,
        run=run,
        conversation_id=conversation_id,
    )

    assert unseen == ()


def test_strict_unseen_counts_accepted_steering_receipts_as_known(monkeypatch):
    from worker.codex_inputs import OrderedInputEvent, load_strictly_unseen_input_events

    conversation_id = uuid.uuid4()
    context = _make_context()
    run = SimpleNamespace(id=uuid.uuid4())
    prior_run_id = uuid.uuid4()
    message_id = context.public_messages[0].id
    event = OrderedInputEvent(
        event_kind="ticket_message",
        source_kind="ticket_message",
        source_id=message_id,
        dedupe_key=f"ticket-message:{message_id}",
        payload_json={
            "author_type": "requester",
            "visibility": "public",
            "source": "requester_reply",
            "body_text": "Please continue.",
        },
        order_key=(1,),
    )
    db = _KnownInputsDb(
        steer_rows=[
            (
                event.dedupe_key,
                event.source_kind,
                event.source_id,
                prior_run_id,
            )
        ],
        turn_rows=[(prior_run_id,)],
    )
    monkeypatch.setattr("worker.codex_inputs.build_ordered_input_events", lambda *args, **kwargs: (event,))

    unseen = load_strictly_unseen_input_events(
        db,
        context=context,
        run=run,
        conversation_id=conversation_id,
    )

    assert unseen == ()


def test_strict_unseen_retains_non_message_state_changes_for_future_publication_fencing(monkeypatch):
    from worker.codex_inputs import KnownConversationInputs, OrderedInputEvent, load_strictly_unseen_input_events

    conversation_id = uuid.uuid4()
    context = _make_context()
    run = SimpleNamespace(id=uuid.uuid4())
    message_id = context.public_messages[0].id
    accepted_message = OrderedInputEvent(
        event_kind="ticket_message",
        source_kind="ticket_message",
        source_id=message_id,
        dedupe_key=f"ticket-message:{message_id}",
        payload_json={
            "author_type": "requester",
            "visibility": "public",
            "source": "requester_reply",
            "body_text": "Please continue.",
        },
        order_key=(1,),
    )
    state_event = OrderedInputEvent(
        event_kind="ticket_state_snapshot",
        source_kind="ticket",
        source_id=context.ticket.id,
        dedupe_key="ticket-state:updated",
        payload_json={"status": "ai_triage", "last_ai_action": "manual_only"},
        order_key=(2,),
    )
    status_event = OrderedInputEvent(
        event_kind="ticket_status_changed",
        source_kind="ticket_status_history",
        source_id=uuid.uuid4(),
        dedupe_key="ticket-status:late",
        payload_json={"from_status": "waiting_on_user", "to_status": "ai_triage"},
        order_key=(3,),
    )
    monkeypatch.setattr(
        "worker.codex_inputs.build_ordered_input_events",
        lambda *args, **kwargs: (accepted_message, state_event, status_event),
    )
    monkeypatch.setattr(
        "worker.codex_inputs._load_conversation_known_inputs",
        lambda *args, **kwargs: KnownConversationInputs(
            dedupe_keys=frozenset({accepted_message.dedupe_key}),
            causal_ai_run_ids=frozenset(),
            causal_outcome_payloads={},
            outcome_ai_run_ids={},
        ),
    )

    unseen = load_strictly_unseen_input_events(
        object(),
        context=context,
        run=run,
        conversation_id=conversation_id,
        include_turn_summaries=False,
    )

    assert unseen == (state_event, status_event)


def test_causal_known_predicate_excludes_exact_ai_outputs_but_not_human_edits_or_review_feedback():
    from worker.codex_inputs import KnownConversationInputs, OrderedInputEvent, event_is_known_to_conversation

    conversation_id = uuid.uuid4()
    ai_run_id = uuid.uuid4()
    outcome_id = uuid.uuid4()
    edited_outcome_id = uuid.uuid4()
    active_run_id = uuid.uuid4()
    known_inputs = KnownConversationInputs(
        dedupe_keys=frozenset(),
        causal_ai_run_ids=frozenset({ai_run_id}),
        causal_outcome_payloads={
            outcome_id: {"body_markdown": "Original AI reply."},
            edited_outcome_id: {
                "edited": True,
                "published_body_markdown": "Edited by an operator.",
                "original_draft_body_markdown": "Original draft.",
            },
        },
        outcome_ai_run_ids={outcome_id: ai_run_id, edited_outcome_id: ai_run_id},
    )
    ai_output = OrderedInputEvent(
        event_kind="ticket_message",
        source_kind="ticket_message",
        source_id=uuid.uuid4(),
        dedupe_key="ticket-message:ai-output",
        payload_json={
            "author_type": "ai",
            "source": "ai_auto_public",
            "body_text": "Original AI reply.",
            "ai_run_id": str(ai_run_id),
            "codex_turn_outcome_id": str(outcome_id),
        },
        order_key=(1,),
    )
    divergent_ai_output = OrderedInputEvent(
        event_kind="ticket_message",
        source_kind="ticket_message",
        source_id=uuid.uuid4(),
        dedupe_key="ticket-message:divergent-ai-output",
        payload_json={
            "author_type": "ai",
            "source": "ai_auto_public",
            "body_text": "Edited or divergent reply.",
            "ai_run_id": str(ai_run_id),
            "codex_turn_outcome_id": str(outcome_id),
        },
        order_key=(2,),
    )
    human_edited_publication = OrderedInputEvent(
        event_kind="ticket_message",
        source_kind="ticket_message",
        source_id=uuid.uuid4(),
        dedupe_key="ticket-message:edited-publication",
        payload_json={
            "author_type": "ai",
            "source": "ai_draft_published",
            "body_markdown": "Edited by an operator.",
            "body_text": "Edited by an operator.",
            "ai_run_id": str(ai_run_id),
            "codex_turn_outcome_id": str(edited_outcome_id),
        },
        order_key=(3,),
    )
    differing_review_feedback = OrderedInputEvent(
        event_kind="prior_turn_summary",
        source_kind="ai_run",
        source_id=ai_run_id,
        dedupe_key="turn-summary:1:3",
        payload_json={
            "latest_outcome_kind": "draft_rejected",
            "draft": {"status": "rejected"},
            "latest_outcome_payload": {"review_feedback": "Too terse."},
        },
        order_key=(4,),
    )
    active_turn_output = OrderedInputEvent(
        event_kind="ticket_message",
        source_kind="ticket_message",
        source_id=uuid.uuid4(),
        dedupe_key="ticket-message:active-output",
        payload_json={
            "author_type": "ai",
            "source": "ai_internal_note",
            "body_text": "Provisional note.",
            "ai_run_id": str(active_run_id),
        },
        order_key=(5,),
    )

    assert event_is_known_to_conversation(
        object(),
        event=ai_output,
        conversation_id=conversation_id,
        known_inputs=known_inputs,
    )
    assert not event_is_known_to_conversation(
        object(),
        event=divergent_ai_output,
        conversation_id=conversation_id,
        known_inputs=known_inputs,
    )
    assert not event_is_known_to_conversation(
        object(),
        event=human_edited_publication,
        conversation_id=conversation_id,
        known_inputs=known_inputs,
    )
    assert not event_is_known_to_conversation(
        object(),
        event=differing_review_feedback,
        conversation_id=conversation_id,
        known_inputs=known_inputs,
    )
    assert event_is_known_to_conversation(
        object(),
        event=active_turn_output,
        conversation_id=conversation_id,
        known_inputs=known_inputs,
        exclude_ai_run_id=active_run_id,
    )


def test_message_event_uses_stable_canonical_envelope_for_public_and_internal_messages():
    from worker.codex_inputs import build_ordered_input_events

    context = _make_context()
    run = SimpleNamespace(
        id=uuid.uuid4(),
        triggered_by="requester_reply",
        requested_by_user_id=None,
        forced_route_target_id=None,
        forced_specialist_id=None,
        recovered_from_run_id=None,
        recovery_attempt_count=0,
    )

    events = build_ordered_input_events(
        _InputEventsDb(),
        context=context,
        run=run,
        conversation_id=None,
    )
    message_events = [event for event in events if event.source_kind == "ticket_message"]
    by_visibility = {event.payload_json["visibility"]: event for event in message_events}

    assert by_visibility["public"].dedupe_key == f"ticket-message:{context.public_messages[0].id}"
    assert by_visibility["internal"].dedupe_key == f"ticket-message:{context.internal_messages[0].id}"
    assert by_visibility["public"].payload_json["ticket_id"] == str(context.ticket.id)
    assert by_visibility["public"].payload_json["author"]["type"] == "requester"
    assert by_visibility["internal"].payload_json["visibility"] == "internal"
    assert by_visibility["internal"].payload_json["source"] == "human_internal_note"


def test_build_ordered_input_events_projects_all_internal_sources_in_deterministic_order():
    from worker.codex_inputs import build_ordered_input_events

    public_time = datetime(2026, 8, 24, 9, tzinfo=timezone.utc)
    internal_time = datetime(2026, 8, 24, 10, tzinfo=timezone.utc)
    public_messages = (
        SimpleNamespace(
            id=uuid.UUID(int=10),
            author_type="requester",
            visibility="public",
            source="requester_reply",
            created_at=public_time,
            body_text="Requester follow-up.",
        ),
        SimpleNamespace(
            id=uuid.UUID(int=11),
            author_type="ai",
            visibility="public",
            source="ai_draft_published",
            created_at=public_time,
            body_text="Edited by an operator.",
            body_markdown="Edited by an operator.",
            ai_run_id=uuid.uuid4(),
            codex_turn_outcome_id=uuid.uuid4(),
        ),
    )
    internal_messages = (
        SimpleNamespace(
            id=uuid.UUID(int=20),
            author_type="dev_ti",
            visibility="internal",
            source="human_internal_note",
            created_at=internal_time,
            body_text="Human internal note.",
        ),
        SimpleNamespace(
            id=uuid.UUID(int=21),
            author_type="system",
            visibility="internal",
            source="system",
            created_at=internal_time,
            body_text="System workflow message.",
        ),
        SimpleNamespace(
            id=uuid.UUID(int=22),
            author_type="ai",
            visibility="internal",
            source="ai_internal_note",
            created_at=internal_time,
            body_text="AI internal note.",
            body_markdown="AI internal note.",
            ai_run_id=uuid.uuid4(),
            codex_turn_outcome_id=uuid.uuid4(),
        ),
    )
    context = _make_context(public_messages=public_messages, internal_messages=internal_messages)
    run = SimpleNamespace(
        id=uuid.uuid4(),
        triggered_by="requester_reply",
        requested_by_user_id=None,
        forced_route_target_id=None,
        forced_specialist_id=None,
        recovered_from_run_id=None,
        recovery_attempt_count=0,
    )

    events = build_ordered_input_events(
        _InputEventsDb(),
        context=context,
        run=run,
        conversation_id=None,
    )
    message_events = [event for event in events if event.source_kind == "ticket_message"]

    assert [event.payload_json["source"] for event in message_events] == [
        "requester_reply",
        "ai_draft_published",
        "human_internal_note",
        "system",
        "ai_internal_note",
    ]
    assert [event.payload_json["visibility"] for event in message_events] == [
        "public",
        "public",
        "internal",
        "internal",
        "internal",
    ]


def test_strictly_unseen_input_events_exclude_only_exact_causally_known_ai_duplicates(monkeypatch):
    from worker.codex_inputs import KnownConversationInputs, build_ordered_input_events, load_strictly_unseen_input_events

    ai_run_id = uuid.uuid4()
    public_outcome_id = uuid.uuid4()
    internal_outcome_id = uuid.uuid4()
    edited_outcome_id = uuid.uuid4()
    public_messages = (
        SimpleNamespace(
            id=uuid.UUID(int=30),
            author_type="requester",
            visibility="public",
            source="requester_reply",
            created_at=datetime(2026, 8, 24, 9, tzinfo=timezone.utc),
            body_text="Requester follow-up.",
        ),
        SimpleNamespace(
            id=uuid.UUID(int=31),
            author_type="ai",
            visibility="public",
            source="ai_auto_public",
            created_at=datetime(2026, 8, 24, 10, tzinfo=timezone.utc),
            body_text="Original AI reply.",
            body_markdown="Original AI reply.",
            ai_run_id=ai_run_id,
            codex_turn_outcome_id=public_outcome_id,
        ),
        SimpleNamespace(
            id=uuid.UUID(int=32),
            author_type="ai",
            visibility="public",
            source="ai_draft_published",
            created_at=datetime(2026, 8, 24, 11, tzinfo=timezone.utc),
            body_text="Edited by an operator.",
            body_markdown="Edited by an operator.",
            ai_run_id=ai_run_id,
            codex_turn_outcome_id=edited_outcome_id,
        ),
    )
    internal_messages = (
        SimpleNamespace(
            id=uuid.UUID(int=40),
            author_type="dev_ti",
            visibility="internal",
            source="human_internal_note",
            created_at=datetime(2026, 8, 24, 12, tzinfo=timezone.utc),
            body_text="Human internal note.",
        ),
        SimpleNamespace(
            id=uuid.UUID(int=41),
            author_type="system",
            visibility="internal",
            source="system",
            created_at=datetime(2026, 8, 24, 13, tzinfo=timezone.utc),
            body_text="System workflow message.",
        ),
        SimpleNamespace(
            id=uuid.UUID(int=42),
            author_type="ai",
            visibility="internal",
            source="ai_internal_note",
            created_at=datetime(2026, 8, 24, 14, tzinfo=timezone.utc),
            body_text="Original internal note.",
            body_markdown="Original internal note.",
            ai_run_id=ai_run_id,
            codex_turn_outcome_id=internal_outcome_id,
        ),
    )
    context = _make_context(public_messages=public_messages, internal_messages=internal_messages)
    run = SimpleNamespace(
        id=uuid.uuid4(),
        triggered_by="requester_reply",
        requested_by_user_id=None,
        forced_route_target_id=None,
        forced_specialist_id=None,
        recovered_from_run_id=None,
        recovery_attempt_count=0,
    )
    known_inputs = KnownConversationInputs(
        dedupe_keys=frozenset(),
        causal_ai_run_ids=frozenset({ai_run_id}),
        causal_outcome_payloads={
            public_outcome_id: {"body_markdown": "Original AI reply.", "public_reply_markdown": "Original AI reply."},
            internal_outcome_id: {"internal_note_markdown": "Original internal note."},
            edited_outcome_id: {
                "edited": True,
                "published_body_markdown": "Edited by an operator.",
                "original_draft_body_markdown": "Original AI reply.",
            },
        },
        outcome_ai_run_ids={
            public_outcome_id: ai_run_id,
            internal_outcome_id: ai_run_id,
            edited_outcome_id: ai_run_id,
        },
    )
    monkeypatch.setattr("worker.codex_inputs._load_conversation_known_inputs", lambda *args, **kwargs: known_inputs)

    all_sources = [
        event.payload_json["source"]
        for event in build_ordered_input_events(
            _InputEventsDb(),
            context=context,
            run=run,
            conversation_id=uuid.uuid4(),
            include_turn_summaries=False,
        )
        if event.source_kind == "ticket_message"
    ]
    unseen_sources = [
        event.payload_json["source"]
        for event in load_strictly_unseen_input_events(
            _InputEventsDb(),
            context=context,
            run=run,
            conversation_id=uuid.uuid4(),
            include_turn_summaries=False,
        )
        if event.source_kind == "ticket_message"
    ]

    assert all_sources == [
        "requester_reply",
        "ai_auto_public",
        "ai_draft_published",
        "human_internal_note",
        "system",
        "ai_internal_note",
    ]
    assert unseen_sources == [
        "requester_reply",
        "ai_draft_published",
        "human_internal_note",
        "system",
    ]


def test_supported_attachment_message_renders_as_one_bundle():
    from worker.codex_inputs import build_ordered_input_events, render_ticket_message_bundle

    context = _make_context()
    attachment = SimpleNamespace(
        id=uuid.uuid4(),
        ticket_id=context.ticket.id,
        message_id=context.public_messages[0].id,
        visibility="public",
        original_filename="report.pdf",
        stored_path="/tmp/report.pdf",
        mime_type="application/pdf",
        sha256="abc123",
        size_bytes=1024,
        width=None,
        height=None,
        created_at=datetime(2026, 8, 24, 1, tzinfo=timezone.utc),
    )
    context = SimpleNamespace(**{**context.__dict__, "public_attachments": (attachment,)})
    run = SimpleNamespace(
        id=uuid.uuid4(),
        triggered_by="requester_reply",
        requested_by_user_id=None,
        forced_route_target_id=None,
        forced_specialist_id=None,
        recovered_from_run_id=None,
        recovery_attempt_count=0,
    )

    event = next(
        event
        for event in build_ordered_input_events(_InputEventsDb(), context=context, run=run, conversation_id=None)
        if event.source_kind == "ticket_message" and event.payload_json["visibility"] == "public"
    )
    rendered = render_ticket_message_bundle(event)

    assert rendered["body_text"] == "Please continue."
    assert rendered["attachments"][0]["attachment_id"] == str(attachment.id)
    assert rendered["attachments"][0]["safe_input"]["stored_path"] == "/tmp/report.pdf"
    assert event.payload_json["bundle"]["representation_status"] == "supported"


def test_unsupported_or_oversized_attachment_refuses_whole_message_bundle_and_remains_unseen(monkeypatch):
    from worker.codex_inputs import (
        KnownConversationInputs,
        UnsupportedInputBundleError,
        build_ordered_input_events,
        event_is_known_to_conversation,
        load_strictly_unseen_input_events,
        render_ticket_message_bundle,
    )

    conversation_id = uuid.uuid4()
    context = _make_context()
    attachment = SimpleNamespace(
        id=uuid.uuid4(),
        ticket_id=context.ticket.id,
        message_id=context.public_messages[0].id,
        visibility="public",
        original_filename="large.mov",
        stored_path="/tmp/large.mov",
        mime_type="video/quicktime",
        sha256="def456",
        size_bytes=10_000,
        width=None,
        height=None,
        created_at=datetime(2026, 8, 24, 1, tzinfo=timezone.utc),
    )
    context = SimpleNamespace(**{**context.__dict__, "public_attachments": (attachment,)})
    run = SimpleNamespace(
        id=uuid.uuid4(),
        triggered_by="requester_reply",
        requested_by_user_id=None,
        forced_route_target_id=None,
        forced_specialist_id=None,
        recovered_from_run_id=None,
        recovery_attempt_count=0,
    )
    event = next(
        event
        for event in build_ordered_input_events(
            _InputEventsDb(),
            context=context,
            run=run,
            conversation_id=None,
            max_attachment_bytes=100,
        )
        if event.source_kind == "ticket_message" and event.payload_json["visibility"] == "public"
    )
    known_inputs = KnownConversationInputs(
        dedupe_keys=frozenset(),
        causal_ai_run_ids=frozenset(),
        causal_outcome_payloads={},
        outcome_ai_run_ids={},
    )
    monkeypatch.setattr("worker.codex_inputs.build_ordered_input_events", lambda *args, **kwargs: (event,))
    monkeypatch.setattr("worker.codex_inputs._load_conversation_known_inputs", lambda *args, **kwargs: known_inputs)

    with pytest.raises(UnsupportedInputBundleError):
        render_ticket_message_bundle(event)

    assert event.payload_json["bundle"]["representation_status"] == "unsupported"
    assert not event_is_known_to_conversation(
        object(),
        event=event,
        conversation_id=conversation_id,
        known_inputs=known_inputs,
    )
    assert load_strictly_unseen_input_events(
        object(),
        context=context,
        run=run,
        conversation_id=conversation_id,
        max_attachment_bytes=100,
    ) == (event,)


def test_prepare_persistent_specialist_step_replaces_missing_native_session(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")

    from worker import persistent_codex
    from shared.agent_specs import load_agent_spec
    from shared.config import Settings
    from worker.output_contracts import RouterResult
    from worker.step_runner import prepare_step_run

    workspace_dir = tmp_path / "workspace"
    settings = Settings(
        app_base_url="http://localhost:8000",
        app_secret_key="test-secret",
        database_url="postgresql+psycopg://triage:triage@localhost:5432/triage",
        uploads_dir=workspace_dir / "attachments_store",
        triage_workspace_dir=workspace_dir,
        repo_mount_dir=workspace_dir / "app",
        manuals_mount_dir=workspace_dir / "manuals",
        codex_bin="codex",
        codex_api_key="test-key",
        codex_model="",
        codex_timeout_seconds=3600,
        worker_poll_seconds=10,
        auto_support_reply_min_confidence=0.85,
        auto_confirm_intent_min_confidence=0.90,
        max_images_per_message=3,
        max_image_bytes=5 * 1024 * 1024,
        session_default_hours=12,
        session_remember_days=30,
    )
    settings = Settings(
        **{
            **settings.__dict__,
            "codex_conversations_enabled": True,
            "codex_home": workspace_dir / ".codex",
        }
    )
    context = _make_context()
    spec = load_agent_spec("support")
    router_result = RouterResult.model_validate(
        {
            "route_target_id": "support",
            "routing_rationale": "Support path.",
        }
    )
    prepared = prepare_step_run(
        settings,
        run_id=uuid.uuid4(),
        ticket_id=context.ticket.id,
        worker_instance_id="worker-test",
        step_index=2,
        step_kind="specialist",
        spec=spec,
        context=context,
        router_result=router_result,
        target_route_target_id="support",
    )
    run = SimpleNamespace(id=prepared.run_id, worker_pid=1234, last_heartbeat_at=None)
    conversation = SimpleNamespace(id=uuid.uuid4(), ticket_id=prepared.ticket_id, status="recovery_required")
    session = SimpleNamespace(
        id=uuid.uuid4(),
        conversation_id=conversation.id,
        thread_id="thread-stale",
        lease_owner_run_id=None,
        lease_worker_instance_id=None,
        lease_acquired_at=None,
        lease_heartbeat_at=None,
        lease_expires_at=None,
        status="active",
        started_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
        ended_at=None,
    )
    fake_db = _PrepareDb(conversation=conversation, session=session, prior_turn_count=1)

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr(persistent_codex, "session_scope", fake_session_scope)
    monkeypatch.setattr(persistent_codex, "load_owned_running_run", lambda db, **kwargs: run)
    monkeypatch.setattr(persistent_codex, "_load_locked_conversation", lambda db, **kwargs: conversation)
    monkeypatch.setattr(persistent_codex, "_load_locked_active_session", lambda db, **kwargs: session)
    prompt_state = SimpleNamespace(
        conversation_id=conversation.id,
        active_session_id=session.id,
        prompt_mode="recovery_replay",
        recovery_required=True,
        pending_events=(),
        input_hash="frozen-input",
    )

    persistent = persistent_codex.prepare_persistent_specialist_step(
        settings,
        prepared=prepared,
        prompt_state=prompt_state,
    )

    assert persistent.command_spec.resumed is False
    assert session.status == "replaced"
    assert conversation.status == "active"
    assert persistent.command_spec.runtime_codex_home == settings.resolved_codex_home


def test_stale_unaccepted_turn_creates_recovery_boundary_before_next_turn(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")

    from worker import persistent_codex
    from shared.agent_specs import load_agent_spec
    from shared.config import Settings
    from worker.output_contracts import RouterResult
    from worker.step_runner import prepare_step_run

    workspace_dir = tmp_path / "workspace"
    settings = Settings(
        app_base_url="http://localhost:8000",
        app_secret_key="test-secret",
        database_url="postgresql+psycopg://triage:triage@localhost:5432/triage",
        uploads_dir=workspace_dir / "attachments_store",
        triage_workspace_dir=workspace_dir,
        repo_mount_dir=workspace_dir / "app",
        manuals_mount_dir=workspace_dir / "manuals",
        codex_bin="codex",
        codex_api_key="test-key",
        codex_model="",
        codex_timeout_seconds=3600,
        worker_poll_seconds=10,
        auto_support_reply_min_confidence=0.85,
        auto_confirm_intent_min_confidence=0.90,
        max_images_per_message=3,
        max_image_bytes=5 * 1024 * 1024,
        session_default_hours=12,
        session_remember_days=30,
    )
    settings = Settings(
        **{
            **settings.__dict__,
            "codex_conversations_enabled": True,
            "codex_home": workspace_dir / ".codex",
        }
    )
    context = _make_context()
    next_run_id = uuid.uuid4()
    prepared = prepare_step_run(
        settings,
        run_id=next_run_id,
        ticket_id=context.ticket.id,
        worker_instance_id="worker-test",
        step_index=2,
        step_kind="specialist",
        spec=load_agent_spec("support"),
        context=context,
        router_result=RouterResult.model_validate(
            {
                "route_target_id": "support",
                "routing_rationale": "Support path.",
            }
        ),
        target_route_target_id="support",
    )
    stale_run = SimpleNamespace(id=uuid.uuid4())
    next_run = SimpleNamespace(id=prepared.run_id, worker_pid=1234, last_heartbeat_at=None)
    conversation = SimpleNamespace(id=uuid.uuid4(), ticket_id=prepared.ticket_id, status="active")
    stale_session = SimpleNamespace(
        id=uuid.uuid4(),
        conversation_id=conversation.id,
        thread_id="thread-uncertain",
        lease_owner_run_id=stale_run.id,
        lease_worker_instance_id="worker-stale",
        lease_acquired_at=datetime.now(timezone.utc),
        lease_heartbeat_at=datetime.now(timezone.utc),
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        status="active",
        started_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
        ended_at=None,
    )
    stale_turn = SimpleNamespace(
        id=uuid.uuid4(),
        ai_run_id=stale_run.id,
        conversation_id=conversation.id,
        session_id=stale_session.id,
        turn_index=1,
        accepted_at=None,
        status="running",
        ended_at=None,
    )

    class RecoveryBoundaryDb:
        def __init__(self):
            self.added = []
            self.active_session = stale_session
            self.flush_calls = 0

        def add(self, item):
            self.added.append(item)
            if getattr(item, "id", None) is None:
                item.id = uuid.uuid4()
            if item.__class__.__name__ == "CodexSession":
                self.active_session = item

        def flush(self):
            self.flush_calls += 1

        def get(self, model, key):
            if getattr(model, "__name__", "") == "CodexConversation" and key == conversation.id:
                return conversation
            return None

        def execute(self, statement):
            descriptions = statement.column_descriptions
            first_name = descriptions[0]["name"]
            entity = descriptions[0].get("entity")
            first_type = descriptions[0].get("type")
            entity_name = getattr(entity or first_type, "__name__", "")
            if first_name == "count" or "count" in first_name:
                return _FakeScalarResult([1])
            if first_name == "coalesce":
                return _FakeScalarResult([1])
            if entity_name == "CodexTurn":
                return _FakeScalarResult([stale_turn])
            if entity_name == "CodexSession":
                return _FakeScalarResult([stale_session])
            if entity_name == "CodexTurnSteer":
                return _FakeScalarResult([])
            raise AssertionError(f"unexpected execute call: {first_name}")

    fake_db = RecoveryBoundaryDb()

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr(persistent_codex, "session_scope", fake_session_scope)
    monkeypatch.setattr(persistent_codex, "load_owned_running_run", lambda db, **kwargs: next_run)
    monkeypatch.setattr(persistent_codex, "_load_locked_conversation", lambda db, **kwargs: conversation)
    monkeypatch.setattr(
        persistent_codex,
        "_load_locked_active_session",
        lambda db, **kwargs: fake_db.active_session if fake_db.active_session.ended_at is None else None,
    )

    assert persistent_codex.handle_stale_persistent_run(
        fake_db,
        run=stale_run,
        stale_timeout_seconds=600,
    )
    assert stale_turn.status == "interrupted"
    assert stale_session.status == "replaced"
    assert stale_session.ended_at is not None
    assert conversation.status == "recovery_required"

    persistent = persistent_codex.prepare_persistent_specialist_step(
        settings,
        prepared=prepared,
        prompt_state=SimpleNamespace(
            conversation_id=conversation.id,
            active_session_id=None,
            prompt_mode="recovery_replay",
            recovery_required=True,
            pending_events=(),
            input_hash="next-event-input",
        ),
    )

    replacement_sessions = [
        item for item in fake_db.added if item.__class__.__name__ == "CodexSession" and item is not stale_session
    ]
    assert len(replacement_sessions) == 1
    assert persistent.session_id == replacement_sessions[0].id
    assert persistent.session_id != stale_session.id
    assert persistent.command_spec.resumed is False
    assert "thread-uncertain" not in persistent.command_spec.command
    assert replacement_sessions[0].lease_owner_run_id == next_run.id
    assert conversation.status == "active"
