from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models import USER_ROLES, User
from shared.security import hash_password, utc_now, verify_password

_MIN_PASSWORD_LENGTH = 8
_EMAIL_ADDRESS_RE = re.compile(r"^[^@\s]+@[^@\s]+$")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def validate_email_address(email: str) -> str:
    normalized = normalize_email(email)
    if not normalized:
        raise ValueError("Email is required.")
    if _EMAIL_ADDRESS_RE.fullmatch(normalized) is None:
        raise ValueError("Invalid email address.")
    return normalized


def normalize_display_name(display_name: str) -> str:
    normalized = display_name.strip()
    if not normalized:
        raise ValueError("Display name is required.")
    return normalized


def validate_password(password: str) -> str:
    if not isinstance(password, str) or not password.strip():
        raise ValueError("Password is required.")
    if len(password) < _MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {_MIN_PASSWORD_LENGTH} characters.")
    return password


def get_user_by_email(db: Session, email: str) -> User | None:
    statement = select(User).where(User.email == normalize_email(email))
    return db.execute(statement).scalar_one_or_none()


def create_user(db: Session, *, email: str, display_name: str, password: str, role: str) -> User:
    if role not in USER_ROLES:
        raise ValueError(f"Unsupported role: {role}")
    normalized_email = validate_email_address(email)
    if get_user_by_email(db, normalized_email) is not None:
        raise ValueError(f"User already exists: {normalized_email}")
    now = utc_now()
    user = User(
        email=normalized_email,
        display_name=normalize_display_name(display_name),
        password_hash=hash_password(validate_password(password)),
        role=role,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    return user


def ensure_admin_user(db: Session, *, email: str, display_name: str, password: str) -> tuple[User, str]:
    normalized_email = validate_email_address(email)
    normalized_display_name = display_name.strip()
    existing = get_user_by_email(db, normalized_email)
    if existing is None:
        return (
            create_user(
                db,
                email=normalized_email,
                display_name=normalized_display_name,
                password=password,
                role="admin",
            ),
            "created",
        )
    if existing.role != "admin":
        raise ValueError(f"Existing user {normalized_email} has role {existing.role}, not admin")
    if not existing.is_active:
        raise ValueError(f"Existing admin {normalized_email} is inactive")
    if existing.display_name.strip() != normalized_display_name:
        raise ValueError(f"Existing admin {normalized_email} has a different display name")
    if not verify_password(password, existing.password_hash):
        raise ValueError(f"Existing admin {normalized_email} has a different password")
    return existing, "unchanged"


def set_password(db: Session, *, email: str, password: str) -> User:
    user = get_user_by_email(db, email)
    if user is None:
        raise ValueError(f"Unknown user: {normalize_email(email)}")
    user.password_hash = hash_password(validate_password(password))
    user.updated_at = utc_now()
    return user


def deactivate_user(db: Session, *, email: str) -> User:
    user = get_user_by_email(db, email)
    if user is None:
        raise ValueError(f"Unknown user: {normalize_email(email)}")
    user.is_active = False
    user.updated_at = utc_now()
    return user


def update_user(
    db: Session,
    *,
    user: User,
    display_name: str,
    role: str,
    password: str | None = None,
) -> User:
    if role not in USER_ROLES:
        raise ValueError(f"Unsupported role: {role}")
    user.display_name = normalize_display_name(display_name)
    user.role = role
    if password is not None and password.strip():
        user.password_hash = hash_password(validate_password(password))
    user.updated_at = utc_now()
    return user


def set_user_active_state(db: Session, *, user: User, is_active: bool) -> User:
    user.is_active = is_active
    user.updated_at = utc_now()
    return user
