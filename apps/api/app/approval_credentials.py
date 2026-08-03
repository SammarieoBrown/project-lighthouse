"""Issue short-lived human API credentials without storing bearer secrets.

They are recent-authentication proof for protected human reads and approval
actions, not long-lived application sessions. The issuing command verifies an
existing active user's password, stores only a SHA-256 digest of a random token,
and displays the plaintext token once on the controlling terminal.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import secrets
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import session_scope
from .models import AppUser, HumanCredential

_PASSWORD_PREFIX = "scrypt"
_SCRYPT_N = 1 << 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_MAXMEM = 64 * 1024 * 1024
_MIN_PASSWORD_LENGTH = 12
_TOKEN_LIFETIME = timedelta(minutes=5)
_TOKEN_PREFIX = "lh_human_"


class CredentialError(Exception):
    """A credential operation was refused without exposing which check failed."""


@dataclass(frozen=True, slots=True)
class IssuedCredential:
    token: str
    credential_id: uuid.UUID
    expires_at: datetime


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_human_token(token: str) -> str:
    """Digest an opaque bearer token for lookup and at-rest storage."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    """Encode a password with stdlib scrypt and a fresh random salt."""
    if not (_MIN_PASSWORD_LENGTH <= len(password) <= 1024):
        raise CredentialError(
            f"password must be {_MIN_PASSWORD_LENGTH} to 1024 characters"
        )
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )
    return "$".join(
        (
            _PASSWORD_PREFIX,
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            _b64encode(salt),
            _b64encode(derived),
        )
    )


def verify_password(password: str, encoded: str | None) -> bool:
    """Verify without raising on malformed or unsupported stored hashes."""
    if not encoded:
        return False
    try:
        prefix, n_raw, r_raw, p_raw, salt_raw, expected_raw = encoded.split("$")
        if prefix != _PASSWORD_PREFIX:
            return False
        n, r, p = int(n_raw), int(r_raw), int(p_raw)
        # Refuse attacker-controlled work factors from a corrupted database.
        if (n, r, p) != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P):
            return False
        salt = _b64decode(salt_raw)
        expected = _b64decode(expected_raw)
        if len(salt) != 16 or len(expected) != _SCRYPT_DKLEN:
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
            maxmem=_SCRYPT_MAXMEM,
        )
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(actual, expected)


def _active_user_by_email(session: Session, email: str) -> AppUser:
    users = list(
        session.scalars(
            select(AppUser)
            .where(func.lower(AppUser.email) == email.strip().lower())
            .limit(2)
        )
    )
    if len(users) != 1 or not users[0].active:
        raise CredentialError("human authentication failed")
    return users[0]


def set_human_password(session: Session, *, email: str, password: str) -> AppUser:
    """Set an existing active user's password from an interactive operator."""
    user = _active_user_by_email(session, email)
    user.password_hash = hash_password(password)
    session.flush()
    return user


def issue_human_credential(
    session: Session,
    *,
    email: str,
    password: str,
    now: datetime | None = None,
) -> IssuedCredential:
    """Verify an active human user and mint one five-minute API credential."""
    user = _active_user_by_email(session, email)
    if not verify_password(password, user.password_hash):
        raise CredentialError("human authentication failed")

    reauthenticated_at = now or datetime.now(UTC)
    token = _TOKEN_PREFIX + secrets.token_urlsafe(32)
    credential = HumanCredential(
        user_id=user.id,
        token_hash=hash_human_token(token),
        reauthenticated_at=reauthenticated_at,
        expires_at=reauthenticated_at + _TOKEN_LIFETIME,
    )
    session.add(credential)
    session.flush()
    return IssuedCredential(
        token=token,
        credential_id=credential.id,
        expires_at=credential.expires_at,
    )


def _write_once_to_tty(message: str) -> None:
    """Bypass stdout/stderr so a bearer secret is not captured as command output."""
    try:
        with open("/dev/tty", "w", encoding="utf-8") as tty:
            tty.write(message)
            tty.flush()
    except OSError as exc:
        raise CredentialError(
            "a controlling terminal is required; the token was not emitted"
        ) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage short-lived Lighthouse human API credentials."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("set-password", "issue"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--email", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "set-password":
            password = getpass.getpass("New user password: ")
            confirmation = getpass.getpass("Confirm password: ")
            if not hmac.compare_digest(password, confirmation):
                raise CredentialError("passwords do not match")
            with session_scope() as session:
                set_human_password(session, email=args.email, password=password)
            _write_once_to_tty("User password updated.\n")
            return 0

        password = getpass.getpass("User password: ")
        with session_scope() as session:
            issued = issue_human_credential(
                session, email=args.email, password=password
            )
        _write_once_to_tty(
            "Bearer token (shown once; keep it in memory only):\n"
            f"{issued.token}\n"
            f"Expires at {issued.expires_at.isoformat()}\n"
        )
        return 0
    except CredentialError as exc:
        # This contains no password or token. Authentication failures are
        # intentionally generic so the CLI does not enumerate users.
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised as an operator command
    raise SystemExit(main())
