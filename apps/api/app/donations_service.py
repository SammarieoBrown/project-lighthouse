"""Donations: intake, pooling, draw-down, and the journey a donor can follow.

**The platform records and directs; it does not hold funds.** DON-01 puts the
money in a fiscal sponsor's account — a registered charity partner — and this
service records that it arrived and what it was earmarked for. Every donation
row carries ``simulated`` and it is true for the whole buildathon, because a
figure on a public page that looks like real money and is not is the single
most damaging thing this module could publish.

**Donor identity is a handle, never a name.** DON-02 makes pool activity
public in real time, and a public record of what arrived is a different
product from a public record of who gave it. The handle is what a donor
chooses to be known by; nothing here stores anything else about them.

**Pools are event-wide or parish, and nothing narrower.** Category scoping is
P1 by an explicit decision (PRD 11.3): finer pools fragment the money and
constrain the allocation agent before there is volume to justify either.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lighthouse_contracts import (
    ActorKind,
    DisbursementStatus,
    Event,
    PayerRoute,
    ResourceKind,
)

from app import ledger
from app.models import (
    Allocation,
    AllocationPlan,
    Claim,
    Disbursement,
    Donation,
    DonationPool,
    StormFile,
)

#: PRD 11.3. EVENT and PARISH only in P0.
POOL_SCOPES = ("EVENT", "PARISH")

#: The public portal never shows a bucket smaller than this (LGR-02). Applied
#: to the donor journey too: "your donation reached 3 households" is a much
#: smaller step from "which 3" than it looks.
MIN_AGGREGATION_BUCKET = 10


class DonationServiceError(RuntimeError):
    """Base class for safe, non-PII donation failures."""


class PoolNotFound(DonationServiceError):
    pass


class DonationRejected(DonationServiceError):
    pass


@dataclass(frozen=True, slots=True)
class DonationReceipt:
    donation: Donation
    pool_balance: Decimal


def _money(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def create_pool(
    session: Session, *, name: str, scope_kind: str, scope_value: str | None = None
) -> DonationPool:
    scope = scope_kind.strip().upper()
    if scope not in POOL_SCOPES:
        raise DonationRejected(f"pool scope must be one of {', '.join(POOL_SCOPES)}")
    if scope == "PARISH" and not scope_value:
        raise DonationRejected("a parish pool must name its parish")
    pool = DonationPool(
        name=name.strip(),
        scope_kind=scope,
        scope_value=scope_value.strip() if scope_value else None,
        balance=Decimal("0.00"),
    )
    session.add(pool)
    session.flush()
    return pool


def record_donation(
    session: Session,
    *,
    pool_id: uuid.UUID,
    donor_handle: str,
    amount: float | Decimal,
    currency: str = "JMD",
    simulated: bool = True,
    now: datetime | None = None,
) -> DonationReceipt:
    """Record money arriving and credit the pool (DON-01, DON-02).

    The row and the balance move in one transaction, so the public balance can
    never show money that has no donation behind it.
    """
    value = _money(amount)
    if value <= 0:
        raise DonationRejected("a donation must be a positive amount")
    handle = donor_handle.strip()
    if not handle:
        raise DonationRejected("a donor handle is required")

    pool = session.scalar(
        select(DonationPool).where(DonationPool.id == pool_id).with_for_update()
    )
    if pool is None:
        raise PoolNotFound("donation pool does not exist")

    donation = Donation(
        pool_id=pool.id,
        donor_handle=handle,
        amount=value,
        currency=currency.strip().upper(),
        simulated=simulated,
        received_at=now or datetime.now(UTC),
    )
    session.add(donation)
    pool.balance = (pool.balance or Decimal("0.00")) + value
    session.flush()

    ledger.append(
        session,
        action=str(Event.DONATION_RECEIVED),
        subject_type="donation",
        subject_id=donation.id,
        payload={
            "donation_id": str(donation.id),
            "pool_id": str(pool.id),
            "pool_name": pool.name,
            "scope_kind": pool.scope_kind,
            "scope_value": pool.scope_value,
            # A handle, never a name. See the module docstring.
            "donor_handle": handle,
            "amount": f"{value:.2f}",
            "currency": donation.currency,
            "pool_balance_after": f"{pool.balance:.2f}",
            # Said in every entry, because a public figure that looks like real
            # money and is not is the worst thing this module could publish.
            "simulated": simulated,
        },
        actor_kind=ActorKind.SYSTEM,
    )
    return DonationReceipt(donation=donation, pool_balance=pool.balance)


def draw_down(session: Session, pool_id: uuid.UUID, amount: Decimal) -> Decimal:
    """Spend from a pool against a signed allocation (DON-03).

    Locked and decremented in the signing transaction, the same shape as the
    stock decrement. ``donation_pool.balance >= 0`` is the backstop the row
    lock cannot be talked out of.
    """
    pool = session.scalar(
        select(DonationPool).where(DonationPool.id == pool_id).with_for_update()
    )
    if pool is None:
        raise PoolNotFound("donation pool does not exist")
    if (pool.balance or Decimal("0.00")) < amount:
        raise DonationRejected(
            f"pool holds {pool.balance or Decimal('0.00'):.2f} against a "
            f"{amount:.2f} allocation"
        )
    pool.balance = pool.balance - amount
    session.flush()
    return pool.balance


def pool_balances(session: Session) -> list[dict]:
    """DON-02. Public, real time, and aggregate only."""
    rows = session.execute(
        select(
            DonationPool.id,
            DonationPool.name,
            DonationPool.scope_kind,
            DonationPool.scope_value,
            DonationPool.balance,
            func.count(Donation.id).label("donations"),
            func.coalesce(func.sum(Donation.amount), 0).label("received"),
        )
        .outerjoin(Donation, Donation.pool_id == DonationPool.id)
        .group_by(DonationPool.id)
        .order_by(DonationPool.name)
    ).all()
    return [
        {
            "pool_id": str(row.id),
            "name": row.name,
            "scope_kind": row.scope_kind,
            "scope_value": row.scope_value,
            "balance": f"{row.balance:.2f}",
            "total_received": f"{row.received:.2f}",
            "donation_count": row.donations,
            "simulated": True,
        }
        for row in rows
    ]


def donor_journey(session: Session, donation_id: uuid.UUID) -> dict:
    """DON-04. Received, pooled, allocated, disbursed, confirmed.

    Households are counted, never named, and the parishes reached are listed
    only once the pool has served enough of them to be an aggregate rather
    than a description of somebody's address (LGR-02).
    """
    donation = session.get(Donation, donation_id)
    if donation is None:
        raise DonationRejected("donation does not exist")
    pool = session.get(DonationPool, donation.pool_id)

    funded = (
        select(Allocation.id, Allocation.claim_id, Allocation.resource, Allocation.sku)
        .join(AllocationPlan, AllocationPlan.id == Allocation.plan_id)
        .where(
            Allocation.pool_id == donation.pool_id,
            Allocation.payer_route == PayerRoute.DONOR_POOL,
            AllocationPlan.approval_id.is_not(None),
        )
        .subquery()
    )
    allocations = session.execute(select(funded)).all()
    household_count = len({row.claim_id for row in allocations})

    delivered = session.execute(
        select(
            func.count(Disbursement.id).label("confirmed"),
            func.min(Disbursement.confirmed_at).label("first_confirmed_at"),
        )
        .join(Allocation, Allocation.id == Disbursement.allocation_id)
        .where(
            Allocation.pool_id == donation.pool_id,
            Disbursement.status == DisbursementStatus.CONFIRMED,
        )
    ).one()

    parishes = session.scalars(
        select(StormFile.parish)
        .join(Claim, Claim.storm_file_id == StormFile.id)
        .join(Allocation, Allocation.claim_id == Claim.id)
        .where(Allocation.pool_id == donation.pool_id)
        .distinct()
    ).all()

    items = sorted({row.sku for row in allocations if row.resource is ResourceKind.ITEM and row.sku})

    return {
        "donation_id": str(donation.id),
        "donor_handle": donation.donor_handle,
        "simulated": donation.simulated,
        "received": {
            "amount": f"{donation.amount:.2f}",
            "currency": donation.currency,
            "at": donation.received_at.isoformat(),
        },
        "pooled": {
            "pool_id": str(pool.id),
            "pool_name": pool.name,
            "scope_kind": pool.scope_kind,
            "scope_value": pool.scope_value,
            "balance_now": f"{pool.balance:.2f}",
        },
        "allocated": {
            "household_count": household_count,
            "line_count": len(allocations),
            "items": items,
            # Withheld until the pool has served a real aggregate. Below the
            # bucket, "reached 2 households in Black River" is close to naming
            # an address.
            "parishes": (
                sorted(p for p in parishes if p)
                if household_count >= MIN_AGGREGATION_BUCKET
                else []
            ),
            "parishes_withheld_until_bucket": household_count < MIN_AGGREGATION_BUCKET,
        },
        "disbursed_and_confirmed": {
            "confirmed_count": delivered.confirmed or 0,
            "first_confirmed_at": (
                delivered.first_confirmed_at.isoformat()
                if delivered.first_confirmed_at
                else None
            ),
        },
    }


__all__ = [
    "MIN_AGGREGATION_BUCKET",
    "POOL_SCOPES",
    "DonationReceipt",
    "DonationRejected",
    "DonationServiceError",
    "PoolNotFound",
    "create_pool",
    "donor_journey",
    "draw_down",
    "pool_balances",
    "record_donation",
]
