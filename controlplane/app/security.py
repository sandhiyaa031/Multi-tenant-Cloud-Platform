import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, InvalidHashError

from app.config import get_settings

_hasher = PasswordHasher()
# Verified against when the email is unknown, so that "no such user" and "wrong
# password" take the same time and cannot be told apart by timing.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))
_ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (Argon2Error, InvalidHashError):
        return False


def create_access_token(user_id: UUID, org_id: UUID) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    claims = {
        "sub": str(user_id),
        "org": str(org_id),
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_ttl_minutes),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=_ALGORITHM)


def decode_access_token(token: str) -> tuple[UUID, UUID]:
    """Returns (user_id, org_id). Raises jwt.InvalidTokenError on any problem.

    The role is deliberately not in the token: it is read from the database on
    every request, so demoting or removing a member takes effect immediately.
    """
    claims = jwt.decode(
        token, get_settings().jwt_secret, algorithms=[_ALGORITHM], options={"require": ["sub", "org", "exp"]}
    )
    try:
        return UUID(claims["sub"]), UUID(claims["org"])
    except ValueError as exc:
        raise jwt.InvalidTokenError("malformed subject") from exc


def new_one_time_token() -> tuple[str, bytes]:
    """Returns (token to send to the user, hash to store)."""
    token = secrets.token_urlsafe(32)
    return token, hash_one_time_token(token)


def hash_one_time_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()
