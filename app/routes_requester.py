from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.formparsers import MultiPartException

from app.i18n import (
    DEFAULT_UI_LOCALE,
    requester_author_label,
    requester_role_suffix_label,
    requester_status_change_summary,
    requester_status_label,
    resolve_ui_locale,
    timeline_lane_label,
)
from app.auth import get_current_user, get_required_auth_session, require_requester_user, validate_csrf_token
from app.render import render_markdown_to_html
from app.timeline import build_author_label, load_ticket_status_history, load_users_by_ids, merge_timeline_items, serialize_status_changes
from app.ui import build_template_context, templates
from app.uploads import (
    UploadValidationError,
    get_form_attachments,
    parse_multipart_form,
    persist_validated_attachment,
    validate_attachment_upload,
)
from shared.config import Settings, get_settings
from shared.db import db_session_dependency
from shared.integrations import build_slack_runtime_context
from shared.models import SessionRecord, Ticket, TicketAttachment, TicketMessage, TicketView, User
from shared.permissions import can_access_all_tickets
from shared.ticketing import (
    add_requester_reply,
    create_requester_ticket,
    resolve_ticket_for_requester,
    upsert_ticket_view,
)

router = APIRouter()


def _ticket_detail_path(*, current_user: User, reference: str) -> str:
    if can_access_all_tickets(current_user):
        return f"/ops/tickets/{reference}"
    return f"/app/tickets/{reference}"


def _last_public_message_item_id(timeline: list[dict[str, object]]) -> str | None:
    for item in reversed(timeline):
        if item.get("kind") == "message":
            item_id = item.get("id")
            if isinstance(item_id, str) and item_id:
                return item_id
    return None


def _load_requester_ticket_or_404(db: Session, *, reference: str, requester_id) -> Ticket:
    ticket = db.execute(
        select(Ticket).where(Ticket.reference == reference, Ticket.created_by_user_id == requester_id)
    ).scalar_one_or_none()
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    return ticket


def _load_public_ticket_messages(db: Session, *, ticket_id) -> list[TicketMessage]:
    return list(
        db.execute(
            select(TicketMessage)
            .where(TicketMessage.ticket_id == ticket_id, TicketMessage.visibility == "public")
            .order_by(TicketMessage.created_at.asc(), TicketMessage.id.asc())
        ).scalars()
    )


def _load_attachments_by_message(db: Session, *, ticket_id, visibility: str = "public") -> dict[Any, list[TicketAttachment]]:
    attachments = list(
        db.execute(
            select(TicketAttachment)
            .where(TicketAttachment.ticket_id == ticket_id, TicketAttachment.visibility == visibility)
            .order_by(TicketAttachment.created_at.asc())
        ).scalars()
    )
    grouped: dict[Any, list[TicketAttachment]] = defaultdict(list)
    for attachment in attachments:
        grouped[attachment.message_id].append(attachment)
    return grouped


def _serialize_public_thread(db: Session, *, ticket_id, ui_locale: str = DEFAULT_UI_LOCALE) -> list[dict[str, object]]:
    attachments_by_message = _load_attachments_by_message(db, ticket_id=ticket_id)
    messages = _load_public_ticket_messages(db, ticket_id=ticket_id)
    users_by_id = load_users_by_ids(db, (message.author_user_id for message in messages))
    thread: list[dict[str, object]] = []
    for message in messages:
        thread.append(
            {
                "kind": "message",
                "id": str(message.id),
                "created_at": message.created_at,
                "lane": "public",
                "lane_label": timeline_lane_label("public", ui_locale),
                "author_type": message.author_type,
                "author_label": build_author_label(
                    author_type=message.author_type,
                    display_name=users_by_id.get(message.author_user_id).display_name if message.author_user_id in users_by_id else None,
                    fallback_label=lambda author_type: requester_author_label(author_type, ui_locale),
                    role_suffix_label=lambda author_type: requester_role_suffix_label(author_type, ui_locale),
                ),
                "source": message.source,
                "body_markdown": message.body_markdown,
                "body_html": render_markdown_to_html(message.body_markdown),
                "attachments": attachments_by_message.get(message.id, []),
            }
        )
    return thread


def _build_requester_timeline(db: Session, *, ticket_id, ui_locale: str = DEFAULT_UI_LOCALE) -> list[dict[str, object]]:
    history_entries = load_ticket_status_history(db, ticket_id=ticket_id)
    users_by_id = load_users_by_ids(db, (getattr(entry, "changed_by_user_id", None) for entry in history_entries))
    return merge_timeline_items(
        _serialize_public_thread(db, ticket_id=ticket_id, ui_locale=ui_locale),
        serialize_status_changes(
            history_entries,
            status_label=lambda status: requester_status_label(status, ui_locale),
            actor_label=lambda author_type: requester_author_label(author_type, ui_locale),
            actor_role_suffix_label=lambda author_type: requester_role_suffix_label(author_type, ui_locale),
            status_summary=lambda from_status_label, to_status_label: requester_status_change_summary(to_status_label, ui_locale),
            lane_label=timeline_lane_label("status", ui_locale),
            user_display_names={user_id: user.display_name for user_id, user in users_by_id.items()},
        ),
    )


def _parse_bool(value: str | None) -> bool:
    return value in {"on", "true", "1", "yes"}


async def _parse_requester_message_form(
    request: Request,
    *,
    settings: Settings,
) -> tuple[str, str, list]:
    try:
        form = await parse_multipart_form(request, settings)
    except MultiPartException as exc:
        raise UploadValidationError(str(exc)) from exc
    body = str(form.get("body", "")).strip()
    csrf_token = str(form.get("csrf_token", "")).strip()
    uploads = get_form_attachments(form)
    attachments = [await validate_attachment_upload(upload, settings) for upload in uploads]
    return body, csrf_token, attachments


async def _parse_ticket_create_form(
    request: Request,
    *,
    settings: Settings,
) -> tuple[str, str, bool, str, list]:
    try:
        form = await parse_multipart_form(request, settings)
    except MultiPartException as exc:
        raise UploadValidationError(str(exc)) from exc
    title = str(form.get("title", "")).strip()
    description = str(form.get("description", "")).strip()
    urgent = _parse_bool(form.get("urgent"))
    csrf_token = str(form.get("csrf_token", "")).strip()
    uploads = get_form_attachments(form)
    attachments = [await validate_attachment_upload(upload, settings) for upload in uploads]
    return title, description, urgent, csrf_token, attachments


def _cleanup_paths(paths: list[Path]) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue


def _ticket_list_rows(db: Session, *, requester_id) -> list[dict[str, object]]:
    tickets = list(
        db.execute(
            select(Ticket)
            .where(Ticket.created_by_user_id == requester_id)
            .order_by(Ticket.updated_at.desc())
        ).scalars()
    )
    views = {
        view.ticket_id: view.last_viewed_at
        for view in db.execute(select(TicketView).where(TicketView.user_id == requester_id)).scalars()
    }
    rows: list[dict[str, object]] = []
    for ticket in tickets:
        last_viewed_at = views.get(ticket.id)
        rows.append(
            {
                "ticket": ticket,
                "updated_for_user": last_viewed_at is None or ticket.updated_at > last_viewed_at,
            }
        )
    return rows


@router.get("/app", response_class=HTMLResponse)
@router.get("/app/tickets", response_class=HTMLResponse)
def requester_ticket_list(
    request: Request,
    current_user: User = Depends(require_requester_user),
    auth_session: SessionRecord = Depends(get_required_auth_session),
    db: Session = Depends(db_session_dependency),
):
    db.commit()
    return templates.TemplateResponse(
        request,
        "requester_ticket_list.html",
        build_template_context(
            request=request,
            current_user=current_user,
            auth_session=auth_session,
            extra={"tickets": _ticket_list_rows(db, requester_id=current_user.id)},
        ),
    )


@router.get("/app/tickets/new", response_class=HTMLResponse)
def requester_ticket_new_page(
    request: Request,
    current_user: User = Depends(require_requester_user),
    auth_session: SessionRecord = Depends(get_required_auth_session),
    db: Session = Depends(db_session_dependency),
):
    db.commit()
    return templates.TemplateResponse(
        request,
        "requester_ticket_new.html",
        build_template_context(request=request, current_user=current_user, auth_session=auth_session),
    )


@router.post("/app/tickets")
async def requester_ticket_create(
    request: Request,
    current_user: User = Depends(require_requester_user),
    auth_session: SessionRecord = Depends(get_required_auth_session),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(db_session_dependency),
):
    title = ""
    description = ""
    urgent = False
    try:
        title, description, urgent, csrf_token, upload_attachments = await _parse_ticket_create_form(
            request,
            settings=settings,
        )
        validate_csrf_token(auth_session, csrf_token)
        if not description:
            raise UploadValidationError("Description is required.")
        if len(upload_attachments) > settings.max_images_per_message:
            raise UploadValidationError(f"Attach at most {settings.max_images_per_message} files.")
    except UploadValidationError as exc:
        return templates.TemplateResponse(
            request,
            "requester_ticket_new.html",
            build_template_context(
                request=request,
                current_user=current_user,
                auth_session=auth_session,
                extra={
                    "error": str(exc),
                    "form_title": title,
                    "form_description": description,
                    "form_urgent": urgent,
                },
                ui_switch_path="/app/tickets/new",
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    saved_paths: list[Path] = []
    slack_runtime = build_slack_runtime_context(settings)
    try:
        ticket, _, persisted_attachments, _ = create_requester_ticket(
            db,
            settings=settings,
            slack_runtime=slack_runtime,
            requester=current_user,
            title=title,
            description_markdown=description,
            urgent=urgent,
            attachments=upload_attachments,
        )
        for attachment, upload in zip(persisted_attachments, upload_attachments):
            path = Path(attachment.stored_path)
            persist_validated_attachment(path, upload)
            saved_paths.append(path)
        db.commit()
    except Exception:
        db.rollback()
        _cleanup_paths(saved_paths)
        raise
    return RedirectResponse(
        _ticket_detail_path(current_user=current_user, reference=ticket.reference),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/app/tickets/{reference}", response_class=HTMLResponse)
def requester_ticket_detail(
    reference: str,
    request: Request,
    current_user: User = Depends(require_requester_user),
    auth_session: SessionRecord = Depends(get_required_auth_session),
    db: Session = Depends(db_session_dependency),
):
    ui_locale = resolve_ui_locale(request)
    ticket = _load_requester_ticket_or_404(db, reference=reference, requester_id=current_user.id)
    if can_access_all_tickets(current_user):
        return RedirectResponse(
            _ticket_detail_path(current_user=current_user, reference=ticket.reference),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    upsert_ticket_view(db, user_id=current_user.id, ticket_id=ticket.id)
    timeline = _build_requester_timeline(db, ticket_id=ticket.id, ui_locale=ui_locale)
    db.commit()
    return templates.TemplateResponse(
        request,
        "requester_ticket_detail.html",
        build_template_context(
            request=request,
            current_user=current_user,
            auth_session=auth_session,
            extra={
                "ticket": ticket,
                "timeline": timeline,
                "auto_scroll_message_id": _last_public_message_item_id(timeline),
            },
            ui_locale=ui_locale,
        ),
    )


@router.post("/app/tickets/{reference}/reply")
async def requester_ticket_reply(
    reference: str,
    request: Request,
    current_user: User = Depends(require_requester_user),
    auth_session: SessionRecord = Depends(get_required_auth_session),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(db_session_dependency),
):
    ui_locale = resolve_ui_locale(request)
    ticket = _load_requester_ticket_or_404(db, reference=reference, requester_id=current_user.id)
    body = ""
    try:
        body, csrf_token, upload_attachments = await _parse_requester_message_form(request, settings=settings)
        validate_csrf_token(auth_session, csrf_token)
        if not body:
            raise UploadValidationError("Reply text is required.")
        if len(upload_attachments) > settings.max_images_per_message:
            raise UploadValidationError(f"Attach at most {settings.max_images_per_message} files.")
    except UploadValidationError as exc:
        return templates.TemplateResponse(
            request,
            "requester_ticket_detail.html",
            build_template_context(
                request=request,
                current_user=current_user,
                auth_session=auth_session,
                extra={
                    "ticket": ticket,
                    "timeline": _build_requester_timeline(db, ticket_id=ticket.id, ui_locale=ui_locale),
                    "error": str(exc),
                    "reply_body": body,
                },
                ui_locale=ui_locale,
                ui_switch_path=f"/app/tickets/{ticket.reference}",
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    saved_paths: list[Path] = []
    slack_runtime = build_slack_runtime_context(settings)
    try:
        _, persisted_attachments, _ = add_requester_reply(
            db,
            settings=settings,
            slack_runtime=slack_runtime,
            ticket=ticket,
            requester=current_user,
            body_markdown=body,
            attachments=upload_attachments,
        )
        for attachment, upload in zip(persisted_attachments, upload_attachments):
            path = Path(attachment.stored_path)
            persist_validated_attachment(path, upload)
            saved_paths.append(path)
        db.commit()
    except Exception:
        db.rollback()
        _cleanup_paths(saved_paths)
        raise
    return RedirectResponse(
        _ticket_detail_path(current_user=current_user, reference=ticket.reference),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/app/tickets/{reference}/resolve")
def requester_ticket_resolve(
    reference: str,
    current_user: User = Depends(require_requester_user),
    auth_session: SessionRecord = Depends(get_required_auth_session),
    settings: Settings = Depends(get_settings),
    csrf_token: str = Form(...),
    db: Session = Depends(db_session_dependency),
):
    validate_csrf_token(auth_session, csrf_token)
    ticket = _load_requester_ticket_or_404(db, reference=reference, requester_id=current_user.id)
    resolve_ticket_for_requester(
        db,
        slack_runtime=build_slack_runtime_context(settings),
        ticket=ticket,
        requester=current_user,
    )
    db.commit()
    return RedirectResponse(
        _ticket_detail_path(current_user=current_user, reference=ticket.reference),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/attachments/{attachment_id}")
def attachment_download(
    attachment_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(db_session_dependency),
):
    try:
        attachment_uuid = uuid.UUID(attachment_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found") from exc
    attachment = db.get(TicketAttachment, attachment_uuid)
    if attachment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found")
    ticket = db.get(Ticket, attachment.ticket_id)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment ticket not found")
    if can_access_all_tickets(current_user):
        allowed = True
    else:
        allowed = attachment.visibility == "public" and ticket.created_by_user_id == current_user.id
    if not allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Attachment access denied")
    db.commit()
    return FileResponse(path=attachment.stored_path, media_type=attachment.mime_type, filename=attachment.original_filename)
