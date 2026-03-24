from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from shared.models import PreauthLoginSession
from shared.security import generate_csrf_token, generate_opaque_token, hash_token, utc_now

PREAUTH_LOGIN_TTL = timedelta(minutes=10)


def _cleanup_expired_preauth_logins(db: Session) -> None:
    db.execute(delete(PreauthLoginSession).where(PreauthLoginSession.expires_at <= utc_now()))


def create_preauth_login_session(
    db: Session,
    *,
    next_path: str | None,
    user_agent: str | None,
    ip_address: str | None,
) -> tuple[PreauthLoginSession, str]:
    _cleanup_expired_preauth_logins(db)
    raw_token = generate_opaque_token()
    now = utc_now()
    record = PreauthLoginSession(
        token_hash=hash_token(raw_token),
        csrf_token=generate_csrf_token(),
        next_path=next_path,
        expires_at=now + PREAUTH_LOGIN_TTL,
        created_at=now,
        user_agent=user_agent,
        ip_address=ip_address,
    )
    db.add(record)
    return record, raw_token


def get_valid_preauth_login_session(db: Session, raw_token: str | None) -> PreauthLoginSession | None:
    if not raw_token:
        return None
    _cleanup_expired_preauth_logins(db)
    statement = select(PreauthLoginSession).where(
        PreauthLoginSession.token_hash == hash_token(raw_token),
        PreauthLoginSession.expires_at > utc_now(),
    )
    return db.execute(statement).scalar_one_or_none()


def invalidate_preauth_login_session(db: Session, raw_token: str | None) -> None:
    if not raw_token:
        return
    db.execute(delete(PreauthLoginSession).where(PreauthLoginSession.token_hash == hash_token(raw_token)))
