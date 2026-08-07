"""Operator accounts, and the ledger record of who granted what.

These guard the property the module exists for: that authority cannot be
granted without leaving a chained record. A test that only checked the row was
created would pass against the bug this module was written to fix.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app import ledger
from app.models import AppUser, LedgerEntry
from app.operators import (
    LIVE_ROLES,
    OperatorError,
    change_role,
    create_operator,
    deactivate_operator,
)
from lighthouse_contracts import ActorKind, AppRole


def _entries(session, action: str) -> list[LedgerEntry]:
    return list(
        session.scalars(
            select(LedgerEntry).where(LedgerEntry.action == action).order_by(LedgerEntry.seq)
        )
    )


def _director(session, email="director@example.org") -> AppUser:
    create_operator(
        session,
        email=email,
        display_name="First Director",
        role=AppRole.DIRECTOR,
    )
    return session.scalars(
        select(AppUser).where(AppUser.email == email)
    ).one()


def test_first_account_is_genesis_and_says_so(session):
    result = create_operator(
        session,
        email="Director@Example.org",
        display_name="First Director",
        role=AppRole.DIRECTOR,
    )

    assert result.genesis is True
    assert result.email == "director@example.org"  # normalised

    entry = _entries(session, "user.created")[-1]
    assert entry.subject_type == "app_user"
    assert entry.subject_id == result.user_id
    # Nobody granted it, and the entry does not pretend otherwise.
    assert entry.actor_kind is ActorKind.SYSTEM
    assert entry.actor_id is None
    assert entry.payload["genesis"] is True
    assert "direct database access" in entry.payload["note"]


def test_second_account_without_a_granting_operator_is_refused(session):
    _director(session)

    with pytest.raises(OperatorError, match="only the first account"):
        create_operator(
            session,
            email="sneaky@example.org",
            display_name="Sneaky",
            role=AppRole.DIRECTOR,
        )


def test_granted_account_names_its_granter_in_the_chain(session):
    director = _director(session)

    result = create_operator(
        session,
        email="clerk@example.org",
        display_name="Review Clerk",
        role=AppRole.REVIEW_CLERK,
        granted_by=director,
    )

    entry = _entries(session, "user.created")[-1]
    assert entry.actor_kind is ActorKind.HUMAN
    assert entry.actor_id == director.id
    assert entry.payload["genesis"] is False
    assert entry.payload["granted_by_email"] == director.email
    assert entry.payload["role"] == AppRole.REVIEW_CLERK.value
    assert result.genesis is False


@pytest.mark.parametrize(
    "role",
    sorted(set(AppRole) - LIVE_ROLES, key=lambda r: r.value),
)
def test_roles_that_authorise_nothing_cannot_be_created(session, role):
    """An account that can sign in and do nothing is a trap, not a feature."""
    director = _director(session)

    with pytest.raises(OperatorError, match="authorises no route"):
        create_operator(
            session,
            email="dead@example.org",
            display_name="Dead Account",
            role=role,
            granted_by=director,
        )

    assert session.scalar(
        select(func.count()).select_from(AppUser).where(AppUser.email == "dead@example.org")
    ) == 0


def test_duplicate_email_is_refused(session):
    director = _director(session)

    with pytest.raises(OperatorError, match="already exists"):
        create_operator(
            session,
            email="DIRECTOR@example.org",
            display_name="Impostor",
            role=AppRole.AUDITOR,
            granted_by=director,
        )


def test_role_change_is_chained_and_cannot_be_self_applied(session):
    director = _director(session)
    create_operator(
        session,
        email="clerk@example.org",
        display_name="Review Clerk",
        role=AppRole.REVIEW_CLERK,
        granted_by=director,
    )

    change_role(
        session,
        email="clerk@example.org",
        role=AppRole.AUDITOR,
        granted_by=director,
    )
    entry = _entries(session, "user.role_changed")[-1]
    assert entry.payload["from_role"] == AppRole.REVIEW_CLERK.value
    assert entry.payload["to_role"] == AppRole.AUDITOR.value
    assert entry.actor_id == director.id

    # Separation of duties: nobody promotes themselves.
    with pytest.raises(OperatorError, match="own role"):
        change_role(
            session,
            email=director.email,
            role=AppRole.FINANCE_OFFICER,
            granted_by=director,
        )


def test_deactivation_is_chained_and_cannot_be_self_applied(session):
    director = _director(session)
    create_operator(
        session,
        email="clerk@example.org",
        display_name="Review Clerk",
        role=AppRole.REVIEW_CLERK,
        granted_by=director,
    )

    deactivate_operator(session, email="clerk@example.org", granted_by=director)

    clerk = session.scalars(
        select(AppUser).where(AppUser.email == "clerk@example.org")
    ).one()
    assert clerk.active is False
    entry = _entries(session, "user.deactivated")[-1]
    assert entry.actor_id == director.id
    assert entry.payload["email"] == "clerk@example.org"

    with pytest.raises(OperatorError, match="already inactive"):
        deactivate_operator(session, email="clerk@example.org", granted_by=director)

    with pytest.raises(OperatorError, match="own account"):
        deactivate_operator(session, email=director.email, granted_by=director)


def test_granting_authority_leaves_the_chain_valid(session):
    """The whole point: these entries are part of the same chain as the money."""
    director = _director(session)
    create_operator(
        session,
        email="finance@example.org",
        display_name="Finance Officer",
        role=AppRole.FINANCE_OFFICER,
        granted_by=director,
    )
    change_role(
        session,
        email="finance@example.org",
        role=AppRole.AUDITOR,
        granted_by=director,
    )
    deactivate_operator(session, email="finance@example.org", granted_by=director)
    session.flush()

    assert ledger.verify_chain(session) is True
