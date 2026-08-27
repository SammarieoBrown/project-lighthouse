"""The public donation surface (DON-01, DON-02, DON-04).

Unauthenticated on purpose: a donor is a member of the public, pool balances
are public by DON-02, and a journey view a donor cannot open without an
account is not a journey view. What keeps that safe is that nothing here
returns anything household-identifiable — counts and parishes, never names,
and parishes only above the aggregation bucket.

Every response says ``simulated: true``. The buildathon processor is a
simulation and a figure that looks like real money and is not is the worst
thing this surface could publish.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .db import session_scope
from .donations_service import (
    DonationRejected,
    PoolNotFound,
    donor_journey,
    pool_balances,
    record_donation,
)

router = APIRouter(prefix="/v1/public", tags=["donations"])


class DonationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pool_id: uuid.UUID
    #: What the donor chooses to be known by. Not validated as a real name,
    #: because it is deliberately not one.
    donor_handle: str = Field(min_length=1, max_length=60)
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    currency: str = Field(default="JMD", min_length=3, max_length=3)

    @field_validator("donor_handle")
    @classmethod
    def normalize_handle(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("a donor handle is required")
        return normalized


@router.get("/pools")
def pools_route(response: Response) -> dict:
    """DON-02. Real-time balances, aggregate only."""
    response.headers["Cache-Control"] = "no-store"
    with session_scope() as session:
        return {"pools": pool_balances(session), "simulated": True}


@router.post("/donations", status_code=status.HTTP_201_CREATED)
def donate_route(request: DonationRequest, response: Response) -> dict:
    """DON-01, simulated. The fiscal sponsor holds funds; we record and direct."""
    response.headers["Cache-Control"] = "no-store"
    with session_scope() as session:
        try:
            receipt = record_donation(
                session,
                pool_id=request.pool_id,
                donor_handle=request.donor_handle,
                amount=request.amount,
                currency=request.currency,
                simulated=True,
            )
        except PoolNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="pool not found"
            ) from exc
        except DonationRejected as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            ) from exc
        return {
            "donation_id": str(receipt.donation.id),
            "pool_id": str(receipt.donation.pool_id),
            "amount": f"{receipt.donation.amount:.2f}",
            "currency": receipt.donation.currency,
            "pool_balance": f"{receipt.pool_balance:.2f}",
            "simulated": True,
            "custody": "held by the fiscal sponsor; this platform records and directs",
        }


@router.get("/donations/{donation_id}/journey")
def journey_route(donation_id: uuid.UUID, response: Response) -> dict:
    """DON-04. Received, pooled, allocated, disbursed, confirmed."""
    response.headers["Cache-Control"] = "no-store"
    with session_scope() as session:
        try:
            return donor_journey(session, donation_id)
        except DonationRejected as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="donation not found"
            ) from exc


__all__ = ["DonationRequest", "donate_route", "journey_route", "pools_route", "router"]
