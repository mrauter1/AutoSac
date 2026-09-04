from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import case, or_, select
from sqlalchemy.orm import Session
from starlette.formparsers import MultiPartException

from app.i18n import get_translator, resolve_ui_locale
from app.auth import (
    get_current_user,
    get_required_auth_session,
    require_ops_user,
    require_requester_user,
    validate_csrf_token,
)
from app.requester_view import build_requester_ticket_detail_context
from app.ticket_index import (
    COMMON_TICKET_SORTS,
    DEFAULT_TICKET_SORT,
    common_ticket_order_clauses,
    escaped_ilike_pattern,
    normalize_ticket_sort,
)
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

_REQUESTER_FILTER_QUERY_KEYS = frozenset({"q", "state", "sort", "updated_since_viewed"})
_REQUESTER_STATES = frozenset({"open", "waiting_on_user", "resolved"})
_REQUESTER_SORTS = COMMON_TICKET_SORTS | {"needs_reply_first"}
_REQUESTER_LIST_PATH = "/app/tickets"


class _RequesterMessageFormError(UploadValidationError):
    def __init__(self, message: str, *, body: str, return_to: str):
        super().__init__(message)
        self.body = body
        self.return_to = return_to


def _ticket_detail_path(*, current_user: User, reference: str) -> str:
    if can_access_all_tickets(current_user):
        return f"/ops/tickets/{reference}"
    return f"/app/tickets/{reference}"


def _ticket_creation_paths(*, current_user: User) -> dict[str, str]:
    if can_access_all_tickets(current_user):
        return {
            "new_ticket_path": "/ops/tickets/new",
            "ticket_create_action": "/ops/tickets",
            "ticket_list_path": "/ops/board",
        }
    return {
        "new_ticket_path": "/app/tickets/new",
        "ticket_create_action": "/app/tickets",
        "ticket_list_path": "/app/tickets",
    }


def _load_requester_ticket_or_404(db: Session, *, reference: str, requester_id) -> Ticket:
    ticket = db.execute(
        select(Ticket).where(Ticket.reference == reference, Ticket.created_by_user_id == requester_id)
    ).scalar_one_or_none()
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    return ticket


def _parse_bool(value: str | None) -> bool:
    return value in {"on", "true", "1", "yes"}


def _read_requester_filters(request: Request) -> dict[str, object]:
    query = request.query_params
    state = query.get("state", "").strip()
    return {
        "q": query.get("q", "").strip()[:120],
        "state": state if state in _REQUESTER_STATES else "",
        "sort": normalize_ticket_sort(query.get("sort"), allowed=_REQUESTER_SORTS),
        "updated_since_viewed": _parse_bool(query.get("updated_since_viewed")),
    }


def _requester_query_items(filters: dict[str, object]) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    query = str(filters.get("q", "")).strip()
    state = str(filters.get("state", "")).strip()
    sort_key = normalize_ticket_sort(str(filters.get("sort", "")), allowed=_REQUESTER_SORTS)
    if query:
        items.append(("q", query))
    if state:
        items.append(("state", state))
    if filters.get("updated_since_viewed"):
        items.append(("updated_since_viewed", "on"))
    if sort_key != DEFAULT_TICKET_SORT:
        items.append(("sort", sort_key))
    return items


def _requester_active_filter_items(filters: dict[str, object]) -> list[tuple[str, str]]:
    return [(key, value) for key, value in _requester_query_items(filters) if key != "sort"]


def _requester_list_url(filters: dict[str, object]) -> str:
    query = urlencode(_requester_query_items(filters))
    return f"{_REQUESTER_LIST_PATH}?{query}" if query else _REQUESTER_LIST_PATH


def _requester_filter_chips(filters: dict[str, object]) -> list[dict[str, str]]:
    chips: list[dict[str, str]] = []
    for key, value in _requester_active_filter_items(filters):
        remaining = dict(filters)
        remaining[key] = False if isinstance(filters.get(key), bool) else ""
        chips.append(
            {
                "key": key,
                "value": value,
                "remaining_url": _requester_list_url(remaining),
            }
        )
    return chips


def _sanitize_requester_return_to(value: str | None) -> str:
    candidate = (value or "").strip()
    if not candidate:
        return _REQUESTER_LIST_PATH
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc or parsed.path != _REQUESTER_LIST_PATH or parsed.fragment:
        return _REQUESTER_LIST_PATH
    if len(parsed.query) > 1024:
        return _REQUESTER_LIST_PATH
    items = parse_qsl(parsed.query, keep_blank_values=True)
    keys = [key for key, _ in items]
    if len(keys) != len(set(keys)) or any(key not in _REQUESTER_FILTER_QUERY_KEYS for key in keys):
        return _REQUESTER_LIST_PATH
    values = dict(items)
    query_value = values.get("q", "")
    state = values.get("state", "")
    sort_key = values.get("sort", DEFAULT_TICKET_SORT)
    updated = values.get("updated_since_viewed")
    if len(query_value) > 120:
        return _REQUESTER_LIST_PATH
    if state and state not in _REQUESTER_STATES:
        return _REQUESTER_LIST_PATH
    if sort_key not in _REQUESTER_SORTS:
        return _REQUESTER_LIST_PATH
    if updated is not None and updated not in {"on", "true", "1", "yes"}:
        return _REQUESTER_LIST_PATH
    return _requester_list_url(
        {
            "q": query_value.strip(),
            "state": state,
            "sort": sort_key,
            "updated_since_viewed": updated is not None,
        }
    )


def _requester_ticket_detail_path(reference: str, return_to: str | None = None) -> str:
    path = f"/app/tickets/{reference}"
    if not return_to:
        return path
    return f"{path}?{urlencode({'return_to': _sanitize_requester_return_to(return_to)})}"


async def _parse_requester_message_form(
    request: Request,
    *,
    settings: Settings,
) -> tuple[str, str, str, list]:
    try:
        form = await parse_multipart_form(request, settings)
    except MultiPartException as exc:
        raise UploadValidationError(str(exc)) from exc
    body = str(form.get("body", "")).strip()
    csrf_token = str(form.get("csrf_token", "")).strip()
    return_to = str(form.get("return_to", "")).strip()
    uploads = get_form_attachments(form)
    try:
        attachments = [await validate_attachment_upload(upload, settings) for upload in uploads]
    except UploadValidationError as exc:
        raise _RequesterMessageFormError(str(exc), body=body, return_to=return_to) from exc
    return body, csrf_token, return_to, attachments


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


def _load_requester_ticket_rows(
    db: Session,
    *,
    requester_id,
    filters: dict[str, object],
) -> list[dict[str, object]]:
    statement = select(Ticket).where(Ticket.created_by_user_id == requester_id)
    search_filter = str(filters["q"])
    state_filter = str(filters["state"])
    sort_key = str(filters["sort"])
    if search_filter:
        pattern = escaped_ilike_pattern(search_filter)
        statement = statement.where(
            or_(
                Ticket.reference.ilike(pattern, escape="\\"),
                Ticket.title.ilike(pattern, escape="\\"),
            )
        )
    if state_filter == "open":
        statement = statement.where(Ticket.status != "resolved")
    elif state_filter:
        statement = statement.where(Ticket.status == state_filter)
    if sort_key == "needs_reply_first":
        statement = statement.order_by(
            case((Ticket.status == "waiting_on_user", 0), else_=1),
            Ticket.updated_at.desc(),
            Ticket.reference_num.desc(),
        )
    else:
        statement = statement.order_by(*common_ticket_order_clauses(sort_key))
    tickets = list(db.execute(statement).scalars())
    if not tickets:
        return []
    ticket_ids = [ticket.id for ticket in tickets]
    views = {
        view.ticket_id: view.last_viewed_at
        for view in db.execute(
            select(TicketView).where(TicketView.user_id == requester_id, TicketView.ticket_id.in_(ticket_ids))
        ).scalars()
    }
    rows: list[dict[str, object]] = []
    for ticket in tickets:
        last_viewed_at = views.get(ticket.id)
        updated_for_user = last_viewed_at is None or ticket.updated_at > last_viewed_at
        if filters["updated_since_viewed"] and not updated_for_user:
            continue
        rows.append(
            {
                "ticket": ticket,
                "updated_for_user": updated_for_user,
            }
        )
    return rows


def _requester_list_context(db: Session, *, requester_id, filters: dict[str, object]) -> dict[str, object]:
    rows = _load_requester_ticket_rows(db, requester_id=requester_id, filters=filters)
    query_items = _requester_query_items(filters)
    return {
        "tickets": rows,
        "result_count": len(rows),
        "filters": filters,
        "state_options": ("open", "waiting_on_user", "resolved"),
        "sort_options": (
            "updated_desc",
            "updated_asc",
            "created_desc",
            "created_asc",
            "needs_reply_first",
        ),
        "filter_chips": _requester_filter_chips(filters),
        "active_filter_count": len(_requester_active_filter_items(filters)),
        "has_query_state": bool(query_items),
        "requester_list_url": _requester_list_url(filters),
    }


@router.get("/app", response_class=HTMLResponse)
@router.get("/app/tickets", response_class=HTMLResponse)
def requester_ticket_list(
    request: Request,
    current_user: User = Depends(require_requester_user),
    auth_session: SessionRecord = Depends(get_required_auth_session),
    db: Session = Depends(db_session_dependency),
):
    filters = _read_requester_filters(request)
    canonical_url = _requester_list_url(filters)
    canonical_query = urlsplit(canonical_url).query
    if not is_htmx_request(request) and request.url.query != canonical_query:
        return RedirectResponse(canonical_url, status_code=status.HTTP_303_SEE_OTHER)
    db.commit()
    creation_paths = _ticket_creation_paths(current_user=current_user)
    context = build_template_context(
        request=request,
        current_user=current_user,
        auth_session=auth_session,
        extra={
            **_requester_list_context(db, requester_id=current_user.id, filters=filters),
            "new_ticket_path": creation_paths["new_ticket_path"],
        },
    )
    htmx_request = is_htmx_request(request)
    template_name = "requester_ticket_list_results.html" if htmx_request else "requester_ticket_list.html"
    response = templates.TemplateResponse(request, template_name, context)
    if htmx_request:
        response.headers["HX-Push-Url"] = canonical_url
    return response


@router.get(
    "/ops/tickets/new",
    response_class=HTMLResponse,
    name="ops_ticket_new_page",
    dependencies=[Depends(require_ops_user)],
)
@router.get("/app/tickets/new", response_class=HTMLResponse, name="requester_ticket_new_page")
def requester_ticket_new_page(
    request: Request,
    current_user: User = Depends(require_requester_user),
    auth_session: SessionRecord = Depends(get_required_auth_session),
    db: Session = Depends(db_session_dependency),
):
    db.commit()
    creation_paths = _ticket_creation_paths(current_user=current_user)
    return templates.TemplateResponse(
        request,
        "requester_ticket_new.html",
        build_template_context(
            request=request,
            current_user=current_user,
            auth_session=auth_session,
            extra=creation_paths,
            ui_switch_path=creation_paths["new_ticket_path"],
        ),
    )


@router.post("/ops/tickets", name="ops_ticket_create", dependencies=[Depends(require_ops_user)])
@router.post("/app/tickets", name="requester_ticket_create")
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
    creation_paths = _ticket_creation_paths(current_user=current_user)
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
                    **creation_paths,
                    "error": str(exc),
                    "form_title": title,
                    "form_description": description,
                    "form_urgent": urgent,
                },
                ui_switch_path=creation_paths["new_ticket_path"],
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
    requester_return_url = _sanitize_requester_return_to(request.query_params.get("return_to"))
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
    detail_context.update(
        {
            "requester_return_url": requester_return_url,
            "requester_return_to": requester_return_url,
            "live_detail_url": _requester_ticket_detail_path(ticket.reference, requester_return_url),
        }
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
    requester_return_url = _REQUESTER_LIST_PATH
    try:
        body, csrf_token, return_to, upload_attachments = await _parse_requester_message_form(request, settings=settings)
        requester_return_url = _sanitize_requester_return_to(return_to)
        validate_csrf_token(auth_session, csrf_token)
        if not body:
            raise UploadValidationError("Reply text is required.")
        if len(upload_attachments) > settings.max_images_per_message:
            raise UploadValidationError(f"Attach at most {settings.max_images_per_message} files.")
    except UploadValidationError as exc:
        if isinstance(exc, _RequesterMessageFormError):
            body = exc.body
            requester_return_url = _sanitize_requester_return_to(exc.return_to)
        detail_context = build_requester_ticket_detail_context(
            db,
            ticket=ticket,
            ui_locale=ui_locale,
            stale_timeout_seconds=settings.ai_run_stale_timeout_seconds,
            reply_body=body,
        )
        detail_context.update(
            {
                "requester_return_url": requester_return_url,
                "requester_return_to": requester_return_url,
                "live_detail_url": _requester_ticket_detail_path(ticket.reference, requester_return_url),
            }
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
                ui_switch_path=_requester_ticket_detail_path(ticket.reference, requester_return_url),
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
        _requester_ticket_detail_path(ticket.reference, requester_return_url),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/app/tickets/{reference}/resolve")
def requester_ticket_resolve(
    reference: str,
    current_user: User = Depends(require_requester_user),
    auth_session: SessionRecord = Depends(get_required_auth_session),
    settings: Settings = Depends(get_settings),
    csrf_token: str = Form(...),
    return_to: str = Form(default=""),
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
        _requester_ticket_detail_path(ticket.reference, _sanitize_requester_return_to(return_to)),
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
