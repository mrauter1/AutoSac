from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
from types import SimpleNamespace
import uuid

import httpx
import pytest

from shared.config import Settings, SlackSettings
from shared.slack_dm import SlackWebApiResponse, encrypt_slack_bot_token


def _make_slack_settings(
    *,
    enabled: bool = True,
    is_valid: bool = True,
    config_error_code: str | None = None,
    config_error_summary: str | None = None,
    delivery_batch_size: int = 5,
    delivery_max_attempts: int = 3,
    delivery_stale_lock_seconds: int = 120,
    bot_token: str | None = "xoxb-test-token",
) -> SlackSettings:
    return SlackSettings(
        enabled=enabled,
        notify_ticket_created=True,
        notify_public_message_added=True,
        notify_status_changed=True,
        http_timeout_seconds=10,
        delivery_batch_size=delivery_batch_size,
        delivery_max_attempts=delivery_max_attempts,
        delivery_stale_lock_seconds=delivery_stale_lock_seconds,
        has_stored_token=bot_token is not None,
        bot_token_ciphertext=(
            encrypt_slack_bot_token("test-secret", bot_token) if bot_token is not None else None
        ),
        team_id="T123",
        team_name="AutoSac",
        bot_user_id="B123",
        is_valid=is_valid,
        config_error_code=config_error_code,
        config_error_summary=config_error_summary,
        routing_mode="dm",
    )


def _make_settings(tmp_path: Path, *, slack: SlackSettings | None = None) -> Settings:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        app_base_url="https://autosac.example.local",
        app_secret_key="test-secret",
        database_url="postgresql+psycopg://triage:triage@localhost:5432/triage",
        uploads_dir=tmp_path / "uploads",
        triage_workspace_dir=workspace_dir,
        repo_mount_dir=workspace_dir / "app",
        manuals_mount_dir=workspace_dir / "manuals",
        codex_bin="codex",
        codex_api_key="test-key",
        codex_model="gpt-test",
        codex_timeout_seconds=3600,
        worker_poll_seconds=10,
        auto_support_reply_min_confidence=0.85,
        auto_confirm_intent_min_confidence=0.90,
        max_images_per_message=3,
        max_image_bytes=5 * 1024 * 1024,
        session_default_hours=12,
        session_remember_days=30,
        slack=slack or _make_slack_settings(),
    )


def _make_slack_runtime(settings: Settings):
    from worker.slack_delivery import build_worker_slack_runtime_context

    return build_worker_slack_runtime_context(settings)


def _make_web_api_response(
    method: str,
    *,
    http_status: int = 200,
    ok: bool,
    error: str | None = None,
    body_extra: dict | None = None,
    retry_after_seconds: int | None = None,
) -> SlackWebApiResponse:
    body_json: dict[str, object] = {"ok": ok}
    if error is not None:
        body_json["error"] = error
    if body_extra:
        body_json.update(body_extra)
    return SlackWebApiResponse(
        method=method,
        http_status=http_status,
        body_json=body_json,
        retry_after_seconds=retry_after_seconds,
    )


def _load_symbols():
    pytest.importorskip("sqlalchemy")
    from worker.main import WorkerIdentity, start_slack_delivery_thread
    from worker.slack_delivery import (
        ClaimedDeliveryTarget,
        DeliveryOutcome,
        claim_delivery_targets,
        classify_delivery_attempt,
        delivery_loop,
        deliver_claimed_target,
        finalize_delivery_claim,
        load_claimed_processing_target,
        recover_stale_delivery_targets,
        render_slack_message,
        restore_claimed_delivery_targets,
        run_delivery_cycle,
        run_delivery_cycle_preflight,
    )

    return {
        "ClaimedDeliveryTarget": ClaimedDeliveryTarget,
        "DeliveryOutcome": DeliveryOutcome,
        "WorkerIdentity": WorkerIdentity,
        "claim_delivery_targets": claim_delivery_targets,
        "classify_delivery_attempt": classify_delivery_attempt,
        "delivery_loop": delivery_loop,
        "deliver_claimed_target": deliver_claimed_target,
        "finalize_delivery_claim": finalize_delivery_claim,
        "load_claimed_processing_target": load_claimed_processing_target,
        "recover_stale_delivery_targets": recover_stale_delivery_targets,
        "render_slack_message": render_slack_message,
        "restore_claimed_delivery_targets": restore_claimed_delivery_targets,
        "run_delivery_cycle": run_delivery_cycle,
        "run_delivery_cycle_preflight": run_delivery_cycle_preflight,
        "start_slack_delivery_thread": start_slack_delivery_thread,
    }


class _FakeScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def __iter__(self):
        return iter(self._rows)


class _FakeScalarOneOrNoneResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _ClaimDb:
    def __init__(self, *, targets, events_by_id):
        self.targets = list(targets)
        self.events_by_id = dict(events_by_id)
        self.statements = []

    def execute(self, statement):
        self.statements.append(statement)
        return _FakeScalarResult(self.targets)

    def get(self, model, key):
        if getattr(model, "__name__", "") == "IntegrationEvent":
            return self.events_by_id.get(key)
        return None


class _TargetStateDb:
    def __init__(self, *, target, owned: bool = True):
        self.target = target
        self.owned = owned
        self.statements = []

    def execute(self, statement):
        self.statements.append(statement)
        if self.target is None or not self.owned:
            return _FakeScalarOneOrNoneResult(None)
        return _FakeScalarOneOrNoneResult(self.target)


def _make_claimed_target(
    symbols,
    *,
    attempt_count: int = 1,
    previous_delivery_status: str = "pending",
    previous_attempt_count: int | None = None,
):
    if previous_attempt_count is None:
        previous_attempt_count = max(attempt_count - 1, 0)
    recipient_user_id = uuid.uuid4()
    event_id = uuid.uuid4()
    return symbols["ClaimedDeliveryTarget"](
        target_id=uuid.uuid4(),
        event_id=event_id,
        event_type="ticket.public_message_added",
        target_name=f"user:{recipient_user_id}",
        recipient_user_id=recipient_user_id,
        recipient_reason="requester",
        previous_delivery_status=previous_delivery_status,
        previous_attempt_count=previous_attempt_count,
        attempt_count=attempt_count,
        locked_by="worker-test",
        claim_token=uuid.uuid4(),
        payload_json={
            "ticket_reference": "T-000123",
            "ticket_url": "https://autosac.example.local/ops/tickets/T-000123",
            "message_author_type": "requester",
            "message_preview": "Hello <@U123>\n<!channel>\t& team",
        },
    )


def test_render_slack_message_escapes_user_derived_fields(tmp_path):
    symbols = _load_symbols()

    text = symbols["render_slack_message"](
        event_type="ticket.public_message_added",
        payload_json={
            "ticket_reference": "T-000123",
            "ticket_url": "https://autosac.example.local/ops/tickets/T-000123",
            "message_author_type": "requester",
            "message_preview": "Hello <@U123>\n<!channel>\t& team",
        },
    )

    assert "Nova mensagem publica em T-000123 por requester" in text
    assert "&lt;@U123&gt;" in text
    assert "&lt;!channel&gt;" in text
    assert "&amp; team" in text
    assert "<!channel>" not in text


def test_sanitize_operator_summary_redacts_urls():
    from worker.slack_delivery import _sanitize_operator_summary

    sanitized = _sanitize_operator_summary(
        "boom https://hooks.slack.com/services/T000/B000/SECRET and hooks.slack.com/services/T111/B111/SECRET",
    )

    assert "hooks.slack.com/services" not in sanitized
    assert sanitized.count("[redacted-url]") == 2


def test_claim_delivery_targets_marks_rows_processing_and_uses_skip_locked(tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path, slack=_make_slack_settings(delivery_batch_size=2))
    slack_runtime = _make_slack_runtime(settings)
    first_event_id = uuid.uuid4()
    second_event_id = uuid.uuid4()
    first_recipient = uuid.uuid4()
    second_recipient = uuid.uuid4()
    first_target = SimpleNamespace(
        id=uuid.uuid4(),
        event_id=first_event_id,
        target_name=f"user:{first_recipient}",
        recipient_user_id=first_recipient,
        recipient_reason="requester",
        delivery_status="pending",
        attempt_count=0,
        next_attempt_at=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
        created_at=datetime(2026, 4, 10, 11, 0, tzinfo=timezone.utc),
        locked_at=None,
        locked_by=None,
        claim_token=None,
    )
    second_target = SimpleNamespace(
        id=uuid.uuid4(),
        event_id=second_event_id,
        target_name=f"user:{second_recipient}",
        recipient_user_id=second_recipient,
        recipient_reason="assignee",
        delivery_status="failed",
        attempt_count=2,
        next_attempt_at=datetime(2026, 4, 10, 12, 1, tzinfo=timezone.utc),
        created_at=datetime(2026, 4, 10, 11, 1, tzinfo=timezone.utc),
        locked_at=None,
        locked_by=None,
        claim_token=None,
    )
    db = _ClaimDb(
        targets=[first_target, second_target],
        events_by_id={
            first_event_id: SimpleNamespace(
                event_type="ticket.created",
                payload_json={"ticket_reference": "T-1", "ticket_url": "https://x", "ticket_title": "A"},
            ),
            second_event_id: SimpleNamespace(
                event_type="ticket.status_changed",
                payload_json={"ticket_reference": "T-2", "ticket_url": "https://x", "status_from": "new", "status_to": "resolved"},
            ),
        },
    )

    claimed = symbols["claim_delivery_targets"](
        db,
        slack_runtime=slack_runtime,
        locked_by="worker-test",
    )

    assert [item.event_type for item in claimed] == ["ticket.created", "ticket.status_changed"]
    assert claimed[0].recipient_user_id == first_recipient
    assert claimed[0].recipient_reason == "requester"
    assert claimed[1].recipient_user_id == second_recipient
    assert claimed[1].recipient_reason == "assignee"
    assert first_target.delivery_status == "processing"
    assert first_target.attempt_count == 1
    assert first_target.locked_by == "worker-test"
    assert first_target.claim_token is not None
    assert second_target.delivery_status == "processing"
    assert second_target.attempt_count == 3
    assert second_target.locked_by == "worker-test"
    assert second_target.claim_token is not None
    assert claimed[0].claim_token == first_target.claim_token
    assert claimed[1].claim_token == second_target.claim_token
    assert db.statements[0]._for_update_arg.skip_locked is True


def test_recover_stale_delivery_targets_preserves_attempt_count_and_clears_lock(tmp_path, monkeypatch):
    symbols = _load_symbols()
    fixed_now = datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc)
    settings = _make_settings(tmp_path)
    stale_target = SimpleNamespace(
        id=uuid.uuid4(),
        event_id=uuid.uuid4(),
        target_name=f"user:{uuid.uuid4()}",
        recipient_user_id=uuid.uuid4(),
        recipient_reason="requester",
        delivery_status="processing",
        attempt_count=4,
        next_attempt_at=datetime(2026, 4, 10, 13, 0, tzinfo=timezone.utc),
        created_at=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
        locked_at=datetime(2026, 4, 10, 13, 30, tzinfo=timezone.utc),
        locked_by="worker-old",
        claim_token=uuid.uuid4(),
        last_error=None,
    )
    db = _ClaimDb(targets=[stale_target], events_by_id={})

    monkeypatch.setattr("worker.slack_delivery.utc_now", lambda: fixed_now)
    slack_runtime = _make_slack_runtime(settings)

    recovered = symbols["recover_stale_delivery_targets"](db, slack_runtime=slack_runtime)

    assert recovered == [stale_target]
    assert stale_target.delivery_status == "failed"
    assert stale_target.attempt_count == 4
    assert stale_target.locked_at is None
    assert stale_target.locked_by is None
    assert stale_target.claim_token is None
    assert stale_target.next_attempt_at == fixed_now
    assert "stale_lock_recovered" in stale_target.last_error
    assert "recipient_user_id=" in stale_target.last_error
    assert db.statements[0]._for_update_arg.skip_locked is True


def test_load_claimed_processing_target_uses_claim_token_and_for_update(tmp_path):
    symbols = _load_symbols()
    target_row = SimpleNamespace(id=uuid.uuid4())
    fake_db = _TargetStateDb(target=target_row)
    claim_token = uuid.uuid4()

    loaded = symbols["load_claimed_processing_target"](
        fake_db,
        target_id=target_row.id,
        claim_token=claim_token,
    )

    assert loaded is target_row
    assert fake_db.statements[0]._for_update_arg is not None
    assert "claim_token" in str(fake_db.statements[0])


def test_deliver_claimed_target_marks_sent_on_success_and_uses_current_recipient_slack_id(monkeypatch, tmp_path):
    symbols = _load_symbols()
    fixed_now = datetime(2026, 4, 10, 14, 5, tzinfo=timezone.utc)
    settings = _make_settings(tmp_path)
    claimed_target = _make_claimed_target(symbols)
    target_row = SimpleNamespace(
        id=claimed_target.target_id,
        delivery_status="processing",
        attempt_count=claimed_target.attempt_count,
        next_attempt_at=datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc),
        locked_at=datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc),
        locked_by="worker-test",
        claim_token=claimed_target.claim_token,
        last_error="boom",
        sent_at=None,
        dead_lettered_at=None,
    )
    fake_db = _TargetStateDb(target=target_row)
    observed = []
    calls = {}

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    def fake_load_recipient(_settings, *, recipient_user_id):
        assert recipient_user_id == claimed_target.recipient_user_id
        return SimpleNamespace(id=recipient_user_id, is_active=True, slack_user_id="U999")

    def fake_open_conversation(*, bot_token: str, slack_user_id: str, timeout_seconds: int):
        calls["open"] = {
            "bot_token": bot_token,
            "slack_user_id": slack_user_id,
            "timeout_seconds": timeout_seconds,
        }
        return _make_web_api_response(
            "conversations.open",
            ok=True,
            body_extra={"channel": {"id": "D123"}},
        )

    def fake_post_message(*, bot_token: str, channel_id: str, text: str, timeout_seconds: int):
        calls["post"] = {
            "bot_token": bot_token,
            "channel_id": channel_id,
            "text": text,
            "timeout_seconds": timeout_seconds,
        }
        return _make_web_api_response("chat.postMessage", ok=True)

    monkeypatch.setattr("worker.slack_delivery.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.slack_delivery.utc_now", lambda: fixed_now)
    monkeypatch.setattr("worker.slack_delivery.log_worker_event", lambda event, **payload: observed.append((event, payload)))
    slack_runtime = _make_slack_runtime(settings)

    symbols["deliver_claimed_target"](
        slack_runtime,
        claimed_target=claimed_target,
        bot_token="xoxb-token",
        load_recipient=fake_load_recipient,
        open_conversation=fake_open_conversation,
        post_message=fake_post_message,
    )

    assert calls["open"] == {
        "bot_token": "xoxb-token",
        "slack_user_id": "U999",
        "timeout_seconds": 10,
    }
    assert calls["post"]["bot_token"] == "xoxb-token"
    assert calls["post"]["channel_id"] == "D123"
    assert "Nova mensagem publica em T-000123 por requester" in calls["post"]["text"]
    assert target_row.delivery_status == "sent"
    assert target_row.sent_at == fixed_now
    assert target_row.dead_lettered_at is None
    assert target_row.last_error is None
    assert target_row.locked_at is None
    assert target_row.locked_by is None
    assert target_row.claim_token is None
    assert observed == [
        (
            "slack_delivery_sent",
            {
                "event_id": str(claimed_target.event_id),
                "target_name": claimed_target.target_name,
                "recipient_user_id": str(claimed_target.recipient_user_id),
                "recipient_reason": claimed_target.recipient_reason,
                "delivery_status": "sent",
                "attempt_count": claimed_target.attempt_count,
                "locked_by": claimed_target.locked_by,
                "claim_token": str(claimed_target.claim_token),
                "http_status": 200,
            },
        )
    ]


def test_classify_delivery_attempt_requires_ok_true_for_conversations_open(tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path)
    claimed_target = _make_claimed_target(symbols)
    slack_runtime = _make_slack_runtime(settings)

    outcome = symbols["classify_delivery_attempt"](
        slack_runtime,
        claimed_target=claimed_target,
        bot_token="xoxb-token",
        load_recipient=lambda _settings, *, recipient_user_id: SimpleNamespace(
            id=recipient_user_id,
            is_active=True,
            slack_user_id="U123",
        ),
        open_conversation=lambda **_kwargs: _make_web_api_response(
            "conversations.open",
            ok=False,
            error="user_not_found",
        ),
    )

    assert outcome.kind == "dead_letter_terminal"
    assert outcome.failure_class == "user_not_found"
    assert outcome.terminal_reason == "terminal_failure"


def test_classify_delivery_attempt_requires_ok_true_for_chat_post_message(monkeypatch, tmp_path):
    symbols = _load_symbols()
    fixed_now = datetime(2026, 4, 10, 14, 6, tzinfo=timezone.utc)
    settings = _make_settings(tmp_path, slack=_make_slack_settings(delivery_max_attempts=4))
    claimed_target = _make_claimed_target(symbols, attempt_count=2)
    monkeypatch.setattr("worker.slack_delivery.utc_now", lambda: fixed_now)
    slack_runtime = _make_slack_runtime(settings)

    outcome = symbols["classify_delivery_attempt"](
        slack_runtime,
        claimed_target=claimed_target,
        bot_token="xoxb-token",
        load_recipient=lambda _settings, *, recipient_user_id: SimpleNamespace(
            id=recipient_user_id,
            is_active=True,
            slack_user_id="U123",
        ),
        open_conversation=lambda **_kwargs: _make_web_api_response(
            "conversations.open",
            ok=True,
            body_extra={"channel": {"id": "D123"}},
        ),
        post_message=lambda **_kwargs: _make_web_api_response(
            "chat.postMessage",
            ok=False,
            error="internal_error",
        ),
    )

    assert outcome.kind == "retryable_failure"
    assert outcome.failure_class == "internal_error"
    assert outcome.next_attempt_at == fixed_now + timedelta(seconds=120)


@pytest.mark.parametrize(
    ("recipient", "expected_failure_class"),
    [
        (None, "missing_recipient_user"),
        (SimpleNamespace(is_active=False, slack_user_id="U123"), "inactive_recipient_user"),
        (SimpleNamespace(is_active=True, slack_user_id="   "), "missing_recipient_slack_user_id"),
    ],
)
def test_deliver_claimed_target_dead_letters_invalid_recipients_without_slack_calls(
    monkeypatch,
    tmp_path,
    recipient,
    expected_failure_class,
):
    symbols = _load_symbols()
    fixed_now = datetime(2026, 4, 10, 14, 7, tzinfo=timezone.utc)
    settings = _make_settings(tmp_path)
    claimed_target = _make_claimed_target(symbols)
    target_row = SimpleNamespace(
        id=claimed_target.target_id,
        delivery_status="processing",
        attempt_count=claimed_target.attempt_count,
        next_attempt_at=datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc),
        locked_at=datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc),
        locked_by="worker-test",
        claim_token=claimed_target.claim_token,
        last_error=None,
        sent_at=None,
        dead_lettered_at=None,
    )
    fake_db = _TargetStateDb(target=target_row)
    send_calls = {"open": 0, "post": 0}

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.slack_delivery.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.slack_delivery.utc_now", lambda: fixed_now)
    slack_runtime = _make_slack_runtime(settings)

    symbols["deliver_claimed_target"](
        slack_runtime,
        claimed_target=claimed_target,
        bot_token="xoxb-token",
        load_recipient=lambda _settings, *, recipient_user_id: recipient,
        open_conversation=lambda **_kwargs: send_calls.__setitem__("open", send_calls["open"] + 1),
        post_message=lambda **_kwargs: send_calls.__setitem__("post", send_calls["post"] + 1),
    )

    assert send_calls == {"open": 0, "post": 0}
    assert target_row.delivery_status == "dead_letter"
    assert target_row.dead_lettered_at == fixed_now
    assert expected_failure_class in target_row.last_error


def test_deliver_claimed_target_retries_transport_errors(monkeypatch, tmp_path):
    symbols = _load_symbols()
    fixed_now = datetime(2026, 4, 10, 14, 6, tzinfo=timezone.utc)
    settings = _make_settings(tmp_path)
    claimed_target = _make_claimed_target(symbols, attempt_count=2)
    target_row = SimpleNamespace(
        id=claimed_target.target_id,
        delivery_status="processing",
        attempt_count=claimed_target.attempt_count,
        next_attempt_at=datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc),
        locked_at=datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc),
        locked_by="worker-test",
        claim_token=claimed_target.claim_token,
        last_error=None,
        sent_at=None,
        dead_lettered_at=None,
    )
    fake_db = _TargetStateDb(target=target_row)
    observed = []

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    request = httpx.Request("POST", "https://slack.com/api/conversations.open")

    monkeypatch.setattr("worker.slack_delivery.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.slack_delivery.utc_now", lambda: fixed_now)
    monkeypatch.setattr("worker.slack_delivery.log_worker_event", lambda event, **payload: observed.append((event, payload)))
    slack_runtime = _make_slack_runtime(settings)

    symbols["deliver_claimed_target"](
        slack_runtime,
        claimed_target=claimed_target,
        bot_token="xoxb-token",
        load_recipient=lambda _settings, *, recipient_user_id: SimpleNamespace(
            id=recipient_user_id,
            is_active=True,
            slack_user_id="U123",
        ),
        open_conversation=lambda **_kwargs: (_ for _ in ()).throw(httpx.ReadTimeout("timed out", request=request)),
    )

    assert target_row.delivery_status == "failed"
    assert target_row.sent_at is None
    assert target_row.dead_lettered_at is None
    assert target_row.locked_at is None
    assert target_row.locked_by is None
    assert target_row.claim_token is None
    assert target_row.next_attempt_at == fixed_now + timedelta(seconds=120)
    assert "ReadTimeout" in target_row.last_error
    assert observed == [
        (
            "slack_delivery_retry_scheduled",
            {
                "event_id": str(claimed_target.event_id),
                "target_name": claimed_target.target_name,
                "recipient_user_id": str(claimed_target.recipient_user_id),
                "recipient_reason": claimed_target.recipient_reason,
                "delivery_status": "failed",
                "attempt_count": claimed_target.attempt_count,
                "locked_by": claimed_target.locked_by,
                "claim_token": str(claimed_target.claim_token),
                "next_attempt_at": (fixed_now + timedelta(seconds=120)).isoformat(),
                "failure_class": "ReadTimeout",
            },
        )
    ]


def test_classify_delivery_attempt_honors_retry_after_floor_for_429(monkeypatch, tmp_path):
    symbols = _load_symbols()
    fixed_now = datetime(2026, 4, 10, 14, 6, tzinfo=timezone.utc)
    settings = _make_settings(tmp_path, slack=_make_slack_settings(delivery_max_attempts=4))
    claimed_target = _make_claimed_target(symbols, attempt_count=2)
    monkeypatch.setattr("worker.slack_delivery.utc_now", lambda: fixed_now)
    slack_runtime = _make_slack_runtime(settings)

    outcome = symbols["classify_delivery_attempt"](
        slack_runtime,
        claimed_target=claimed_target,
        bot_token="xoxb-token",
        load_recipient=lambda _settings, *, recipient_user_id: SimpleNamespace(
            id=recipient_user_id,
            is_active=True,
            slack_user_id="U123",
        ),
        open_conversation=lambda **_kwargs: _make_web_api_response(
            "conversations.open",
            ok=True,
            body_extra={"channel": {"id": "D123"}},
        ),
        post_message=lambda **_kwargs: _make_web_api_response(
            "chat.postMessage",
            http_status=429,
            ok=False,
            error="ratelimited",
            retry_after_seconds=240,
        ),
    )

    assert outcome.kind == "retryable_failure"
    assert outcome.failure_class == "http_429"
    assert outcome.next_attempt_at == fixed_now + timedelta(seconds=240)


def test_deliver_claimed_target_dead_letters_recipient_specific_slack_errors(monkeypatch, tmp_path):
    symbols = _load_symbols()
    fixed_now = datetime(2026, 4, 10, 14, 8, tzinfo=timezone.utc)
    settings = _make_settings(tmp_path)
    claimed_target = _make_claimed_target(symbols)
    target_row = SimpleNamespace(
        id=claimed_target.target_id,
        delivery_status="processing",
        attempt_count=claimed_target.attempt_count,
        next_attempt_at=datetime(2026, 4, 10, 15, 0, tzinfo=timezone.utc),
        locked_at=datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc),
        locked_by="worker-test",
        claim_token=claimed_target.claim_token,
        last_error=None,
        sent_at=None,
        dead_lettered_at=None,
    )
    fake_db = _TargetStateDb(target=target_row)

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.slack_delivery.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.slack_delivery.utc_now", lambda: fixed_now)
    slack_runtime = _make_slack_runtime(settings)

    symbols["deliver_claimed_target"](
        slack_runtime,
        claimed_target=claimed_target,
        bot_token="xoxb-token",
        load_recipient=lambda _settings, *, recipient_user_id: SimpleNamespace(
            id=recipient_user_id,
            is_active=True,
            slack_user_id="U123",
        ),
        open_conversation=lambda **_kwargs: _make_web_api_response(
            "conversations.open",
            ok=True,
            body_extra={"channel": {"id": "D123"}},
        ),
        post_message=lambda **_kwargs: _make_web_api_response(
            "chat.postMessage",
            ok=False,
            error="channel_not_found",
        ),
    )

    assert target_row.delivery_status == "dead_letter"
    assert target_row.dead_lettered_at == fixed_now
    assert "recipient_error=channel_not_found" in target_row.last_error


def test_deliver_claimed_target_returns_suppression_and_skips_finalization_when_send_finds_invalid_auth(monkeypatch, tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path)
    claimed_target = _make_claimed_target(symbols)
    finalize_calls = {"count": 0}
    slack_runtime = _make_slack_runtime(settings)

    @contextmanager
    def fake_session_scope(_settings):
        finalize_calls["count"] += 1
        yield SimpleNamespace()

    monkeypatch.setattr("worker.slack_delivery.session_scope", fake_session_scope)

    suppression = symbols["deliver_claimed_target"](
        slack_runtime,
        claimed_target=claimed_target,
        bot_token="xoxb-token",
        load_recipient=lambda _settings, *, recipient_user_id: SimpleNamespace(
            id=recipient_user_id,
            is_active=True,
            slack_user_id="U123",
        ),
        open_conversation=lambda **_kwargs: _make_web_api_response(
            "conversations.open",
            ok=False,
            error="invalid_auth",
        ),
    )

    assert suppression is not None
    assert suppression.reason == "invalid_config"
    assert suppression.config_error_code == "invalid_auth"
    assert suppression.config_error_summary == "Slack conversations.open returned invalid_auth"
    assert suppression.delivery_halted is True
    assert finalize_calls["count"] == 0


def test_finalize_delivery_claim_uses_supplied_retryable_outcome_without_recomputing(monkeypatch, tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path, slack=_make_slack_settings(delivery_max_attempts=1))
    claimed_target = _make_claimed_target(symbols, attempt_count=5)
    supplied_next_attempt_at = datetime(2026, 4, 10, 16, 0, tzinfo=timezone.utc)
    target_row = SimpleNamespace(
        id=claimed_target.target_id,
        delivery_status="processing",
        attempt_count=claimed_target.attempt_count,
        next_attempt_at=datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc),
        locked_at=datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc),
        locked_by="worker-test",
        claim_token=claimed_target.claim_token,
        last_error=None,
        sent_at=None,
        dead_lettered_at=None,
    )
    fake_db = _TargetStateDb(target=target_row)

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.slack_delivery.session_scope", fake_session_scope)
    slack_runtime = _make_slack_runtime(settings)
    outcome = symbols["DeliveryOutcome"].retryable_failure(
        last_error="retryable",
        failure_class="ReadTimeout",
        next_attempt_at=supplied_next_attempt_at,
    )

    result = symbols["finalize_delivery_claim"](
        slack_runtime,
        claimed_target=claimed_target,
        outcome=outcome,
    )

    assert result == "failed"
    assert target_row.delivery_status == "failed"
    assert target_row.next_attempt_at == supplied_next_attempt_at
    assert target_row.dead_lettered_at is None
    assert target_row.claim_token is None


def test_finalize_delivery_claim_returns_ownership_lost_without_mutation(monkeypatch, tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path)
    claimed_target = _make_claimed_target(symbols)
    target_row = SimpleNamespace(
        id=claimed_target.target_id,
        delivery_status="processing",
        attempt_count=claimed_target.attempt_count + 1,
        next_attempt_at=datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc),
        locked_at=datetime(2026, 4, 10, 14, 5, tzinfo=timezone.utc),
        locked_by="worker-new",
        claim_token=uuid.uuid4(),
        last_error="keep-me",
        sent_at=None,
        dead_lettered_at=None,
    )
    fake_db = _TargetStateDb(target=target_row, owned=False)

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.slack_delivery.session_scope", fake_session_scope)
    slack_runtime = _make_slack_runtime(settings)

    result = symbols["finalize_delivery_claim"](
        slack_runtime,
        claimed_target=claimed_target,
        outcome=symbols["DeliveryOutcome"].sent(http_status=200),
    )

    assert result == "ownership_lost"
    assert target_row.delivery_status == "processing"
    assert target_row.locked_by == "worker-new"
    assert target_row.claim_token is not None
    assert target_row.last_error == "keep-me"


def test_restore_claimed_delivery_targets_restores_preclaim_state_for_owned_rows(monkeypatch, tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path)
    claimed_target = _make_claimed_target(
        symbols,
        attempt_count=3,
        previous_delivery_status="failed",
        previous_attempt_count=2,
    )
    target_row = SimpleNamespace(
        id=claimed_target.target_id,
        delivery_status="processing",
        attempt_count=claimed_target.attempt_count,
        next_attempt_at=datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc),
        locked_at=datetime(2026, 4, 10, 14, 5, tzinfo=timezone.utc),
        locked_by=claimed_target.locked_by,
        claim_token=claimed_target.claim_token,
        last_error="keep-me",
        sent_at=None,
        dead_lettered_at=None,
    )

    @contextmanager
    def fake_session_scope(_settings):
        yield SimpleNamespace()

    def fake_load_claimed_processing_target(_db, *, target_id, claim_token):
        assert target_id == claimed_target.target_id
        assert claim_token == claimed_target.claim_token
        return target_row

    monkeypatch.setattr("worker.slack_delivery.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.slack_delivery.load_claimed_processing_target", fake_load_claimed_processing_target)
    slack_runtime = _make_slack_runtime(settings)

    symbols["restore_claimed_delivery_targets"](
        slack_runtime,
        claimed_targets=[claimed_target],
    )

    assert target_row.delivery_status == "failed"
    assert target_row.attempt_count == 2
    assert target_row.locked_at is None
    assert target_row.locked_by is None
    assert target_row.claim_token is None
    assert target_row.last_error == "keep-me"


def test_deliver_claimed_target_logs_ownership_lost_with_recipient_fields(monkeypatch, tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path)
    claimed_target = _make_claimed_target(symbols)
    target_row = SimpleNamespace(
        id=claimed_target.target_id,
        delivery_status="processing",
        attempt_count=claimed_target.attempt_count + 1,
        next_attempt_at=datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc),
        locked_at=datetime(2026, 4, 10, 14, 5, tzinfo=timezone.utc),
        locked_by="worker-new",
        claim_token=uuid.uuid4(),
        last_error="keep-me",
        sent_at=None,
        dead_lettered_at=None,
    )
    fake_db = _TargetStateDb(target=target_row, owned=False)
    observed = []

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.slack_delivery.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.slack_delivery.log_worker_event", lambda event, **payload: observed.append((event, payload)))
    slack_runtime = _make_slack_runtime(settings)

    symbols["deliver_claimed_target"](
        slack_runtime,
        claimed_target=claimed_target,
        bot_token="xoxb-token",
        load_recipient=lambda _settings, *, recipient_user_id: SimpleNamespace(
            id=recipient_user_id,
            is_active=True,
            slack_user_id="U123",
        ),
        open_conversation=lambda **_kwargs: _make_web_api_response(
            "conversations.open",
            ok=True,
            body_extra={"channel": {"id": "D123"}},
        ),
        post_message=lambda **_kwargs: _make_web_api_response("chat.postMessage", ok=True),
    )

    assert observed == [
        (
            "slack_delivery_ownership_lost",
            {
                "level": "warning",
                "event_id": str(claimed_target.event_id),
                "target_id": str(claimed_target.target_id),
                "target_name": claimed_target.target_name,
                "recipient_user_id": str(claimed_target.recipient_user_id),
                "recipient_reason": claimed_target.recipient_reason,
                "claimed_attempt_count": claimed_target.attempt_count,
                "claimed_locked_by": claimed_target.locked_by,
                "claim_token": str(claimed_target.claim_token),
            },
        )
    ]


def test_deliver_claimed_target_dead_letters_when_retry_budget_is_exhausted(monkeypatch, tmp_path):
    symbols = _load_symbols()
    fixed_now = datetime(2026, 4, 10, 14, 9, tzinfo=timezone.utc)
    settings = _make_settings(tmp_path, slack=_make_slack_settings(delivery_max_attempts=3))
    claimed_target = _make_claimed_target(symbols, attempt_count=3)
    target_row = SimpleNamespace(
        id=claimed_target.target_id,
        delivery_status="processing",
        attempt_count=claimed_target.attempt_count,
        next_attempt_at=datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc),
        locked_at=datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc),
        locked_by="worker-test",
        claim_token=claimed_target.claim_token,
        last_error=None,
        sent_at=None,
        dead_lettered_at=None,
    )
    fake_db = _TargetStateDb(target=target_row)
    observed = []

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.slack_delivery.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.slack_delivery.utc_now", lambda: fixed_now)
    monkeypatch.setattr("worker.slack_delivery.log_worker_event", lambda event, **payload: observed.append((event, payload)))
    slack_runtime = _make_slack_runtime(settings)

    symbols["deliver_claimed_target"](
        slack_runtime,
        claimed_target=claimed_target,
        bot_token="xoxb-token",
        load_recipient=lambda _settings, *, recipient_user_id: SimpleNamespace(
            id=recipient_user_id,
            is_active=True,
            slack_user_id="U123",
        ),
        open_conversation=lambda **_kwargs: _make_web_api_response(
            "conversations.open",
            ok=True,
            body_extra={"channel": {"id": "D123"}},
        ),
        post_message=lambda **_kwargs: _make_web_api_response(
            "chat.postMessage",
            http_status=500,
            ok=False,
        ),
    )

    assert target_row.delivery_status == "dead_letter"
    assert target_row.dead_lettered_at == fixed_now
    assert target_row.claim_token is None
    assert "retryable_http_status=500" in target_row.last_error
    assert observed == [
        (
            "slack_delivery_dead_lettered",
            {
                "event_id": str(claimed_target.event_id),
                "target_name": claimed_target.target_name,
                "recipient_user_id": str(claimed_target.recipient_user_id),
                "recipient_reason": claimed_target.recipient_reason,
                "delivery_status": "dead_letter",
                "attempt_count": claimed_target.attempt_count,
                "locked_by": claimed_target.locked_by,
                "claim_token": str(claimed_target.claim_token),
                "http_status": 500,
                "failure_class": "http_5xx",
                "retry_exhausted": True,
            },
        )
    ]


def test_run_delivery_cycle_uses_db_loaded_runtime_and_logs_claim_with_recipient_fields(monkeypatch, tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path)
    slack_runtime = _make_slack_runtime(settings)
    claimed_target = _make_claimed_target(symbols)
    observed = []
    built_dbs = []
    recovered = []
    claimed = []
    delivered = []

    @contextmanager
    def fake_session_scope(_settings):
        yield SimpleNamespace()

    def fake_build_runtime(_settings, *, db=None, slack=None):
        built_dbs.append(db)
        return slack_runtime

    def fake_preflight(passed_runtime, *, auth_test=None):
        assert passed_runtime is slack_runtime
        return "xoxb-token", None

    def fake_recover_stale_delivery_targets(_db, *, slack_runtime):
        recovered.append(slack_runtime)
        return []

    def fake_claim_delivery_targets(_db, *, slack_runtime, locked_by):
        claimed.append((slack_runtime, locked_by))
        return [claimed_target]

    def fake_deliver_claimed_target(passed_runtime, *, claimed_target, bot_token, **_kwargs):
        delivered.append((passed_runtime, claimed_target, bot_token))
        return None

    monkeypatch.setattr("worker.slack_delivery.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.slack_delivery.build_worker_slack_runtime_context", fake_build_runtime)
    monkeypatch.setattr("worker.slack_delivery.run_delivery_cycle_preflight", fake_preflight)
    monkeypatch.setattr("worker.slack_delivery.recover_stale_delivery_targets", fake_recover_stale_delivery_targets)
    monkeypatch.setattr("worker.slack_delivery.claim_delivery_targets", fake_claim_delivery_targets)
    monkeypatch.setattr("worker.slack_delivery.deliver_claimed_target", fake_deliver_claimed_target)
    monkeypatch.setattr("worker.slack_delivery.log_worker_event", lambda event, **payload: observed.append((event, payload)))

    symbols["run_delivery_cycle"](settings, worker_instance_id="worker-test")

    assert len(built_dbs) == 1
    assert recovered == [slack_runtime]
    assert claimed == [(slack_runtime, "worker-test")]
    assert delivered == [(slack_runtime, claimed_target, "xoxb-token")]
    assert observed == [
        (
            "slack_target_claimed",
            {
                "event_id": str(claimed_target.event_id),
                "target_name": claimed_target.target_name,
                "recipient_user_id": str(claimed_target.recipient_user_id),
                "recipient_reason": claimed_target.recipient_reason,
                "delivery_status": "processing",
                "attempt_count": claimed_target.attempt_count,
                "locked_by": claimed_target.locked_by,
                "claim_token": str(claimed_target.claim_token),
            },
        )
    ]


def test_run_delivery_cycle_suppresses_on_auth_test_invalid_config_and_persists_health(monkeypatch, tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path)
    observed = []
    persisted = []

    monkeypatch.setattr(
        "worker.slack_delivery.slack_api_auth_test",
        lambda **_kwargs: _make_web_api_response("auth.test", ok=False, error="invalid_auth"),
    )
    monkeypatch.setattr("worker.slack_delivery.persist_delivery_health_snapshot", lambda _runtime, *, snapshot: persisted.append(snapshot))
    monkeypatch.setattr(
        "worker.slack_delivery.recover_stale_delivery_targets",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("stale recovery should be skipped")),
    )
    monkeypatch.setattr(
        "worker.slack_delivery.claim_delivery_targets",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("claim should be skipped")),
    )
    monkeypatch.setattr("worker.slack_delivery.log_worker_event", lambda event, **payload: observed.append((event, payload)))
    slack_runtime = _make_slack_runtime(settings)

    symbols["run_delivery_cycle"](slack_runtime, worker_instance_id="worker-test")

    assert len(persisted) == 1
    assert persisted[0].status == "invalid_config"
    assert persisted[0].error_code == "invalid_auth"
    assert persisted[0].summary == "Slack auth.test returned invalid_auth"
    assert observed == [
        (
            "slack_delivery_suppressed",
            {
                "suppression_reason": "invalid_config",
                "claim_skipped": True,
                "stale_lock_recovery_skipped": True,
                "config_error_code": "invalid_auth",
                "config_error_summary": "Slack auth.test returned invalid_auth",
            },
        )
    ]


def test_run_delivery_cycle_restores_unfinalized_claims_when_send_hits_missing_scope(monkeypatch, tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path, slack=_make_slack_settings(delivery_batch_size=2))
    slack_runtime = _make_slack_runtime(settings)
    first_claimed = _make_claimed_target(
        symbols,
        attempt_count=1,
        previous_delivery_status="pending",
        previous_attempt_count=0,
    )
    second_claimed = _make_claimed_target(
        symbols,
        attempt_count=3,
        previous_delivery_status="failed",
        previous_attempt_count=2,
    )
    first_row = SimpleNamespace(
        id=first_claimed.target_id,
        delivery_status="processing",
        attempt_count=first_claimed.attempt_count,
        next_attempt_at=datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc),
        locked_at=datetime(2026, 4, 10, 14, 5, tzinfo=timezone.utc),
        locked_by=first_claimed.locked_by,
        claim_token=first_claimed.claim_token,
        last_error=None,
        sent_at=None,
        dead_lettered_at=None,
    )
    second_row = SimpleNamespace(
        id=second_claimed.target_id,
        delivery_status="processing",
        attempt_count=second_claimed.attempt_count,
        next_attempt_at=datetime(2026, 4, 10, 14, 1, tzinfo=timezone.utc),
        locked_at=datetime(2026, 4, 10, 14, 6, tzinfo=timezone.utc),
        locked_by=second_claimed.locked_by,
        claim_token=second_claimed.claim_token,
        last_error="retryable",
        sent_at=None,
        dead_lettered_at=None,
    )
    rows_by_id = {
        first_claimed.target_id: first_row,
        second_claimed.target_id: second_row,
    }
    observed = []
    persisted = []
    open_calls = []

    @contextmanager
    def fake_session_scope(_settings):
        yield SimpleNamespace()

    def fake_load_claimed_processing_target(_db, *, target_id, claim_token):
        row = rows_by_id.get(target_id)
        if row is None or row.delivery_status != "processing" or row.claim_token != claim_token:
            return None
        return row

    monkeypatch.setattr("worker.slack_delivery.session_scope", fake_session_scope)
    monkeypatch.setattr(
        "worker.slack_delivery.recover_stale_delivery_targets",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "worker.slack_delivery.claim_delivery_targets",
        lambda *_args, **_kwargs: [first_claimed, second_claimed],
    )
    monkeypatch.setattr(
        "worker.slack_delivery.load_claimed_processing_target",
        fake_load_claimed_processing_target,
    )
    monkeypatch.setattr(
        "worker.slack_delivery.load_delivery_recipient",
        lambda _settings, *, recipient_user_id: SimpleNamespace(
            id=recipient_user_id,
            is_active=True,
            slack_user_id="U123",
        ),
    )
    monkeypatch.setattr(
        "worker.slack_delivery.slack_api_auth_test",
        lambda **_kwargs: _make_web_api_response("auth.test", ok=True),
    )

    def fake_open_conversation(**kwargs):
        open_calls.append(kwargs)
        return _make_web_api_response("conversations.open", ok=False, error="missing_scope")

    monkeypatch.setattr("worker.slack_delivery.slack_api_conversations_open", fake_open_conversation)
    monkeypatch.setattr(
        "worker.slack_delivery.slack_api_chat_post_message",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("chat.postMessage should not be called")),
    )
    monkeypatch.setattr("worker.slack_delivery.persist_delivery_health_snapshot", lambda _runtime, *, snapshot: persisted.append(snapshot))
    monkeypatch.setattr("worker.slack_delivery.log_worker_event", lambda event, **payload: observed.append((event, payload)))

    symbols["run_delivery_cycle"](slack_runtime, worker_instance_id="worker-test")

    assert len(open_calls) == 1
    assert [snapshot.status for snapshot in persisted] == ["healthy", "invalid_config"]
    assert persisted[1].error_code == "missing_scope"
    assert persisted[1].summary == "Slack conversations.open returned missing_scope"
    assert first_row.delivery_status == "pending"
    assert first_row.attempt_count == 0
    assert first_row.locked_at is None
    assert first_row.locked_by is None
    assert first_row.claim_token is None
    assert second_row.delivery_status == "failed"
    assert second_row.attempt_count == 2
    assert second_row.locked_at is None
    assert second_row.locked_by is None
    assert second_row.claim_token is None
    assert second_row.last_error == "retryable"
    assert observed == [
        (
            "slack_target_claimed",
            {
                "event_id": str(first_claimed.event_id),
                "target_name": first_claimed.target_name,
                "recipient_user_id": str(first_claimed.recipient_user_id),
                "recipient_reason": first_claimed.recipient_reason,
                "delivery_status": "processing",
                "attempt_count": first_claimed.attempt_count,
                "locked_by": first_claimed.locked_by,
                "claim_token": str(first_claimed.claim_token),
            },
        ),
        (
            "slack_delivery_suppressed",
            {
                "suppression_reason": "invalid_config",
                "claim_skipped": False,
                "stale_lock_recovery_skipped": False,
                "delivery_halted": True,
                "config_error_code": "missing_scope",
                "config_error_summary": "Slack conversations.open returned missing_scope",
            },
        ),
    ]


def test_run_delivery_cycle_skips_disabled_slack_without_mutating_rows(monkeypatch, tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path, slack=_make_slack_settings(enabled=False))
    observed = []

    monkeypatch.setattr(
        "worker.slack_delivery.recover_stale_delivery_targets",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("disabled slack should skip row work")),
    )
    monkeypatch.setattr(
        "worker.slack_delivery.claim_delivery_targets",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("disabled slack should skip row work")),
    )
    monkeypatch.setattr("worker.slack_delivery.log_worker_event", lambda event, **payload: observed.append((event, payload)))
    slack_runtime = _make_slack_runtime(settings)

    symbols["run_delivery_cycle"](slack_runtime, worker_instance_id="worker-test")

    assert observed == []


def test_run_delivery_cycle_preflight_persists_healthy_snapshot_on_success(monkeypatch, tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path)
    persisted = []
    slack_runtime = _make_slack_runtime(settings)

    monkeypatch.setattr(
        "worker.slack_delivery.slack_api_auth_test",
        lambda **_kwargs: _make_web_api_response(
            "auth.test",
            ok=True,
            body_extra={"team_id": "T999", "team": "Workspace", "user_id": "B999"},
        ),
    )
    monkeypatch.setattr("worker.slack_delivery.persist_delivery_health_snapshot", lambda _runtime, *, snapshot: persisted.append(snapshot))

    bot_token, suppression = symbols["run_delivery_cycle_preflight"](slack_runtime)

    assert suppression is None
    assert bot_token == "xoxb-test-token"
    assert len(persisted) == 1
    assert persisted[0].status == "healthy"
    assert persisted[0].team_id == "T999"
    assert persisted[0].team_name == "Workspace"
    assert persisted[0].bot_user_id == "B999"


def test_run_delivery_cycle_preflight_allows_transport_errors_to_continue(monkeypatch, tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path)
    slack_runtime = _make_slack_runtime(settings)

    monkeypatch.setattr(
        "worker.slack_delivery.slack_api_auth_test",
        lambda **_kwargs: (_ for _ in ()).throw(httpx.ConnectError("boom")),
    )

    bot_token, suppression = symbols["run_delivery_cycle_preflight"](slack_runtime)

    assert bot_token == "xoxb-test-token"
    assert suppression is None


def test_delivery_loop_runs_until_stop_event(monkeypatch, tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path)
    slack_runtime = _make_slack_runtime(settings)
    stop_event = threading.Event()
    observed = {"cycles": 0}

    def fake_run_delivery_cycle(_slack_runtime, *, worker_instance_id):
        assert worker_instance_id == "worker-test"
        observed["cycles"] += 1
        stop_event.set()

    monkeypatch.setattr("worker.slack_delivery.run_delivery_cycle", fake_run_delivery_cycle)

    symbols["delivery_loop"](
        slack_runtime,
        worker_instance_id="worker-test",
        stop_event=stop_event,
        interval_seconds=0,
    )

    assert observed["cycles"] == 1


def test_start_slack_delivery_thread_wires_worker_instance_id(monkeypatch, tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path)
    worker_identity = symbols["WorkerIdentity"](worker_pid=2222, worker_instance_id="worker-test")
    observed = {}

    class _FakeThread:
        def __init__(self, *, target, kwargs, name, daemon):
            observed["target"] = target
            observed["kwargs"] = kwargs
            observed["name"] = name
            observed["daemon"] = daemon

        def start(self):
            observed["started"] = True

    monkeypatch.setattr("worker.main.threading.Thread", _FakeThread)

    thread = symbols["start_slack_delivery_thread"](
        settings,
        worker_identity=worker_identity,
    )

    assert isinstance(thread, _FakeThread)
    assert observed["target"] is not None
    assert observed["kwargs"]["worker_instance_id"] == "worker-test"
    assert observed["kwargs"]["slack_runtime"] == settings
    assert observed["name"] == "worker-slack-delivery"
    assert observed["daemon"] is True
    assert observed["started"] is True
