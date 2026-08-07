"""Operator sign-in, and the step-up that precedes moving money.

Two tiers, and the split is the point.

``HumanCredential`` was always documented as "recent-authentication proof for
protected human reads and approval actions, **not** long-lived application
sessions". That is step-up authentication: proof that a human re-authenticated
moments before they approved something. But nothing ever provided the session it
was stepping up *from*, so in practice an operator had to run a CLI on a
terminal to read a queue. The guarantee was real and the product was unusable.

So: a signed cookie carries an eight-hour shift and lets an operator read the
queues they are entitled to. Approving an allocation or signing a disbursement
still demands the password again and still mints the same five-minute
credential, unchanged. The strong guarantee stays exactly where it was; only the
reading of a list gets easier.

Nothing here is a new credential type. Step-up issues the same
``HumanCredential`` the CLI issues, so every route keeps its existing
``authenticate_human`` check and its role allowlist.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from fastapi import APIRouter, Cookie, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .approval_credentials import (
    CredentialError,
    hash_password,
    issue_human_credential,
    verify_password,
)
from .config import get_settings
from .db import session_scope
from .models import AppUser

router = APIRouter(prefix="/v1/auth", tags=["auth"])

SESSION_COOKIE = "lh_session"

#: One shift. Long enough that an operator working an event is not signed out
#: mid-storm, short enough that a forgotten browser is not a standing grant.
SESSION_LIFETIME = timedelta(hours=8)

_MAX_CLOCK_SKEW = timedelta(minutes=1)


class SignInRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class SignInResponse(BaseModel):
    email: str
    display_name: str
    role: str
    expires_at: datetime


class StepUpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=1, max_length=1024)


class StepUpResponse(BaseModel):
    token: str
    expires_at: datetime


def _unauthorized() -> HTTPException:
    """One shape for every failure, so nothing here enumerates accounts."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="valid operator sign-in required",
    )


@lru_cache(maxsize=1)
def _absent_password() -> str:
    """A real scrypt hash to check against when no account matched.

    ``verify_password`` returns immediately on a null hash, so without this a
    missing account answers in microseconds and a real one takes scrypt's full
    work factor. That difference is a usable account-enumeration oracle even
    though the status code is identical. Computed once, verified always.
    """
    return hash_password(secrets.token_urlsafe(32))


def _secret() -> bytes:
    """The cookie signing key, and a startup failure rather than a default.

    A generated fallback would sign cookies that survive exactly until the next
    deploy, and a constant fallback would let anyone holding this source forge a
    Director session. Neither is acceptable, so an unset secret disables sign-in
    and says so.
    """
    settings = get_settings()
    secret = (settings.session_secret or "").strip()
    if len(secret) < 32:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "operator sign-in is not configured: SESSION_SECRET must be set "
                "to at least 32 characters"
            ),
        )
    return secret.encode("utf-8")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(payload: str) -> str:
    return _b64(hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).digest())


def mint_session(user: AppUser, *, now: datetime | None = None) -> tuple[str, datetime]:
    issued = now or datetime.now(UTC)
    expires = issued + SESSION_LIFETIME
    payload = f"{user.id}.{int(expires.timestamp())}"
    return f"{payload}.{_sign(payload)}", expires


def read_session(
    session: Session,
    cookie: str | None,
    *,
    now: datetime | None = None,
) -> AppUser:
    """Resolve a cookie to an active user, or raise 401.

    Deliberately re-reads the user on every request rather than trusting
    anything carried in the cookie. Deactivating an operator has to take effect
    on their next call, not when their shift happens to end — so ``active`` is
    checked here and not baked into the token.
    """
    if not cookie or cookie.count(".") != 2:
        raise _unauthorized()
    user_raw, expiry_raw, signature = cookie.split(".")
    payload = f"{user_raw}.{expiry_raw}"

    if not hmac.compare_digest(signature, _sign(payload)):
        raise _unauthorized()

    try:
        expires = datetime.fromtimestamp(int(expiry_raw), tz=UTC)
        user_id = uuid.UUID(user_raw)
    except (ValueError, OverflowError, OSError):
        raise _unauthorized() from None

    current = now or datetime.now(UTC)
    if expires <= current or expires > current + SESSION_LIFETIME + _MAX_CLOCK_SKEW:
        raise _unauthorized()

    user = session.get(AppUser, user_id)
    if user is None or not user.active:
        raise _unauthorized()
    return user


def _set_cookie(response: Response, value: str, expires: datetime) -> None:
    settings = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        value,
        expires=expires,
        httponly=True,
        # Lax rather than Strict: the console navigates to itself from the EOC
        # map and the simulator, and Strict would drop the cookie on those
        # cross-page loads. Nothing here is a GET that changes state.
        samesite="lax",
        secure=settings.environment != "local",
        path="/",
    )


@router.post("/session", response_model=SignInResponse)
def sign_in(body: SignInRequest, response: Response) -> SignInResponse:
    """Exchange a password for an eight-hour session cookie."""
    with session_scope() as session:
        user = session.scalars(
            select(AppUser)
            .where(func.lower(AppUser.email) == body.email.strip().lower())
            .limit(1)
        ).one_or_none()
        # One failure shape whether the account is missing, inactive or the
        # password is wrong. A caller learns whether they are signed in and
        # nothing else. verify_password is still called against a dummy hash on
        # the missing-account path so the response time does not answer the
        # question the status code refuses to.
        stored = user.password_hash if user is not None else _absent_password()
        correct = verify_password(body.password, stored)
        if user is None or not user.active or not correct:
            raise _unauthorized()

        cookie, expires = mint_session(user)
        _set_cookie(response, cookie, expires)
        return SignInResponse(
            email=user.email,
            display_name=user.display_name,
            role=user.role.value,
            expires_at=expires,
        )


@router.get("/session", response_model=SignInResponse)
def current_session(lh_session: str | None = Cookie(default=None)) -> SignInResponse:
    """Who am I. The console calls this on load to restore its header."""
    with session_scope() as session:
        user = read_session(session, lh_session)
        return SignInResponse(
            email=user.email,
            display_name=user.display_name,
            role=user.role.value,
            expires_at=datetime.now(UTC) + SESSION_LIFETIME,
        )


@router.delete("/session", status_code=status.HTTP_204_NO_CONTENT)
def sign_out(response: Response) -> Response:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.post("/step-up", response_model=StepUpResponse)
def step_up(
    body: StepUpRequest,
    lh_session: str | None = Cookie(default=None),
) -> StepUpResponse:
    """Re-prove the password and mint the five-minute approval credential.

    The credential returned here is identical to the one the CLI issues. It is
    not stored by the console beyond the tab's memory, and every protected route
    validates it through the unchanged ``authenticate_human``.
    """
    with session_scope() as session:
        user = read_session(session, lh_session)
        try:
            issued = issue_human_credential(
                session, email=user.email, password=body.password
            )
        except CredentialError:
            raise _unauthorized() from None
        return StepUpResponse(token=issued.token, expires_at=issued.expires_at)


__all__ = [
    "SESSION_COOKIE",
    "SESSION_LIFETIME",
    "mint_session",
    "read_session",
    "router",
]
