from __future__ import annotations

from pathlib import Path
import uuid

import pytest

from shared.config import Settings, SlackSettings


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


def _make_settings(tmp_path: Path, *, app_secret_key: str = "test-secret", slack: SlackSettings | None = None) -> Settings:
    workspace_dir = tmp_path / "workspace"
    return Settings(
        app_base_url="https://autosac.example.local",
        app_secret_key=app_secret_key,
        database_url="postgresql+psycopg://triage:triage@localhost:5432/triage",
        uploads_dir=tmp_path / "uploads",
        triage_workspace_dir=workspace_dir,
        repo_mount_dir=workspace_dir / "app",
        manuals_mount_dir=workspace_dir / "manuals",
        codex_bin="codex",
        codex_api_key="key",
        codex_model="",
        codex_timeout_seconds=3600,
        worker_poll_seconds=10,
        auto_support_reply_min_confidence=0.85,
        auto_confirm_intent_min_confidence=0.90,
        max_images_per_message=3,
        max_image_bytes=5 * 1024 * 1024,
        session_default_hours=12,
        session_remember_days=30,
        slack=slack or SlackSettings(),
    )


def test_slack_dm_foundation_migration_adds_settings_user_mapping_and_recipient_fields():
    migration_source = Path(
        "shared/migrations/versions/20260410_0012_slack_dm_persistence_runtime_foundation.py"
    ).read_text(encoding="utf-8")

    assert 'revision = "20260410_0012"' in migration_source
    assert 'down_revision = "20260410_0011"' in migration_source
    assert 'op.execute("DELETE FROM integration_event_targets")' in migration_source
    assert 'op.create_table(\n        "slack_dm_settings"' in migration_source
    assert 'op.add_column("users", sa.Column("slack_user_id", sa.Text(), nullable=True))' in migration_source
    assert 'op.add_column("integration_event_targets", sa.Column("recipient_user_id"' in migration_source
    assert 'op.add_column("integration_event_targets", sa.Column("recipient_reason", sa.Text(), nullable=True))' in migration_source
    assert '"target_kind IN (\'slack_dm\')"' in migration_source


def test_load_slack_dm_settings_defaults_to_disabled_dm_mode_when_row_missing(tmp_path):
    pytest.importorskip("sqlalchemy")

    from shared.slack_dm import load_slack_dm_settings

    db = _FakeStateDb()
    settings = _make_settings(
        tmp_path,
        slack=SlackSettings(
            enabled=True,
            notify_ticket_created=True,
            message_preview_max_chars=111,
        ),
    )

    loaded = load_slack_dm_settings(db, app_settings=settings)

    assert loaded.enabled is False
    assert loaded.notify_ticket_created is False
    assert loaded.message_preview_max_chars == 200
    assert loaded.has_stored_token is False
    assert loaded.is_valid is True
    assert loaded.routing_mode == "dm"


def test_encrypt_and_decrypt_slack_bot_token_round_trip(tmp_path):
    from shared.slack_dm import decrypt_slack_bot_token, encrypt_slack_bot_token

    ciphertext = encrypt_slack_bot_token("test-secret", "xoxb-test-token")

    assert ciphertext != "xoxb-test-token"
    assert decrypt_slack_bot_token("test-secret", ciphertext) == "xoxb-test-token"


def test_upsert_slack_dm_settings_encrypts_token_and_requires_stored_token_when_enabled(tmp_path):
    pytest.importorskip("sqlalchemy")

    from shared.slack_dm import SlackDMSettingsError, SlackDMSettingsInput, load_slack_dm_settings, upsert_slack_dm_settings

    db = _FakeStateDb()
    settings = _make_settings(tmp_path)

    with pytest.raises(SlackDMSettingsError):
        upsert_slack_dm_settings(
            db,
            app_settings=settings,
            values=SlackDMSettingsInput(enabled=True),
            updated_by_user_id=uuid.uuid4(),
        )

    row = upsert_slack_dm_settings(
        db,
        app_settings=settings,
        values=SlackDMSettingsInput(
            enabled=True,
            notify_ticket_created=True,
            bot_token="  xoxb-test-token  ",
        ),
        updated_by_user_id=uuid.uuid4(),
    )

    assert row.bot_token_ciphertext is not None
    assert row.bot_token_ciphertext != "xoxb-test-token"
    loaded = load_slack_dm_settings(db, app_settings=settings)
    assert loaded.enabled is True
    assert loaded.notify_ticket_created is True
    assert loaded.has_stored_token is True
    assert loaded.is_valid is True


def test_upsert_slack_dm_settings_preserves_existing_token_when_blank_input_is_submitted(tmp_path):
    pytest.importorskip("sqlalchemy")

    from shared.slack_dm import (
        SlackDMSettingsInput,
        decrypt_slack_bot_token,
        load_slack_dm_settings,
        upsert_slack_dm_settings,
    )

    db = _FakeStateDb()
    settings = _make_settings(tmp_path)

    original = upsert_slack_dm_settings(
        db,
        app_settings=settings,
        values=SlackDMSettingsInput(
            enabled=False,
            notify_ticket_created=True,
            bot_token="xoxb-initial-token",
        ),
        updated_by_user_id=uuid.uuid4(),
    )

    updated = upsert_slack_dm_settings(
        db,
        app_settings=settings,
        values=SlackDMSettingsInput(
            enabled=True,
            notify_public_message_added=True,
            bot_token="   ",
        ),
        updated_by_user_id=uuid.uuid4(),
    )
    loaded = load_slack_dm_settings(db, app_settings=settings)

    assert updated.bot_token_ciphertext == original.bot_token_ciphertext
    assert decrypt_slack_bot_token(settings.app_secret_key, updated.bot_token_ciphertext or "") == "xoxb-initial-token"
    assert loaded.enabled is True
    assert loaded.notify_ticket_created is False
    assert loaded.notify_public_message_added is True
    assert loaded.has_stored_token is True
    assert loaded.is_valid is True


def test_load_slack_dm_settings_flags_undecryptable_token_and_runtime_context_uses_db_values(tmp_path):
    pytest.importorskip("sqlalchemy")

    from shared.integrations import build_slack_runtime_context
    from shared.models import SlackDMSettings
    from shared.slack_dm import encrypt_slack_bot_token, load_slack_dm_settings

    db = _FakeStateDb()
    db.add(
        SlackDMSettings(
            singleton_key="default",
            enabled=True,
            bot_token_ciphertext=encrypt_slack_bot_token("old-secret", "xoxb-old-token"),
            notify_ticket_created=True,
            message_preview_max_chars=333,
        )
    )
    settings = _make_settings(tmp_path, app_secret_key="new-secret", slack=SlackSettings(message_preview_max_chars=111))

    loaded = load_slack_dm_settings(db, app_settings=settings)
    runtime = build_slack_runtime_context(settings, db=db)

    assert loaded.is_valid is False
    assert loaded.config_error_code == "slack_bot_token_undecryptable"
    assert runtime.slack.message_preview_max_chars == 333
    assert runtime.slack.config_error_code == "slack_bot_token_undecryptable"
    assert runtime.slack.routing_mode == "dm"


def test_valid_db_loaded_slack_runtime_uses_dm_routing_without_webhook_targets(tmp_path):
    pytest.importorskip("sqlalchemy")

    from shared.integrations import build_slack_runtime_context, resolve_routing_decision
    from shared.models import SlackDMSettings
    from shared.slack_dm import encrypt_slack_bot_token

    db = _FakeStateDb()
    db.add(
        SlackDMSettings(
            singleton_key="default",
            enabled=True,
            bot_token_ciphertext=encrypt_slack_bot_token("test-secret", "xoxb-test-token"),
            notify_ticket_created=True,
        )
    )
    settings = _make_settings(tmp_path)

    runtime = build_slack_runtime_context(settings, db=db)
    decision = resolve_routing_decision(runtime.slack, event_type="ticket.created")

    assert runtime.slack.routing_mode == "dm"
    assert runtime.slack.is_valid is True
    assert decision.routing_result == "suppressed_no_recipients"
    assert decision.target_name is None
    assert decision.config_error_code is None


def test_clear_slack_dm_token_disables_delivery_and_preserves_workspace_metadata(tmp_path):
    pytest.importorskip("sqlalchemy")

    from shared.models import SlackDMSettings
    from shared.slack_dm import clear_slack_dm_token, encrypt_slack_bot_token, load_slack_dm_settings

    db = _FakeStateDb()
    db.add(
        SlackDMSettings(
            singleton_key="default",
            enabled=True,
            bot_token_ciphertext=encrypt_slack_bot_token("test-secret", "xoxb-test-token"),
            team_id="T123",
            team_name="AutoSac",
            bot_user_id="U123",
            notify_ticket_created=True,
        )
    )
    settings = _make_settings(tmp_path)

    cleared = clear_slack_dm_token(db, updated_by_user_id=uuid.uuid4())
    loaded = load_slack_dm_settings(db, app_settings=settings)

    assert cleared.enabled is False
    assert cleared.bot_token_ciphertext is None
    assert cleared.team_id == "T123"
    assert cleared.team_name == "AutoSac"
    assert cleared.bot_user_id == "U123"
    assert loaded.enabled is False
    assert loaded.has_stored_token is False
    assert loaded.team_id == "T123"
    assert loaded.team_name == "AutoSac"
    assert loaded.bot_user_id == "U123"
    assert loaded.is_valid is True
    assert loaded.routing_mode == "dm"


def test_persist_slack_delivery_health_upserts_system_state(tmp_path):
    pytest.importorskip("sqlalchemy")

    from shared.slack_dm import (
        SLACK_DM_DELIVERY_HEALTH_STATE_KEY,
        SlackDeliveryHealthSnapshot,
        load_slack_delivery_health,
        persist_slack_delivery_health,
    )

    db = _FakeStateDb()

    persist_slack_delivery_health(
        db,
        snapshot=SlackDeliveryHealthSnapshot(
            status="invalid_config",
            checked_at="2026-04-10T20:00:00+00:00",
            error_code="invalid_auth",
            summary="Slack auth.test returned invalid_auth",
        ),
    )

    stored = db.objects[("SystemState", SLACK_DM_DELIVERY_HEALTH_STATE_KEY)]
    loaded = load_slack_delivery_health(db)

    assert stored.value_json["status"] == "invalid_config"
    assert loaded is not None
    assert loaded.error_code == "invalid_auth"
    assert loaded.summary == "Slack auth.test returned invalid_auth"
