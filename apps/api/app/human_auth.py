"""Shared bearer authentication for human-only API surfaces."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from lighthouse_contracts import AppRole

from .approval_credentials import hash_human_token
from .models import AppUser, HumanCredential

_MAX_TOKEN_LENGTH = 512
_MIN_TOKEN_LENGTH = 32
_MAX_REAUTH_AGE = timedelta(minutes=5)
_MAX_CLOCK_SKEW = timedelta(minutes=1)
_EXPIRY_SAFETY_MARGIN = timedelta(seconds=2)


@dataclass(frozen=True, slots=True)
class AuthenticatedHuman:
    user: AppUser
    credential: HumanCredential


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="valid recent human authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise _unauthorized()
    scheme, separator, token = authorization.partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or token != token.strip()
        or " " in token
        or not (_MIN_TOKEN_LENGTH <= len(token) <= _MAX_TOKEN_LENGTH)
    ):
        raise _unauthorized()
    return token


def authenticate_human(
    session: Session,
    authorization: str | None,
    *,
    allowed_roles: Collection[AppRole],
    now: datetime | None = None,
) -> AuthenticatedHuman:
    """Authenticate an active user with recent proof and enforce route roles.

    Route handlers pass the raw optional ``Authorization`` header and their
    explicit role allowlist. Failures are already safe FastAPI 401/403 errors,
    so every protected route uses the same non-enumerating behavior.
    """
    token = _bearer_token(authorization)
    current = now or datetime.now(UTC)
    row = session.execute(
        select(HumanCredential, AppUser)
        .join(AppUser, AppUser.id == HumanCredential.user_id)
        .where(HumanCredential.token_hash == hash_human_token(token))
    ).one_or_none()

    if row is None:
        raise _unauthorized()
    credential, user = row
    if (
        not user.active
        or credential.revoked_at is not None
        or credential.expires_at <= current + _EXPIRY_SAFETY_MARGIN
        or credential.reauthenticated_at < current - _MAX_REAUTH_AGE
        or credential.reauthenticated_at > current + _MAX_CLOCK_SKEW
    ):
        raise _unauthorized()

    if user.role not in frozenset(allowed_roles):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="authenticated user is not permitted to perform this action",
        )

    return AuthenticatedHuman(user=user, credential=credential)


__all__ = ["AuthenticatedHuman", "authenticate_human"]
