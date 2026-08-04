"""Public, PII-safe ledger and aggregate relief proof.

Only three household-money milestones are publishable: allocation approved (no
movement), demo execution initiated, and demo confirmation recorded.  The
serializer rebuilds each response from a closed field allowlist and never emits
internal IDs, claim references, provider references, household classifications,
or exact timestamps.
"""

from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass
from datetime import UTC, date
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from lighthouse_contracts import (
    DisbursementChannel,
    Event,
    GateKind,
    PayerRoute,
    ResourceKind,
)

from . import ledger
from .db import session_scope
from .models import LedgerEntry
from .public_taxonomy import validate_public_taxonomy
from .settlement_executor import (
    SIMULATED_EXECUTOR_PROVENANCE,
    SIMULATED_EXECUTOR_PROVIDER,
)

router = APIRouter(prefix="/v1/public", tags=["public-ledger"])

_AGGREGATE_CACHE_TTL_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class _AggregateCache:
    head: tuple[int | None, str | None]
    expires_at: float
    value: dict


_aggregate_cache: _AggregateCache | None = None
_aggregate_cache_lock = threading.Lock()


class PublicAllocation(BaseModel):
    resource: ResourceKind
    amount: Decimal
    currency: str
    payer_route: PayerRoute
    synthetic: bool


class PublicApproval(BaseModel):
    gate: Literal["ALLOCATION_PLAN"] = "ALLOCATION_PLAN"


class PublicAllocationMoneyMovement(BaseModel):
    status: Literal["NOT_INITIATED_AT_APPROVAL"] = "NOT_INITIATED_AT_APPROVAL"


class PublicSettlement(BaseModel):
    resource: ResourceKind
    amount: Decimal
    currency: str
    payer_route: PayerRoute
    channel: DisbursementChannel
    executor_provenance: Literal["SIMULATED_DEMO"] = "SIMULATED_DEMO"
    simulated: Literal[True] = True


class PublicExecutedMoneyMovement(BaseModel):
    status: Literal["SIMULATION_EXECUTED_NO_REAL_FUNDS"] = (
        "SIMULATION_EXECUTED_NO_REAL_FUNDS"
    )


class PublicConfirmedMoneyMovement(BaseModel):
    status: Literal["SIMULATED_CONFIRMATION_RECORDED_NO_REAL_FUNDS"] = (
        "SIMULATED_CONFIRMATION_RECORDED_NO_REAL_FUNDS"
    )


class PublicAllocationLedgerEntry(BaseModel):
    seq: int
    prev_hash: str | None
    hash: str
    payload_hash: str
    action: Literal["allocation.approved"]
    recorded_on: date
    allocation: PublicAllocation
    approval: PublicApproval
    money_movement: PublicAllocationMoneyMovement


class PublicExecutionLedgerEntry(BaseModel):
    seq: int
    prev_hash: str | None
    hash: str
    payload_hash: str
    action: Literal["disbursement.executed"]
    recorded_on: date
    settlement: PublicSettlement
    money_movement: PublicExecutedMoneyMovement


class PublicConfirmationLedgerEntry(BaseModel):
    seq: int
    prev_hash: str | None
    hash: str
    payload_hash: str
    action: Literal["disbursement.confirmed"]
    recorded_on: date
    settlement: PublicSettlement
    money_movement: PublicConfirmedMoneyMovement


PublicLedgerEntry = Annotated[
    PublicAllocationLedgerEntry
    | PublicExecutionLedgerEntry
    | PublicConfirmationLedgerEntry,
    Field(discriminator="action"),
]


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


class PublicConfirmedReliefBucket(BaseModel):
    channel: DisbursementChannel
    count: int
    amount: Decimal
    currency: Literal["JMD"] = "JMD"
    executor_provenance: Literal["SIMULATED_DEMO"] = "SIMULATED_DEMO"


class PublicConfirmedReliefAggregate(BaseModel):
    scope: Literal["CONFIRMED_SIMULATED_RELIEF_ONLY"] = (
        "CONFIRMED_SIMULATED_RELIEF_ONLY"
    )
    count: int
    amount: Decimal
    currency: Literal["JMD"] = "JMD"
    no_real_money_moved: Literal[True] = True
    by_channel: list[PublicConfirmedReliefBucket]


class PublicLedgerResponse(BaseModel):
    entries: list[PublicLedgerEntry]
    aggregate: PublicConfirmedReliefAggregate
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


def _recorded_on(row: LedgerEntry) -> date:
    if row.ts.tzinfo is None or row.ts.utcoffset() is None:
        raise ValueError("ledger timestamp has no timezone")
    return row.ts.astimezone(UTC).date()


def _release(payload: dict) -> dict:
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
        raise ValueError("unexpected fixed relief grant")
    return {
        "resource": resource,
        "amount": amount,
        "currency": currency,
        "payer_route": payer_route,
    }


def _public_allocation_entry(row: LedgerEntry, payload: dict) -> dict:
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
    allocation = {**_release(payload), "synthetic": synthetic}
    return {
        "seq": row.seq,
        "prev_hash": row.prev_hash,
        "hash": row.hash,
        "payload_hash": row.payload_hash,
        "action": "allocation.approved",
        "recorded_on": _recorded_on(row),
        "allocation": allocation,
        "approval": {"gate": "ALLOCATION_PLAN"},
        "money_movement": {"status": "NOT_INITIATED_AT_APPROVAL"},
    }


def _public_settlement_entry(row: LedgerEntry, payload: dict) -> dict:
    action = row.action
    if payload.get("event") != action:
        raise ValueError("event identity mismatch")
    if payload.get("gate") != str(GateKind.DISBURSEMENT_BATCH):
        raise ValueError("unexpected settlement gate")
    if payload.get("executor_provider") != SIMULATED_EXECUTOR_PROVIDER:
        raise ValueError("unexpected executor provider")
    if payload.get("executor_provenance") != SIMULATED_EXECUTOR_PROVENANCE:
        raise ValueError("unexpected executor provenance")
    if payload.get("simulated") is not True:
        raise ValueError("settlement is not explicitly simulated")
    movement = {
        str(Event.DISBURSEMENT_EXECUTED): "SIMULATION_EXECUTED_NO_REAL_FUNDS",
        str(Event.DISBURSEMENT_CONFIRMED): (
            "SIMULATED_CONFIRMATION_RECORDED_NO_REAL_FUNDS"
        ),
    }.get(action)
    if movement is None or payload.get("money_movement") != movement:
        raise ValueError("unexpected settlement movement state")
    channel = DisbursementChannel(payload["channel"])
    if channel is DisbursementChannel.GOODS:
        raise ValueError("cash settlement cannot use the GOODS channel")
    settlement = {
        **_release(payload),
        "channel": channel,
        "executor_provenance": "SIMULATED_DEMO",
        "simulated": True,
    }
    return {
        "seq": row.seq,
        "prev_hash": row.prev_hash,
        "hash": row.hash,
        "payload_hash": row.payload_hash,
        "action": action,
        "recorded_on": _recorded_on(row),
        "settlement": settlement,
        "money_movement": {"status": movement},
    }


def _public_entry(row: LedgerEntry) -> dict:
    """Whitelist fields; never serialize an arbitrary internal payload."""
    if row.subject_id is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"public ledger entry {row.seq} is not publishable",
        )
    payload = row.payload or {}
    try:
        if row.action == str(Event.ALLOCATION_APPROVED):
            return _public_allocation_entry(row, payload)
        return _public_settlement_entry(row, payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"public ledger entry {row.seq} is not publishable",
        ) from exc


def clear_aggregate_cache() -> None:
    global _aggregate_cache
    with _aggregate_cache_lock:
        _aggregate_cache = None


def _confirmed_aggregate(
    session,
    *,
    head: tuple[int | None, str | None],
) -> dict:
    """Aggregate immutable confirmed receipts, never mutable household rows."""
    global _aggregate_cache
    now = time.monotonic()
    with _aggregate_cache_lock:
        cached = _aggregate_cache
        if cached is not None and cached.head == head and cached.expires_at > now:
            return copy.deepcopy(cached.value)

    rows = list(
        session.scalars(
            select(LedgerEntry)
            .where(LedgerEntry.action == str(Event.DISBURSEMENT_CONFIRMED))
            .order_by(LedgerEntry.seq)
        )
    )
    grouped: dict[DisbursementChannel, dict] = {}
    for row in rows:
        public = _public_settlement_entry(row, row.payload or {})
        settlement = public["settlement"]
        channel = settlement["channel"]
        bucket = grouped.setdefault(
            channel,
            {
                "channel": channel,
                "count": 0,
                "amount": Decimal("0.00"),
                "currency": "JMD",
                "executor_provenance": "SIMULATED_DEMO",
            },
        )
        bucket["count"] += 1
        bucket["amount"] += settlement["amount"]
    buckets = [grouped[channel] for channel in sorted(grouped, key=str)]
    value = {
        "scope": "CONFIRMED_SIMULATED_RELIEF_ONLY",
        "count": sum(bucket["count"] for bucket in buckets),
        "amount": sum(
            (bucket["amount"] for bucket in buckets),
            start=Decimal("0.00"),
        ),
        "currency": "JMD",
        "no_real_money_moved": True,
        "by_channel": buckets,
    }
    with _aggregate_cache_lock:
        _aggregate_cache = _AggregateCache(
            head=head,
            expires_at=now + _AGGREGATE_CACHE_TTL_SECONDS,
            value=copy.deepcopy(value),
        )
    return value


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
                    LedgerEntry.action.in_(
                        (
                            str(Event.ALLOCATION_APPROVED),
                            str(Event.DISBURSEMENT_EXECUTED),
                            str(Event.DISBURSEMENT_CONFIRMED),
                        )
                    ),
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
            "aggregate": _confirmed_aggregate(
                session,
                head=(head_seq, head_hash),
            ),
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


__all__ = [
    "PublicLedgerResponse",
    "clear_aggregate_cache",
    "read_public_ledger",
    "router",
]
