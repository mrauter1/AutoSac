from __future__ import annotations

from urllib.parse import urlencode, urlsplit

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.config import Settings, get_settings
from shared.contracts import PREAUTH_SESSION_COOKIE_NAME, SESSION_COOKIE_NAME
from shared.db import db_session_dependency
from shared.models import PreAuthSessionRecord, SessionRecord, User
from shared.permissions import is_ops_user, is_requester
from shared.sessions import (
    create_preauth_session,
    create_server_session,
    get_valid_preauth_session_by_token,
    get_valid_session_by_token,
    invalidate_preauth_session,
    invalidate_session,
    refresh_preauth_session,
)


class BrowserRedirectRequired(Exception):
    def __init__(self, location: str):
        self.location = location


def sanitize_next_path(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip()
    if not candidate or not candidate.startswith("/") or candidate.startswith("//"):
        return None
    if "\\" in candidate or any(ord(char) < 32 for char in candidate):
        return None
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return None
    sanitized = parsed.path
    if parsed.query:
        sanitized = f"{sanitized}?{parsed.query}"
    return sanitized


def login_redirect_path(next_path: str | None = None) -> str:
    sanitized = sanitize_next_path(next_path)
    if sanitized:
        return f"/login?{urlencode({'next': sanitized})}"
    return "/login"


def next_path_from_request(request: Request) -> str | None:
    current = request.url.path
    if request.url.query:
        current = f"{current}?{request.url.query}"
    return sanitize_next_path(current)


def resolve_post_login_redirect(next_path: str | None, *, fallback: str) -> str:
    sanitized = sanitize_next_path(next_path)
    if sanitized is None:
        return fallback
    if urlsplit(sanitized).path == "/login":
        return fallback
    return sanitized


def get_settings_dependency() -> Settings:
    return get_settings()


def get_optional_auth_session(
    request: Request,
    db: Session = Depends(db_session_dependency),
) -> SessionRecord | None:
    return get_valid_session_by_token(db, request.cookies.get(SESSION_COOKIE_NAME))


def get_optional_preauth_session(
    request: Request,
    db: Session = Depends(db_session_dependency),
) -> PreAuthSessionRecord | None:
    return get_valid_preauth_session_by_token(db, request.cookies.get(PREAUTH_SESSION_COOKIE_NAME))


def load_active_user_for_session(db: Session, auth_session: SessionRecord | None) -> User | None:
    if auth_session is None:
        return None
    return db.execute(select(User).where(User.id == auth_session.user_id, User.is_active.is_(True))).scalar_one_or_none()


def get_current_user(
    auth_session: SessionRecord | None = Depends(get_optional_auth_session),
    db: Session = Depends(db_session_dependency),
) -> User:
    user = load_active_user_for_session(db, auth_session)
    if auth_session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session is no longer valid")
    return user


def get_required_auth_session(
    auth_session: SessionRecord | None = Depends(get_optional_auth_session),
) -> SessionRecord:
    if auth_session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return auth_session


def get_required_browser_auth_session(
    request: Request,
    auth_session: SessionRecord | None = Depends(get_optional_auth_session),
) -> SessionRecord:
    if auth_session is None:
        raise BrowserRedirectRequired(login_redirect_path(next_path_from_request(request)))
    return auth_session


def require_browser_user(
    request: Request,
    auth_session: SessionRecord | None = Depends(get_optional_auth_session),
    db: Session = Depends(db_session_dependency),
) -> User:
    user = load_active_user_for_session(db, auth_session)
    if user is None:
        raise BrowserRedirectRequired(login_redirect_path(next_path_from_request(request)))
    return user


def require_requester_user(current_user: User = Depends(get_current_user)) -> User:
    if not is_requester(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Requester access required")
    return current_user


def require_browser_requester_user(current_user: User = Depends(require_browser_user)) -> User:
    if not is_requester(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Requester access required")
    return current_user


def require_ops_user(current_user: User = Depends(get_current_user)) -> User:
    if not is_ops_user(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Ops access required")
    return current_user


def require_browser_ops_user(current_user: User = Depends(require_browser_user)) -> User:
    if not is_ops_user(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Ops access required")
    return current_user


def validate_csrf_token(auth_session: SessionRecord, provided_token: str | None) -> None:
    if not provided_token or provided_token != auth_session.csrf_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")


def require_csrf(
    csrf_token: str | None,
    *,
    auth_session: SessionRecord,
) -> None:
    validate_csrf_token(auth_session, csrf_token)


def begin_user_session(
    *,
    request: Request,
    response: Response,
    db: Session,
    user: User,
    remember_me: bool,
    settings: Settings,
) -> SessionRecord:
    auth_session, raw_token = create_server_session(
        db,
        settings=settings,
        user=user,
        remember_me=remember_me,
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
    )
    max_age = settings.session_remember_days * 24 * 60 * 60 if remember_me else None
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=raw_token,
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
        path="/",
        max_age=max_age,
        expires=max_age,
    )
    return auth_session


def issue_login_preauth_session(
    *,
    request: Request,
    response: Response,
    db: Session,
    settings: Settings,
    preauth_session: PreAuthSessionRecord | None,
) -> PreAuthSessionRecord:
    raw_token = request.cookies.get(PREAUTH_SESSION_COOKIE_NAME)
    if preauth_session is None or not raw_token:
        preauth_session, raw_token = create_preauth_session(
            db,
            settings=settings,
            user_agent=request.headers.get("user-agent"),
            ip_address=request.client.host if request.client else None,
        )
    else:
        refresh_preauth_session(
            preauth_session,
            settings=settings,
            user_agent=request.headers.get("user-agent"),
            ip_address=request.client.host if request.client else None,
        )
    response.set_cookie(
        key=PREAUTH_SESSION_COOKIE_NAME,
        value=raw_token,
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
        path="/",
    )
    return preauth_session


def end_user_session(*, request: Request, response: Response, db: Session) -> None:
    invalidate_session(db, request.cookies.get(SESSION_COOKIE_NAME))
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")


def end_login_preauth_session(*, request: Request, response: Response, db: Session) -> None:
    invalidate_preauth_session(db, request.cookies.get(PREAUTH_SESSION_COOKIE_NAME))
    response.delete_cookie(PREAUTH_SESSION_COOKIE_NAME, path="/")
