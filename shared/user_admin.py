from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models import USER_ROLES, User
from shared.security import hash_password, utc_now, verify_password


def normalize_email(email: str) -> str:
    return email.strip().lower()


def get_user_by_email(db: Session, email: str) -> User | None:
    statement = select(User).where(User.email == normalize_email(email))
    return db.execute(statement).scalar_one_or_none()


def create_user(db: Session, *, email: str, display_name: str, password: str, role: str) -> User:
    if role not in USER_ROLES:
        raise ValueError(f"Unsupported role: {role}")
    normalized_email = normalize_email(email)
    if get_user_by_email(db, normalized_email) is not None:
        raise ValueError(f"User already exists: {normalized_email}")
    now = utc_now()
    user = User(
        email=normalized_email,
        display_name=display_name.strip(),
        password_hash=hash_password(password),
        role=role,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    return user


def ensure_admin_user(db: Session, *, email: str, display_name: str, password: str) -> tuple[User, str]:
    normalized_email = normalize_email(email)
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
    user.password_hash = hash_password(password)
    user.updated_at = utc_now()
    return user


def deactivate_user(db: Session, *, email: str) -> User:
    user = get_user_by_email(db, email)
    if user is None:
        raise ValueError(f"Unknown user: {normalize_email(email)}")
    user.is_active = False
    user.updated_at = utc_now()
    return user
