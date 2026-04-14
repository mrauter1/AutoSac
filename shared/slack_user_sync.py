from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import re
import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shared.config import Settings
from shared.db import session_scope
from shared.models import SystemState, User
from shared.security import utc_now
from shared.slack_dm import (
    SlackDMTokenError,
    SlackWebApiResponse,
    load_slack_dm_settings,
    resolve_slack_bot_token,
    slack_api_users_list,
)
from shared.user_admin import normalize_email

SLACK_DM_USER_SYNC_STATE_KEY = "slack_dm_user_sync"
_SYNC_PAGE_LIMIT = 200
_AUTH_FAILURE_ERROR_CODES = {"account_inactive", "invalid_auth", "not_authed", "token_revoked"}
_SCOPE_FAILURE_ERROR_CODES = {"missing_scope", "no_permission", "not_allowed_token_type"}
_SINGLE_LINE_WHITESPACE_RE = re.compile(r"\s+", re.UNICODE)


@dataclass(frozen=True)
class SlackUserSyncSnapshot:
    status: str
    checked_at: str | None = None
    requested_at: str | None = None
    started_at: str | None = None
    trigger: str | None = None
    requested_by_user_id: str | None = None
    error_code: str | None = None
    summary: str | None = None
    matched_count: int | None = None
    updated_count: int | None = None
    no_match_count: int | None = None
    conflict_count: int | None = None


@dataclass(frozen=True)
class SlackUserSyncRequest:
    requested_at: str | None = None
    started_at: str | None = None
    trigger: str | None = None
    requested_by_user_id: str | None = None


@dataclass(frozen=True)
class MissingSlackUserCandidate:
    user_id: uuid.UUID
    email: str


def load_slack_user_sync_state(db: Session) -> SlackUserSyncSnapshot | None:
    getter = getattr(db, "get", None)
    if not callable(getter):
        return None
    state = getter(SystemState, SLACK_DM_USER_SYNC_STATE_KEY)
    if state is None or not isinstance(state.value_json, dict):
        return None
    payload = state.value_json
    return SlackUserSyncSnapshot(
        status=_read_string(payload, "status") or "unknown",
        checked_at=_read_string(payload, "checked_at"),
        requested_at=_read_string(payload, "requested_at"),
        started_at=_read_string(payload, "started_at"),
        trigger=_read_string(payload, "trigger"),
        requested_by_user_id=_read_string(payload, "requested_by_user_id"),
        error_code=_read_string(payload, "error_code"),
        summary=_read_string(payload, "summary"),
        matched_count=_read_int(payload, "matched_count"),
        updated_count=_read_int(payload, "updated_count"),
        no_match_count=_read_int(payload, "no_match_count"),
        conflict_count=_read_int(payload, "conflict_count"),
    )


def persist_slack_user_sync_state(
    db: Session,
    *,
    snapshot: SlackUserSyncSnapshot,
    updated_at=None,
) -> SystemState:
    getter = getattr(db, "get", None)
    state = getter(SystemState, SLACK_DM_USER_SYNC_STATE_KEY) if callable(getter) else None
    existing_payload = state.value_json if state is not None and isinstance(state.value_json, dict) else {}
    request_pending = bool(existing_payload.get("request_pending"))
    resolved_updated_at = updated_at or utc_now()
    payload = _snapshot_payload(snapshot)
    if request_pending:
        payload["status"] = "requested"
        payload["request_pending"] = True
        payload["requested_at"] = _read_string(existing_payload, "requested_at")
        payload["trigger"] = _read_string(existing_payload, "trigger")
        payload["requested_by_user_id"] = _read_string(existing_payload, "requested_by_user_id")
    else:
        payload["request_pending"] = False
    if state is None:
        state = SystemState(
            key=SLACK_DM_USER_SYNC_STATE_KEY,
            value_json=payload,
            updated_at=resolved_updated_at,
        )
        db.add(state)
        return state
    state.value_json = payload
    state.updated_at = resolved_updated_at
    return state


def request_slack_user_sync(
    db: Session,
    *,
    trigger: str,
    requested_by_user_id: uuid.UUID | None = None,
    updated_at=None,
) -> SystemState:
    getter = getattr(db, "get", None)
    state = getter(SystemState, SLACK_DM_USER_SYNC_STATE_KEY) if callable(getter) else None
    resolved_updated_at = updated_at or utc_now()
    payload = dict(state.value_json) if state is not None and isinstance(state.value_json, dict) else {}
    payload["request_pending"] = True
    payload["requested_at"] = resolved_updated_at.isoformat()
    payload["trigger"] = trigger.strip()
    payload["requested_by_user_id"] = str(requested_by_user_id) if requested_by_user_id is not None else None
    if payload.get("status") != "running":
        payload["status"] = "requested"
    if state is None:
        state = SystemState(
            key=SLACK_DM_USER_SYNC_STATE_KEY,
            value_json=payload,
            updated_at=resolved_updated_at,
        )
        db.add(state)
        return state
    state.value_json = payload
    state.updated_at = resolved_updated_at
    return state


def claim_requested_slack_user_sync(
    db: Session,
    *,
    worker_instance_id: str,
    started_at=None,
) -> SlackUserSyncRequest | None:
    statement = (
        select(SystemState)
        .where(SystemState.key == SLACK_DM_USER_SYNC_STATE_KEY)
        .with_for_update(skip_locked=True)
    )
    state = db.execute(statement).scalar_one_or_none()
    if state is None or not isinstance(state.value_json, dict):
        return None
    payload = dict(state.value_json)
    if not payload.get("request_pending"):
        return None
    resolved_started_at = started_at or utc_now()
    payload["request_pending"] = False
    payload["status"] = "running"
    payload["started_at"] = resolved_started_at.isoformat()
    payload["running_worker_instance_id"] = worker_instance_id
    state.value_json = payload
    state.updated_at = resolved_started_at
    return SlackUserSyncRequest(
        requested_at=_read_string(payload, "requested_at"),
        started_at=payload["started_at"],
        trigger=_read_string(payload, "trigger"),
        requested_by_user_id=_read_string(payload, "requested_by_user_id"),
    )


def load_users_missing_slack_user_id(db: Session) -> list[MissingSlackUserCandidate]:
    statement = (
        select(User.id, User.email)
        .where(User.slack_user_id.is_(None))
        .order_by(User.created_at, User.id)
    )
    return [
        MissingSlackUserCandidate(user_id=user_id, email=email)
        for user_id, email in db.execute(statement).all()
        if isinstance(user_id, uuid.UUID) and isinstance(email, str)
    ]


def fetch_slack_directory_members_by_email(
    *,
    bot_token: str,
    timeout_seconds: int,
) -> tuple[dict[str, str], set[str]]:
    by_email: dict[str, str] = {}
    ambiguous_emails: set[str] = set()
    cursor: str | None = None
    eligible_member_seen = False
    email_field_seen = False
    while True:
        try:
            response = slack_api_users_list(
                bot_token=bot_token,
                timeout_seconds=timeout_seconds,
                limit=_SYNC_PAGE_LIMIT,
                cursor=cursor,
            )
        except httpx.TransportError as exc:
            raise SlackUserSyncRequestError(
                error_code="users_list_request_failed",
                summary="Slack users.list request failed.",
            ) from exc
        invalid_config = _build_invalid_config_error(response)
        if invalid_config is not None:
            raise invalid_config
        if not response.ok or not isinstance(response.body_json, dict):
            raise SlackUserSyncRequestError(
                error_code=response.error_code or _build_http_error_code(response),
                summary=_build_response_summary(response),
            )
        members = response.body_json.get("members")
        if not isinstance(members, list):
            raise SlackUserSyncRequestError(
                error_code="users_list_invalid_response",
                summary="Slack users.list returned an invalid response.",
            )
        for member in members:
            if not isinstance(member, dict):
                continue
            if member.get("deleted") is True or member.get("is_bot") is True or member.get("is_app_user") is True:
                continue
            eligible_member_seen = True
            slack_user_id = member.get("id")
            profile = member.get("profile")
            email = profile.get("email") if isinstance(profile, dict) else None
            if not isinstance(slack_user_id, str) or not slack_user_id.strip():
                continue
            if not isinstance(email, str) or not email.strip():
                continue
            email_field_seen = True
            normalized_email = normalize_email(email)
            if normalized_email in ambiguous_emails:
                continue
            existing_slack_user_id = by_email.get(normalized_email)
            if existing_slack_user_id is not None and existing_slack_user_id != slack_user_id.strip():
                ambiguous_emails.add(normalized_email)
                by_email.pop(normalized_email, None)
                continue
            by_email[normalized_email] = slack_user_id.strip()
        cursor = _read_next_cursor(response.body_json)
        if not cursor:
            if eligible_member_seen and not email_field_seen:
                raise SlackUserSyncInvalidConfig(
                    error_code="users_list_missing_email_scope",
                    summary="Slack users.list did not expose profile.email. The bot token likely needs users:read and users:read.email.",
                )
            return by_email, ambiguous_emails


def match_missing_users_by_email(
    missing_users: list[MissingSlackUserCandidate],
    *,
    slack_users_by_email: dict[str, str],
    ambiguous_emails: set[str],
) -> tuple[dict[uuid.UUID, str], int, int]:
    matches: dict[uuid.UUID, str] = {}
    no_match_count = 0
    conflict_count = 0
    for candidate in missing_users:
        normalized_email = normalize_email(candidate.email)
        if normalized_email in ambiguous_emails:
            conflict_count += 1
            continue
        slack_user_id = slack_users_by_email.get(normalized_email)
        if slack_user_id is None:
            no_match_count += 1
            continue
        matches[candidate.user_id] = slack_user_id
    return matches, no_match_count, conflict_count


def apply_slack_user_id_matches(
    db: Session,
    *,
    matches: dict[uuid.UUID, str],
) -> tuple[int, int]:
    if not matches:
        return 0, 0
    users = {
        user.id: user
        for user in db.execute(select(User).where(User.id.in_(tuple(matches)))).scalars()
    }
    occupied_by_slack_user_id = {
        user.slack_user_id: user.id
        for user in db.execute(select(User).where(User.slack_user_id.in_(tuple(set(matches.values()))))).scalars()
        if isinstance(user.slack_user_id, str)
    }
    updated_count = 0
    conflict_count = 0
    for user_id, slack_user_id in matches.items():
        user = users.get(user_id)
        if user is None:
            continue
        if user.slack_user_id is not None:
            continue
        occupant_id = occupied_by_slack_user_id.get(slack_user_id)
        if occupant_id is not None and occupant_id != user.id:
            conflict_count += 1
            continue
        try:
            with db.begin_nested():
                user.slack_user_id = slack_user_id
                user.updated_at = utc_now()
                db.flush()
        except IntegrityError:
            db.expire(user)
            conflict_count += 1
            continue
        occupied_by_slack_user_id[slack_user_id] = user.id
        updated_count += 1
    return updated_count, conflict_count


def sync_slack_user_ids_by_email(
    settings: Settings,
    *,
    trigger: str | None = None,
    started_at: str | None = None,
    requested_at: str | None = None,
    requested_by_user_id: str | None = None,
) -> SlackUserSyncSnapshot:
    with session_scope(settings) as db:
        slack_settings = load_slack_dm_settings(db, app_settings=settings)
        missing_users = load_users_missing_slack_user_id(db)
    checked_at = utc_now().isoformat()
    if not missing_users:
        return SlackUserSyncSnapshot(
            status="succeeded",
            checked_at=checked_at,
            requested_at=requested_at,
            started_at=started_at,
            trigger=trigger,
            requested_by_user_id=requested_by_user_id,
            matched_count=0,
            updated_count=0,
            no_match_count=0,
            conflict_count=0,
            summary="No AutoSac users were missing Slack user IDs.",
        )
    try:
        bot_token = resolve_slack_bot_token(
            slack_settings,
            app_secret_key=settings.app_secret_key,
        )
    except SlackDMTokenError as exc:
        return SlackUserSyncSnapshot(
            status="invalid_config",
            checked_at=checked_at,
            requested_at=requested_at,
            started_at=started_at,
            trigger=trigger,
            requested_by_user_id=requested_by_user_id,
            error_code="slack_bot_token_unusable",
            summary=_sanitize_summary(str(exc)),
        )
    try:
        slack_users_by_email, ambiguous_emails = fetch_slack_directory_members_by_email(
            bot_token=bot_token,
            timeout_seconds=slack_settings.http_timeout_seconds,
        )
    except SlackUserSyncInvalidConfig as exc:
        return SlackUserSyncSnapshot(
            status="invalid_config",
            checked_at=checked_at,
            requested_at=requested_at,
            started_at=started_at,
            trigger=trigger,
            requested_by_user_id=requested_by_user_id,
            error_code=exc.error_code,
            summary=exc.summary,
        )
    except SlackUserSyncRequestError as exc:
        return SlackUserSyncSnapshot(
            status="failed",
            checked_at=checked_at,
            requested_at=requested_at,
            started_at=started_at,
            trigger=trigger,
            requested_by_user_id=requested_by_user_id,
            error_code=exc.error_code,
            summary=exc.summary,
        )
    matches, no_match_count, initial_conflict_count = match_missing_users_by_email(
        missing_users,
        slack_users_by_email=slack_users_by_email,
        ambiguous_emails=ambiguous_emails,
    )
    with session_scope(settings) as db:
        updated_count, apply_conflict_count = apply_slack_user_id_matches(db, matches=matches)
    matched_count = len(matches)
    conflict_count = initial_conflict_count + apply_conflict_count
    return SlackUserSyncSnapshot(
        status="succeeded",
        checked_at=checked_at,
        requested_at=requested_at,
        started_at=started_at,
        trigger=trigger,
        requested_by_user_id=requested_by_user_id,
        matched_count=matched_count,
        updated_count=updated_count,
        no_match_count=no_match_count,
        conflict_count=conflict_count,
        summary=_build_success_summary(
            matched_count=matched_count,
            updated_count=updated_count,
            no_match_count=no_match_count,
            conflict_count=conflict_count,
        ),
    )


class SlackUserSyncInvalidConfig(RuntimeError):
    def __init__(self, *, error_code: str, summary: str):
        super().__init__(summary)
        self.error_code = error_code
        self.summary = summary


class SlackUserSyncRequestError(RuntimeError):
    def __init__(self, *, error_code: str, summary: str):
        super().__init__(summary)
        self.error_code = error_code
        self.summary = summary


def _snapshot_payload(snapshot: SlackUserSyncSnapshot) -> dict[str, Any]:
    return {
        "status": snapshot.status,
        "checked_at": snapshot.checked_at,
        "requested_at": snapshot.requested_at,
        "started_at": snapshot.started_at,
        "trigger": snapshot.trigger,
        "requested_by_user_id": snapshot.requested_by_user_id,
        "error_code": snapshot.error_code,
        "summary": snapshot.summary,
        "matched_count": snapshot.matched_count,
        "updated_count": snapshot.updated_count,
        "no_match_count": snapshot.no_match_count,
        "conflict_count": snapshot.conflict_count,
    }


def _build_invalid_config_error(response: SlackWebApiResponse) -> SlackUserSyncInvalidConfig | None:
    error_code = response.error_code
    if error_code not in _AUTH_FAILURE_ERROR_CODES and error_code not in _SCOPE_FAILURE_ERROR_CODES:
        if response.http_status not in {401, 403}:
            return None
    return SlackUserSyncInvalidConfig(
        error_code=error_code or _build_http_error_code(response),
        summary=_build_response_summary(response),
    )


def _build_response_summary(response: SlackWebApiResponse) -> str:
    if response.error_code:
        return f"Slack {response.method} returned {response.error_code}"
    return f"Slack {response.method} returned HTTP {response.http_status}"


def _build_http_error_code(response: SlackWebApiResponse) -> str:
    return f"{response.method.replace('.', '_')}_http_{response.http_status}"


def _read_next_cursor(payload: dict[str, Any]) -> str | None:
    response_metadata = payload.get("response_metadata")
    if not isinstance(response_metadata, dict):
        return None
    next_cursor = response_metadata.get("next_cursor")
    if not isinstance(next_cursor, str):
        return None
    stripped = next_cursor.strip()
    return stripped or None


def _build_success_summary(
    *,
    matched_count: int,
    updated_count: int,
    no_match_count: int,
    conflict_count: int,
) -> str:
    return (
        f"Matched {matched_count} user(s) by email, updated {updated_count}, "
        f"left {no_match_count} unmatched, and skipped {conflict_count} conflict(s)."
    )


def _read_string(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _read_int(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _sanitize_summary(value: str) -> str:
    return _SINGLE_LINE_WHITESPACE_RE.sub(" ", value).strip()
