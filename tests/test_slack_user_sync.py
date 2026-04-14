from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from shared.config import Settings, SlackSettings
from shared.slack_dm import SlackWebApiResponse


class _FakeStateDb:
    def __init__(self):
        self.objects: dict[tuple[str, str], object] = {}
        self.added: list[object] = []

    def get(self, model, key):
        return self.objects.get((getattr(model, "__name__", ""), key))

    def add(self, item):
        self.added.append(item)
        key = getattr(item, "singleton_key", None) or getattr(item, "key", None)
        if key is not None:
            self.objects[(type(item).__name__, key)] = item


class _PendingOnlyStateDb:
    def __init__(self):
        self.objects: dict[tuple[str, str], object] = {}
        self.added: list[object] = []
        self.new: list[object] = []

    def get(self, model, key):
        return self.objects.get((getattr(model, "__name__", ""), key))

    def add(self, item):
        self.added.append(item)
        self.new.append(item)


def _make_settings(tmp_path: Path, *, slack: SlackSettings | None = None) -> Settings:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        app_base_url="https://autosac.example.local",
        app_secret_key="test-secret",
        database_url="postgresql+psycopg://triage:triage@localhost:5432/triage",
        uploads_dir=workspace_dir / "attachments_store",
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
        slack=slack or SlackSettings(routing_mode="dm"),
    )


def test_request_and_persist_slack_user_sync_state_round_trip(tmp_path):
    pytest.importorskip("sqlalchemy")

    from shared.slack_user_sync import (
        SLACK_DM_USER_SYNC_STATE_KEY,
        SlackUserSyncSnapshot,
        load_slack_user_sync_state,
        persist_slack_user_sync_state,
        request_slack_user_sync,
    )

    db = _FakeStateDb()

    request_slack_user_sync(
        db,
        trigger="worker_started",
        requested_by_user_id=uuid.uuid4(),
    )
    persist_slack_user_sync_state(
        db,
        snapshot=SlackUserSyncSnapshot(
            status="succeeded",
            checked_at="2026-04-13T12:00:00+00:00",
            matched_count=3,
            updated_count=2,
            no_match_count=1,
            conflict_count=0,
            summary="Matched 3 user(s) by email, updated 2, left 1 unmatched, and skipped 0 conflict(s).",
        ),
    )

    stored = db.objects[("SystemState", SLACK_DM_USER_SYNC_STATE_KEY)]
    loaded = load_slack_user_sync_state(db)

    assert stored.value_json["status"] == "requested"
    assert stored.value_json["request_pending"] is True
    assert loaded is not None
    assert loaded.status == "requested"
    assert loaded.checked_at == "2026-04-13T12:00:00+00:00"
    assert loaded.updated_count == 2


def test_request_slack_user_sync_reuses_pending_state_row():
    pytest.importorskip("sqlalchemy")

    from shared.slack_user_sync import request_slack_user_sync
    from shared.models import SystemState

    db = _PendingOnlyStateDb()
    pending_state = SystemState(
        key="slack_dm_user_sync",
        value_json={"status": "unknown", "request_pending": False},
    )
    db.add(pending_state)

    updated_state = request_slack_user_sync(db, trigger="worker_started")

    assert updated_state is pending_state
    assert len(db.added) == 1
    assert pending_state.value_json["status"] == "requested"
    assert pending_state.value_json["request_pending"] is True
    assert pending_state.value_json["trigger"] == "worker_started"


def test_fetch_slack_directory_members_by_email_skips_deleted_and_marks_ambiguous(monkeypatch):
    pytest.importorskip("sqlalchemy")

    from shared.slack_user_sync import fetch_slack_directory_members_by_email

    responses = iter(
        [
            SlackWebApiResponse(
                method="users.list",
                http_status=200,
                body_json={
                    "ok": True,
                    "members": [
                        {"id": "U001", "deleted": False, "is_bot": False, "profile": {"email": "alice@example.com"}},
                        {"id": "U002", "deleted": True, "is_bot": False, "profile": {"email": "deleted@example.com"}},
                        {"id": "UBOT", "deleted": False, "is_bot": True, "profile": {"email": "bot@example.com"}},
                    ],
                    "response_metadata": {"next_cursor": "next-page"},
                },
            ),
            SlackWebApiResponse(
                method="users.list",
                http_status=200,
                body_json={
                    "ok": True,
                    "members": [
                        {"id": "U003", "deleted": False, "is_bot": False, "profile": {"email": "alice@example.com"}},
                        {"id": "U004", "deleted": False, "is_bot": False, "profile": {"email": "bob@example.com"}},
                    ],
                    "response_metadata": {"next_cursor": ""},
                },
            ),
        ]
    )

    monkeypatch.setattr(
        "shared.slack_user_sync.slack_api_users_list",
        lambda **_kwargs: next(responses),
    )

    by_email, ambiguous_emails = fetch_slack_directory_members_by_email(
        bot_token="xoxb-token",
        timeout_seconds=10,
    )

    assert by_email == {"bob@example.com": "U004"}
    assert ambiguous_emails == {"alice@example.com"}


def test_fetch_slack_directory_members_by_email_flags_missing_email_scope(monkeypatch):
    pytest.importorskip("sqlalchemy")

    from shared.slack_user_sync import SlackUserSyncInvalidConfig, fetch_slack_directory_members_by_email

    monkeypatch.setattr(
        "shared.slack_user_sync.slack_api_users_list",
        lambda **_kwargs: SlackWebApiResponse(
            method="users.list",
            http_status=200,
            body_json={
                "ok": True,
                "members": [
                    {"id": "U001", "deleted": False, "is_bot": False, "profile": {}},
                ],
                "response_metadata": {"next_cursor": ""},
            },
        ),
    )

    with pytest.raises(SlackUserSyncInvalidConfig) as exc_info:
        fetch_slack_directory_members_by_email(
            bot_token="xoxb-token",
            timeout_seconds=10,
        )

    assert exc_info.value.error_code == "users_list_missing_email_scope"


def test_sync_slack_user_ids_by_email_summarizes_matches_and_conflicts(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")

    from shared.slack_user_sync import MissingSlackUserCandidate, sync_slack_user_ids_by_email

    settings = _make_settings(tmp_path, slack=SlackSettings(routing_mode="dm"))
    sessions = [SimpleNamespace(name="load"), SimpleNamespace(name="apply")]

    @contextmanager
    def fake_session_scope(_settings):
        yield sessions.pop(0)

    monkeypatch.setattr("shared.slack_user_sync.session_scope", fake_session_scope)
    monkeypatch.setattr(
        "shared.slack_user_sync.load_slack_dm_settings",
        lambda db, app_settings: SlackSettings(
            has_stored_token=True,
            bot_token_ciphertext="ciphertext",
            http_timeout_seconds=12,
            routing_mode="dm",
        ),
    )
    monkeypatch.setattr(
        "shared.slack_user_sync.load_users_missing_slack_user_id",
        lambda db: [
            MissingSlackUserCandidate(user_id=uuid.uuid4(), email="alice@example.com"),
            MissingSlackUserCandidate(user_id=uuid.uuid4(), email="bob@example.com"),
            MissingSlackUserCandidate(user_id=uuid.uuid4(), email="carol@example.com"),
        ],
    )
    monkeypatch.setattr("shared.slack_user_sync.resolve_slack_bot_token", lambda slack, *, app_secret_key: "xoxb-token")
    monkeypatch.setattr(
        "shared.slack_user_sync.fetch_slack_directory_members_by_email",
        lambda **_kwargs: (
            {
                "alice@example.com": "U001",
                "bob@example.com": "U002",
            },
            set(),
        ),
    )
    monkeypatch.setattr(
        "shared.slack_user_sync.apply_slack_user_id_matches",
        lambda db, *, matches: (1, 1),
    )

    snapshot = sync_slack_user_ids_by_email(
        settings,
        trigger="settings_saved",
        started_at="2026-04-13T12:00:00+00:00",
        requested_at="2026-04-13T11:59:00+00:00",
    )

    assert snapshot.status == "succeeded"
    assert snapshot.trigger == "settings_saved"
    assert snapshot.matched_count == 2
    assert snapshot.updated_count == 1
    assert snapshot.no_match_count == 1
    assert snapshot.conflict_count == 1
    assert snapshot.summary == "Matched 2 user(s) by email, updated 1, left 1 unmatched, and skipped 1 conflict(s)."


def test_run_requested_slack_user_sync_persists_snapshot_and_logs(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")

    from shared.slack_user_sync import SlackUserSyncRequest, SlackUserSyncSnapshot
    from worker.slack_user_sync import run_requested_slack_user_sync

    settings = _make_settings(tmp_path)
    observed = {"persisted": [], "events": []}

    @contextmanager
    def fake_session_scope(_settings):
        yield SimpleNamespace()

    monkeypatch.setattr("worker.slack_user_sync.session_scope", fake_session_scope)
    monkeypatch.setattr(
        "worker.slack_user_sync.claim_requested_slack_user_sync",
        lambda db, *, worker_instance_id, started_at=None: SlackUserSyncRequest(
            requested_at="2026-04-13T11:59:00+00:00",
            started_at="2026-04-13T12:00:00+00:00",
            trigger="worker_started",
        ),
    )
    monkeypatch.setattr(
        "worker.slack_user_sync.sync_slack_user_ids_by_email",
        lambda _settings, **kwargs: SlackUserSyncSnapshot(
            status="succeeded",
            checked_at="2026-04-13T12:00:02+00:00",
            started_at=kwargs["started_at"],
            requested_at=kwargs["requested_at"],
            trigger=kwargs["trigger"],
            matched_count=2,
            updated_count=2,
            no_match_count=0,
            conflict_count=0,
            summary="Matched 2 user(s) by email, updated 2, left 0 unmatched, and skipped 0 conflict(s).",
        ),
    )
    monkeypatch.setattr(
        "worker.slack_user_sync.persist_slack_user_sync_state",
        lambda db, *, snapshot, updated_at=None: observed["persisted"].append(snapshot),
    )
    monkeypatch.setattr(
        "worker.slack_user_sync.log_worker_event",
        lambda event, **payload: observed["events"].append((event, payload)),
    )

    handled = run_requested_slack_user_sync(settings, worker_instance_id="worker-test")

    assert handled is True
    assert observed["persisted"][0].updated_count == 2
    assert observed["events"][0][0] == "slack_user_sync_started"
    assert observed["events"][1][0] == "slack_user_sync_completed"
    assert observed["events"][1][1]["worker_instance_id"] == "worker-test"


def test_start_slack_user_sync_thread_wires_worker_instance_id(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")

    from worker.main import WorkerIdentity, start_slack_user_sync_thread

    settings = _make_settings(tmp_path)
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

    thread = start_slack_user_sync_thread(
        settings,
        worker_identity=WorkerIdentity(worker_pid=1111, worker_instance_id="worker-sync"),
    )

    assert isinstance(thread, _FakeThread)
    assert observed["target"] is not None
    assert observed["kwargs"]["settings"] is settings
    assert observed["kwargs"]["worker_instance_id"] == "worker-sync"
    assert observed["name"] == "worker-slack-user-sync"
    assert observed["daemon"] is True
    assert observed["started"] is True
