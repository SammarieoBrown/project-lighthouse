"""Finance-signed, confirmation-bound Act 3 settlement flow.

The release supports one execution adapter: an explicitly enabled demo
simulator.  It never contacts a payment rail and every API/ledger response says
so.  A real disbursement cannot be represented by this module; future provider
adapters must return an authenticated provider confirmation before they may set
``CONFIRMED``.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import desc, select, text
from sqlalchemy.orm import Session

from lighthouse_contracts import (
    ActorKind,
    AgentName,
    AppRole,
    ClaimStatus,
    DisbursementChannel,
    DisbursementStatus,
    Event,
    GateKind,
    StormFileState,
)

from . import ledger, statemachine
from .config import get_settings
from .db import session_scope
from .human_auth import AuthenticatedHuman, authenticate_human
from .models import (
    Allocation,
    AllocationPlan,
    AppUser,
    Approval,
    Claim,
    Disbursement,
    DisbursementBatch,
    LedgerEntry,
    StormFile,
)
from .settlement_executor import (
    ExecutorUnavailable,
    SIMULATED_EXECUTOR_PROVENANCE,
    SIMULATED_EXECUTOR_PROVIDER,
    SimulatedDemoExecutor,
    configured_executor,
)

router = APIRouter(prefix="/v1", tags=["disbursements"])

_CASH_GRANT = Decimal("45000.00")
_IDEMPOTENCY_MAX_LENGTH = 200
_SUPPORTED_CASH_CHANNELS = frozenset(
    {
        DisbursementChannel.BANK,
        DisbursementChannel.MOBILE_MONEY,
        DisbursementChannel.VOUCHER,
    }
)
_SETTLEMENT_READ_ROLES = {
    AppRole.DIRECTOR,
    AppRole.FINANCE_OFFICER,
    AppRole.AUDITOR,
}


class BatchSignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: DisbursementChannel
    executor_provenance: Literal["SIMULATED_DEMO"]
    note: str | None = Field(default=None, max_length=500)

    @field_validator("note")
    @classmethod
    def normalize_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_channel(self) -> BatchSignRequest:
        if self.channel not in _SUPPORTED_CASH_CHANNELS:
            raise ValueError("cash relief supports BANK, MOBILE_MONEY, or VOUCHER")
        return self


class SimulatedExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    executor_provenance: Literal["SIMULATED_DEMO"]
    acknowledge_no_real_money: Literal[True]


class FinanceSignerResponse(BaseModel):
    id: uuid.UUID
    display_name: str
    role: Literal["FINANCE_OFFICER"]


class BatchApprovalResponse(BaseModel):
    id: uuid.UUID
    gate: Literal["DISBURSEMENT_BATCH"]
    approved_by: FinanceSignerResponse
    approved_at: datetime
    reauthenticated_at: datetime


class SignedBatchResponse(BaseModel):
    id: uuid.UUID
    channel: DisbursementChannel
    total: Decimal
    currency: Literal["JMD"] = "JMD"
    snapshot_hash: str
    state: Literal["SIGNED"] = "SIGNED"


class PendingDisbursementResponse(BaseModel):
    id: uuid.UUID
    allocation_id: uuid.UUID
    batch_id: uuid.UUID
    channel: DisbursementChannel
    amount: Decimal
    currency: Literal["JMD"] = "JMD"
    status: DisbursementStatus
    simulated: Literal[True] = True
    executor_provider: Literal["LIGHTHOUSE_DEMO_EXECUTOR_V1"]
    executor_provenance: Literal["SIMULATED_DEMO"] = "SIMULATED_DEMO"
    snapshot_hash: str


class SettlementLedgerReceipt(BaseModel):
    seq: int
    hash: str
    action: str
    recorded_at: datetime


class BatchSignResponse(BaseModel):
    approval: BatchApprovalResponse
    batch: SignedBatchResponse
    disbursement: PendingDisbursementResponse
    ledger: SettlementLedgerReceipt
    money_movement: Literal["NOT_INITIATED"] = "NOT_INITIATED"
    no_real_money_moved: Literal[True] = True
    idempotent_replay: bool


class SimulatedExecutionResponse(BaseModel):
    disbursement: PendingDisbursementResponse
    execution_ledger: SettlementLedgerReceipt
    confirmation_ledger: SettlementLedgerReceipt
    provider_confirmation_ref: str
    provider_confirmation_hash: str
    confirmed_at: datetime
    money_movement: Literal["SIMULATED_CONFIRMATION_ONLY"] = (
        "SIMULATED_CONFIRMATION_ONLY"
    )
    no_real_money_moved: Literal[True] = True
    idempotent_replay: bool


class SettlementQueueItem(BaseModel):
    allocation_id: uuid.UUID
    claim_ref: str
    #: Null for goods. A tarpaulin coming off a shelf has no amount, and
    #: inventing one here would be a valuation nobody made.
    amount: Decimal | None
    currency: Literal["JMD"] = "JMD"
    #: Who funded it. GOV_RELIEF or a donor pool — an insurer settles with its
    #: policyholder and never appears on this queue.
    payer_route: Literal["GOV_RELIEF", "DONOR_POOL"] = "GOV_RELIEF"
    resource: Literal["CASH", "ITEM"] = "CASH"
    sku: str | None = None
    quantity: int | None = None
    #: PAY-04's headline number, per claim: filed -> first relief confirmed.
    #: Null until the claim settles, because until then there is no answer.
    time_to_relief_hours: float | None = None
    state: Literal[
        "AWAITING_FINANCE_SIGNATURE",
        "SIGNED_PENDING_SIMULATED_EXECUTION",
        "SIMULATED_EXECUTING",
        "SIMULATED_CONFIRMED",
        "SIMULATED_FAILED",
    ]
    batch_id: uuid.UUID | None
    disbursement_id: uuid.UUID | None
    channel: DisbursementChannel | None
    executor_provenance: Literal["SIMULATED_DEMO"] | None
    provider_confirmation_ref: str | None
    confirmed_at: datetime | None


class SettlementQueueResponse(BaseModel):
    settlements: list[SettlementQueueItem]
    execution: dict


class SettlementServiceError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class BatchSignOutcome:
    approval: Approval
    signer: AppUser
    batch: DisbursementBatch
    disbursement: Disbursement
    allocation: Allocation
    ledger_entry: LedgerEntry
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    disbursement: Disbursement
    allocation: Allocation
    executed_entry: LedgerEntry
    confirmed_entry: LedgerEntry
    idempotent_replay: bool


def _validate_idempotency_key(value: str | None) -> str:
    if value is None:
        raise SettlementServiceError(
            status.HTTP_400_BAD_REQUEST, "Idempotency-Key header is required"
        )
    if (
        value != value.strip()
        or not (1 <= len(value) <= _IDEMPOTENCY_MAX_LENGTH)
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise SettlementServiceError(
            status.HTTP_400_BAD_REQUEST,
            "Idempotency-Key must be 1-200 visible ASCII characters",
        )
    return value


def _idempotency_lock(session: Session, actor_id: uuid.UUID, key: str) -> None:
    digest = hashlib.sha256(f"settlement:{actor_id}:{key}".encode("utf-8")).digest()
    lock_key = int.from_bytes(digest[:8], byteorder="big", signed=True)
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})


def _batch_request_hash(
    allocation_id: uuid.UUID, request: BatchSignRequest
) -> str:
    return ledger.hash_payload(
        {
            "allocation_id": str(allocation_id),
            "request": request.model_dump(mode="json", exclude_none=False),
        }
    )


def _execution_request_hash(
    disbursement_id: uuid.UUID, request: SimulatedExecutionRequest
) -> str:
    return ledger.hash_payload(
        {
            "disbursement_id": str(disbursement_id),
            "request": request.model_dump(mode="json"),
        }
    )


def _receipt(
    *,
    action: Event,
    approval: Approval,
    batch: DisbursementBatch,
    disbursement: Disbursement,
    allocation: Allocation,
    money_movement: str,
    execution_request_hash: str | None = None,
    provider_confirmation_ref: str | None = None,
    provider_confirmation_hash: str | None = None,
) -> dict:
    payload = {
        "approval_id": str(approval.id),
        "batch_id": str(batch.id),
        "batch_snapshot_hash": batch.snapshot_hash,
        "disbursement_id": str(disbursement.id),
        "disbursement_snapshot_hash": disbursement.snapshot_hash,
        "allocation_id": str(allocation.id),
        "allocation_verification_snapshot_hash": (
            allocation.verification_snapshot_hash
        ),
        "gate": str(GateKind.DISBURSEMENT_BATCH),
        "resource": str(allocation.resource),
        "amount": f"{allocation.amount:.2f}",
        "currency": allocation.currency,
        "payer_route": str(allocation.payer_route),
        "channel": str(disbursement.channel),
        "executor_provider": disbursement.executor_provider,
        "executor_provenance": SIMULATED_EXECUTOR_PROVENANCE,
        "simulated": disbursement.simulated,
        "money_movement": money_movement,
    }
    if execution_request_hash is not None:
        payload["execution_request_hash"] = execution_request_hash
    if provider_confirmation_ref is not None:
        payload["provider_confirmation_ref"] = provider_confirmation_ref
    if provider_confirmation_hash is not None:
        payload["provider_confirmation_hash"] = provider_confirmation_hash
    payload["event"] = str(action)
    return payload


def _signed_batch_entry(
    session: Session, batch_id: uuid.UUID
) -> LedgerEntry | None:
    return session.scalar(
        select(LedgerEntry).where(
            LedgerEntry.action == str(Event.DISBURSEMENT_BATCH_SIGNED),
            LedgerEntry.subject_type == "disbursement_batch",
            LedgerEntry.subject_id == batch_id,
        )
    )


def _existing_batch_outcome(
    session: Session,
    approval: Approval,
    *,
    request_hash: str,
) -> BatchSignOutcome:
    if approval.request_hash != request_hash:
        raise SettlementServiceError(
            status.HTTP_409_CONFLICT,
            "Idempotency-Key was already used for a different request",
        )
    if (
        approval.gate is not GateKind.DISBURSEMENT_BATCH
        or approval.subject_type != "disbursement_batch"
    ):
        raise SettlementServiceError(
            status.HTTP_409_CONFLICT,
            "Idempotency-Key was already used for a different operation",
        )
    batch = session.scalar(
        select(DisbursementBatch).where(DisbursementBatch.approval_id == approval.id)
    )
    disbursement = (
        session.scalar(
            select(Disbursement).where(Disbursement.batch_id == batch.id)
        )
        if batch is not None
        else None
    )
    allocation = (
        session.get(Allocation, disbursement.allocation_id)
        if disbursement is not None
        else None
    )
    entry = _signed_batch_entry(session, batch.id) if batch is not None else None
    signer = session.get(AppUser, approval.approved_by)
    if any(value is None for value in (batch, disbursement, allocation, entry, signer)):
        raise RuntimeError(
            f"idempotent batch approval {approval.id} is missing signed records"
        )
    return BatchSignOutcome(
        approval=approval,
        signer=signer,
        batch=batch,
        disbursement=disbursement,
        allocation=allocation,
        ledger_entry=entry,
        idempotent_replay=True,
    )


def sign_disbursement_batch(
    session: Session,
    *,
    human: AuthenticatedHuman,
    allocation_id: uuid.UUID,
    request: BatchSignRequest,
    idempotency_key: str | None,
) -> BatchSignOutcome:
    """Bind one approved allocation to one Finance-signed demo batch."""
    key = _validate_idempotency_key(idempotency_key)
    request_hash = _batch_request_hash(allocation_id, request)
    _idempotency_lock(session, human.user.id, key)

    existing = session.scalar(
        select(Approval).where(
            Approval.approved_by == human.user.id,
            Approval.idempotency_key == key,
        )
    )
    if existing is not None:
        return _existing_batch_outcome(
            session,
            existing,
            request_hash=request_hash,
        )

    row = session.execute(
        select(Allocation, AllocationPlan, Claim, StormFile)
        .join(AllocationPlan, AllocationPlan.id == Allocation.plan_id)
        .join(Claim, Claim.id == Allocation.claim_id)
        .join(StormFile, StormFile.id == Claim.storm_file_id)
        .where(Allocation.id == allocation_id)
        .with_for_update(of=Allocation)
    ).one_or_none()
    if row is None:
        raise SettlementServiceError(status.HTTP_404_NOT_FOUND, "allocation not found")
    allocation, plan, claim, storm_file = row
    if plan.approval_id is None:
        raise SettlementServiceError(
            status.HTTP_409_CONFLICT, "allocation plan is not signed"
        )
    if claim.status is not ClaimStatus.VERIFIED:
        raise SettlementServiceError(
            status.HTTP_409_CONFLICT,
            "allocation claim must remain VERIFIED before batch signing",
        )
    if storm_file.state is not StormFileState.VERIFIED:
        raise SettlementServiceError(
            status.HTTP_409_CONFLICT,
            "owning storm file must remain VERIFIED before batch signing",
        )
    if allocation.amount != _CASH_GRANT or allocation.currency != "JMD":
        raise SettlementServiceError(
            status.HTTP_409_CONFLICT, "allocation is outside the fixed release policy"
        )
    if session.scalar(
        select(Disbursement.id)
        .where(Disbursement.allocation_id == allocation.id)
        .limit(1)
    ) is not None:
        raise SettlementServiceError(
            status.HTTP_409_CONFLICT,
            "allocation already belongs to a disbursement",
        )

    session.execute(
        text(
            "SET CONSTRAINTS disbursement_batch_receipt_complete_trigger, "
            "disbursement_receipt_complete_trigger DEFERRED"
        )
    )
    batch_id = uuid.uuid4()
    approval = Approval(
        id=uuid.uuid4(),
        gate=GateKind.DISBURSEMENT_BATCH,
        subject_type="disbursement_batch",
        subject_id=batch_id,
        approved_by=human.user.id,
        role_at_time=human.user.role,
        reauth_at=human.credential.reauthenticated_at,
        note=request.note,
        idempotency_key=key,
        request_hash=request_hash,
    )
    session.add(approval)
    session.flush()

    batch = DisbursementBatch(
        id=batch_id,
        channel=request.channel,
        total=_CASH_GRANT,
        approval_id=approval.id,
    )
    session.add(batch)
    session.flush()
    session.refresh(batch)

    disbursement = Disbursement(
        id=uuid.uuid4(),
        allocation_id=allocation.id,
        batch_id=batch.id,
        approval_id=approval.id,
        channel=request.channel,
        status=DisbursementStatus.PENDING,
        simulated=True,
        executor_provider=SIMULATED_EXECUTOR_PROVIDER,
    )
    session.add(disbursement)
    session.flush()
    session.refresh(disbursement)

    entry = ledger.append(
        session,
        action=str(Event.DISBURSEMENT_BATCH_SIGNED),
        subject_type="disbursement_batch",
        subject_id=batch.id,
        actor_kind=ActorKind.HUMAN,
        actor_id=human.user.id,
        payload=_receipt(
            action=Event.DISBURSEMENT_BATCH_SIGNED,
            approval=approval,
            batch=batch,
            disbursement=disbursement,
            allocation=allocation,
            money_movement="NOT_INITIATED_AT_BATCH_SIGNATURE",
        ),
    )
    session.execute(
        text(
            "SET CONSTRAINTS disbursement_batch_receipt_complete_trigger, "
            "disbursement_receipt_complete_trigger IMMEDIATE"
        )
    )
    return BatchSignOutcome(
        approval=approval,
        signer=human.user,
        batch=batch,
        disbursement=disbursement,
        allocation=allocation,
        ledger_entry=entry,
        idempotent_replay=False,
    )


def _execution_entries(
    session: Session, disbursement_id: uuid.UUID
) -> tuple[LedgerEntry | None, LedgerEntry | None]:
    executed = session.scalar(
        select(LedgerEntry).where(
            LedgerEntry.action == str(Event.DISBURSEMENT_EXECUTED),
            LedgerEntry.subject_type == "disbursement",
            LedgerEntry.subject_id == disbursement_id,
        )
    )
    confirmed = session.scalar(
        select(LedgerEntry).where(
            LedgerEntry.action == str(Event.DISBURSEMENT_CONFIRMED),
            LedgerEntry.subject_type == "disbursement",
            LedgerEntry.subject_id == disbursement_id,
        )
    )
    return executed, confirmed


def _existing_execution_outcome(
    session: Session,
    *,
    disbursement: Disbursement,
    human: AuthenticatedHuman,
    key: str,
    request_hash: str,
) -> ExecutionOutcome:
    if (
        disbursement.execution_requested_by != human.user.id
        or disbursement.execution_idempotency_key != key
        or disbursement.execution_request_hash != request_hash
    ):
        raise SettlementServiceError(
            status.HTTP_409_CONFLICT,
            "disbursement execution already has a different idempotent intent",
        )
    if disbursement.status is not DisbursementStatus.CONFIRMED:
        raise SettlementServiceError(
            status.HTTP_409_CONFLICT,
            f"disbursement is already {disbursement.status}",
        )
    executed, confirmed = _execution_entries(session, disbursement.id)
    allocation = session.get(Allocation, disbursement.allocation_id)
    if executed is None or confirmed is None or allocation is None:
        raise RuntimeError(
            f"confirmed disbursement {disbursement.id} is missing its receipts"
        )
    return ExecutionOutcome(
        disbursement=disbursement,
        allocation=allocation,
        executed_entry=executed,
        confirmed_entry=confirmed,
        idempotent_replay=True,
    )


def execute_simulated_disbursement(
    session: Session,
    *,
    human: AuthenticatedHuman,
    disbursement_id: uuid.UUID,
    request: SimulatedExecutionRequest,
    idempotency_key: str | None,
    executor: SimulatedDemoExecutor,
) -> ExecutionOutcome:
    """Execute and confirm locally, then settle the claim through the state machine."""
    key = _validate_idempotency_key(idempotency_key)
    request_hash = _execution_request_hash(disbursement_id, request)
    _idempotency_lock(session, human.user.id, key)

    key_owner = session.scalar(
        select(Disbursement).where(
            Disbursement.execution_requested_by == human.user.id,
            Disbursement.execution_idempotency_key == key,
        )
    )
    if key_owner is not None and key_owner.id != disbursement_id:
        raise SettlementServiceError(
            status.HTTP_409_CONFLICT,
            "Idempotency-Key was already used for a different disbursement",
        )

    row = session.execute(
        select(Disbursement, DisbursementBatch, Approval, Allocation, Claim, StormFile)
        .join(DisbursementBatch, DisbursementBatch.id == Disbursement.batch_id)
        .join(Approval, Approval.id == Disbursement.approval_id)
        .join(Allocation, Allocation.id == Disbursement.allocation_id)
        .join(Claim, Claim.id == Allocation.claim_id)
        .join(StormFile, StormFile.id == Claim.storm_file_id)
        .where(Disbursement.id == disbursement_id)
        .with_for_update(of=Disbursement)
    ).one_or_none()
    if row is None:
        raise SettlementServiceError(
            status.HTTP_404_NOT_FOUND, "disbursement not found"
        )
    disbursement, batch, approval, allocation, claim, storm_file = row
    if disbursement.status is not DisbursementStatus.PENDING:
        return _existing_execution_outcome(
            session,
            disbursement=disbursement,
            human=human,
            key=key,
            request_hash=request_hash,
        )
    if (
        approval.gate is not GateKind.DISBURSEMENT_BATCH
        or approval.subject_type != "disbursement_batch"
        or approval.subject_id != batch.id
        or batch.approval_id != approval.id
        or disbursement.approval_id != approval.id
    ):
        raise SettlementServiceError(
            status.HTTP_409_CONFLICT, "disbursement is not bound to its signed batch"
        )
    if (
        not disbursement.simulated
        or disbursement.executor_provider != executor.provider
        or request.executor_provenance != executor.provenance
    ):
        raise SettlementServiceError(
            status.HTTP_409_CONFLICT,
            "disbursement executor provenance does not match the signed batch",
        )
    if claim.status is not ClaimStatus.VERIFIED or storm_file.state is not StormFileState.VERIFIED:
        raise SettlementServiceError(
            status.HTTP_409_CONFLICT,
            "claim and storm file must remain VERIFIED before execution",
        )

    session.execute(
        text("SET CONSTRAINTS disbursement_receipt_complete_trigger DEFERRED")
    )
    executed_at = datetime.now(UTC)
    disbursement.status = DisbursementStatus.EXECUTING
    disbursement.execution_requested_by = human.user.id
    disbursement.execution_idempotency_key = key
    disbursement.execution_request_hash = request_hash
    disbursement.executed_at = executed_at
    session.flush()

    executed_entry = ledger.append(
        session,
        action=str(Event.DISBURSEMENT_EXECUTED),
        subject_type="disbursement",
        subject_id=disbursement.id,
        actor_kind=ActorKind.HUMAN,
        actor_id=human.user.id,
        payload=_receipt(
            action=Event.DISBURSEMENT_EXECUTED,
            approval=approval,
            batch=batch,
            disbursement=disbursement,
            allocation=allocation,
            money_movement="SIMULATION_EXECUTED_NO_REAL_FUNDS",
            execution_request_hash=request_hash,
        ),
    )
    session.execute(
        text("SET CONSTRAINTS disbursement_receipt_complete_trigger IMMEDIATE")
    )

    confirmation = executor.execute(
        disbursement_id=disbursement.id,
        disbursement_snapshot_hash=disbursement.snapshot_hash,
        request_hash=request_hash,
        amount=allocation.amount or Decimal("0"),
        currency=allocation.currency,
        channel=disbursement.channel,
        executed_at=executed_at,
    )

    session.execute(
        text("SET CONSTRAINTS disbursement_receipt_complete_trigger DEFERRED")
    )
    disbursement.status = DisbursementStatus.CONFIRMED
    disbursement.external_ref = confirmation.reference
    disbursement.provider_confirmation_hash = confirmation.receipt_hash
    disbursement.confirmed_at = confirmation.confirmed_at
    session.flush()

    confirmed_entry = ledger.append(
        session,
        action=str(Event.DISBURSEMENT_CONFIRMED),
        subject_type="disbursement",
        subject_id=disbursement.id,
        actor_kind=ActorKind.AGENT,
        agent=AgentName.LEDGER_AGENT,
        payload=_receipt(
            action=Event.DISBURSEMENT_CONFIRMED,
            approval=approval,
            batch=batch,
            disbursement=disbursement,
            allocation=allocation,
            money_movement="SIMULATED_CONFIRMATION_RECORDED_NO_REAL_FUNDS",
            execution_request_hash=request_hash,
            provider_confirmation_ref=confirmation.reference,
            provider_confirmation_hash=confirmation.receipt_hash,
        ),
    )
    session.execute(
        text("SET CONSTRAINTS disbursement_receipt_complete_trigger IMMEDIATE")
    )

    statemachine.transition_claim(
        session,
        claim,
        ClaimStatus.SETTLED,
        actor_kind=ActorKind.AGENT,
        agent=AgentName.LEDGER_AGENT,
        payload={
            "disbursement_provenance": SIMULATED_EXECUTOR_PROVENANCE,
            "no_real_money_moved": True,
        },
    )
    statemachine.transition(
        session,
        storm_file,
        StormFileState.SETTLED,
        actor_kind=ActorKind.AGENT,
        agent=AgentName.LEDGER_AGENT,
        payload={
            "disbursement_provenance": SIMULATED_EXECUTOR_PROVENANCE,
            "no_real_money_moved": True,
        },
        enqueue_follow_on=False,
    )
    return ExecutionOutcome(
        disbursement=disbursement,
        allocation=allocation,
        executed_entry=executed_entry,
        confirmed_entry=confirmed_entry,
        idempotent_replay=False,
    )


def list_settlements(session: Session, *, limit: int = 100) -> list[dict]:
    rows = session.execute(
        select(Allocation, Claim, Disbursement, DisbursementBatch)
        .join(AllocationPlan, AllocationPlan.id == Allocation.plan_id)
        .join(Claim, Claim.id == Allocation.claim_id)
        .outerjoin(Disbursement, Disbursement.allocation_id == Allocation.id)
        .outerjoin(DisbursementBatch, DisbursementBatch.id == Disbursement.batch_id)
        .where(AllocationPlan.approval_id.is_not(None))
        .order_by(desc(Allocation.created_at), desc(Allocation.id))
        .limit(limit)
    ).all()
    items: list[dict] = []
    for allocation, claim, disbursement, batch in rows:
        if disbursement is None:
            state = "AWAITING_FINANCE_SIGNATURE"
        elif disbursement.status is DisbursementStatus.PENDING:
            state = "SIGNED_PENDING_SIMULATED_EXECUTION"
        elif disbursement.status is DisbursementStatus.EXECUTING:
            state = "SIMULATED_EXECUTING"
        elif disbursement.status is DisbursementStatus.CONFIRMED:
            state = "SIMULATED_CONFIRMED"
        else:
            state = "SIMULATED_FAILED"
        items.append(
            {
                "allocation_id": allocation.id,
                "claim_ref": claim.claim_ref,
                "amount": allocation.amount,
                "currency": allocation.currency,
                "payer_route": str(allocation.payer_route),
                "resource": str(allocation.resource),
                "sku": allocation.sku,
                "quantity": allocation.quantity,
                "time_to_relief_hours": statemachine.time_to_relief_hours(claim),
                "state": state,
                "batch_id": batch.id if batch else None,
                "disbursement_id": disbursement.id if disbursement else None,
                "channel": disbursement.channel if disbursement else None,
                "executor_provenance": (
                    SIMULATED_EXECUTOR_PROVENANCE if disbursement else None
                ),
                "provider_confirmation_ref": (
                    disbursement.external_ref if disbursement else None
                ),
                "confirmed_at": disbursement.confirmed_at if disbursement else None,
            }
        )
    return items


def _ledger_response(entry: LedgerEntry) -> dict:
    return {
        "seq": entry.seq,
        "hash": entry.hash,
        "action": entry.action,
        "recorded_at": entry.ts,
    }


def _disbursement_response(
    disbursement: Disbursement, allocation: Allocation
) -> dict:
    return {
        "id": disbursement.id,
        "allocation_id": disbursement.allocation_id,
        "batch_id": disbursement.batch_id,
        "channel": disbursement.channel,
        "amount": allocation.amount,
        "currency": allocation.currency,
        "status": disbursement.status,
        "simulated": True,
        "executor_provider": disbursement.executor_provider,
        "executor_provenance": SIMULATED_EXECUTOR_PROVENANCE,
        "snapshot_hash": disbursement.snapshot_hash,
    }


def _batch_response(outcome: BatchSignOutcome) -> dict:
    return {
        "approval": {
            "id": outcome.approval.id,
            "gate": "DISBURSEMENT_BATCH",
            "approved_by": {
                "id": outcome.signer.id,
                "display_name": outcome.signer.display_name,
                "role": "FINANCE_OFFICER",
            },
            "approved_at": outcome.approval.approved_at,
            "reauthenticated_at": outcome.approval.reauth_at,
        },
        "batch": {
            "id": outcome.batch.id,
            "channel": outcome.batch.channel,
            "total": outcome.batch.total,
            "currency": "JMD",
            "snapshot_hash": outcome.batch.snapshot_hash,
            "state": "SIGNED",
        },
        "disbursement": _disbursement_response(
            outcome.disbursement, outcome.allocation
        ),
        "ledger": _ledger_response(outcome.ledger_entry),
        "money_movement": "NOT_INITIATED",
        "no_real_money_moved": True,
        "idempotent_replay": outcome.idempotent_replay,
    }


def _execution_response(outcome: ExecutionOutcome) -> dict:
    disbursement = outcome.disbursement
    if (
        disbursement.external_ref is None
        or disbursement.provider_confirmation_hash is None
        or disbursement.confirmed_at is None
    ):
        raise RuntimeError("confirmed disbursement is missing provider confirmation")
    return {
        "disbursement": _disbursement_response(disbursement, outcome.allocation),
        "execution_ledger": _ledger_response(outcome.executed_entry),
        "confirmation_ledger": _ledger_response(outcome.confirmed_entry),
        "provider_confirmation_ref": disbursement.external_ref,
        "provider_confirmation_hash": disbursement.provider_confirmation_hash,
        "confirmed_at": disbursement.confirmed_at,
        "money_movement": "SIMULATED_CONFIRMATION_ONLY",
        "no_real_money_moved": True,
        "idempotent_replay": outcome.idempotent_replay,
    }


@router.get("/settlements", response_model=SettlementQueueResponse)
def settlements_route(
    response: Response,
    authorization: str | None = Header(default=None),
    limit: int = Query(default=100, ge=1, le=200),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    with session_scope() as session:
        authenticate_human(
            session,
            authorization,
            allowed_roles=_SETTLEMENT_READ_ROLES,
        )
        settings = get_settings()
        enabled = settings.disbursement_executor_mode == "simulated"
        return {
            "settlements": list_settlements(session, limit=limit),
            "execution": {
                "enabled": enabled,
                "executor_provenance": (
                    SIMULATED_EXECUTOR_PROVENANCE if enabled else None
                ),
                "no_real_payment_provider": True,
            },
        }


@router.post(
    "/allocations/{allocation_id}/disbursements/sign",
    response_model=BatchSignResponse,
    status_code=status.HTTP_201_CREATED,
)
def sign_batch_route(
    allocation_id: uuid.UUID,
    request: BatchSignRequest,
    response: Response,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    with session_scope() as session:
        human = authenticate_human(
            session,
            authorization,
            allowed_roles={AppRole.FINANCE_OFFICER},
        )
        try:
            outcome = sign_disbursement_batch(
                session,
                human=human,
                allocation_id=allocation_id,
                request=request,
                idempotency_key=idempotency_key,
            )
        except SettlementServiceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        if outcome.idempotent_replay:
            response.status_code = status.HTTP_200_OK
        return _batch_response(outcome)


@router.post(
    "/disbursements/{disbursement_id}/execute",
    response_model=SimulatedExecutionResponse,
)
def execute_disbursement_route(
    disbursement_id: uuid.UUID,
    request: SimulatedExecutionRequest,
    response: Response,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    with session_scope() as session:
        human = authenticate_human(
            session,
            authorization,
            allowed_roles={AppRole.FINANCE_OFFICER},
        )
        try:
            executor = configured_executor(get_settings())
            outcome = execute_simulated_disbursement(
                session,
                human=human,
                disbursement_id=disbursement_id,
                request=request,
                idempotency_key=idempotency_key,
                executor=executor,
            )
        except ExecutorUnavailable as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc
        except SettlementServiceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        if outcome.idempotent_replay:
            response.status_code = status.HTTP_200_OK
        return _execution_response(outcome)


__all__ = [
    "BatchSignRequest",
    "BatchSignResponse",
    "SettlementQueueResponse",
    "SimulatedExecutionRequest",
    "SimulatedExecutionResponse",
    "execute_simulated_disbursement",
    "list_settlements",
    "router",
    "sign_disbursement_batch",
]
