from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import secrets
import hmac

try:
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
except ModuleNotFoundError:  # pragma: no cover - exercised in script-level tests
    PasswordHasher = None
    InvalidHashError = VerificationError = VerifyMismatchError = ValueError

from shared.config import Settings

_PBKDF2_PREFIX = "pbkdf2_sha256"
_PBKDF2_ITERATIONS = 600_000
_PBKDF2_SALT_BYTES = 16
_PASSWORD_HASHER = PasswordHasher() if PasswordHasher is not None else None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(password: str) -> str:
    if _PASSWORD_HASHER is not None:
        return _PASSWORD_HASHER.hash(password)
    salt = secrets.token_bytes(_PBKDF2_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        _PBKDF2_ITERATIONS,
    )
    return f"{_PBKDF2_PREFIX}${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def _verify_pbkdf2_password(password: str, password_hash: str) -> bool:
    try:
        prefix, raw_iterations, salt_hex, digest_hex = password_hash.split("$", 3)
        if prefix != _PBKDF2_PREFIX:
            return False
        iterations = int(raw_iterations)
        salt = bytes.fromhex(salt_hex)
        expected_digest = bytes.fromhex(digest_hex)
    except (TypeError, ValueError):
        return False
    candidate_digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(candidate_digest, expected_digest)


def verify_password(password: str, password_hash: str) -> bool:
    if not isinstance(password, str) or not isinstance(password_hash, str) or not password_hash.strip():
        return False
    if password_hash.startswith(f"{_PBKDF2_PREFIX}$"):
        return _verify_pbkdf2_password(password, password_hash)
    if _PASSWORD_HASHER is None:
        return False
    try:
        return _PASSWORD_HASHER.verify(password_hash, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError, TypeError, ValueError):
        return False


def generate_opaque_token() -> str:
    return secrets.token_urlsafe(32)


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def calculate_session_expiry(settings: Settings, remember_me: bool, now: datetime | None = None) -> datetime:
    base = now or utc_now()
    if remember_me:
        return base + timedelta(days=settings.session_remember_days)
    return base + timedelta(hours=settings.session_default_hours)
