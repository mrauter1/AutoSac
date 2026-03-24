from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit
import posixpath

from fastapi.templating import Jinja2Templates

from shared.models import SessionRecord, User
from shared.permissions import is_ops_user

APP_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

REQUESTER_STATUS_LABELS = {
    "new": "Reviewing",
    "ai_triage": "Reviewing",
    "waiting_on_user": "Waiting for your reply",
    "waiting_on_dev_ti": "Waiting on team",
    "resolved": "Resolved",
}

REQUESTER_AUTHOR_LABELS = {
    "requester": "You",
    "dev_ti": "Team",
    "ai": "AI",
    "system": "System",
}

OPS_STATUS_LABELS = {
    "new": "New",
    "ai_triage": "AI Triage",
    "waiting_on_user": "Waiting on User",
    "waiting_on_dev_ti": "Waiting on Dev/TI",
    "resolved": "Resolved",
}

OPS_AUTHOR_LABELS = {
    "requester": "Requester",
    "dev_ti": "Dev/TI",
    "ai": "AI",
    "system": "System",
}


def requester_status_label(status: str) -> str:
    return REQUESTER_STATUS_LABELS.get(status, status)


def requester_author_label(author_type: str) -> str:
    return REQUESTER_AUTHOR_LABELS.get(author_type, author_type.replace("_", " ").title())


def ops_status_label(status: str) -> str:
    return OPS_STATUS_LABELS.get(status, status.replace("_", " ").title())


def ops_author_label(author_type: str) -> str:
    return OPS_AUTHOR_LABELS.get(author_type, author_type.replace("_", " ").title())


def post_login_redirect_path(user: User) -> str:
    if is_ops_user(user):
        return "/ops"
    return "/app"


def is_htmx_request(request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def sanitize_next_path(next_path: str | None) -> str | None:
    if next_path is None:
        return None
    candidate = next_path.strip()
    if not candidate:
        return None
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc or candidate.startswith("//") or not parsed.path.startswith("/"):
        return None
    normalized_path = posixpath.normpath(parsed.path)
    if not normalized_path.startswith("/"):
        normalized_path = f"/{normalized_path}"
    if normalized_path == "/login":
        return None
    query = urlencode(parse_qsl(parsed.query, keep_blank_values=True), doseq=True)
    return f"{normalized_path}?{query}" if query else normalized_path


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
) -> dict[str, object]:
    context: dict[str, object] = {
        "request": request,
        "current_user": current_user,
        "csrf_token": auth_session.csrf_token if auth_session is not None else "",
        "requester_status_label": requester_status_label,
        "requester_author_label": requester_author_label,
        "ops_status_label": ops_status_label,
        "ops_author_label": ops_author_label,
        "is_ops_user": is_ops_user(current_user) if current_user is not None else False,
    }
    if extra:
        context.update(extra)
    return context
