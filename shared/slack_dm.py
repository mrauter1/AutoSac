from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any
import uuid

import httpx
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy.orm import Session

from shared.config import (
    SLACK_DELIVERY_BATCH_SIZE_DEFAULT,
    SLACK_DELIVERY_MAX_ATTEMPTS_DEFAULT,
    SLACK_DELIVERY_STALE_LOCK_SECONDS_DEFAULT,
    SLACK_HTTP_TIMEOUT_SECONDS_DEFAULT,
    SLACK_MESSAGE_PREVIEW_MAX_CHARS_DEFAULT,
    Settings,
    SlackSettings,
)
from shared.models import SlackDMSettings, SystemState
from shared.security import utc_now

SLACK_DM_SETTINGS_SINGLETON_KEY = "default"
SLACK_DM_DELIVERY_HEALTH_STATE_KEY = "slack_dm_delivery_health"
SLACK_DM_TOKEN_HKDF_INFO = b"autosac-slack-dm-token-v1"
SLACK_WEB_API_BASE_URL = "https://slack.com/api"


class SlackDMSettingsError(ValueError):
    """Raised when persisted or submitted Slack DM settings are invalid."""


class SlackDMTokenError(RuntimeError):
    """Raised when the stored Slack DM token is missing or unusable."""


@dataclass(frozen=True)
class SlackDMSettingsInput:
    enabled: bool
    notify_ticket_created: bool = False
    notify_public_message_added: bool = False
    notify_status_changed: bool = False
    message_preview_max_chars: int = SLACK_MESSAGE_PREVIEW_MAX_CHARS_DEFAULT
    http_timeout_seconds: int = SLACK_HTTP_TIMEOUT_SECONDS_DEFAULT
    delivery_batch_size: int = SLACK_DELIVERY_BATCH_SIZE_DEFAULT
    delivery_max_attempts: int = SLACK_DELIVERY_MAX_ATTEMPTS_DEFAULT
    delivery_stale_lock_seconds: int = SLACK_DELIVERY_STALE_LOCK_SECONDS_DEFAULT
    bot_token: str | None = None


@dataclass(frozen=True)
class SlackAuthTestResult:
    team_id: str | None
    team_name: str | None
    bot_user_id: str | None


@dataclass(frozen=True)
class SlackDeliveryHealthSnapshot:
    status: str
    checked_at: str | None = None
    error_code: str | None = None
    summary: str | None = None
    team_id: str | None = None
    team_name: str | None = None
    bot_user_id: str | None = None


@dataclass(frozen=True)
class SlackWebApiResponse:
    method: str
    http_status: int
    body_json: dict[str, Any] | None
    retry_after_seconds: int | None = None

    @property
    def ok(self) -> bool:
        return (
            200 <= self.http_status < 300
            and isinstance(self.body_json, dict)
            and self.body_json.get("ok") is True
        )

    @property
    def error_code(self) -> str | None:
        if not isinstance(self.body_json, dict):
            return None
        error = self.body_json.get("error")
        if isinstance(error, str) and error.strip():
            return error.strip()
        return None


def validate_slack_dm_settings_input(values: SlackDMSettingsInput) -> SlackDMSettingsInput:
    if values.message_preview_max_chars < 4:
        raise SlackDMSettingsError("message_preview_max_chars must be greater than or equal to 4")
    if values.http_timeout_seconds < 1 or values.http_timeout_seconds > 30:
        raise SlackDMSettingsError("http_timeout_seconds must be between 1 and 30 inclusive")
    if values.delivery_batch_size < 1:
        raise SlackDMSettingsError("delivery_batch_size must be greater than or equal to 1")
    if values.delivery_max_attempts < 1:
        raise SlackDMSettingsError("delivery_max_attempts must be greater than or equal to 1")
    if values.delivery_stale_lock_seconds <= values.http_timeout_seconds:
        raise SlackDMSettingsError("delivery_stale_lock_seconds must be greater than http_timeout_seconds")
    normalized_token = None
    if values.bot_token is not None:
        stripped = values.bot_token.strip()
        normalized_token = stripped or None
    return SlackDMSettingsInput(
        enabled=values.enabled,
        notify_ticket_created=values.notify_ticket_created,
        notify_public_message_added=values.notify_public_message_added,
        notify_status_changed=values.notify_status_changed,
        message_preview_max_chars=values.message_preview_max_chars,
        http_timeout_seconds=values.http_timeout_seconds,
        delivery_batch_size=values.delivery_batch_size,
        delivery_max_attempts=values.delivery_max_attempts,
        delivery_stale_lock_seconds=values.delivery_stale_lock_seconds,
        bot_token=normalized_token,
    )


def build_default_slack_settings() -> SlackSettings:
    return SlackSettings()


def _derive_slack_token_key(app_secret_key: str) -> bytes:
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=SLACK_DM_TOKEN_HKDF_INFO,
    )
    derived = hkdf.derive(app_secret_key.encode("utf-8"))
    return base64.urlsafe_b64encode(derived)


def encrypt_slack_bot_token(app_secret_key: str, bot_token: str) -> str:
    normalized = bot_token.strip()
    if not normalized:
        raise SlackDMTokenError("Slack bot token is required")
    return Fernet(_derive_slack_token_key(app_secret_key)).encrypt(normalized.encode("utf-8")).decode("utf-8")


def decrypt_slack_bot_token(app_secret_key: str, bot_token_ciphertext: str) -> str:
    try:
        plaintext = Fernet(_derive_slack_token_key(app_secret_key)).decrypt(bot_token_ciphertext.encode("utf-8"))
    except InvalidToken as exc:
        raise SlackDMTokenError("Stored Slack bot token could not be decrypted") from exc
    return plaintext.decode("utf-8")


def resolve_slack_bot_token(slack: SlackSettings, *, app_secret_key: str) -> str:
    if not slack.bot_token_ciphertext:
        raise SlackDMTokenError("No stored Slack bot token is available")
    return decrypt_slack_bot_token(app_secret_key, slack.bot_token_ciphertext)


def load_slack_delivery_health(db: Session) -> SlackDeliveryHealthSnapshot | None:
    getter = getattr(db, "get", None)
    if not callable(getter):
        return None
    state = getter(SystemState, SLACK_DM_DELIVERY_HEALTH_STATE_KEY)
    if state is None or not isinstance(state.value_json, dict):
        return None
    payload = state.value_json
    return SlackDeliveryHealthSnapshot(
        status=str(payload.get("status") or "unknown"),
        checked_at=payload.get("checked_at") if isinstance(payload.get("checked_at"), str) else None,
        error_code=payload.get("error_code") if isinstance(payload.get("error_code"), str) else None,
        summary=payload.get("summary") if isinstance(payload.get("summary"), str) else None,
        team_id=payload.get("team_id") if isinstance(payload.get("team_id"), str) else None,
        team_name=payload.get("team_name") if isinstance(payload.get("team_name"), str) else None,
        bot_user_id=payload.get("bot_user_id") if isinstance(payload.get("bot_user_id"), str) else None,
    )


def persist_slack_delivery_health(
    db: Session,
    *,
    snapshot: SlackDeliveryHealthSnapshot,
    updated_at=None,
) -> SystemState:
    getter = getattr(db, "get", None)
    state = getter(SystemState, SLACK_DM_DELIVERY_HEALTH_STATE_KEY) if callable(getter) else None
    resolved_updated_at = updated_at or utc_now()
    payload = {
        "status": snapshot.status,
        "checked_at": snapshot.checked_at,
        "error_code": snapshot.error_code,
        "summary": snapshot.summary,
        "team_id": snapshot.team_id,
        "team_name": snapshot.team_name,
        "bot_user_id": snapshot.bot_user_id,
    }
    if state is None:
        state = SystemState(
            key=SLACK_DM_DELIVERY_HEALTH_STATE_KEY,
            value_json=payload,
            updated_at=resolved_updated_at,
        )
        db.add(state)
        return state
    state.value_json = payload
    state.updated_at = resolved_updated_at
    return state


def load_slack_dm_settings(db: Session, *, app_settings: Settings) -> SlackSettings:
    getter = getattr(db, "get", None)
    if not callable(getter):
        return build_default_slack_settings()
    row = getter(SlackDMSettings, SLACK_DM_SETTINGS_SINGLETON_KEY)
    if row is None:
        return build_default_slack_settings()

    error_code: str | None = None
    error_summary: str | None = None

    def record_error(code: str, summary: str) -> None:
        nonlocal error_code, error_summary
        if error_code is None:
            error_code = code
            error_summary = summary

    notify_ticket_created = bool(row.notify_ticket_created)
    notify_public_message_added = bool(row.notify_public_message_added)
    notify_status_changed = bool(row.notify_status_changed)
    message_preview_max_chars = (
        row.message_preview_max_chars
        if row.message_preview_max_chars is not None
        else SLACK_MESSAGE_PREVIEW_MAX_CHARS_DEFAULT
    )
    http_timeout_seconds = (
        row.http_timeout_seconds
        if row.http_timeout_seconds is not None
        else SLACK_HTTP_TIMEOUT_SECONDS_DEFAULT
    )
    delivery_batch_size = (
        row.delivery_batch_size
        if row.delivery_batch_size is not None
        else SLACK_DELIVERY_BATCH_SIZE_DEFAULT
    )
    delivery_max_attempts = (
        row.delivery_max_attempts
        if row.delivery_max_attempts is not None
        else SLACK_DELIVERY_MAX_ATTEMPTS_DEFAULT
    )
    delivery_stale_lock_seconds = (
        row.delivery_stale_lock_seconds
        if row.delivery_stale_lock_seconds is not None
        else SLACK_DELIVERY_STALE_LOCK_SECONDS_DEFAULT
    )

    try:
        validate_slack_dm_settings_input(
            SlackDMSettingsInput(
                enabled=row.enabled,
                notify_ticket_created=notify_ticket_created,
                notify_public_message_added=notify_public_message_added,
                notify_status_changed=notify_status_changed,
                message_preview_max_chars=message_preview_max_chars,
                http_timeout_seconds=http_timeout_seconds,
                delivery_batch_size=delivery_batch_size,
                delivery_max_attempts=delivery_max_attempts,
                delivery_stale_lock_seconds=delivery_stale_lock_seconds,
            )
        )
    except SlackDMSettingsError as exc:
        record_error("slack_dm_settings_invalid", str(exc))

    has_stored_token = bool(row.bot_token_ciphertext)
    if has_stored_token:
        try:
            decrypt_slack_bot_token(app_settings.app_secret_key, row.bot_token_ciphertext)
        except SlackDMTokenError:
            record_error(
                "slack_bot_token_undecryptable",
                "Stored Slack bot token could not be decrypted with APP_SECRET_KEY",
            )
    elif row.enabled:
        record_error(
            "slack_bot_token_missing",
            "Slack DM delivery cannot be enabled without a stored bot token",
        )

    return SlackSettings(
        enabled=row.enabled,
        notify_ticket_created=notify_ticket_created,
        notify_public_message_added=notify_public_message_added,
        notify_status_changed=notify_status_changed,
        message_preview_max_chars=message_preview_max_chars,
        http_timeout_seconds=http_timeout_seconds,
        delivery_batch_size=delivery_batch_size,
        delivery_max_attempts=delivery_max_attempts,
        delivery_stale_lock_seconds=delivery_stale_lock_seconds,
        has_stored_token=has_stored_token,
        bot_token_ciphertext=row.bot_token_ciphertext,
        team_id=row.team_id,
        team_name=row.team_name,
        bot_user_id=row.bot_user_id,
        validated_at=row.validated_at,
        updated_by_user_id=row.updated_by_user_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        is_valid=error_code is None,
        config_error_code=error_code,
        config_error_summary=error_summary,
    )


def upsert_slack_dm_settings(
    db: Session,
    *,
    app_settings: Settings,
    values: SlackDMSettingsInput,
    updated_by_user_id: uuid.UUID | None = None,
    auth_result: SlackAuthTestResult | None = None,
    updated_at=None,
) -> SlackDMSettings:
    validated = validate_slack_dm_settings_input(values)
    getter = getattr(db, "get", None)
    row = getter(SlackDMSettings, SLACK_DM_SETTINGS_SINGLETON_KEY) if callable(getter) else None
    resolved_updated_at = updated_at or utc_now()
    if row is None:
        row = SlackDMSettings(
            singleton_key=SLACK_DM_SETTINGS_SINGLETON_KEY,
            created_at=resolved_updated_at,
            updated_at=resolved_updated_at,
        )
        db.add(row)

    bot_token_ciphertext = row.bot_token_ciphertext
    if validated.bot_token is not None:
        bot_token_ciphertext = encrypt_slack_bot_token(app_settings.app_secret_key, validated.bot_token)
    if validated.enabled and not bot_token_ciphertext:
        raise SlackDMSettingsError("Slack DM delivery cannot be enabled without a stored bot token")

    row.enabled = validated.enabled
    row.bot_token_ciphertext = bot_token_ciphertext
    row.notify_ticket_created = validated.notify_ticket_created
    row.notify_public_message_added = validated.notify_public_message_added
    row.notify_status_changed = validated.notify_status_changed
    row.message_preview_max_chars = validated.message_preview_max_chars
    row.http_timeout_seconds = validated.http_timeout_seconds
    row.delivery_batch_size = validated.delivery_batch_size
    row.delivery_max_attempts = validated.delivery_max_attempts
    row.delivery_stale_lock_seconds = validated.delivery_stale_lock_seconds
    row.updated_by_user_id = updated_by_user_id
    row.updated_at = resolved_updated_at
    if auth_result is not None:
        row.team_id = auth_result.team_id
        row.team_name = auth_result.team_name
        row.bot_user_id = auth_result.bot_user_id
        row.validated_at = resolved_updated_at
    return row


def clear_slack_dm_token(
    db: Session,
    *,
    updated_by_user_id: uuid.UUID | None = None,
    updated_at=None,
) -> SlackDMSettings:
    resolved_updated_at = updated_at or utc_now()
    getter = getattr(db, "get", None)
    row = getter(SlackDMSettings, SLACK_DM_SETTINGS_SINGLETON_KEY) if callable(getter) else None
    if row is None:
        row = SlackDMSettings(
            singleton_key=SLACK_DM_SETTINGS_SINGLETON_KEY,
            created_at=resolved_updated_at,
            updated_at=resolved_updated_at,
        )
        db.add(row)
    row.enabled = False
    row.bot_token_ciphertext = None
    row.updated_by_user_id = updated_by_user_id
    row.updated_at = resolved_updated_at
    return row


def slack_api_auth_test(*, bot_token: str, timeout_seconds: int) -> SlackWebApiResponse:
    return _call_slack_web_api(method="auth.test", bot_token=bot_token, payload={}, timeout_seconds=timeout_seconds)


def slack_api_conversations_open(*, bot_token: str, slack_user_id: str, timeout_seconds: int) -> SlackWebApiResponse:
    return _call_slack_web_api(
        method="conversations.open",
        bot_token=bot_token,
        payload={"users": slack_user_id},
        timeout_seconds=timeout_seconds,
    )


def slack_api_chat_post_message(
    *,
    bot_token: str,
    channel_id: str,
    text: str,
    timeout_seconds: int,
) -> SlackWebApiResponse:
    return _call_slack_web_api(
        method="chat.postMessage",
        bot_token=bot_token,
        payload={"channel": channel_id, "text": text},
        timeout_seconds=timeout_seconds,
    )


def parse_slack_auth_test_result(response: SlackWebApiResponse) -> SlackAuthTestResult:
    if not response.ok or not isinstance(response.body_json, dict):
        raise SlackDMSettingsError("Slack auth.test did not succeed")
    payload = response.body_json
    team_id = payload.get("team_id") if isinstance(payload.get("team_id"), str) else None
    team_name = payload.get("team") if isinstance(payload.get("team"), str) else None
    bot_user_id = payload.get("user_id") if isinstance(payload.get("user_id"), str) else None
    return SlackAuthTestResult(team_id=team_id, team_name=team_name, bot_user_id=bot_user_id)


def _call_slack_web_api(
    *,
    method: str,
    bot_token: str,
    payload: dict[str, Any],
    timeout_seconds: int,
) -> SlackWebApiResponse:
    response = httpx.post(
        f"{SLACK_WEB_API_BASE_URL}/{method}",
        json=payload,
        headers={
            "Authorization": f"Bearer {bot_token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        timeout=httpx.Timeout(timeout_seconds),
        follow_redirects=False,
    )
    retry_after_seconds = _parse_retry_after(response.headers.get("Retry-After"))
    body_json: dict[str, Any] | None = None
    try:
        parsed_body = response.json()
        if isinstance(parsed_body, dict):
            body_json = parsed_body
    except ValueError:
        body_json = None
    return SlackWebApiResponse(
        method=method,
        http_status=int(response.status_code),
        body_json=body_json,
        retry_after_seconds=retry_after_seconds,
    )


def _parse_retry_after(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
