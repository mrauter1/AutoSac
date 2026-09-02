from __future__ import annotations

from pathlib import Path
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.formparsers import MultiPartException

from app.i18n import get_translator, resolve_ui_locale
from app.auth import get_current_user, get_required_auth_session, require_requester_user, validate_csrf_token
from app.requester_view import build_requester_ticket_detail_context
from app.ticket_live import if_none_match_matches, load_ticket_live_state, ticket_live_representation_etag
from app.ui import build_template_context, is_htmx_request, templates
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
from shared.models import SessionRecord, Ticket, TicketAttachment, TicketView, User
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


def _load_requester_ticket_or_404(db: Session, *, reference: str, requester_id) -> Ticket:
    ticket = db.execute(
        select(Ticket).where(Ticket.reference == reference, Ticket.created_by_user_id == requester_id)
    ).scalar_one_or_none()
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    return ticket


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
    slack_runtime = build_slack_runtime_context(settings, db=db)
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
    settings: Settings = Depends(get_settings),
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
    detail_context = build_requester_ticket_detail_context(
        db,
        ticket=ticket,
        ui_locale=ui_locale,
        stale_timeout_seconds=settings.ai_run_stale_timeout_seconds,
    )
    db.commit()
    context = build_template_context(
        request=request,
        current_user=current_user,
        auth_session=auth_session,
        extra=detail_context,
        ui_locale=ui_locale,
    )
    if is_htmx_request(request) and request.headers.get("X-AutoSac-Live-Refresh", "").lower() == "true":
        response = templates.TemplateResponse(request, "requester_ticket_live_fragments.html", context)
        response.headers["X-Ticket-Content-Version"] = detail_context["live_state"].content_version
        return response
    return templates.TemplateResponse(
        request,
        "requester_ticket_detail.html",
        context,
    )


@router.get("/app/tickets/{reference}/live-state")
def requester_ticket_live_state(
    reference: str,
    request: Request,
    current_user: User = Depends(require_requester_user),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(db_session_dependency),
):
    if can_access_all_tickets(current_user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    ticket = _load_requester_ticket_or_404(db, reference=reference, requester_id=current_user.id)
    live_state = load_ticket_live_state(
        db,
        ticket=ticket,
        audience="requester",
        stale_timeout_seconds=settings.ai_run_stale_timeout_seconds,
    )
    ui_locale = resolve_ui_locale(request)
    etag = ticket_live_representation_etag(version=live_state.version, ui_locale=ui_locale)
    headers = {
        "ETag": etag,
        "Cache-Control": "private, no-cache",
        "Vary": "Cookie, Accept-Language",
    }
    if if_none_match_matches(request.headers.get("If-None-Match"), etag=etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    translator = get_translator(ui_locale)
    return JSONResponse(
        {
            "active": live_state.active,
            "phase": live_state.phase,
            "label": translator(f"ticket.live.requester.{live_state.phase}"),
            "version": live_state.version,
            "content_version": live_state.content_version,
            "started_at": live_state.started_at.isoformat() if live_state.started_at is not None else None,
            "delayed": live_state.delayed,
            "run_key": getattr(live_state, "run_key", None),
        },
        headers=headers,
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
        detail_context = build_requester_ticket_detail_context(
            db,
            ticket=ticket,
            ui_locale=ui_locale,
            stale_timeout_seconds=settings.ai_run_stale_timeout_seconds,
            reply_body=body,
        )
        return templates.TemplateResponse(
            request,
            "requester_ticket_detail.html",
            build_template_context(
                request=request,
                current_user=current_user,
                auth_session=auth_session,
                extra={
                    **detail_context,
                    "error": str(exc),
                },
                ui_locale=ui_locale,
                ui_switch_path=f"/app/tickets/{ticket.reference}",
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    saved_paths: list[Path] = []
    slack_runtime = build_slack_runtime_context(settings, db=db)
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
        slack_runtime=build_slack_runtime_context(settings, db=db),
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
