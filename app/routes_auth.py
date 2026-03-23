from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import (
    begin_user_session,
    end_login_preauth_session,
    end_user_session,
    get_optional_auth_session,
    get_optional_preauth_session,
    get_settings_dependency,
    issue_login_preauth_session,
    load_active_user_for_session,
    resolve_post_login_redirect,
    get_required_auth_session,
    sanitize_next_path,
    validate_csrf_token,
)
from app.ui import build_template_context, post_login_redirect_path, templates
from shared.config import Settings
from shared.db import db_session_dependency
from shared.models import PreAuthSessionRecord, SessionRecord, User
from shared.security import verify_password
from shared.user_admin import get_user_by_email

router = APIRouter()


def _login_template_response(
    *,
    request: Request,
    db: Session,
    settings: Settings,
    auth_session: SessionRecord | None,
    preauth_session: PreAuthSessionRecord | None,
    next_path: str | None,
    email: str = "",
    error: str | None = None,
    status_code: int = status.HTTP_200_OK,
):
    cookie_response = Response()
    refreshed_preauth = issue_login_preauth_session(
        request=request,
        response=cookie_response,
        db=db,
        settings=settings,
        preauth_session=preauth_session,
    )
    return templates.TemplateResponse(
        request,
        "login.html",
        build_template_context(
            request=request,
            current_user=None,
            auth_session=auth_session,
            extra={
                "csrf_token": refreshed_preauth.csrf_token,
                "next_path": next_path or "",
                "email": email,
                "error": error,
            },
        ),
        status_code=status_code,
        headers=dict(cookie_response.headers),
    )


def get_current_user_optional(
    auth_session: SessionRecord | None = Depends(get_optional_auth_session),
    db: Session = Depends(db_session_dependency),
) -> User | None:
    return load_active_user_for_session(db, auth_session)


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    settings: Settings = Depends(get_settings_dependency),
    db: Session = Depends(db_session_dependency),
    preauth_session: PreAuthSessionRecord | None = Depends(get_optional_preauth_session),
    auth_session: SessionRecord | None = Depends(get_optional_auth_session),
    current_user: User | None = Depends(get_current_user_optional),
):
    next_path = sanitize_next_path(request.query_params.get("next"))
    if current_user is not None:
        return RedirectResponse(
            resolve_post_login_redirect(next_path, fallback=post_login_redirect_path(current_user)),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    response = _login_template_response(
        request=request,
        db=db,
        settings=settings,
        auth_session=auth_session,
        preauth_session=preauth_session,
        next_path=next_path,
    )
    db.commit()
    return response


@router.post("/login")
def login_action(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    csrf_token: str | None = Form(default=None),
    next_path: str | None = Form(default=None, alias="next"),
    remember_me: str | None = Form(default=None),
    settings: Settings = Depends(get_settings_dependency),
    db: Session = Depends(db_session_dependency),
    preauth_session: PreAuthSessionRecord | None = Depends(get_optional_preauth_session),
    auth_session: SessionRecord | None = Depends(get_optional_auth_session),
    current_user: User | None = Depends(get_current_user_optional),
):
    sanitized_next = sanitize_next_path(next_path)
    if current_user is not None:
        return RedirectResponse(
            resolve_post_login_redirect(sanitized_next, fallback=post_login_redirect_path(current_user)),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    def _login_error_response(*, message: str, status_code: int):
        response = _login_template_response(
            request=request,
            db=db,
            settings=settings,
            auth_session=auth_session,
            preauth_session=preauth_session,
            next_path=sanitized_next,
            email=email.strip(),
            error=message,
            status_code=status_code,
        )
        db.commit()
        return response

    if preauth_session is None or not csrf_token or csrf_token != preauth_session.csrf_token:
        return _login_error_response(message="Your login session expired. Please try again.", status_code=status.HTTP_403_FORBIDDEN)

    user = get_user_by_email(db, email)
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        return _login_error_response(message="Invalid email or password.", status_code=status.HTTP_400_BAD_REQUEST)

    response = RedirectResponse(
        resolve_post_login_redirect(sanitized_next, fallback=post_login_redirect_path(user)),
        status_code=status.HTTP_303_SEE_OTHER,
    )
    begin_user_session(
        request=request,
        response=response,
        db=db,
        user=user,
        remember_me=remember_me == "on",
        settings=settings,
    )
    end_login_preauth_session(request=request, response=response, db=db)
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
