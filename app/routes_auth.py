from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import (
    begin_user_session,
    end_user_session,
    get_optional_auth_session,
    get_settings_dependency,
    get_required_auth_session,
    validate_csrf_token,
)
from app.ui import build_template_context, post_login_redirect_path, sanitize_next_path, templates
from shared.config import Settings
from shared.contracts import PREAUTH_LOGIN_COOKIE_NAME
from shared.db import db_session_dependency
from shared.models import PreauthLoginSession, SessionRecord, User
from shared.preauth_login import create_preauth_login_session, get_valid_preauth_login_session, invalidate_preauth_login_session
from shared.security import verify_password
from shared.user_admin import get_user_by_email

router = APIRouter()


def get_current_user_optional(
    auth_session: SessionRecord | None = Depends(get_optional_auth_session),
    db: Session = Depends(db_session_dependency),
) -> User | None:
    if auth_session is None:
        return None
    user = db.get(User, auth_session.user_id)
    if user is None or not user.is_active:
        return None
    return user


def _set_preauth_login_cookie(response, *, raw_token: str, settings: Settings) -> None:
    response.set_cookie(
        key=PREAUTH_LOGIN_COOKIE_NAME,
        value=raw_token,
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
        path="/login",
        max_age=600,
        expires=600,
    )


def _delete_preauth_login_cookie(response) -> None:
    response.delete_cookie(PREAUTH_LOGIN_COOKIE_NAME, path="/login")


def _issue_login_challenge(
    *,
    request: Request,
    db: Session,
    settings: Settings,
    next_path: str | None,
    email: str = "",
    error: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    invalidate_preauth_login_session(db, request.cookies.get(PREAUTH_LOGIN_COOKIE_NAME))
    challenge, raw_token = create_preauth_login_session(
        db,
        next_path=next_path,
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
    )
    response = templates.TemplateResponse(
        request,
        "login.html",
        build_template_context(
            request=request,
            current_user=None,
            extra={
                "error": error,
                "email": email,
                "login_csrf_token": challenge.csrf_token,
                "next_path": challenge.next_path or "",
            },
        ),
        status_code=status_code,
    )
    _set_preauth_login_cookie(response, raw_token=raw_token, settings=settings)
    db.commit()
    return response


def _get_valid_login_challenge(request: Request, db: Session) -> PreauthLoginSession | None:
    return get_valid_preauth_login_session(db, request.cookies.get(PREAUTH_LOGIN_COOKIE_NAME))


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    next: str | None = None,
    settings: Settings = Depends(get_settings_dependency),
    db: Session = Depends(db_session_dependency),
    current_user: User | None = Depends(get_current_user_optional),
):
    if current_user is not None:
        return RedirectResponse(post_login_redirect_path(current_user), status_code=status.HTTP_303_SEE_OTHER)
    return _issue_login_challenge(
        request=request,
        db=db,
        settings=settings,
        next_path=sanitize_next_path(next),
    )


@router.post("/login")
def login_action(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
    remember_me: str | None = Form(default=None),
    settings: Settings = Depends(get_settings_dependency),
    db: Session = Depends(db_session_dependency),
    current_user: User | None = Depends(get_current_user_optional),
):
    if current_user is not None:
        return RedirectResponse(post_login_redirect_path(current_user), status_code=status.HTTP_303_SEE_OTHER)
    challenge = _get_valid_login_challenge(request, db)
    if challenge is None:
        return _issue_login_challenge(
            request=request,
            db=db,
            settings=settings,
            next_path=None,
            email=email.strip(),
            error="Your login form expired. Please try again.",
            status_code=status.HTTP_403_FORBIDDEN,
        )
    if not secrets.compare_digest(challenge.csrf_token, csrf_token):
        return _issue_login_challenge(
            request=request,
            db=db,
            settings=settings,
            next_path=challenge.next_path,
            email=email.strip(),
            error="Invalid login form token. Please try again.",
            status_code=status.HTTP_403_FORBIDDEN,
        )

    user = get_user_by_email(db, email)
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        return _issue_login_challenge(
            request=request,
            db=db,
            settings=settings,
            next_path=challenge.next_path,
            email=email.strip(),
            error="Invalid email or password.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    response = RedirectResponse(challenge.next_path or post_login_redirect_path(user), status_code=status.HTTP_303_SEE_OTHER)
    begin_user_session(
        request=request,
        response=response,
        db=db,
        user=user,
        remember_me=remember_me == "on",
        settings=settings,
    )
    invalidate_preauth_login_session(db, request.cookies.get(PREAUTH_LOGIN_COOKIE_NAME))
    _delete_preauth_login_cookie(response)
    db.commit()
    return response


@router.post("/logout")
def logout_action(
    request: Request,
    csrf_token: str = Form(...),
    auth_session: SessionRecord = Depends(get_required_auth_session),
    db: Session = Depends(db_session_dependency),
):
    validate_csrf_token(auth_session, csrf_token)
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    end_user_session(request=request, response=response, db=db)
    db.commit()
    return response
