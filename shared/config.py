from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import json
from pathlib import Path
import re
from urllib.parse import urlparse
import os

from dotenv import load_dotenv

from shared.contracts import (
    DEFAULT_MANUALS_MOUNT_DIR,
    DEFAULT_REPO_MOUNT_DIR,
    DEFAULT_TRIAGE_WORKSPACE_DIR,
    DEFAULT_UPLOADS_DIR,
)
from shared.agent_specs import WORKSPACE_SKILLS_RELATIVE_DIR


PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env", override=False)

_SLACK_TARGET_NAME_RE = re.compile(r"^[a-z0-9_-]+$")
_ENV_TRUE_VALUES = {"1", "true", "yes", "on"}
_ENV_FALSE_VALUES = {"0", "false", "no", "off"}

SLACK_MESSAGE_PREVIEW_MAX_CHARS_DEFAULT = 200
SLACK_HTTP_TIMEOUT_SECONDS_DEFAULT = 10
SLACK_DELIVERY_BATCH_SIZE_DEFAULT = 10
SLACK_DELIVERY_MAX_ATTEMPTS_DEFAULT = 5
SLACK_DELIVERY_STALE_LOCK_SECONDS_DEFAULT = 120


class SettingsError(RuntimeError):
    """Raised when required environment configuration is missing or invalid."""


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SettingsError(f"Missing required environment variable: {name}")
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return Path(raw).expanduser()


def _normalize_ui_locale(value: str) -> str:
    candidate = value.strip()
    lowered = candidate.lower()
    if lowered == "en" or lowered.startswith("en-"):
        return "en"
    if lowered in {"pt", "pt-br", "pt_br"} or lowered.startswith("pt-"):
        return "pt-BR"
    raise SettingsError("UI_DEFAULT_LOCALE must be one of: en, pt-BR")


def _env_ui_locale(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return _normalize_ui_locale(default)
    return _normalize_ui_locale(raw)


def get_database_url() -> str:
    return _required_env("DATABASE_URL")


def get_default_ui_locale() -> str:
    return _env_ui_locale("UI_DEFAULT_LOCALE", "en")


@dataclass(frozen=True)
class SlackTargetSettings:
    name: str
    enabled: bool
    webhook_url: str


@dataclass(frozen=True)
class SlackSettings:
    enabled: bool = False
    default_target_name: str | None = None
    targets: tuple[SlackTargetSettings, ...] = ()
    notify_ticket_created: bool = False
    notify_public_message_added: bool = False
    notify_status_changed: bool = False
    message_preview_max_chars: int = SLACK_MESSAGE_PREVIEW_MAX_CHARS_DEFAULT
    http_timeout_seconds: int = SLACK_HTTP_TIMEOUT_SECONDS_DEFAULT
    delivery_batch_size: int = SLACK_DELIVERY_BATCH_SIZE_DEFAULT
    delivery_max_attempts: int = SLACK_DELIVERY_MAX_ATTEMPTS_DEFAULT
    delivery_stale_lock_seconds: int = SLACK_DELIVERY_STALE_LOCK_SECONDS_DEFAULT
    is_valid: bool = True
    config_error_code: str | None = None
    config_error_summary: str | None = None

    @property
    def any_notify_enabled(self) -> bool:
        return (
            self.notify_ticket_created
            or self.notify_public_message_added
            or self.notify_status_changed
        )

    def get_target(self, target_name: str | None) -> SlackTargetSettings | None:
        if target_name is None:
            return None
        for target in self.targets:
            if target.name == target_name:
                return target
        return None


def _env_soft_bool(name: str, default: bool) -> tuple[bool, str | None, str | None]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default, None, None
    lowered = raw.strip().lower()
    if lowered in _ENV_TRUE_VALUES:
        return True, None, None
    if lowered in _ENV_FALSE_VALUES:
        return False, None, None
    return default, f"{name.lower()}_invalid", f"{name} must be a boolean value"


def _env_soft_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> tuple[int, str | None, str | None]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default, None, None
    try:
        value = int(raw)
    except ValueError:
        return default, f"{name.lower()}_invalid", f"{name} must be an integer"
    if minimum is not None and value < minimum:
        return default, f"{name.lower()}_invalid", f"{name} must be greater than or equal to {minimum}"
    if maximum is not None and value > maximum:
        return default, f"{name.lower()}_invalid", f"{name} must be less than or equal to {maximum}"
    return value, None, None


def _is_valid_slack_webhook_url(value: str) -> bool:
    if value == "" or value.isspace():
        return False
    if any(character.isspace() for character in value):
        return False
    parsed = urlparse(value)
    if parsed.scheme.lower() != "https":
        return False
    if not parsed.netloc or not parsed.hostname:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if parsed.fragment:
        return False
    return True


def _load_slack_settings() -> SlackSettings:
    error_code: str | None = None
    error_summary: str | None = None

    def record_error(code: str | None, summary: str | None) -> None:
        nonlocal error_code, error_summary
        if code is None or summary is None or error_code is not None:
            return
        error_code = code
        error_summary = summary

    enabled, code, summary = _env_soft_bool("SLACK_ENABLED", False)
    record_error(code, summary)
    notify_ticket_created, code, summary = _env_soft_bool("SLACK_NOTIFY_TICKET_CREATED", False)
    record_error(code, summary)
    notify_public_message_added, code, summary = _env_soft_bool("SLACK_NOTIFY_PUBLIC_MESSAGE_ADDED", False)
    record_error(code, summary)
    notify_status_changed, code, summary = _env_soft_bool("SLACK_NOTIFY_STATUS_CHANGED", False)
    record_error(code, summary)

    message_preview_max_chars, code, summary = _env_soft_int(
        "SLACK_MESSAGE_PREVIEW_MAX_CHARS",
        SLACK_MESSAGE_PREVIEW_MAX_CHARS_DEFAULT,
        minimum=4,
    )
    record_error(code, summary)
    http_timeout_seconds, code, summary = _env_soft_int(
        "SLACK_HTTP_TIMEOUT_SECONDS",
        SLACK_HTTP_TIMEOUT_SECONDS_DEFAULT,
        minimum=1,
        maximum=30,
    )
    record_error(code, summary)
    delivery_batch_size, code, summary = _env_soft_int(
        "SLACK_DELIVERY_BATCH_SIZE",
        SLACK_DELIVERY_BATCH_SIZE_DEFAULT,
        minimum=1,
    )
    record_error(code, summary)
    delivery_max_attempts, code, summary = _env_soft_int(
        "SLACK_DELIVERY_MAX_ATTEMPTS",
        SLACK_DELIVERY_MAX_ATTEMPTS_DEFAULT,
        minimum=1,
    )
    record_error(code, summary)
    delivery_stale_lock_seconds, code, summary = _env_soft_int(
        "SLACK_DELIVERY_STALE_LOCK_SECONDS",
        SLACK_DELIVERY_STALE_LOCK_SECONDS_DEFAULT,
        minimum=1,
    )
    record_error(code, summary)

    if delivery_stale_lock_seconds <= http_timeout_seconds:
        record_error(
            "slack_delivery_stale_lock_seconds_invalid",
            "SLACK_DELIVERY_STALE_LOCK_SECONDS must be greater than SLACK_HTTP_TIMEOUT_SECONDS",
        )

    targets_payload: dict[str, object] = {}
    raw_targets = os.environ.get("SLACK_TARGETS_JSON")
    if raw_targets is None or raw_targets.strip() == "":
        if enabled:
            record_error(
                "slack_targets_json_missing",
                "SLACK_TARGETS_JSON is required when SLACK_ENABLED=true",
            )
    else:
        try:
            parsed_targets = json.loads(raw_targets)
        except json.JSONDecodeError:
            record_error("slack_targets_json_parse_error", "SLACK_TARGETS_JSON must be a valid JSON object")
        else:
            if not isinstance(parsed_targets, dict):
                record_error("slack_targets_json_not_object", "SLACK_TARGETS_JSON must decode to a JSON object")
            else:
                targets_payload = parsed_targets

    parsed_targets_list: list[SlackTargetSettings] = []
    for target_name, target_value in targets_payload.items():
        if not isinstance(target_name, str) or _SLACK_TARGET_NAME_RE.fullmatch(target_name) is None:
            record_error("slack_target_name_invalid", "SLACK_TARGETS_JSON contains an invalid target name")
            continue
        if not isinstance(target_value, dict):
            record_error("slack_target_entry_invalid", f"Slack target {target_name} must be a JSON object")
            continue
        target_enabled = target_value.get("enabled")
        if not isinstance(target_enabled, bool):
            record_error("slack_target_enabled_invalid", f"Slack target {target_name} must define boolean enabled")
            continue
        webhook_url = target_value.get("webhook_url")
        if not isinstance(webhook_url, str) or not _is_valid_slack_webhook_url(webhook_url):
            record_error(
                "slack_target_webhook_url_invalid",
                f"Slack target {target_name} must define a valid absolute HTTPS webhook_url",
            )
            continue
        parsed_targets_list.append(
            SlackTargetSettings(
                name=target_name,
                enabled=target_enabled,
                webhook_url=webhook_url,
            )
        )

    default_target_name_raw = os.environ.get("SLACK_DEFAULT_TARGET_NAME")
    default_target_name = None
    if default_target_name_raw is not None and default_target_name_raw.strip() != "":
        default_target_name = default_target_name_raw

    any_notify_enabled = (
        notify_ticket_created
        or notify_public_message_added
        or notify_status_changed
    )
    if enabled and any_notify_enabled:
        if default_target_name_raw is None or default_target_name_raw.strip() == "":
            record_error(
                "slack_default_target_missing",
                "SLACK_DEFAULT_TARGET_NAME is required when Slack delivery and any SLACK_NOTIFY_* flag are enabled",
            )
        elif _SLACK_TARGET_NAME_RE.fullmatch(default_target_name_raw) is None:
            record_error(
                "slack_default_target_invalid",
                "SLACK_DEFAULT_TARGET_NAME must match ^[a-z0-9_-]+$",
            )
        elif not any(target.name == default_target_name_raw for target in parsed_targets_list):
            record_error(
                "slack_default_target_not_found",
                "SLACK_DEFAULT_TARGET_NAME must reference a target defined in SLACK_TARGETS_JSON",
            )

    return SlackSettings(
        enabled=enabled,
        default_target_name=default_target_name,
        targets=tuple(parsed_targets_list),
        notify_ticket_created=notify_ticket_created,
        notify_public_message_added=notify_public_message_added,
        notify_status_changed=notify_status_changed,
        message_preview_max_chars=message_preview_max_chars,
        http_timeout_seconds=http_timeout_seconds,
        delivery_batch_size=delivery_batch_size,
        delivery_max_attempts=delivery_max_attempts,
        delivery_stale_lock_seconds=delivery_stale_lock_seconds,
        is_valid=error_code is None,
        config_error_code=error_code,
        config_error_summary=error_summary,
    )


@dataclass(frozen=True)
class Settings:
    app_base_url: str
    app_secret_key: str
    database_url: str
    uploads_dir: Path
    triage_workspace_dir: Path
    repo_mount_dir: Path
    manuals_mount_dir: Path
    codex_bin: str
    codex_api_key: str | None
    codex_model: str
    codex_timeout_seconds: int
    worker_poll_seconds: int
    auto_support_reply_min_confidence: float
    auto_confirm_intent_min_confidence: float
    max_images_per_message: int
    max_image_bytes: int
    session_default_hours: int
    session_remember_days: int
    worker_heartbeat_seconds: int = 60
    ai_run_stale_timeout_seconds: int = 300
    ai_run_max_recovery_attempts: int = 3
    default_ui_locale: str = "en"
    slack: SlackSettings = field(default_factory=SlackSettings)

    @property
    def secure_cookies(self) -> bool:
        return urlparse(self.app_base_url).scheme.lower() == "https"

    @property
    def runs_dir(self) -> Path:
        return self.triage_workspace_dir / "runs"

    @property
    def workspace_skills_dir(self) -> Path:
        return self.triage_workspace_dir / WORKSPACE_SKILLS_RELATIVE_DIR

    def workspace_skill_file_path(self, skill_id: str) -> Path:
        return self.workspace_skills_dir / skill_id / "SKILL.md"

    @property
    def workspace_agents_path(self) -> Path:
        return self.triage_workspace_dir / "AGENTS.md"

    def validate_contracts(self) -> None:
        if self.max_images_per_message <= 0:
            raise SettingsError("MAX_IMAGES_PER_MESSAGE must be positive")
        if self.max_image_bytes <= 0:
            raise SettingsError("MAX_IMAGE_BYTES must be positive")
        if self.session_default_hours <= 0:
            raise SettingsError("SESSION_DEFAULT_HOURS must be positive")
        if self.session_remember_days <= 0:
            raise SettingsError("SESSION_REMEMBER_DAYS must be positive")
        if self.worker_poll_seconds <= 0:
            raise SettingsError("WORKER_POLL_SECONDS must be positive")
        if self.worker_heartbeat_seconds <= 0:
            raise SettingsError("WORKER_HEARTBEAT_SECONDS must be positive")
        if self.codex_timeout_seconds <= 0:
            raise SettingsError("CODEX_TIMEOUT_SECONDS must be positive")
        if self.ai_run_stale_timeout_seconds <= self.worker_heartbeat_seconds:
            raise SettingsError("AI_RUN_STALE_TIMEOUT_SECONDS must be greater than WORKER_HEARTBEAT_SECONDS")
        if self.ai_run_max_recovery_attempts < 0:
            raise SettingsError("AI_RUN_MAX_RECOVERY_ATTEMPTS must be zero or positive")
        _normalize_ui_locale(self.default_ui_locale)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    codex_api_key = os.environ.get("CODEX_API_KEY", "").strip() or None
    settings = Settings(
        app_base_url=_required_env("APP_BASE_URL"),
        app_secret_key=_required_env("APP_SECRET_KEY"),
        database_url=get_database_url(),
        uploads_dir=_env_path("UPLOADS_DIR", DEFAULT_UPLOADS_DIR),
        triage_workspace_dir=_env_path("TRIAGE_WORKSPACE_DIR", DEFAULT_TRIAGE_WORKSPACE_DIR),
        repo_mount_dir=_env_path("REPO_MOUNT_DIR", DEFAULT_REPO_MOUNT_DIR),
        manuals_mount_dir=_env_path("MANUALS_MOUNT_DIR", DEFAULT_MANUALS_MOUNT_DIR),
        codex_bin=_required_env("CODEX_BIN"),
        codex_api_key=codex_api_key,
        codex_model=os.environ.get("CODEX_MODEL", "").strip(),
        codex_timeout_seconds=_env_int("CODEX_TIMEOUT_SECONDS", 3600),
        worker_poll_seconds=_env_int("WORKER_POLL_SECONDS", 10),
        auto_support_reply_min_confidence=_env_float("AUTO_SUPPORT_REPLY_MIN_CONFIDENCE", 0.85),
        auto_confirm_intent_min_confidence=_env_float("AUTO_CONFIRM_INTENT_MIN_CONFIDENCE", 0.90),
        max_images_per_message=_env_int("MAX_IMAGES_PER_MESSAGE", 3),
        max_image_bytes=_env_int("MAX_IMAGE_BYTES", 5 * 1024 * 1024),
        session_default_hours=_env_int("SESSION_DEFAULT_HOURS", 12),
        session_remember_days=_env_int("SESSION_REMEMBER_DAYS", 30),
        worker_heartbeat_seconds=_env_int("WORKER_HEARTBEAT_SECONDS", 60),
        ai_run_stale_timeout_seconds=_env_int("AI_RUN_STALE_TIMEOUT_SECONDS", 300),
        ai_run_max_recovery_attempts=_env_int("AI_RUN_MAX_RECOVERY_ATTEMPTS", 3),
        default_ui_locale=get_default_ui_locale(),
        slack=_load_slack_settings(),
    )
    settings.validate_contracts()
    return settings
