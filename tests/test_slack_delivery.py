from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
from types import SimpleNamespace
import uuid

import httpx
import pytest

from shared.config import Settings, SlackSettings, SlackTargetSettings


def _make_slack_settings(
    *,
    enabled: bool = True,
    default_target_name: str | None = "ops_primary",
    targets: tuple[SlackTargetSettings, ...] | None = None,
    is_valid: bool = True,
    config_error_code: str | None = None,
    config_error_summary: str | None = None,
    delivery_batch_size: int = 5,
    delivery_max_attempts: int = 3,
    delivery_stale_lock_seconds: int = 120,
) -> SlackSettings:
    resolved_targets = (
        (
            SlackTargetSettings(
                name="ops_primary",
                enabled=True,
                webhook_url="https://hooks.slack.com/services/T000/B000/XXXX",
            ),
        )
        if targets is None
        else targets
    )
    return SlackSettings(
        enabled=enabled,
        default_target_name=default_target_name,
        targets=resolved_targets,
        notify_ticket_created=True,
        notify_public_message_added=True,
        notify_status_changed=True,
        http_timeout_seconds=10,
        delivery_batch_size=delivery_batch_size,
        delivery_max_attempts=delivery_max_attempts,
        delivery_stale_lock_seconds=delivery_stale_lock_seconds,
        is_valid=is_valid,
        config_error_code=config_error_code,
        config_error_summary=config_error_summary,
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


def _load_symbols():
    pytest.importorskip("sqlalchemy")
    from worker.main import WorkerIdentity, start_slack_delivery_thread
    from worker.slack_delivery import (
        ClaimedDeliveryTarget,
        DeliveryOutcome,
        build_worker_slack_runtime_context,
        claim_delivery_targets,
        classify_delivery_attempt,
        delivery_loop,
        deliver_claimed_target,
        finalize_delivery_claim,
        load_claimed_processing_target,
        recover_stale_delivery_targets,
        render_slack_message,
        run_delivery_cycle,
        send_slack_webhook,
    )

    return {
        "ClaimedDeliveryTarget": ClaimedDeliveryTarget,
        "DeliveryOutcome": DeliveryOutcome,
        "WorkerIdentity": WorkerIdentity,
        "build_worker_slack_runtime_context": build_worker_slack_runtime_context,
        "claim_delivery_targets": claim_delivery_targets,
        "classify_delivery_attempt": classify_delivery_attempt,
        "delivery_loop": delivery_loop,
        "deliver_claimed_target": deliver_claimed_target,
        "finalize_delivery_claim": finalize_delivery_claim,
        "load_claimed_processing_target": load_claimed_processing_target,
        "recover_stale_delivery_targets": recover_stale_delivery_targets,
        "render_slack_message": render_slack_message,
        "run_delivery_cycle": run_delivery_cycle,
        "send_slack_webhook": send_slack_webhook,
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


def _make_claimed_target(symbols, *, attempt_count: int = 1, target_name: str = "ops_primary"):
    event_id = uuid.uuid4()
    return symbols["ClaimedDeliveryTarget"](
        target_id=uuid.uuid4(),
        event_id=event_id,
        event_type="ticket.public_message_added",
        target_name=target_name,
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


def test_send_slack_webhook_enforces_total_timeout(monkeypatch, tmp_path):
    symbols = _load_symbols()

    async def fake_post_slack_webhook_async(*, webhook_url: str, text: str, timeout_seconds: int) -> int:
        assert webhook_url == "https://hooks.slack.com/services/T000/B000/XXXX"
        assert text == "hello"
        assert timeout_seconds == 0.01
        await asyncio.sleep(0.05)
        return 200

    monkeypatch.setattr("worker.slack_delivery._post_slack_webhook_async", fake_post_slack_webhook_async)

    with pytest.raises(httpx.ReadTimeout):
        symbols["send_slack_webhook"](
            webhook_url="https://hooks.slack.com/services/T000/B000/XXXX",
            text="hello",
            timeout_seconds=0.01,
        )


def test_sanitize_operator_summary_redacts_urls():
    from worker.slack_delivery import _sanitize_operator_summary

    sanitized = _sanitize_operator_summary(
        "boom https://hooks.slack.com/services/T000/B000/SECRET and hooks.slack.com/services/T111/B111/SECRET",
    )

    assert "hooks.slack.com/services" not in sanitized
    assert sanitized.count("[redacted-url]") == 2


def test_deliver_claimed_target_redacts_urls_from_retryable_errors(monkeypatch, tmp_path):
    symbols = _load_symbols()
    fixed_now = datetime(2026, 4, 10, 14, 6, tzinfo=timezone.utc)
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

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.slack_delivery.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.slack_delivery.utc_now", lambda: fixed_now)
    slack_runtime = _make_slack_runtime(settings)

    symbols["deliver_claimed_target"](
        slack_runtime,
        claimed_target=claimed_target,
        send_webhook=lambda **_kwargs: (_ for _ in ()).throw(
            httpx.TransportError("boom https://hooks.slack.com/services/T000/B000/SECRET failed")
        ),
    )

    assert target_row.delivery_status == "failed"
    assert "[redacted-url]" in target_row.last_error
    assert "hooks.slack.com/services" not in target_row.last_error


def test_claim_delivery_targets_marks_rows_processing_and_uses_skip_locked(tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path, slack=_make_slack_settings(delivery_batch_size=2))
    slack_runtime = _make_slack_runtime(settings)
    first_event_id = uuid.uuid4()
    second_event_id = uuid.uuid4()
    first_target = SimpleNamespace(
        id=uuid.uuid4(),
        event_id=first_event_id,
        target_name="ops_primary",
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
        target_name="ops_primary",
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
            first_event_id: SimpleNamespace(event_type="ticket.created", payload_json={"ticket_reference": "T-1", "ticket_url": "https://x", "ticket_title": "A"}),
            second_event_id: SimpleNamespace(event_type="ticket.status_changed", payload_json={"ticket_reference": "T-2", "ticket_url": "https://x", "status_from": "new", "status_to": "resolved"}),
        },
    )

    claimed = symbols["claim_delivery_targets"](
        db,
        slack_runtime=slack_runtime,
        locked_by="worker-test",
    )

    assert [item.event_type for item in claimed] == ["ticket.created", "ticket.status_changed"]
    assert claimed[0].payload_json["ticket_reference"] == "T-1"
    assert claimed[1].payload_json["ticket_reference"] == "T-2"
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
        target_name="ops_primary",
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
    assert "\n" not in stale_target.last_error
    assert db.statements[0]._for_update_arg.skip_locked is True


def test_deliver_claimed_target_marks_sent_on_success(monkeypatch, tmp_path):
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
        send_webhook=lambda **_kwargs: 200,
    )

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
                "delivery_status": "sent",
                "attempt_count": claimed_target.attempt_count,
                "locked_by": claimed_target.locked_by,
                "claim_token": str(claimed_target.claim_token),
                "http_status": 200,
            },
        )
    ]


def test_load_claimed_processing_target_uses_claim_token_and_for_update(tmp_path):
    _load_symbols()
    from worker.slack_delivery import load_claimed_processing_target

    target_row = SimpleNamespace(id=uuid.uuid4())
    fake_db = _TargetStateDb(target=target_row)
    claim_token = uuid.uuid4()

    loaded = load_claimed_processing_target(
        fake_db,
        target_id=target_row.id,
        claim_token=claim_token,
    )

    assert loaded is target_row
    assert fake_db.statements[0]._for_update_arg is not None
    assert "claim_token" in str(fake_db.statements[0])


def test_classify_delivery_attempt_builds_retryable_outcome_before_finalization(monkeypatch, tmp_path):
    symbols = _load_symbols()
    fixed_now = datetime(2026, 4, 10, 14, 6, tzinfo=timezone.utc)
    settings = _make_settings(tmp_path, slack=_make_slack_settings(delivery_max_attempts=4))
    claimed_target = _make_claimed_target(symbols, attempt_count=2)

    monkeypatch.setattr("worker.slack_delivery.utc_now", lambda: fixed_now)
    slack_runtime = _make_slack_runtime(settings)

    outcome = symbols["classify_delivery_attempt"](
        slack_runtime,
        claimed_target=claimed_target,
        send_webhook=lambda **_kwargs: 500,
    )

    assert outcome.kind == "retryable_failure"
    assert outcome.http_status == 500
    assert outcome.failure_class == "http_5xx"
    assert outcome.terminal_reason is None
    assert outcome.next_attempt_at == fixed_now + timedelta(seconds=120)
    assert "retryable_http_status=500" in outcome.last_error


def test_classify_delivery_attempt_converts_retry_exhaustion_before_finalization(monkeypatch, tmp_path):
    symbols = _load_symbols()
    fixed_now = datetime(2026, 4, 10, 14, 9, tzinfo=timezone.utc)
    settings = _make_settings(tmp_path, slack=_make_slack_settings(delivery_max_attempts=3))
    claimed_target = _make_claimed_target(symbols, attempt_count=3)

    monkeypatch.setattr("worker.slack_delivery.utc_now", lambda: fixed_now)
    slack_runtime = _make_slack_runtime(settings)

    outcome = symbols["classify_delivery_attempt"](
        slack_runtime,
        claimed_target=claimed_target,
        send_webhook=lambda **_kwargs: 500,
    )

    assert outcome.kind == "dead_letter_terminal"
    assert outcome.http_status == 500
    assert outcome.failure_class == "http_5xx"
    assert outcome.terminal_reason == "retry_exhausted"
    assert outcome.next_attempt_at is None
    assert "retryable_http_status=500" in outcome.last_error


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


def test_deliver_claimed_target_skips_sent_update_when_ownership_is_lost(monkeypatch, tmp_path):
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
        send_webhook=lambda **_kwargs: 200,
    )

    assert target_row.delivery_status == "processing"
    assert target_row.attempt_count == claimed_target.attempt_count + 1
    assert target_row.locked_by == "worker-new"
    assert target_row.last_error == "keep-me"
    assert target_row.sent_at is None
    assert observed == [
        (
            "slack_delivery_ownership_lost",
            {
                "level": "warning",
                "event_id": str(claimed_target.event_id),
                "target_id": str(claimed_target.target_id),
                "target_name": claimed_target.target_name,
                "claimed_attempt_count": claimed_target.attempt_count,
                "claimed_locked_by": claimed_target.locked_by,
                "claim_token": str(claimed_target.claim_token),
            },
        )
    ]


def test_deliver_claimed_target_retries_retryable_failures(monkeypatch, tmp_path):
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

    monkeypatch.setattr("worker.slack_delivery.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.slack_delivery.utc_now", lambda: fixed_now)
    monkeypatch.setattr("worker.slack_delivery.log_worker_event", lambda event, **payload: observed.append((event, payload)))
    slack_runtime = _make_slack_runtime(settings)

    request = httpx.Request("POST", "https://hooks.slack.com/services/T000/B000/XXXX")

    symbols["deliver_claimed_target"](
        slack_runtime,
        claimed_target=claimed_target,
        send_webhook=lambda **_kwargs: (_ for _ in ()).throw(httpx.ReadTimeout("timed out", request=request)),
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
                "delivery_status": "failed",
                "attempt_count": claimed_target.attempt_count,
                "locked_by": claimed_target.locked_by,
                "claim_token": str(claimed_target.claim_token),
                "next_attempt_at": (fixed_now + timedelta(seconds=120)).isoformat(),
                "failure_class": "ReadTimeout",
            },
        )
    ]


def test_deliver_claimed_target_skips_retry_update_when_ownership_is_lost(monkeypatch, tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path)
    claimed_target = _make_claimed_target(symbols, attempt_count=2)
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

    request = httpx.Request("POST", "https://hooks.slack.com/services/T000/B000/XXXX")

    monkeypatch.setattr("worker.slack_delivery.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.slack_delivery.log_worker_event", lambda event, **payload: observed.append((event, payload)))
    slack_runtime = _make_slack_runtime(settings)

    symbols["deliver_claimed_target"](
        slack_runtime,
        claimed_target=claimed_target,
        send_webhook=lambda **_kwargs: (_ for _ in ()).throw(httpx.ReadTimeout("timed out", request=request)),
    )

    assert target_row.delivery_status == "processing"
    assert target_row.attempt_count == claimed_target.attempt_count + 1
    assert target_row.locked_by == "worker-new"
    assert target_row.last_error == "keep-me"
    assert target_row.next_attempt_at == datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc)
    assert observed == [
        (
            "slack_delivery_ownership_lost",
            {
                "level": "warning",
                "event_id": str(claimed_target.event_id),
                "target_id": str(claimed_target.target_id),
                "target_name": claimed_target.target_name,
                "claimed_attempt_count": claimed_target.attempt_count,
                "claimed_locked_by": claimed_target.locked_by,
                "claim_token": str(claimed_target.claim_token),
            },
        )
    ]


@pytest.mark.parametrize(
    ("targets", "target_name", "expected_error"),
    [
        ((), "ops_primary", "missing_target_config"),
        ((SlackTargetSettings(name="ops_primary", enabled=False, webhook_url="https://hooks.slack.com/services/T000/B000/XXXX"),), "ops_primary", "target_disabled"),
    ],
)
def test_deliver_claimed_target_dead_letters_when_target_is_missing_or_disabled(
    monkeypatch,
    tmp_path,
    targets,
    target_name,
    expected_error,
):
    symbols = _load_symbols()
    fixed_now = datetime(2026, 4, 10, 14, 7, tzinfo=timezone.utc)
    settings = _make_settings(tmp_path, slack=_make_slack_settings(targets=targets))
    claimed_target = _make_claimed_target(symbols, target_name=target_name)
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
    send_calls = {"count": 0}

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    monkeypatch.setattr("worker.slack_delivery.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.slack_delivery.utc_now", lambda: fixed_now)
    slack_runtime = _make_slack_runtime(settings)

    symbols["deliver_claimed_target"](
        slack_runtime,
        claimed_target=claimed_target,
        send_webhook=lambda **_kwargs: send_calls.__setitem__("count", send_calls["count"] + 1),
    )

    assert send_calls["count"] == 0
    assert target_row.delivery_status == "dead_letter"
    assert target_row.dead_lettered_at == fixed_now
    assert target_row.sent_at is None
    assert target_row.locked_at is None
    assert target_row.locked_by is None
    assert target_row.claim_token is None
    assert expected_error in target_row.last_error


def test_deliver_claimed_target_dead_letters_terminal_http_and_preserves_next_attempt(monkeypatch, tmp_path):
    symbols = _load_symbols()
    fixed_now = datetime(2026, 4, 10, 14, 8, tzinfo=timezone.utc)
    settings = _make_settings(tmp_path)
    claimed_target = _make_claimed_target(symbols)
    original_next_attempt_at = datetime(2026, 4, 10, 15, 0, tzinfo=timezone.utc)
    target_row = SimpleNamespace(
        id=claimed_target.target_id,
        delivery_status="processing",
        attempt_count=claimed_target.attempt_count,
        next_attempt_at=original_next_attempt_at,
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
        send_webhook=lambda **_kwargs: 302,
    )

    assert target_row.delivery_status == "dead_letter"
    assert target_row.dead_lettered_at == fixed_now
    assert target_row.next_attempt_at == original_next_attempt_at
    assert target_row.claim_token is None
    assert "terminal_http_status=302" in target_row.last_error


def test_deliver_claimed_target_dead_letters_malformed_payload_without_http(monkeypatch, tmp_path):
    symbols = _load_symbols()
    fixed_now = datetime(2026, 4, 10, 14, 8, tzinfo=timezone.utc)
    settings = _make_settings(tmp_path)
    original_next_attempt_at = datetime(2026, 4, 10, 15, 0, tzinfo=timezone.utc)
    claimed_target = symbols["ClaimedDeliveryTarget"](
        target_id=uuid.uuid4(),
        event_id=uuid.uuid4(),
        event_type="ticket.public_message_added",
        target_name="ops_primary",
        attempt_count=2,
        locked_by="worker-test",
        claim_token=uuid.uuid4(),
        payload_json={
            "ticket_url": "https://autosac.example.local/ops/tickets/T-000123",
            "message_author_type": "requester",
            "message_preview": "missing reference",
        },
    )
    target_row = SimpleNamespace(
        id=claimed_target.target_id,
        delivery_status="processing",
        attempt_count=claimed_target.attempt_count,
        next_attempt_at=original_next_attempt_at,
        locked_at=datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc),
        locked_by="worker-test",
        claim_token=claimed_target.claim_token,
        last_error=None,
        sent_at=None,
        dead_lettered_at=None,
    )
    fake_db = _TargetStateDb(target=target_row)
    send_calls = {"count": 0}

    @contextmanager
    def fake_session_scope(_settings):
        yield fake_db

    def fake_send_webhook(**_kwargs):
        send_calls["count"] += 1
        return 200

    monkeypatch.setattr("worker.slack_delivery.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.slack_delivery.utc_now", lambda: fixed_now)
    slack_runtime = _make_slack_runtime(settings)

    symbols["deliver_claimed_target"](
        slack_runtime,
        claimed_target=claimed_target,
        send_webhook=fake_send_webhook,
    )

    assert send_calls["count"] == 0
    assert target_row.delivery_status == "dead_letter"
    assert target_row.attempt_count == claimed_target.attempt_count
    assert target_row.dead_lettered_at == fixed_now
    assert target_row.sent_at is None
    assert target_row.locked_at is None
    assert target_row.locked_by is None
    assert target_row.claim_token is None
    assert target_row.next_attempt_at == original_next_attempt_at
    assert "ticket_reference must be a non-empty string" in target_row.last_error


def test_deliver_claimed_target_skips_dead_letter_update_when_ownership_is_lost(monkeypatch, tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path)
    claimed_target = _make_claimed_target(symbols)
    original_dead_lettered_at = datetime(2026, 4, 10, 13, 45, tzinfo=timezone.utc)
    target_row = SimpleNamespace(
        id=claimed_target.target_id,
        delivery_status="dead_letter",
        attempt_count=claimed_target.attempt_count + 1,
        next_attempt_at=datetime(2026, 4, 10, 15, 0, tzinfo=timezone.utc),
        locked_at=None,
        locked_by=None,
        claim_token=uuid.uuid4(),
        last_error="terminal_http_status=302",
        sent_at=None,
        dead_lettered_at=original_dead_lettered_at,
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
        send_webhook=lambda **_kwargs: 302,
    )

    assert target_row.delivery_status == "dead_letter"
    assert target_row.attempt_count == claimed_target.attempt_count + 1
    assert target_row.locked_by is None
    assert target_row.last_error == "terminal_http_status=302"
    assert target_row.dead_lettered_at == original_dead_lettered_at
    assert observed == [
        (
            "slack_delivery_ownership_lost",
            {
                "level": "warning",
                "event_id": str(claimed_target.event_id),
                "target_id": str(claimed_target.target_id),
                "target_name": claimed_target.target_name,
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
        send_webhook=lambda **_kwargs: 500,
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


def test_run_delivery_cycle_logs_claim_with_claim_token_and_passes_runtime(monkeypatch, tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path)
    slack_runtime = _make_slack_runtime(settings)
    claimed_target = _make_claimed_target(symbols)
    observed = []
    recovered = []
    claimed = []
    delivered = []

    @contextmanager
    def fake_session_scope(_settings):
        yield SimpleNamespace()

    def fake_recover_stale_delivery_targets(_db, *, slack_runtime):
        recovered.append(slack_runtime)
        return []

    def fake_claim_delivery_targets(_db, *, slack_runtime, locked_by):
        claimed.append((slack_runtime, locked_by))
        return [claimed_target]

    def fake_deliver_claimed_target(passed_runtime, *, claimed_target, send_webhook=None):
        delivered.append((passed_runtime, claimed_target))

    monkeypatch.setattr("worker.slack_delivery.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.slack_delivery.recover_stale_delivery_targets", fake_recover_stale_delivery_targets)
    monkeypatch.setattr("worker.slack_delivery.claim_delivery_targets", fake_claim_delivery_targets)
    monkeypatch.setattr("worker.slack_delivery.deliver_claimed_target", fake_deliver_claimed_target)
    monkeypatch.setattr("worker.slack_delivery.log_worker_event", lambda event, **payload: observed.append((event, payload)))

    symbols["run_delivery_cycle"](slack_runtime, worker_instance_id="worker-test")

    assert recovered == [slack_runtime]
    assert claimed == [(slack_runtime, "worker-test")]
    assert delivered == [(slack_runtime, claimed_target)]
    assert observed == [
        (
            "slack_target_claimed",
            {
                "event_id": str(claimed_target.event_id),
                "target_name": claimed_target.target_name,
                "delivery_status": "processing",
                "attempt_count": claimed_target.attempt_count,
                "locked_by": claimed_target.locked_by,
                "claim_token": str(claimed_target.claim_token),
            },
        )
    ]


def test_run_delivery_cycle_logs_invalid_config_suppression_without_row_state(monkeypatch, tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(
        tmp_path,
        slack=_make_slack_settings(
            is_valid=False,
            config_error_code="slack_targets_json_parse_error",
            config_error_summary="SLACK_TARGETS_JSON must be a valid JSON object",
        ),
    )
    observed = []
    session_scope_calls = {"count": 0}

    @contextmanager
    def fake_session_scope(_settings):
        session_scope_calls["count"] += 1
        yield SimpleNamespace()

    monkeypatch.setattr("worker.slack_delivery.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.slack_delivery.log_worker_event", lambda event, **payload: observed.append((event, payload)))
    slack_runtime = _make_slack_runtime(settings)

    symbols["run_delivery_cycle"](slack_runtime, worker_instance_id="worker-test")

    assert session_scope_calls["count"] == 0
    assert observed == [
        (
            "slack_delivery_suppressed",
            {
                "suppression_reason": "invalid_config",
                "claim_skipped": True,
                "stale_lock_recovery_skipped": True,
                "config_error_code": "slack_targets_json_parse_error",
                "config_error_summary": "SLACK_TARGETS_JSON must be a valid JSON object",
            },
        )
    ]
    assert "event_id" not in observed[0][1]
    assert "target_name" not in observed[0][1]
    assert "delivery_status" not in observed[0][1]
    assert "attempt_count" not in observed[0][1]
    assert "locked_by" not in observed[0][1]


def test_run_delivery_cycle_skips_disabled_slack_without_mutating_rows(monkeypatch, tmp_path):
    symbols = _load_symbols()
    settings = _make_settings(tmp_path, slack=_make_slack_settings(enabled=False))
    observed = []
    session_scope_calls = {"count": 0}

    @contextmanager
    def fake_session_scope(_settings):
        session_scope_calls["count"] += 1
        yield SimpleNamespace()

    monkeypatch.setattr("worker.slack_delivery.session_scope", fake_session_scope)
    monkeypatch.setattr("worker.slack_delivery.log_worker_event", lambda event, **payload: observed.append((event, payload)))
    slack_runtime = _make_slack_runtime(settings)

    symbols["run_delivery_cycle"](slack_runtime, worker_instance_id="worker-test")

    assert session_scope_calls["count"] == 0
    assert observed == []


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
    assert observed["kwargs"]["slack_runtime"].settings == settings
    assert observed["name"] == "worker-slack-delivery"
    assert observed["daemon"] is True
    assert observed["started"] is True
