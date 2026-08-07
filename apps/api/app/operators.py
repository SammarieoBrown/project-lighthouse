"""Operator accounts, and the ledger record of who was granted what.

The platform's argument is that agents propose, humans dispose, and the ledger
remembers. Until this module existed the ledger remembered every exercise of
human authority and nothing about its *granting*: anyone with database access
could insert an ``AppUser`` with ``role = DIRECTOR``, set a password, mint a
credential and approve an allocation, and ``verify_chain()`` would pass over a
perfectly intact chain telling a false story. The approval would carry a
legitimate ``approved_by`` and nothing anywhere would record where that Director
came from.

So account creation, role changes and deactivation are chained entries. The
chain now covers the grant of authority as well as its exercise.

**Password changes are deliberately not recorded.** Changing a password is not a
change in what a person may do, and chaining routine credential hygiene would
bury the four entries that matter under noise. The line is drawn at authority.

Creating an account requires database access, so there is no ``ADMIN`` role
check here — it would be theatre. Somebody who can run this could equally issue
the INSERT themselves. What this module guarantees is that doing it *through the
supported path* leaves a record.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lighthouse_contracts import ActorKind, AppRole

from . import ledger
from .db import session_scope
from .models import AppUser

#: The roles that currently gate at least one route.
#:
#: The other five members of ``AppRole`` — ADMIN, PARISH_COORDINATOR,
#: SHELTER_MANAGER, FIELD_TEAM, INSURER_USER — are frozen in the contract and
#: authorise nothing. An account holding one of them would authenticate
#: successfully and then be refused by every endpoint, with no error explaining
#: why. Refusing to create it is kinder than creating a trap.
#:
#: This is a guard at the account boundary, not a contract change. When a role
#: acquires real permissions it joins this set and nothing else moves.
LIVE_ROLES = frozenset(
    {
        AppRole.DIRECTOR,
        AppRole.REVIEW_CLERK,
        AppRole.FINANCE_OFFICER,
        AppRole.AUDITOR,
    }
)

_SUBJECT = "app_user"


class OperatorError(Exception):
    """An account operation was refused."""


@dataclass(frozen=True, slots=True)
class OperatorChange:
    user_id: uuid.UUID
    email: str
    role: AppRole
    genesis: bool


def _normalise_email(email: str) -> str:
    value = email.strip().lower()
    if not value or "@" not in value or len(value) > 320:
        raise OperatorError("an operator email address is required")
    return value


def _require_live_role(role: AppRole) -> AppRole:
    if role not in LIVE_ROLES:
        live = ", ".join(sorted(r.value for r in LIVE_ROLES))
        raise OperatorError(
            f"{role.value} authorises no route today, so an account holding it "
            f"could sign in and do nothing. Live roles: {live}"
        )
    return role


def _by_email(session: Session, email: str) -> AppUser | None:
    return session.scalars(
        select(AppUser).where(func.lower(AppUser.email) == email).limit(1)
    ).one_or_none()


def _actor(granted_by: AppUser | None) -> dict:
    """Ledger actor fields. Genesis has no granting human, and says so."""
    if granted_by is None:
        return {"actor_kind": ActorKind.SYSTEM, "actor_id": None}
    return {"actor_kind": ActorKind.HUMAN, "actor_id": granted_by.id}


def create_operator(
    session: Session,
    *,
    email: str,
    display_name: str,
    role: AppRole,
    granted_by: AppUser | None = None,
) -> OperatorChange:
    """Create one operator account and chain the grant.

    ``granted_by`` is the human authorising this. It may be ``None`` only for
    the very first account in an empty database — the genesis case, which is
    recorded as exactly what it is: an account created by direct database
    access with no prior authority to appeal to. Every later account names the
    human who granted it.
    """
    address = _normalise_email(email)
    _require_live_role(role)
    name = display_name.strip()
    if not name:
        raise OperatorError("an operator display name is required")

    if _by_email(session, address) is not None:
        raise OperatorError(f"an account already exists for {address}")

    existing = session.scalar(select(func.count()).select_from(AppUser)) or 0
    genesis = existing == 0

    # A second account with nobody granting it is not genesis, it is an
    # unauthorised grant wearing genesis's clothes.
    if granted_by is None and not genesis:
        raise OperatorError(
            "only the first account may be created without a granting operator"
        )

    user = AppUser(email=address, display_name=name, role=role, active=True)
    session.add(user)
    session.flush()

    ledger.append(
        session,
        action="user.created",
        subject_type=_SUBJECT,
        subject_id=user.id,
        payload={
            "email": address,
            "display_name": name,
            "role": role.value,
            "genesis": genesis,
            **(
                {
                    "note": (
                        "First operator. Created by direct database access; no "
                        "prior authority existed to grant it."
                    )
                }
                if genesis
                else {"granted_by_email": granted_by.email if granted_by else None}
            ),
        },
        **_actor(granted_by),
    )
    return OperatorChange(user_id=user.id, email=address, role=role, genesis=genesis)


def change_role(
    session: Session,
    *,
    email: str,
    role: AppRole,
    granted_by: AppUser,
) -> OperatorChange:
    """Move an operator to another live role and chain the change."""
    address = _normalise_email(email)
    _require_live_role(role)
    user = _by_email(session, address)
    if user is None:
        raise OperatorError(f"no account exists for {address}")
    if user.id == granted_by.id:
        raise OperatorError("an operator cannot change their own role")

    previous = user.role
    if previous is role:
        raise OperatorError(f"{address} already holds {role.value}")

    user.role = role
    session.flush()

    ledger.append(
        session,
        action="user.role_changed",
        subject_type=_SUBJECT,
        subject_id=user.id,
        payload={
            "email": address,
            "from_role": previous.value,
            "to_role": role.value,
            "granted_by_email": granted_by.email,
        },
        **_actor(granted_by),
    )
    return OperatorChange(user_id=user.id, email=address, role=role, genesis=False)


def deactivate_operator(
    session: Session,
    *,
    email: str,
    granted_by: AppUser,
) -> OperatorChange:
    """Deactivate an operator and chain it.

    No sessions or credentials are deleted here and none need to be:
    ``authenticate_human`` refuses an inactive user, and the session cookie is
    validated against the same flag on every request. Revocation is one boolean
    and it takes effect on the next call.
    """
    address = _normalise_email(email)
    user = _by_email(session, address)
    if user is None:
        raise OperatorError(f"no account exists for {address}")
    if user.id == granted_by.id:
        raise OperatorError("an operator cannot deactivate their own account")
    if not user.active:
        raise OperatorError(f"{address} is already inactive")

    user.active = False
    session.flush()

    ledger.append(
        session,
        action="user.deactivated",
        subject_type=_SUBJECT,
        subject_id=user.id,
        payload={
            "email": address,
            "role": user.role.value,
            "granted_by_email": granted_by.email,
        },
        **_actor(granted_by),
    )
    return OperatorChange(
        user_id=user.id, email=address, role=user.role, genesis=False
    )


def _resolve_granting(session: Session, email: str | None) -> AppUser | None:
    if email is None:
        return None
    user = _by_email(session, _normalise_email(email))
    if user is None or not user.active:
        raise OperatorError("the granting operator must be an active account")
    return user


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Manage Lighthouse operator accounts. Every change is written to "
            "the ledger."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="create an operator account")
    create.add_argument("--email", required=True)
    create.add_argument("--name", required=True)
    create.add_argument(
        "--role",
        required=True,
        choices=sorted(r.value for r in LIVE_ROLES),
    )
    create.add_argument(
        "--granted-by",
        help=(
            "email of the operator authorising this. Omit only for the first "
            "account in an empty database."
        ),
    )

    role = sub.add_parser("set-role", help="move an operator to another role")
    role.add_argument("--email", required=True)
    role.add_argument("--role", required=True, choices=sorted(r.value for r in LIVE_ROLES))
    role.add_argument("--granted-by", required=True)

    off = sub.add_parser("deactivate", help="deactivate an operator account")
    off.add_argument("--email", required=True)
    off.add_argument("--granted-by", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        with session_scope() as session:
            granting = _resolve_granting(session, getattr(args, "granted_by", None))
            if args.command == "create":
                result = create_operator(
                    session,
                    email=args.email,
                    display_name=args.name,
                    role=AppRole(args.role),
                    granted_by=granting,
                )
                where = " (genesis)" if result.genesis else ""
                print(f"Created {result.email} as {result.role.value}{where}.")
                print(
                    "Set a password before they can sign in:\n"
                    f"  uv run python -m app.approval_credentials set-password "
                    f"--email {result.email}"
                )
            elif args.command == "set-role":
                result = change_role(
                    session,
                    email=args.email,
                    role=AppRole(args.role),
                    granted_by=granting,  # type: ignore[arg-type]
                )
                print(f"{result.email} now holds {result.role.value}.")
            else:
                result = deactivate_operator(
                    session,
                    email=args.email,
                    granted_by=granting,  # type: ignore[arg-type]
                )
                print(f"{result.email} deactivated.")
    except OperatorError as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
