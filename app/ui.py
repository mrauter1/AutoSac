from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit
import posixpath

from fastapi.templating import Jinja2Templates

from app.i18n import (
    ai_run_status_label,
    ai_run_step_kind_label,
    bool_label,
    build_locale_switch_links,
    current_request_path,
    format_datetime_utc,
    get_translator,
    impact_level_label,
    none_yet_label,
    ops_author_label,
    ops_status_label,
    publish_mode_recommendation_label,
    requester_author_label,
    requester_role_suffix_label,
    requester_status_label,
    resolve_ui_locale,
    response_confidence_label,
    risk_level_label,
    route_target_kind_label,
    sanitize_ui_switch_path,
    translate_error_text,
    unknown_label,
    unassigned_label,
    user_role_label,
)
from shared.models import SessionRecord, User
from shared.permissions import is_ops_user

APP_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def post_login_redirect_path(user: User) -> str:
    if is_ops_user(user):
        return "/ops"
    return "/app"


def is_htmx_request(request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def sanitize_relative_path(path: str | None, *, allow_login: bool = False) -> str | None:
    if path is None:
        return None
    candidate = path.strip()
    if not candidate:
        return None
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc or candidate.startswith("//") or not parsed.path.startswith("/"):
        return None
    normalized_path = posixpath.normpath(parsed.path)
    if not normalized_path.startswith("/"):
        normalized_path = f"/{normalized_path}"
    if not allow_login and normalized_path == "/login":
        return None
    query = urlencode(parse_qsl(parsed.query, keep_blank_values=True), doseq=True)
    return f"{normalized_path}?{query}" if query else normalized_path


def sanitize_next_path(next_path: str | None) -> str | None:
    return sanitize_relative_path(next_path, allow_login=False)


def login_redirect_path(next_path: str | None = None) -> str:
    safe_next = sanitize_next_path(next_path)
    if not safe_next:
        return "/login"
    return f"/login?{urlencode({'next': safe_next})}"


def request_next_path(request) -> str | None:
    query = request.url.query
    current = f"{request.url.path}?{query}" if query else request.url.path
    return sanitize_next_path(current)


def is_html_navigation_request(request) -> bool:
    if request.method not in {"GET", "HEAD"}:
        return False
    if is_htmx_request(request):
        return False
    path = request.url.path
    return path == "/app" or path.startswith("/app/") or path == "/ops" or path.startswith("/ops/")


def build_template_context(
    *,
    request,
    current_user: User | None = None,
    auth_session: SessionRecord | None = None,
    extra: Mapping[str, object] | None = None,
    ui_locale: str | None = None,
    ui_switch_path: str | None = None,
) -> dict[str, object]:
    resolved_locale = ui_locale or resolve_ui_locale(request)
    translator = get_translator(resolved_locale)
    extra_context = dict(extra or {})
    if isinstance(extra_context.get("error"), str):
        extra_context["error"] = translate_error_text(extra_context["error"], resolved_locale)
    switch_path = sanitize_ui_switch_path(ui_switch_path) or current_request_path(request)
    context: dict[str, object] = {
        "request": request,
        "current_user": current_user,
        "csrf_token": auth_session.csrf_token if auth_session is not None else "",
        "ui_locale": resolved_locale,
        "ui_locale_links": build_locale_switch_links(request, next_path=switch_path),
        "current_path": switch_path,
        "t": translator,
        "format_datetime_utc": lambda value: format_datetime_utc(value, resolved_locale),
        "requester_status_label": lambda status: requester_status_label(status, resolved_locale),
        "requester_author_label": lambda author_type: requester_author_label(author_type, resolved_locale),
        "requester_role_suffix_label": lambda author_type: requester_role_suffix_label(author_type, resolved_locale),
        "ops_status_label": lambda status: ops_status_label(status, resolved_locale),
        "ops_author_label": lambda author_type: ops_author_label(author_type, resolved_locale),
        "user_role_label": lambda role: user_role_label(role, resolved_locale),
        "route_target_kind_label": lambda kind: route_target_kind_label(kind, resolved_locale),
        "ai_run_status_label": lambda status: ai_run_status_label(status, resolved_locale),
        "ai_run_step_kind_label": lambda kind: ai_run_step_kind_label(kind, resolved_locale),
        "publish_mode_recommendation_label": lambda value: publish_mode_recommendation_label(value, resolved_locale),
        "response_confidence_label": lambda value: response_confidence_label(value, resolved_locale),
        "risk_level_label": lambda value: risk_level_label(value, resolved_locale),
        "impact_level_label": lambda value: impact_level_label(value, resolved_locale),
        "bool_label": lambda value: bool_label(value, resolved_locale),
        "unknown_label": unknown_label(resolved_locale),
        "unassigned_label": unassigned_label(resolved_locale),
        "none_yet_label": none_yet_label(resolved_locale),
        "is_ops_user": is_ops_user(current_user) if current_user is not None else False,
    }
    if extra_context:
        context.update(extra_context)
    return context
