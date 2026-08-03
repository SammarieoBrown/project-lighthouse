"""Authenticated, idempotent Act 3 allocation approval.

This module stops at an approved allocation. It does not create a disbursement
batch, call a payment provider, or invent an external transfer reference. The
response and ledger both name that boundary explicitly.
"""

from __future__ import annotations

import hashlib
import math
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import desc, select, text
from sqlalchemy.orm import Session

from lighthouse_contracts import (
    ActorKind,
    AgentName,
    AppRole,
    ClaimStatus,
    Event,
    GateKind,
    PayerRoute,
    ResourceKind,
    StormFileState,
    Verdict,
)

from . import ledger
from .db import session_scope
from .human_auth import AuthenticatedHuman, authenticate_human
from .models import (
    Allocation,
    AllocationPlan,
    Approval,
    Claim,
    LedgerEntry,
    StormFile,
    Verification,
)
from .public_taxonomy import (
    canonical_public_need_category,
    canonical_public_parish,
)

router = APIRouter(prefix="/v1", tags=["approvals"])

_CASH_GRANT = Decimal("45000.00")
_IDEMPOTENCY_MAX_LENGTH = 200
_SIGNAL_NAMES = frozenset(
    {
        "hazard_sufficiency",
        "satellite_change",
        "neighbour_corroboration",
        "registry_match",
        "media_integrity",
    }
)


class AllocationApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource: ResourceKind
    amount: Decimal | None = Field(default=None, max_digits=14, decimal_places=2)
    currency: str = Field(default="JMD", min_length=3, max_length=3)
    payer_route: PayerRoute
    sku: str | None = Field(default=None, max_length=120)
    quantity: int | None = Field(default=None, gt=0)
    note: str | None = Field(default=None, max_length=500)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        normalized = value.strip().upper()
        if len(normalized) != 3 or not normalized.isalpha() or not normalized.isascii():
            raise ValueError("currency must be a three-letter ISO-style code")
        return normalized

    @field_validator("sku", "note")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_resource_shape(self) -> AllocationApprovalRequest:
        if self.resource is not ResourceKind.CASH:
            raise ValueError("this release accepts CASH allocations only")
        if self.amount != _CASH_GRANT or self.currency != "JMD":
            raise ValueError("the current cash grant is exactly JMD 45000.00")
        if self.payer_route is not PayerRoute.GOV_RELIEF:
            raise ValueError("this release accepts GOV_RELIEF as the payer route only")
        if self.sku is not None or self.quantity is not None:
            raise ValueError("cash allocations cannot include sku or quantity")
        return self


class ApprovedByResponse(BaseModel):
    id: uuid.UUID
    display_name: str
    role: AppRole


class ApprovalResponse(BaseModel):
    id: uuid.UUID
    gate: Literal["ALLOCATION_PLAN"]
    approved_by: ApprovedByResponse
    approved_at: datetime
    reauthenticated_at: datetime


class AllocationResponse(BaseModel):
    id: uuid.UUID
    plan_id: uuid.UUID
    claim_id: uuid.UUID
    resource: ResourceKind
    amount: Decimal | None
    currency: str
    payer_route: PayerRoute
    state: Literal["APPROVED_NOT_DISBURSED"] = "APPROVED_NOT_DISBURSED"


class LedgerResponse(BaseModel):
    seq: int
    id: uuid.UUID
    hash: str
    action: Literal["allocation.approved"]
    recorded_at: datetime


class MoneyMovementResponse(BaseModel):
    status: Literal["NOT_INITIATED"] = "NOT_INITIATED"
    disbursement_id: None = None
    external_ref: None = None


class AllocationApprovalResponse(BaseModel):
    approval: ApprovalResponse
    allocation: AllocationResponse
    ledger: LedgerResponse
    money_movement: MoneyMovementResponse
    idempotent_replay: bool


class ApprovalServiceError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ApprovalOutcome:
    approval: Approval
    allocation: Allocation
    ledger_entry: LedgerEntry
    idempotent_replay: bool


def _latest_qualifying_verification(session: Session, claim_id: uuid.UUID) -> Verification:
    verification = session.scalar(
        select(Verification)
        .where(Verification.claim_id == claim_id)
        .order_by(desc(Verification.created_at), desc(Verification.id))
        .limit(1)
    )
    if verification is None:
        raise ApprovalServiceError(
            status.HTTP_409_CONFLICT,
            "claim has no verification eligible for allocation approval",
        )

    signals = verification.signals
    if not isinstance(signals, dict) or set(signals) != _SIGNAL_NAMES:
        raise ApprovalServiceError(
            status.HTTP_409_CONFLICT,
            "latest verification is not eligible for allocation approval",
        )
    if any(
        not isinstance(signals[name], dict)
        or not isinstance(signals[name].get("present"), bool)
        for name in _SIGNAL_NAMES
    ):
        raise ApprovalServiceError(
            status.HTTP_409_CONFLICT,
            "latest verification is not eligible for allocation approval",
        )

    confidence = verification.confidence
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float, Decimal))
        or not math.isfinite(float(confidence))
        or not 0 <= float(confidence) <= 1
    ):
        raise ApprovalServiceError(
            status.HTTP_409_CONFLICT,
            "latest verification is not eligible for allocation approval",
        )

    for name in _SIGNAL_NAMES:
        present = signals[name]["present"]
        score = signals[name].get("score")
        if present:
            if (
                isinstance(score, bool)
                or not isinstance(score, (int, float, Decimal))
                or not math.isfinite(float(score))
                or not 0 <= float(score) <= 1
            ):
                raise ApprovalServiceError(
                    status.HTTP_409_CONFLICT,
                    "latest verification is not eligible for allocation approval",
                )
        elif score is not None:
            raise ApprovalServiceError(
                status.HTTP_409_CONFLICT,
                "latest verification is not eligible for allocation approval",
            )

    if verification.verdict is Verdict.AUTO_VERIFIED:
        eligible = (
            verification.confidence >= 0.85
            and not verification.capped
            and all(signals[name]["present"] for name in _SIGNAL_NAMES)
            and verification.actor_kind is ActorKind.AGENT
            and verification.actor_id is None
            and verification.agent_name == str(AgentName.VERIFICATION_AGENT)
        )
    elif verification.verdict is Verdict.APPROVED:
        eligible = (
            verification.actor_kind is ActorKind.HUMAN
            and verification.actor_id is not None
        )
    else:
        eligible = False

    if not eligible or not verification.snapshot_hash:
        raise ApprovalServiceError(
            status.HTTP_409_CONFLICT,
            "latest verification is not eligible for allocation approval",
        )
    return verification


def allocation_ledger_payload(
    *,
    approval: Approval,
    plan: AllocationPlan,
    allocation: Allocation,
    claim: Claim,
    verification: Verification,
    parish: str,
    need_category: str,
    synthetic: bool,
) -> dict:
    """Build the immutable internal receipt; the public route re-whitelists it."""
    return {
        "approval_id": str(approval.id),
        "plan_id": str(plan.id),
        "allocation_id": str(allocation.id),
        "claim_id": str(claim.id),
        "verification_id": str(verification.id),
        "verification_snapshot_hash": verification.snapshot_hash,
        "gate": str(GateKind.ALLOCATION_PLAN),
        "parish": parish,
        "need_category": need_category,
        "resource": str(allocation.resource),
        "amount": f"{allocation.amount:.2f}",
        "currency": allocation.currency,
        "payer_route": str(allocation.payer_route),
        "synthetic": synthetic,
        "money_movement": "NOT_INITIATED_AT_APPROVAL",
    }


def _validate_idempotency_key(value: str | None) -> str:
    if value is None:
        raise ApprovalServiceError(
            status.HTTP_400_BAD_REQUEST, "Idempotency-Key header is required"
        )
    if (
        value != value.strip()
        or not (1 <= len(value) <= _IDEMPOTENCY_MAX_LENGTH)
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise ApprovalServiceError(
            status.HTTP_400_BAD_REQUEST,
            "Idempotency-Key must be 1-200 visible ASCII characters",
        )
    return value


def _request_hash(
    claim_id: uuid.UUID, request: AllocationApprovalRequest
) -> str:
    return ledger.hash_payload(
        {
            "claim_id": str(claim_id),
            "request": request.model_dump(mode="json", exclude_none=False),
        }
    )


def _idempotency_lock(session: Session, actor_id: uuid.UUID, key: str) -> None:
    digest = hashlib.sha256(f"{actor_id}:{key}".encode("utf-8")).digest()
    lock_key = int.from_bytes(digest[:8], byteorder="big", signed=True)
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})


def _existing_outcome(
    session: Session, approval: Approval, *, request_hash: str
) -> ApprovalOutcome:
    if approval.request_hash != request_hash:
        raise ApprovalServiceError(
            status.HTTP_409_CONFLICT,
            "Idempotency-Key was already used for a different request",
        )

    plan = session.scalar(
        select(AllocationPlan).where(AllocationPlan.approval_id == approval.id)
    )
    allocation = (
        session.scalar(select(Allocation).where(Allocation.plan_id == plan.id))
        if plan is not None
        else None
    )
    entry = (
        session.scalar(
            select(LedgerEntry).where(
                LedgerEntry.action == str(Event.ALLOCATION_APPROVED),
                LedgerEntry.subject_type == "allocation",
                LedgerEntry.subject_id == allocation.id,
            )
        )
        if allocation is not None
        else None
    )
    if allocation is None or entry is None:
        raise RuntimeError(
            f"idempotent approval {approval.id} is missing its allocation or ledger record"
        )
    return ApprovalOutcome(
        approval=approval,
        allocation=allocation,
        ledger_entry=entry,
        idempotent_replay=True,
    )


def approve_claim_allocation(
    session: Session,
    *,
    human: AuthenticatedHuman,
    claim_id: uuid.UUID,
    request: AllocationApprovalRequest,
    idempotency_key: str | None,
) -> ApprovalOutcome:
    """Create the signature, plan, allocation, and ledger row atomically."""
    key = _validate_idempotency_key(idempotency_key)
    request_hash = _request_hash(claim_id, request)
    _idempotency_lock(session, human.user.id, key)

    existing = session.scalar(
        select(Approval).where(
            Approval.approved_by == human.user.id,
            Approval.idempotency_key == key,
        )
    )
    if existing is not None:
        return _existing_outcome(session, existing, request_hash=request_hash)

    claim_row = session.execute(
        select(Claim, StormFile)
        .join(StormFile, StormFile.id == Claim.storm_file_id)
        .where(Claim.id == claim_id)
        .with_for_update(of=Claim)
    ).one_or_none()
    if claim_row is None:
        raise ApprovalServiceError(status.HTTP_404_NOT_FOUND, "claim not found")
    claim, storm_file = claim_row
    if claim.status is not ClaimStatus.VERIFIED:
        raise ApprovalServiceError(
            status.HTTP_409_CONFLICT,
            f"claim must be VERIFIED before allocation approval; current state is {claim.status}",
        )
    if storm_file.state not in {StormFileState.VERIFIED, StormFileState.SETTLED}:
        raise ApprovalServiceError(
            status.HTTP_409_CONFLICT,
            "owning storm file must be VERIFIED or SETTLED before allocation approval",
        )

    verification = _latest_qualifying_verification(session, claim.id)
    try:
        public_parish = canonical_public_parish(storm_file.parish)
        public_need_category = canonical_public_need_category(claim.damage_type)
    except ValueError as exc:
        raise ApprovalServiceError(
            status.HTTP_409_CONFLICT,
            "claim public classification is not supported for allocation approval",
        ) from exc

    duplicate_filters = [
        Allocation.claim_id == claim.id,
        Allocation.resource == request.resource,
        Allocation.payer_route == request.payer_route,
        AllocationPlan.approval_id.is_not(None),
    ]
    if request.resource is ResourceKind.ITEM:
        duplicate_filters.append(Allocation.sku == request.sku)
    duplicate = session.scalar(
        select(Allocation.id)
        .join(AllocationPlan, AllocationPlan.id == Allocation.plan_id)
        .where(*duplicate_filters)
        .limit(1)
    )
    if duplicate is not None:
        raise ApprovalServiceError(
            status.HTTP_409_CONFLICT,
            "an approved allocation already exists for this claim, resource, and payer route",
        )

    session.execute(
        text(
            "SET CONSTRAINTS signed_plan_complete_trigger, "
            "allocation_ledger_complete_trigger DEFERRED"
        )
    )
    plan_id = uuid.uuid4()
    approval = Approval(
        id=uuid.uuid4(),
        gate=GateKind.ALLOCATION_PLAN,
        subject_type="allocation_plan",
        subject_id=plan_id,
        approved_by=human.user.id,
        role_at_time=human.user.role,
        reauth_at=human.credential.reauthenticated_at,
        note=request.note,
        idempotency_key=key,
        request_hash=request_hash,
    )
    plan = AllocationPlan(
        id=plan_id,
        hazard_event_id=claim.hazard_event_id,
        proposed_by="manual_request",
        approval_id=approval.id,
    )
    # The approval points to a pre-generated polymorphic subject ID, while the
    # plan has the real foreign key back to the approval. Flush in that order;
    # there is intentionally no ORM relationship that could guess it for us.
    session.add(approval)
    session.flush()
    session.add(plan)
    session.flush()

    allocation = Allocation(
        id=uuid.uuid4(),
        plan_id=plan.id,
        claim_id=claim.id,
        resource=ResourceKind.CASH,
        sku=None,
        quantity=None,
        amount=_CASH_GRANT,
        currency="JMD",
        payer_route=PayerRoute.GOV_RELIEF,
        verification_id=verification.id,
        verification_snapshot_hash=verification.snapshot_hash,
    )
    session.add(allocation)
    session.flush()

    entry = ledger.append(
        session,
        action=str(Event.ALLOCATION_APPROVED),
        subject_type="allocation",
        subject_id=allocation.id,
        actor_kind=ActorKind.HUMAN,
        actor_id=human.user.id,
        payload=allocation_ledger_payload(
            approval=approval,
            plan=plan,
            allocation=allocation,
            claim=claim,
            verification=verification,
            parish=public_parish,
            need_category=public_need_category,
            synthetic=storm_file.synthetic,
        ),
    )
    # Both cross-row guarantees are deferred so the plan, allocation and ledger
    # can be created atomically in their natural order. Force them here so a
    # caller receives success only after PostgreSQL has checked the complete
    # signed receipt, even when the surrounding transaction remains open.
    session.execute(
        text(
            "SET CONSTRAINTS signed_plan_complete_trigger, "
            "allocation_ledger_complete_trigger IMMEDIATE"
        )
    )
    return ApprovalOutcome(
        approval=approval,
        allocation=allocation,
        ledger_entry=entry,
        idempotent_replay=False,
    )


def _response(outcome: ApprovalOutcome, human: AuthenticatedHuman) -> dict:
    return {
        "approval": {
            "id": outcome.approval.id,
            "gate": "ALLOCATION_PLAN",
            "approved_by": {
                "id": human.user.id,
                "display_name": human.user.display_name,
                "role": outcome.approval.role_at_time,
            },
            "approved_at": outcome.approval.approved_at,
            "reauthenticated_at": outcome.approval.reauth_at,
        },
        "allocation": {
            "id": outcome.allocation.id,
            "plan_id": outcome.allocation.plan_id,
            "claim_id": outcome.allocation.claim_id,
            "resource": outcome.allocation.resource,
            "amount": outcome.allocation.amount,
            "currency": outcome.allocation.currency,
            "payer_route": outcome.allocation.payer_route,
            "state": "APPROVED_NOT_DISBURSED",
        },
        "ledger": {
            "seq": outcome.ledger_entry.seq,
            "id": outcome.ledger_entry.id,
            "hash": outcome.ledger_entry.hash,
            "action": "allocation.approved",
            "recorded_at": outcome.ledger_entry.ts,
        },
        "money_movement": {
            "status": "NOT_INITIATED",
            "disbursement_id": None,
            "external_ref": None,
        },
        "idempotent_replay": outcome.idempotent_replay,
    }


@router.post(
    "/claims/{claim_id}/allocations/approve",
    response_model=AllocationApprovalResponse,
    status_code=status.HTTP_201_CREATED,
)
def approve_allocation_route(
    claim_id: uuid.UUID,
    request: AllocationApprovalRequest,
    response: Response,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    with session_scope() as session:
        human = authenticate_human(
            session,
            authorization,
            allowed_roles={AppRole.DIRECTOR},
        )
        try:
            outcome = approve_claim_allocation(
                session,
                human=human,
                claim_id=claim_id,
                request=request,
                idempotency_key=idempotency_key,
            )
        except ApprovalServiceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        if outcome.idempotent_replay:
            response.status_code = status.HTTP_200_OK
        return _response(outcome, human)


__all__ = [
    "AllocationApprovalRequest",
    "AllocationApprovalResponse",
    "allocation_ledger_payload",
    "approve_claim_allocation",
    "router",
]
