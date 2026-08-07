"""Operator sign-in, and the step-up that still guards money movement.

The property worth guarding is that the session tier is genuinely weaker than
the credential tier and never accidentally becomes a substitute for it: a
cookie must not approve an allocation, and deactivating an operator must take
their session away on the very next request rather than at the end of a shift.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import auth_session
from app.approval_credentials import set_human_password
from app.auth_session import (
    SESSION_COOKIE,
    SESSION_LIFETIME,
    mint_session,
    read_session,
)
from app.human_auth import authenticate_human
from app.models import AppUser
from app.operators import create_operator
from app.web import app
from lighthouse_contracts import AppRole

PASSWORD = "correct horse lighthouse"


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "x" * 48)
    from app.config import get_settings

    get_settings.cache_clear()
    auth_session._absent_password.cache_clear()
    yield
    get_settings.cache_clear()


def _operator(session, role: AppRole = AppRole.DIRECTOR) -> AppUser:
    create_operator(
        session,
        email="director@example.org",
        display_name="First Director",
        role=role,
    )
    user = session.scalars(
        select(AppUser).where(AppUser.email == "director@example.org")
    ).one()
    set_human_password(session, email=user.email, password=PASSWORD)
    session.flush()
    return user


def _client(monkeypatch, session) -> TestClient:
    @contextmanager
    def scoped():
        yield session
        session.flush()

    monkeypatch.setattr(auth_session, "session_scope", scoped)
    return TestClient(app)


# ---------- the cookie itself ----------


def test_minted_session_round_trips(session):
    user = _operator(session)
    cookie, expires = mint_session(user)

    assert read_session(session, cookie).id == user.id
    assert expires - datetime.now(UTC) <= SESSION_LIFETIME


@pytest.mark.parametrize(
    "mangle",
    [
        pytest.param(lambda c: c[:-1] + ("a" if c[-1] != "a" else "b"), id="signature"),
        pytest.param(lambda c: c.replace(c.split(".")[1], "99999999999"), id="expiry"),
        pytest.param(lambda c: "not.a.cookie", id="garbage"),
        pytest.param(lambda c: "", id="empty"),
    ],
)
def test_tampered_cookies_are_refused(session, mangle):
    user = _operator(session)
    cookie, _ = mint_session(user)

    with pytest.raises(HTTPException) as error:
        read_session(session, mangle(cookie))
    assert error.value.status_code == 401


def test_expired_cookie_is_refused(session):
    user = _operator(session)
    cookie, _ = mint_session(user, now=datetime.now(UTC) - SESSION_LIFETIME * 2)

    with pytest.raises(HTTPException) as error:
        read_session(session, cookie)
    assert error.value.status_code == 401


def test_deactivating_an_operator_kills_the_session_immediately(session):
    """Not at the end of the shift. On the next request."""
    user = _operator(session)
    cookie, _ = mint_session(user)
    assert read_session(session, cookie).id == user.id

    user.active = False
    session.flush()

    with pytest.raises(HTTPException) as error:
        read_session(session, cookie)
    assert error.value.status_code == 401


def test_sign_in_is_unavailable_without_a_configured_secret(session, monkeypatch):
    _operator(session)
    monkeypatch.setenv("SESSION_SECRET", "too-short")
    from app.config import get_settings

    get_settings.cache_clear()
    client = _client(monkeypatch, session)

    response = client.post(
        "/v1/auth/session",
        json={"email": "director@example.org", "password": PASSWORD},
    )
    # 503 and not 500: unconfigured is an operational state, not a crash.
    assert response.status_code == 503
    assert "SESSION_SECRET" in response.json()["detail"]


# ---------- sign-in over HTTP ----------


def test_sign_in_sets_an_http_only_cookie(session, monkeypatch):
    _operator(session)
    client = _client(monkeypatch, session)

    response = client.post(
        "/v1/auth/session",
        json={"email": "Director@Example.org", "password": PASSWORD},
    )

    assert response.status_code == 200
    assert response.json()["role"] == AppRole.DIRECTOR.value
    assert SESSION_COOKIE in response.cookies
    assert "httponly" in response.headers["set-cookie"].lower()


@pytest.mark.parametrize(
    ("email", "password"),
    [
        ("director@example.org", "wrong password entirely"),
        ("nobody@example.org", PASSWORD),
    ],
    ids=["wrong-password", "no-such-account"],
)
def test_bad_sign_in_answers_identically(session, monkeypatch, email, password):
    """A caller learns whether they are signed in and nothing else."""
    _operator(session)
    client = _client(monkeypatch, session)

    response = client.post("/v1/auth/session", json={"email": email, "password": password})

    assert response.status_code == 401
    assert response.json()["detail"] == "valid operator sign-in required"
    assert SESSION_COOKIE not in response.cookies


def test_inactive_operator_cannot_sign_in(session, monkeypatch):
    user = _operator(session)
    user.active = False
    session.flush()
    client = _client(monkeypatch, session)

    response = client.post(
        "/v1/auth/session",
        json={"email": "director@example.org", "password": PASSWORD},
    )
    assert response.status_code == 401


# ---------- step-up ----------


def test_step_up_mints_a_credential_the_protected_routes_accept(session, monkeypatch):
    user = _operator(session)
    client = _client(monkeypatch, session)
    cookie, _ = mint_session(user)

    response = client.post(
        "/v1/auth/step-up",
        json={"password": PASSWORD},
        cookies={SESSION_COOKIE: cookie},
    )

    assert response.status_code == 200
    token = response.json()["token"]
    # The whole design: step-up issues the same credential the CLI issues, so
    # every protected route keeps its unchanged authenticate_human check.
    human = authenticate_human(
        session, f"Bearer {token}", allowed_roles={AppRole.DIRECTOR}
    )
    assert human.user.id == user.id


def test_step_up_needs_the_password_not_just_the_cookie(session, monkeypatch):
    """A session alone must never be enough to move money."""
    user = _operator(session)
    client = _client(monkeypatch, session)
    cookie, _ = mint_session(user)

    response = client.post(
        "/v1/auth/step-up",
        json={"password": "not the password"},
        cookies={SESSION_COOKIE: cookie},
    )
    assert response.status_code == 401


def test_step_up_requires_a_session(session, monkeypatch):
    _operator(session)
    client = _client(monkeypatch, session)

    response = client.post("/v1/auth/step-up", json={"password": PASSWORD})
    assert response.status_code == 401


def test_step_up_credential_is_short_lived(session, monkeypatch):
    user = _operator(session)
    client = _client(monkeypatch, session)
    cookie, _ = mint_session(user)

    response = client.post(
        "/v1/auth/step-up",
        json={"password": PASSWORD},
        cookies={SESSION_COOKIE: cookie},
    )

    expires = datetime.fromisoformat(response.json()["expires_at"])
    assert expires - datetime.now(UTC) <= timedelta(minutes=5, seconds=5)
