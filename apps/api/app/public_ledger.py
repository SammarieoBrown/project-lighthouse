"""Public, PII-safe read surface over allowlisted ledger records."""

from __future__ import annotations

from datetime import UTC, date
from decimal import Decimal, InvalidOperation
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select

from lighthouse_contracts import Event, GateKind, PayerRoute, ResourceKind

from . import ledger
from .db import session_scope
from .models import LedgerEntry
from .public_taxonomy import validate_public_taxonomy

router = APIRouter(prefix="/v1/public", tags=["public-ledger"])


class PublicAllocation(BaseModel):
    resource: ResourceKind
    amount: Decimal
    currency: str
    payer_route: PayerRoute
    synthetic: bool


class PublicApproval(BaseModel):
    gate: Literal["ALLOCATION_PLAN"] = "ALLOCATION_PLAN"


class PublicMoneyMovement(BaseModel):
    status: Literal["NOT_INITIATED_AT_APPROVAL"] = "NOT_INITIATED_AT_APPROVAL"


class PublicLedgerEntry(BaseModel):
    seq: int
    prev_hash: str | None
    hash: str
    payload_hash: str
    action: Literal["allocation.approved"]
    recorded_on: date
    allocation: PublicAllocation
    approval: PublicApproval
    money_movement: PublicMoneyMovement


class PublicLedgerPage(BaseModel):
    after_seq: int
    limit: int
    next_after_seq: int
    has_more: bool


class PublicLedgerChain(BaseModel):
    valid: bool
    algorithm: Literal["SHA-256"] = "SHA-256"
    scope: Literal["FULL_INTERNAL_LEDGER"] = "FULL_INTERNAL_LEDGER"
    head_seq: int | None
    head_hash: str | None


class PublicLedgerResponse(BaseModel):
    entries: list[PublicLedgerEntry]
    page: PublicLedgerPage
    chain: PublicLedgerChain


def _currency(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 3
        or not value.isascii()
        or not value.isalpha()
        or value != value.upper()
    ):
        raise ValueError("invalid currency")
    return value


def _money(value: object) -> Decimal:
    if not isinstance(value, (str, int, float, Decimal)) or isinstance(value, bool):
        raise ValueError("expected a decimal amount")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid decimal amount") from exc
    if not amount.is_finite():
        raise ValueError("amount must be finite")
    return amount


def _public_entry(row: LedgerEntry) -> dict:
    """Whitelist fields; never serialize an arbitrary internal payload."""
    payload = row.payload or {}
    try:
        if payload.get("gate") != str(GateKind.ALLOCATION_PLAN):
            raise ValueError("unexpected gate")
        if payload.get("money_movement") != "NOT_INITIATED_AT_APPROVAL":
            raise ValueError("unexpected money-movement state")
        validate_public_taxonomy(
            parish=payload["parish"],
            need_category=payload["need_category"],
        )
        synthetic = payload.get("synthetic")
        if not isinstance(synthetic, bool):
            raise ValueError("synthetic flag is missing")
        resource = ResourceKind(payload["resource"])
        amount = _money(payload.get("amount"))
        currency = _currency(payload["currency"])
        payer_route = PayerRoute(payload["payer_route"])
        if (
            resource is not ResourceKind.CASH
            or amount != Decimal("45000.00")
            or currency != "JMD"
            or payer_route is not PayerRoute.GOV_RELIEF
        ):
            raise ValueError("unexpected allocation grant")
        allocation = {
            "resource": resource,
            "amount": amount,
            "currency": currency,
            "payer_route": payer_route,
            "synthetic": synthetic,
        }
        if row.ts.tzinfo is None or row.ts.utcoffset() is None:
            raise ValueError("ledger timestamp has no timezone")
        recorded_on = row.ts.astimezone(UTC).date()
    except (KeyError, TypeError, ValueError) as exc:
        # A public view must fail closed instead of fabricating a benign-looking
        # value for an internal entry whose shape no longer matches the event.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"public ledger entry {row.seq} is not publishable",
        ) from exc

    if row.subject_id is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"public ledger entry {row.seq} is not publishable",
        )

    return {
        "seq": row.seq,
        "prev_hash": row.prev_hash,
        "hash": row.hash,
        "payload_hash": row.payload_hash,
        "action": "allocation.approved",
        "recorded_on": recorded_on,
        "allocation": allocation,
        "approval": {"gate": "ALLOCATION_PLAN"},
        "money_movement": {"status": "NOT_INITIATED_AT_APPROVAL"},
    }


@router.get("/ledger", response_model=PublicLedgerResponse)
def read_public_ledger(
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    latest: bool = Query(default=False),
) -> dict:
    with session_scope() as session:
        ordering = LedgerEntry.seq.desc() if latest else LedgerEntry.seq.asc()
        candidates = list(
            session.scalars(
                select(LedgerEntry)
                .where(
                    LedgerEntry.seq > after_seq,
                    LedgerEntry.action == str(Event.ALLOCATION_APPROVED),
                    LedgerEntry.subject_type == "allocation",
                )
                .order_by(ordering)
                .limit(limit + 1)
            )
        )
        has_more = len(candidates) > limit
        rows = candidates[:limit]
        if latest:
            rows.reverse()
        head_seq, head_hash = ledger.chain_head(session)
        chain_valid = ledger.cached_verify_chain(
            session,
            head=(head_seq, head_hash),
        )
        if not chain_valid:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="ledger integrity check failed; public entries withheld",
            )

        return {
            "entries": [_public_entry(row) for row in rows],
            "page": {
                "after_seq": after_seq,
                "limit": limit,
                "next_after_seq": rows[-1].seq if rows else after_seq,
                "has_more": has_more,
            },
            "chain": {
                "valid": chain_valid,
                "algorithm": "SHA-256",
                "scope": "FULL_INTERNAL_LEDGER",
                "head_seq": head_seq,
                "head_hash": head_hash,
            },
        }


__all__ = ["PublicLedgerResponse", "read_public_ledger", "router"]
