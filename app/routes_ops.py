from __future__ import annotations

from collections import defaultdict
from typing import Any
import uuid

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai_run_presenters import present_ai_run_output, present_ticket_route_target
from app.i18n import (
    DEFAULT_UI_LOCALE,
    ops_author_label,
    ops_role_suffix_label,
    ops_status_change_summary,
    ops_status_label,
    resolve_ui_locale,
    timeline_lane_label,
)
from app.auth import get_required_auth_session, require_admin_user, require_ops_user, validate_csrf_token
from app.render import render_markdown_to_html
from app.timeline import build_author_label, load_ticket_status_history, load_users_by_ids, merge_timeline_items, serialize_status_changes
from app.ui import build_template_context, is_htmx_request, templates
from shared.config import Settings, get_settings
from shared.db import db_session_dependency
from shared.integrations import build_slack_runtime_context
from shared.models import AIDraft, AIRun, AIRunStep, Ticket, TicketAttachment, TicketMessage, TicketView, User
from shared.permissions import is_admin_user, is_ops_user
from shared.routing_registry import RoutingRegistryError, load_routing_registry
from shared.slack_dm import (
    SlackDMSettingsError,
    SlackDMSettingsInput,
    clear_slack_dm_token,
    load_slack_delivery_health,
    load_slack_dm_settings,
    parse_slack_auth_test_result,
    slack_api_auth_test,
    upsert_slack_dm_settings,
    validate_slack_dm_settings_input,
)
from shared.slack_user_sync import load_slack_user_sync_state, request_slack_user_sync
from shared.user_admin import create_user, set_user_active_state, update_user
from shared.ticketing import (
    add_ops_internal_note,
    add_ops_public_reply,
    assign_ticket_for_ops,
    publish_ai_draft_for_ops,
    reject_ai_draft_for_ops,
    request_manual_rerun,
    set_ticket_status_for_ops,
    upsert_ticket_view,
)

router = APIRouter()

_OPS_DRAFT_REPLY_STATUSES = ("waiting_on_user", "waiting_on_dev_ti", "resolved")
_OPS_PUBLIC_REPLY_STATUSES = ("ai_triage", "waiting_on_user", "waiting_on_dev_ti", "resolved")
_OPS_FILTERABLE_STATUSES = ("new", "ai_triage", "waiting_on_user", "waiting_on_dev_ti", "resolved")
_MANAGEABLE_USER_ROLES = ("requester", "dev_ti")


def _parse_bool(value: str | None) -> bool:
    return value in {"on", "true", "1", "yes"}


def _parse_required_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"on", "true", "1", "yes"}:
        return True
    if normalized in {"off", "false", "0", "no"}:
        return False
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid active state.")


def _allowed_new_user_roles(actor: User) -> tuple[str, ...]:
    if actor.role == "admin":
        return _MANAGEABLE_USER_ROLES
    return ("requester",)


def _can_edit_user_profile(actor: User, target: User) -> bool:
    if actor.role == "admin":
        return True
    return target.role in _MANAGEABLE_USER_ROLES


def _can_change_user_role(actor: User, target: User, requested_role: str | None = None) -> bool:
    if actor.role != "admin" or target.role == "admin":
        return False
    if requested_role is None:
        return True
    return requested_role in _allowed_new_user_roles(actor)


def _can_toggle_user_active(actor: User, target: User) -> bool:
    return target.role != "admin" and _can_edit_user_profile(actor, target)


def _load_users_for_admin(db: Session) -> list[User]:
    return list(db.execute(select(User).order_by(User.is_active.desc(), User.created_at.desc())).scalars())


def _load_user_or_404(db: Session, *, user_id: str) -> User:
    try:
        user_uuid = uuid.UUID(user_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found") from exc
    user = db.get(User, user_uuid)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


def _load_ops_ticket_or_404(db: Session, *, reference: str) -> Ticket:
    ticket = db.execute(select(Ticket).where(Ticket.reference == reference)).scalar_one_or_none()
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    return ticket


def _load_ops_draft_or_404(db: Session, *, draft_id: str) -> AIDraft:
    try:
        draft_uuid = uuid.UUID(draft_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found") from exc
    draft = db.get(AIDraft, draft_uuid)
    if draft is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found")
    return draft


def _lane_label(visibility: str, ui_locale: str) -> str:
    return timeline_lane_label("internal" if visibility == "internal" else "public", ui_locale)


def _load_attachments_by_message(db: Session, *, ticket_id, visibility: str | None = None) -> dict[Any, list[TicketAttachment]]:
    statement = select(TicketAttachment).where(TicketAttachment.ticket_id == ticket_id)
    if visibility is not None:
        statement = statement.where(TicketAttachment.visibility == visibility)
    attachments = list(db.execute(statement.order_by(TicketAttachment.created_at.asc(), TicketAttachment.id.asc())).scalars())
    grouped: dict[Any, list[TicketAttachment]] = defaultdict(list)
    for attachment in attachments:
        grouped[attachment.message_id].append(attachment)
    return grouped


def _serialize_thread(
    db: Session,
    *,
    ticket_id,
    ui_locale: str = DEFAULT_UI_LOCALE,
    visibility: str | None = None,
) -> list[dict[str, object]]:
    attachments_by_message = _load_attachments_by_message(db, ticket_id=ticket_id, visibility=visibility)
    statement = select(TicketMessage).where(TicketMessage.ticket_id == ticket_id)
    if visibility is not None:
        statement = statement.where(TicketMessage.visibility == visibility)
    messages = list(db.execute(statement.order_by(TicketMessage.created_at.asc(), TicketMessage.id.asc())).scalars())
    users_by_id = load_users_by_ids(db, (message.author_user_id for message in messages))
    return [
        {
            "kind": "message",
            "id": str(message.id),
            "created_at": message.created_at,
            "lane": message.visibility,
            "lane_label": _lane_label(message.visibility, ui_locale),
            "author_type": message.author_type,
            "author_label": build_author_label(
                author_type=message.author_type,
                display_name=users_by_id.get(message.author_user_id).display_name if message.author_user_id in users_by_id else None,
                fallback_label=lambda author_type: ops_author_label(author_type, ui_locale),
                role_suffix_label=lambda author_type: ops_role_suffix_label(author_type, ui_locale),
            ),
            "source": message.source,
            "body_markdown": message.body_markdown,
            "body_html": render_markdown_to_html(message.body_markdown),
            "attachments": attachments_by_message.get(message.id, []),
        }
        for message in messages
    ]


def _build_ops_activity_timeline(db: Session, *, ticket_id, ui_locale: str = DEFAULT_UI_LOCALE) -> list[dict[str, object]]:
    history_entries = load_ticket_status_history(db, ticket_id=ticket_id)
    users_by_id = load_users_by_ids(db, (getattr(entry, "changed_by_user_id", None) for entry in history_entries))
    return merge_timeline_items(
        _serialize_thread(db, ticket_id=ticket_id, ui_locale=ui_locale),
        serialize_status_changes(
            history_entries,
            status_label=lambda status: ops_status_label(status, ui_locale),
            actor_label=lambda author_type: ops_author_label(author_type, ui_locale),
            actor_role_suffix_label=lambda author_type: ops_role_suffix_label(author_type, ui_locale),
            status_summary=lambda from_status_label, to_status_label: ops_status_change_summary(
                from_status_label,
                to_status_label,
                ui_locale,
            ),
            lane_label=timeline_lane_label("status", ui_locale),
            user_display_names={user_id: user.display_name for user_id, user in users_by_id.items()},
        ),
    )


def _load_ops_users(db: Session) -> list[User]:
    return list(
        db.execute(
            select(User)
            .where(User.is_active.is_(True), User.role.in_(("dev_ti", "admin")))
            .order_by(User.display_name.asc())
        ).scalars()
    )


def _load_latest_run(db: Session, *, ticket_id) -> AIRun | None:
    return db.execute(
        select(AIRun).where(AIRun.ticket_id == ticket_id).order_by(AIRun.created_at.desc())
    ).scalars().first()


def _load_latest_analysis_run(db: Session, *, ticket_id) -> AIRun | None:
    return db.execute(
        select(AIRun)
        .where(
            AIRun.ticket_id == ticket_id,
            AIRun.status.in_(("succeeded", "human_review")),
            AIRun.final_output_json.is_not(None),
        )
        .order_by(AIRun.created_at.desc())
    ).scalars().first()


def _load_run_steps(db: Session, *, run_id) -> list[AIRunStep]:
    return list(
        db.execute(select(AIRunStep).where(AIRunStep.ai_run_id == run_id).order_by(AIRunStep.step_index.asc())).scalars()
    )


def _load_pending_draft(db: Session, *, ticket_id) -> AIDraft | None:
    return db.execute(
        select(AIDraft)
        .where(AIDraft.ticket_id == ticket_id, AIDraft.status == "pending_approval")
        .order_by(AIDraft.created_at.desc())
    ).scalars().first()


def _load_latest_internal_ai_note(db: Session, *, ticket_id) -> TicketMessage | None:
    return db.execute(
        select(TicketMessage)
        .where(
            TicketMessage.ticket_id == ticket_id,
            TicketMessage.visibility == "internal",
            TicketMessage.source == "ai_internal_note",
        )
        .order_by(TicketMessage.created_at.desc())
    ).scalars().first()


def _ops_route_target_options() -> list[dict[str, str]]:
    registry = load_routing_registry()
    return [
        {
            "id": route_target.id,
            "label": route_target.label,
        }
        for route_target in registry.ops_visible_route_targets()
    ]


def _ops_manual_rerun_specialist_options() -> list[dict[str, str]]:
    registry = load_routing_registry()
    return [
        {
            "route_target_id": option.route_target_id,
            "route_target_label": option.route_target_label,
            "specialist_id": option.specialist_id,
            "specialist_display_name": option.specialist_display_name,
        }
        for option in registry.manual_rerun_specialist_options()
    ]


def _last_public_message_item_id(timeline: list[dict[str, object]]) -> str | None:
    for item in reversed(timeline):
        if item.get("kind") != "message" or item.get("lane") != "public":
            continue
        item_id = item.get("id")
        if isinstance(item_id, str) and item_id:
            return item_id
    return None


def _resolve_manual_rerun_specialist_override(forced_route_target_id: str) -> tuple[str | None, str | None]:
    route_target_value = forced_route_target_id.strip()
    if not route_target_value:
        return None, None
    try:
        option = load_routing_registry().require_manual_rerun_specialist_option(route_target_value)
    except RoutingRegistryError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return option.route_target_id, option.specialist_id


def _normalize_optional_user_id(user_id: str | None) -> str | None:
    candidate = (user_id or "").strip()
    if not candidate:
        return None
    try:
        return str(uuid.UUID(candidate))
    except ValueError:
        return None


def _users_page_path(*, open_create_form: bool = False, editing_user_id: str | None = None) -> str:
    if open_create_form:
        return "/ops/users?create=1"
    normalized_user_id = _normalize_optional_user_id(editing_user_id)
    if normalized_user_id is not None:
        return f"/ops/users?edit_user={normalized_user_id}"
    return "/ops/users"


def _read_users_ui_state(request: Request) -> tuple[bool, str | None]:
    open_create_form = _parse_bool(request.query_params.get("create"))
    editing_user_id = None if open_create_form else _normalize_optional_user_id(request.query_params.get("edit_user"))
    return open_create_form, editing_user_id


def _users_page_extra(
    *,
    db: Session,
    current_user: User,
    error: str | None = None,
    open_create_form: bool = False,
    editing_user_id: str | None = None,
) -> dict[str, object]:
    extra: dict[str, object] = {
        "users": _load_users_for_admin(db),
        "allowed_new_roles": _allowed_new_user_roles(current_user),
        "open_create_form": open_create_form,
        "editing_user_id": None if open_create_form else _normalize_optional_user_id(editing_user_id),
        "can_edit_user_profile": lambda user: _can_edit_user_profile(current_user, user),
        "can_change_user_role": lambda user: _can_change_user_role(current_user, user),
        "can_toggle_user_active": lambda user: _can_toggle_user_active(current_user, user),
    }
    if error is not None:
        extra["error"] = error
    return extra


def _render_ops_users_page(
    request: Request,
    *,
    current_user: User,
    auth_session,
    db: Session,
    status_code: int = status.HTTP_200_OK,
    error: str | None = None,
    open_create_form: bool = False,
    editing_user_id: str | None = None,
):
    return templates.TemplateResponse(
        request,
        "ops_users.html",
        build_template_context(
            request=request,
            current_user=current_user,
            auth_session=auth_session,
            extra=_users_page_extra(
                db=db,
                current_user=current_user,
                error=error,
                open_create_form=open_create_form,
                editing_user_id=editing_user_id,
            ),
            ui_switch_path=_users_page_path(open_create_form=open_create_form, editing_user_id=editing_user_id),
        ),
        status_code=status_code,
    )


def _slack_integration_page_path() -> str:
    return "/ops/integrations/slack"


def _build_slack_form_values(
    *,
    slack_settings,
    overrides: dict[str, object] | None = None,
) -> dict[str, object]:
    values: dict[str, object] = {
        "enabled": slack_settings.enabled,
        "notify_ticket_created": slack_settings.notify_ticket_created,
        "notify_public_message_added": slack_settings.notify_public_message_added,
        "notify_status_changed": slack_settings.notify_status_changed,
        "message_preview_max_chars": slack_settings.message_preview_max_chars,
        "http_timeout_seconds": slack_settings.http_timeout_seconds,
        "delivery_batch_size": slack_settings.delivery_batch_size,
        "delivery_max_attempts": slack_settings.delivery_max_attempts,
        "delivery_stale_lock_seconds": slack_settings.delivery_stale_lock_seconds,
    }
    if overrides:
        values.update(overrides)
    return values


def _slack_integration_page_extra(
    *,
    db: Session,
    current_user: User,
    settings: Settings,
    error: str | None = None,
    form_values: dict[str, object] | None = None,
) -> dict[str, object]:
    slack_settings = load_slack_dm_settings(db, app_settings=settings)
    updated_by_user = None
    getter = getattr(db, "get", None)
    if callable(getter) and slack_settings.updated_by_user_id is not None:
        updated_by_user = getter(User, slack_settings.updated_by_user_id)
    extra: dict[str, object] = {
        "slack_settings": slack_settings,
        "slack_form": _build_slack_form_values(slack_settings=slack_settings, overrides=form_values),
        "slack_delivery_health": load_slack_delivery_health(db),
        "slack_user_sync_state": load_slack_user_sync_state(db),
        "slack_updated_by_user": updated_by_user,
        "can_manage_slack_settings": is_admin_user(current_user),
    }
    if error is not None:
        extra["error"] = error
    return extra


def _render_slack_integration_page(
    request: Request,
    *,
    current_user: User,
    auth_session,
    db: Session,
    settings: Settings,
    status_code: int = status.HTTP_200_OK,
    error: str | None = None,
    form_values: dict[str, object] | None = None,
):
    return templates.TemplateResponse(
        request,
        "ops_slack_integration.html",
        build_template_context(
            request=request,
            current_user=current_user,
            auth_session=auth_session,
            extra=_slack_integration_page_extra(
                db=db,
                current_user=current_user,
                settings=settings,
                error=error,
                form_values=form_values,
            ),
            ui_switch_path=_slack_integration_page_path(),
        ),
        status_code=status_code,
    )


def _resolve_requested_slack_user_id(
    *,
    actor: User,
    submitted_value: str | None,
    current_value: str | None = None,
) -> str | None:
    if is_admin_user(actor):
        if submitted_value is None:
            return current_value
        return submitted_value
    if submitted_value is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return current_value


def _request_slack_user_sync_if_session_configured(
    db: Session,
    *,
    trigger: str,
    requested_by_user_id,
) -> None:
    session_info = getattr(db, "info", None)
    if not isinstance(session_info, dict):
        return
    app_settings = session_info.get("settings")
    if not isinstance(app_settings, Settings):
        return
    slack_settings = load_slack_dm_settings(db, app_settings=app_settings)
    if not slack_settings.has_stored_token:
        return
    request_slack_user_sync(
        db,
        trigger=trigger,
        requested_by_user_id=requested_by_user_id,
    )


def _should_request_slack_user_sync(requested_slack_user_id: str | None) -> bool:
    return requested_slack_user_id in {None, ""}


def _run_slack_auth_test(*, bot_token: str, timeout_seconds: int):
    try:
        response = slack_api_auth_test(bot_token=bot_token, timeout_seconds=timeout_seconds)
    except httpx.HTTPError as exc:
        raise SlackDMSettingsError("Slack auth.test request failed.") from exc
    if not response.ok:
        if response.error_code:
            raise SlackDMSettingsError(f"Slack auth.test failed: {response.error_code}")
        raise SlackDMSettingsError("Slack auth.test failed.")
    return parse_slack_auth_test_result(response)


def _read_filters(request: Request) -> dict[str, object]:
    query = request.query_params
    return {
        "status": query.get("status", "").strip(),
        "route_target_id": query.get("route_target_id", "").strip(),
        "assigned_to": query.get("assigned_to", "").strip(),
        "urgent": _parse_bool(query.get("urgent")),
        "unassigned_only": _parse_bool(query.get("unassigned_only")),
        "created_by_me": _parse_bool(query.get("created_by_me")),
        "needs_approval": _parse_bool(query.get("needs_approval")),
        "updated_since_viewed": _parse_bool(query.get("updated_since_viewed")),
    }


def _load_filtered_ticket_rows(db: Session, *, current_user: User, filters: dict[str, object]) -> list[dict[str, object]]:
    statement = select(Ticket).order_by(Ticket.updated_at.desc())
    status_filter = str(filters["status"])
    route_target_filter = str(filters["route_target_id"])
    assigned_to_filter = str(filters["assigned_to"])
    if status_filter:
        statement = statement.where(Ticket.status == status_filter)
    if route_target_filter:
        statement = statement.where(Ticket.route_target_id == route_target_filter)
    if filters["urgent"]:
        statement = statement.where(Ticket.urgent.is_(True))
    if filters["unassigned_only"]:
        statement = statement.where(Ticket.assigned_to_user_id.is_(None))
    if filters["created_by_me"]:
        statement = statement.where(Ticket.created_by_user_id == current_user.id)
    if assigned_to_filter:
        if assigned_to_filter == "unassigned":
            statement = statement.where(Ticket.assigned_to_user_id.is_(None))
        else:
            try:
                statement = statement.where(Ticket.assigned_to_user_id == uuid.UUID(assigned_to_filter))
            except ValueError:
                return []

    tickets = list(db.execute(statement).scalars())
    if not tickets:
        return []

    ticket_ids = [ticket.id for ticket in tickets]
    views = {
        view.ticket_id: view.last_viewed_at
        for view in db.execute(
            select(TicketView).where(TicketView.user_id == current_user.id, TicketView.ticket_id.in_(ticket_ids))
        ).scalars()
    }
    pending_drafts: dict[uuid.UUID, AIDraft] = {}
    for draft in db.execute(
        select(AIDraft)
        .where(AIDraft.ticket_id.in_(ticket_ids), AIDraft.status == "pending_approval")
        .order_by(AIDraft.created_at.desc())
    ).scalars():
        pending_drafts.setdefault(draft.ticket_id, draft)
    user_ids = {ticket.created_by_user_id for ticket in tickets}
    user_ids.update(ticket.assigned_to_user_id for ticket in tickets if ticket.assigned_to_user_id is not None)
    users = {
        user.id: user
        for user in db.execute(select(User).where(User.id.in_(user_ids))).scalars()
    }

    rows: list[dict[str, object]] = []
    for ticket in tickets:
        last_viewed_at = views.get(ticket.id)
        updated_for_user = last_viewed_at is None or ticket.updated_at > last_viewed_at
        pending_draft = pending_drafts.get(ticket.id)
        if filters["needs_approval"] and pending_draft is None:
            continue
        if filters["updated_since_viewed"] and not updated_for_user:
            continue
        rows.append(
            {
                "ticket": ticket,
                "creator": users.get(ticket.created_by_user_id),
                "assignee": users.get(ticket.assigned_to_user_id),
                "updated_for_user": updated_for_user,
                "pending_draft": pending_draft,
                "route_target_display": present_ticket_route_target(ticket),
            }
        )
    return rows


def _group_ticket_rows(rows: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    groups = {status: [] for status in _OPS_FILTERABLE_STATUSES}
    for row in rows:
        groups[row["ticket"].status].append(row)
    return groups


def _ops_filter_context(db: Session, *, current_user: User, filters: dict[str, object]) -> dict[str, object]:
    rows = _load_filtered_ticket_rows(db, current_user=current_user, filters=filters)
    return {
        "filters": filters,
        "ops_users": _load_ops_users(db),
        "status_options": _OPS_FILTERABLE_STATUSES,
        "route_target_options": _ops_route_target_options(),
        "rows": rows,
        "grouped_rows": _group_ticket_rows(rows),
    }


def _default_public_reply_status(*, ticket: Ticket, current_user: User) -> str:
    if ticket.created_by_user_id == current_user.id and is_ops_user(current_user):
        return "ai_triage"
    return "waiting_on_user"


def _ticket_detail_context(
    db: Session,
    *,
    ticket: Ticket,
    current_user: User,
    ui_locale: str = DEFAULT_UI_LOCALE,
) -> dict[str, object]:
    pending_draft = _load_pending_draft(db, ticket_id=ticket.id)
    latest_run = _load_latest_run(db, ticket_id=ticket.id)
    latest_analysis_run = _load_latest_analysis_run(db, ticket_id=ticket.id)
    latest_run_id = getattr(latest_run, "id", None)
    latest_analysis_run_id = getattr(latest_analysis_run, "id", None)
    latest_run_steps = _load_run_steps(db, run_id=latest_run_id) if latest_run_id is not None else []
    latest_analysis_steps = _load_run_steps(db, run_id=latest_analysis_run_id) if latest_analysis_run_id is not None else []
    latest_ai_note = _load_latest_internal_ai_note(db, ticket_id=ticket.id)
    analysis_view = present_ai_run_output(latest_analysis_run)
    activity_timeline = _build_ops_activity_timeline(db, ticket_id=ticket.id, ui_locale=ui_locale)
    creator = db.get(User, ticket.created_by_user_id)
    assignee = db.get(User, ticket.assigned_to_user_id) if ticket.assigned_to_user_id else None
    return {
        "ticket": ticket,
        "route_target_display": present_ticket_route_target(ticket),
        "creator": creator,
        "assignee": assignee,
        "activity_timeline": activity_timeline,
        "auto_scroll_message_id": _last_public_message_item_id(activity_timeline),
        "ops_users": _load_ops_users(db),
        "status_options": _OPS_FILTERABLE_STATUSES,
        "draft_reply_status_options": _OPS_DRAFT_REPLY_STATUSES,
        "public_reply_status_options": _OPS_PUBLIC_REPLY_STATUSES,
        "default_public_reply_status": _default_public_reply_status(ticket=ticket, current_user=current_user),
        "pending_draft": pending_draft,
        "pending_draft_html": render_markdown_to_html(pending_draft.body_markdown) if pending_draft else "",
        "latest_run": latest_run,
        "latest_analysis_run": latest_analysis_run,
        "latest_run_steps": latest_run_steps,
        "latest_analysis_steps": latest_analysis_steps,
        "latest_ai_note": latest_ai_note,
        "latest_ai_note_html": render_markdown_to_html(latest_ai_note.body_markdown) if latest_ai_note else "",
        "analysis_view": analysis_view,
        "ai_relevant_paths": analysis_view["relevant_paths"],
        "ai_summary_short": analysis_view["summary_short"],
        "ai_summary_internal": analysis_view["summary_internal"],
        "rerun_specialist_options": _ops_manual_rerun_specialist_options(),
    }


def _template_or_partial_response(
    request: Request,
    *,
    template_name: str,
    partial_name: str,
    context: dict[str, object],
):
    if is_htmx_request(request):
        return templates.TemplateResponse(request, partial_name, context)
    return templates.TemplateResponse(request, template_name, context)


@router.get("/ops/users", response_class=HTMLResponse)
def ops_manage_users(
    request: Request,
    current_user: User = Depends(require_ops_user),
    auth_session=Depends(get_required_auth_session),
    db: Session = Depends(db_session_dependency),
):
    open_create_form, editing_user_id = _read_users_ui_state(request)
    db.commit()
    return _render_ops_users_page(
        request,
        current_user=current_user,
        auth_session=auth_session,
        db=db,
        open_create_form=open_create_form,
        editing_user_id=editing_user_id,
    )


@router.get("/ops/integrations/slack", response_class=HTMLResponse)
def ops_slack_integration(
    request: Request,
    current_user: User = Depends(require_admin_user),
    auth_session=Depends(get_required_auth_session),
    db: Session = Depends(db_session_dependency),
    settings: Settings = Depends(get_settings),
):
    db.commit()
    return _render_slack_integration_page(
        request,
        current_user=current_user,
        auth_session=auth_session,
        db=db,
        settings=settings,
    )


@router.post("/ops/integrations/slack")
def ops_save_slack_integration(
    request: Request,
    current_user: User = Depends(require_admin_user),
    auth_session=Depends(get_required_auth_session),
    csrf_token: str = Form(...),
    enabled: str | None = Form(default=None),
    bot_token: str = Form(default=""),
    notify_ticket_created: str | None = Form(default=None),
    notify_public_message_added: str | None = Form(default=None),
    notify_status_changed: str | None = Form(default=None),
    message_preview_max_chars: int = Form(...),
    http_timeout_seconds: int = Form(...),
    delivery_batch_size: int = Form(...),
    delivery_max_attempts: int = Form(...),
    delivery_stale_lock_seconds: int = Form(...),
    db: Session = Depends(db_session_dependency),
    settings: Settings = Depends(get_settings),
):
    validate_csrf_token(auth_session, csrf_token)
    form_values = {
        "enabled": _parse_bool(enabled),
        "notify_ticket_created": _parse_bool(notify_ticket_created),
        "notify_public_message_added": _parse_bool(notify_public_message_added),
        "notify_status_changed": _parse_bool(notify_status_changed),
        "message_preview_max_chars": message_preview_max_chars,
        "http_timeout_seconds": http_timeout_seconds,
        "delivery_batch_size": delivery_batch_size,
        "delivery_max_attempts": delivery_max_attempts,
        "delivery_stale_lock_seconds": delivery_stale_lock_seconds,
    }
    current_settings = load_slack_dm_settings(db, app_settings=settings)
    should_request_user_sync = bool(current_settings.has_stored_token or bot_token.strip())
    try:
        validated = validate_slack_dm_settings_input(
            SlackDMSettingsInput(
                enabled=bool(form_values["enabled"]),
                notify_ticket_created=bool(form_values["notify_ticket_created"]),
                notify_public_message_added=bool(form_values["notify_public_message_added"]),
                notify_status_changed=bool(form_values["notify_status_changed"]),
                message_preview_max_chars=message_preview_max_chars,
                http_timeout_seconds=http_timeout_seconds,
                delivery_batch_size=delivery_batch_size,
                delivery_max_attempts=delivery_max_attempts,
                delivery_stale_lock_seconds=delivery_stale_lock_seconds,
                bot_token=bot_token,
            )
        )
        auth_result = None
        if validated.bot_token is not None:
            auth_result = _run_slack_auth_test(
                bot_token=validated.bot_token,
                timeout_seconds=validated.http_timeout_seconds,
            )
        elif validated.enabled and (
            not current_settings.has_stored_token
            or current_settings.config_error_code in {"slack_bot_token_missing", "slack_bot_token_undecryptable"}
        ):
            raise SlackDMSettingsError("Slack DM delivery cannot be enabled without a stored bot token")
        upsert_slack_dm_settings(
            db,
            app_settings=settings,
            values=validated,
            updated_by_user_id=current_user.id,
            auth_result=auth_result,
        )
        if should_request_user_sync:
            request_slack_user_sync(
                db,
                trigger="settings_saved",
                requested_by_user_id=current_user.id,
            )
    except SlackDMSettingsError as exc:
        db.rollback()
        return _render_slack_integration_page(
            request,
            current_user=current_user,
            auth_session=auth_session,
            db=db,
            settings=settings,
            status_code=status.HTTP_400_BAD_REQUEST,
            error=str(exc),
            form_values=form_values,
        )
    db.commit()
    return RedirectResponse(_slack_integration_page_path(), status_code=status.HTTP_303_SEE_OTHER)


@router.post("/ops/integrations/slack/disconnect")
def ops_disconnect_slack_integration(
    current_user: User = Depends(require_admin_user),
    auth_session=Depends(get_required_auth_session),
    csrf_token: str = Form(...),
    db: Session = Depends(db_session_dependency),
):
    validate_csrf_token(auth_session, csrf_token)
    clear_slack_dm_token(db, updated_by_user_id=current_user.id)
    db.commit()
    return RedirectResponse(_slack_integration_page_path(), status_code=status.HTTP_303_SEE_OTHER)


@router.post("/ops/users/create")
def ops_create_user(
    request: Request,
    current_user: User = Depends(require_ops_user),
    auth_session=Depends(get_required_auth_session),
    csrf_token: str = Form(...),
    email: str = Form(...),
    display_name: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
    slack_user_id: str | None = Form(default=None),
    db: Session = Depends(db_session_dependency),
):
    validate_csrf_token(auth_session, csrf_token)
    requested_role = role.strip()
    if requested_role not in _allowed_new_user_roles(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You cannot create that role")
    requested_slack_user_id = _resolve_requested_slack_user_id(actor=current_user, submitted_value=slack_user_id)
    try:
        create_user(
            db,
            email=email.strip(),
            display_name=display_name.strip(),
            password=password,
            role=requested_role,
            slack_user_id=requested_slack_user_id,
        )
        if _should_request_slack_user_sync(requested_slack_user_id):
            _request_slack_user_sync_if_session_configured(
                db,
                trigger="user_created",
                requested_by_user_id=current_user.id,
            )
    except ValueError as exc:
        db.rollback()
        return _render_ops_users_page(
            request,
            current_user=current_user,
            auth_session=auth_session,
            db=db,
            status_code=status.HTTP_400_BAD_REQUEST,
            error=str(exc),
            open_create_form=True,
        )
    db.commit()
    return RedirectResponse("/ops/users", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/ops/users/{user_id}/update")
def ops_update_user(
    user_id: str,
    request: Request,
    current_user: User = Depends(require_ops_user),
    auth_session=Depends(get_required_auth_session),
    csrf_token: str = Form(...),
    display_name: str = Form(...),
    password: str = Form(default=""),
    role: str = Form(...),
    slack_user_id: str | None = Form(default=None),
    db: Session = Depends(db_session_dependency),
):
    validate_csrf_token(auth_session, csrf_token)
    user = _load_user_or_404(db, user_id=user_id)
    if not _can_edit_user_profile(current_user, user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You cannot manage that user")
    requested_role = role.strip()
    if requested_role != user.role and not _can_change_user_role(current_user, user, requested_role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You cannot assign that role")
    requested_slack_user_id = _resolve_requested_slack_user_id(
        actor=current_user,
        submitted_value=slack_user_id,
        current_value=user.slack_user_id,
    )
    try:
        update_user(
            db,
            user=user,
            display_name=display_name,
            role=requested_role,
            slack_user_id=requested_slack_user_id,
            password=password,
        )
        if _should_request_slack_user_sync(requested_slack_user_id):
            _request_slack_user_sync_if_session_configured(
                db,
                trigger="user_updated",
                requested_by_user_id=current_user.id,
            )
    except ValueError as exc:
        db.rollback()
        return _render_ops_users_page(
            request,
            current_user=current_user,
            auth_session=auth_session,
            db=db,
            status_code=status.HTTP_400_BAD_REQUEST,
            error=str(exc),
            editing_user_id=str(user.id),
        )
    db.commit()
    return RedirectResponse("/ops/users", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/ops/users/{user_id}/set-active")
def ops_set_user_active(
    user_id: str,
    request: Request,
    current_user: User = Depends(require_ops_user),
    auth_session=Depends(get_required_auth_session),
    csrf_token: str = Form(...),
    is_active: str = Form(...),
    db: Session = Depends(db_session_dependency),
):
    validate_csrf_token(auth_session, csrf_token)
    user = _load_user_or_404(db, user_id=user_id)
    if not _can_toggle_user_active(current_user, user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You cannot manage that user")
    set_user_active_state(db, user=user, is_active=_parse_required_bool(is_active))
    db.commit()
    return RedirectResponse("/ops/users", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/ops", response_class=HTMLResponse)
def ops_ticket_list(
    request: Request,
    current_user: User = Depends(require_ops_user),
    auth_session=Depends(get_required_auth_session),
    db: Session = Depends(db_session_dependency),
):
    filters = _read_filters(request)
    db.commit()
    context = build_template_context(
        request=request,
        current_user=current_user,
        auth_session=auth_session,
        extra={
            **_ops_filter_context(db, current_user=current_user, filters=filters),
            "filters_action": "/ops",
            "filters_target_id": "ops-results",
        },
    )
    return _template_or_partial_response(
        request,
        template_name="ops_ticket_list.html",
        partial_name="ops_ticket_rows.html",
        context=context,
    )


@router.get("/ops/board", response_class=HTMLResponse)
def ops_board(
    request: Request,
    current_user: User = Depends(require_ops_user),
    auth_session=Depends(get_required_auth_session),
    db: Session = Depends(db_session_dependency),
):
    filters = _read_filters(request)
    db.commit()
    context = build_template_context(
        request=request,
        current_user=current_user,
        auth_session=auth_session,
        extra={
            **_ops_filter_context(db, current_user=current_user, filters=filters),
            "filters_action": "/ops/board",
            "filters_target_id": "ops-results",
        },
    )
    return _template_or_partial_response(
        request,
        template_name="ops_board.html",
        partial_name="ops_board_columns.html",
        context=context,
    )


@router.get("/ops/tickets/{reference}", response_class=HTMLResponse)
def ops_ticket_detail(
    reference: str,
    request: Request,
    current_user: User = Depends(require_ops_user),
    auth_session=Depends(get_required_auth_session),
    db: Session = Depends(db_session_dependency),
):
    ui_locale = resolve_ui_locale(request)
    ticket = _load_ops_ticket_or_404(db, reference=reference)
    upsert_ticket_view(db, user_id=current_user.id, ticket_id=ticket.id)
    db.commit()
    return templates.TemplateResponse(
        request,
        "ops_ticket_detail.html",
        build_template_context(
            request=request,
            current_user=current_user,
            auth_session=auth_session,
            extra=_ticket_detail_context(db, ticket=ticket, current_user=current_user, ui_locale=ui_locale),
            ui_locale=ui_locale,
        ),
    )


@router.post("/ops/tickets/{reference}/assign")
def ops_assign_ticket(
    reference: str,
    current_user: User = Depends(require_ops_user),
    auth_session=Depends(get_required_auth_session),
    csrf_token: str = Form(...),
    assigned_to_user_id: str = Form(default=""),
    db: Session = Depends(db_session_dependency),
):
    validate_csrf_token(auth_session, csrf_token)
    ticket = _load_ops_ticket_or_404(db, reference=reference)
    assignee = None
    assignee_value = assigned_to_user_id.strip()
    if assignee_value:
        try:
            assignee_uuid = uuid.UUID(assignee_value)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid assignee") from exc
        assignee = db.get(User, assignee_uuid)
        if assignee is None or not assignee.is_active or not is_ops_user(assignee):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid assignee")
    assign_ticket_for_ops(db, ticket=ticket, actor=current_user, assignee=assignee)
    db.commit()
    return RedirectResponse(f"/ops/tickets/{ticket.reference}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/ops/tickets/{reference}/set-status")
def ops_set_ticket_status(
    reference: str,
    current_user: User = Depends(require_ops_user),
    auth_session=Depends(get_required_auth_session),
    settings: Settings = Depends(get_settings),
    csrf_token: str = Form(...),
    next_status: str = Form(...),
    db: Session = Depends(db_session_dependency),
):
    validate_csrf_token(auth_session, csrf_token)
    ticket = _load_ops_ticket_or_404(db, reference=reference)
    try:
        set_ticket_status_for_ops(
            db,
            slack_runtime=build_slack_runtime_context(settings, db=db),
            ticket=ticket,
            actor=current_user,
            next_status=next_status.strip(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    db.commit()
    return RedirectResponse(f"/ops/tickets/{ticket.reference}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/ops/tickets/{reference}/reply-public")
def ops_reply_public(
    reference: str,
    current_user: User = Depends(require_ops_user),
    auth_session=Depends(get_required_auth_session),
    settings: Settings = Depends(get_settings),
    csrf_token: str = Form(...),
    body: str = Form(...),
    next_status: str = Form(...),
    forced_route_target_id: str = Form(default=""),
    db: Session = Depends(db_session_dependency),
):
    validate_csrf_token(auth_session, csrf_token)
    ticket = _load_ops_ticket_or_404(db, reference=reference)
    if not body.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Reply text is required")
    route_target_value, forced_specialist_id = _resolve_manual_rerun_specialist_override(forced_route_target_id)
    try:
        add_ops_public_reply(
            db,
            slack_runtime=build_slack_runtime_context(settings, db=db),
            ticket=ticket,
            actor=current_user,
            body_markdown=body.strip(),
            next_status=next_status.strip(),
            forced_route_target_id=route_target_value,
            forced_specialist_id=forced_specialist_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    db.commit()
    return RedirectResponse(f"/ops/tickets/{ticket.reference}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/ops/tickets/{reference}/note-internal")
def ops_note_internal(
    reference: str,
    current_user: User = Depends(require_ops_user),
    auth_session=Depends(get_required_auth_session),
    csrf_token: str = Form(...),
    body: str = Form(...),
    db: Session = Depends(db_session_dependency),
):
    validate_csrf_token(auth_session, csrf_token)
    ticket = _load_ops_ticket_or_404(db, reference=reference)
    if not body.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Internal note text is required")
    add_ops_internal_note(db, ticket=ticket, actor=current_user, body_markdown=body.strip())
    db.commit()
    return RedirectResponse(f"/ops/tickets/{ticket.reference}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/ops/tickets/{reference}/rerun-ai")
def ops_rerun_ai(
    reference: str,
    current_user: User = Depends(require_ops_user),
    auth_session=Depends(get_required_auth_session),
    settings: Settings = Depends(get_settings),
    csrf_token: str = Form(...),
    forced_route_target_id: str = Form(default=""),
    db: Session = Depends(db_session_dependency),
):
    validate_csrf_token(auth_session, csrf_token)
    ticket = _load_ops_ticket_or_404(db, reference=reference)
    route_target_value, forced_specialist_id = _resolve_manual_rerun_specialist_override(forced_route_target_id)
    request_manual_rerun(
        db,
        slack_runtime=build_slack_runtime_context(settings, db=db),
        ticket=ticket,
        actor=current_user,
        forced_route_target_id=route_target_value,
        forced_specialist_id=forced_specialist_id,
    )
    db.commit()
    return RedirectResponse(f"/ops/tickets/{ticket.reference}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/ops/drafts/{draft_id}/approve-publish")
def ops_approve_publish_draft(
    draft_id: str,
    current_user: User = Depends(require_ops_user),
    auth_session=Depends(get_required_auth_session),
    settings: Settings = Depends(get_settings),
    csrf_token: str = Form(...),
    next_status: str = Form(...),
    db: Session = Depends(db_session_dependency),
):
    validate_csrf_token(auth_session, csrf_token)
    draft = _load_ops_draft_or_404(db, draft_id=draft_id)
    ticket = db.get(Ticket, draft.ticket_id)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    try:
        publish_ai_draft_for_ops(
            db,
            slack_runtime=build_slack_runtime_context(settings, db=db),
            ticket=ticket,
            draft=draft,
            actor=current_user,
            next_status=next_status.strip(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    db.commit()
    return RedirectResponse(f"/ops/tickets/{ticket.reference}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/ops/drafts/{draft_id}/reject")
def ops_reject_draft(
    draft_id: str,
    current_user: User = Depends(require_ops_user),
    auth_session=Depends(get_required_auth_session),
    csrf_token: str = Form(...),
    db: Session = Depends(db_session_dependency),
):
    validate_csrf_token(auth_session, csrf_token)
    draft = _load_ops_draft_or_404(db, draft_id=draft_id)
    ticket = db.get(Ticket, draft.ticket_id)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    try:
        reject_ai_draft_for_ops(db, ticket=ticket, draft=draft, actor=current_user)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    db.commit()
    return RedirectResponse(f"/ops/tickets/{ticket.reference}", status_code=status.HTTP_303_SEE_OTHER)
