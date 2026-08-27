"""Reconciliation and anomaly flagging (LGR-04).

The one agent that audits the others. It reads what the money path recorded,
looks for the four ways that record can be wrong, and writes what it finds
into the same append-only ledger it is auditing — LGR-04 requires the flags be
ledger entries themselves, so a missed payment and the discovery of a missed
payment are both permanent.

**It never resolves its own findings.** That is written into the contract, and
it is the whole reason this agent is allowed to be autonomous: an auditor that
can close its own findings is not an auditor. Nothing here updates a status,
retries a payment, or clears a flag. It observes and it tells a human.

Its transition authority (T8, C6) is exercised elsewhere — the confirmation
endpoint moves a household to SETTLED at the moment a disbursement confirms,
which is where that belongs. What was missing until now is the audit.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lighthouse_contracts import (
    ActorKind,
    AgentName,
    DisbursementChannel,
    DisbursementStatus,
    Event,
    ResourceKind,
    StormFileState,
)
from lighthouse_contracts.agents import AnomalyFlag, LedgerAgentOutput

from app import ledger
from app.models import (
    Allocation,
    Claim,
    Disbursement,
    LedgerEntry,
    StormFile,
)

#: How long a disbursement may sit mid-execution before it is worth a human's
#: attention. Generous on purpose: the point is to catch the payment that
#: silently never landed, not to page someone about a slow afternoon.
STUCK_AFTER = timedelta(hours=6)


class LedgerAgentError(RuntimeError):
    """Base class for safe, non-PII reconciliation failures."""


@dataclass(frozen=True, slots=True)
class ReconciliationRun:
    output: LedgerAgentOutput
    flagged: int


def _scope(hazard_event_id: uuid.UUID, since: datetime | None, channel):
    filters = [Claim.hazard_event_id == hazard_event_id]
    if since is not None:
        filters.append(Disbursement.confirmed_at >= since)
    if channel is not None:
        filters.append(Disbursement.channel == channel)
    return filters


def _duplicates(session: Session, filters) -> list[AnomalyFlag]:
    """Two confirmed payments against one allocation is a household paid twice
    and, somewhere, a household not paid at all."""
    rows = session.execute(
        select(Disbursement.allocation_id, func.count(Disbursement.id).label("n"))
        .join(Allocation, Allocation.id == Disbursement.allocation_id)
        .join(Claim, Claim.id == Allocation.claim_id)
        .where(Disbursement.status == DisbursementStatus.CONFIRMED, *filters)
        .group_by(Disbursement.allocation_id)
        .having(func.count(Disbursement.id) > 1)
    ).all()
    flags = [
        AnomalyFlag(
            kind="DUPLICATE",
            subject_type="allocation",
            subject_id=row.allocation_id,
            detail=f"{row.n} confirmed disbursements against one allocation",
        )
        for row in rows
    ]

    # The provider's own reference reused across two payments says the same
    # thing from the other side of the wire.
    refs = session.execute(
        select(Disbursement.external_ref, func.count(Disbursement.id).label("n"))
        .join(Allocation, Allocation.id == Disbursement.allocation_id)
        .join(Claim, Claim.id == Allocation.claim_id)
        .where(Disbursement.external_ref.is_not(None), *filters)
        .group_by(Disbursement.external_ref)
        .having(func.count(Disbursement.id) > 1)
    ).all()
    for row in refs:
        subject = session.scalar(
            select(Disbursement.id).where(Disbursement.external_ref == row.external_ref)
        )
        flags.append(
            AnomalyFlag(
                kind="DUPLICATE",
                subject_type="disbursement",
                subject_id=subject,
                detail=f"provider reference reused across {row.n} disbursements",
            )
        )
    return flags


def _orphans(session: Session, hazard_event_id: uuid.UUID) -> list[AnomalyFlag]:
    """A confirmation in the ledger with no payment behind it.

    The ledger is append-only and the payment tables are not, so the two can
    in principle disagree. If they ever do, the ledger is the record that
    matters and the gap is the finding.
    """
    live = select(Disbursement.id)
    rows = session.execute(
        select(LedgerEntry.subject_id, LedgerEntry.id)
        .where(
            LedgerEntry.action == str(Event.DISBURSEMENT_CONFIRMED),
            LedgerEntry.subject_type == "disbursement",
            LedgerEntry.subject_id.not_in(live),
        )
        .limit(50)
    ).all()
    return [
        AnomalyFlag(
            kind="ORPHAN",
            subject_type="disbursement",
            subject_id=row.subject_id,
            detail="ledger records a confirmation with no disbursement behind it",
        )
        for row in rows
    ]


def _amount_mismatches(session: Session, filters) -> list[AnomalyFlag]:
    """What the receipt says was paid against what was signed for.

    Cash only. A goods row carries no amount by design, so there is no figure
    to disagree about — the reconciliation for goods is the delivery
    confirmation (LGX-03), which does not exist yet.
    """
    rows = session.execute(
        select(Disbursement.id, Allocation.amount, LedgerEntry.payload)
        .join(Allocation, Allocation.id == Disbursement.allocation_id)
        .join(Claim, Claim.id == Allocation.claim_id)
        .join(
            LedgerEntry,
            (LedgerEntry.subject_id == Disbursement.id)
            & (LedgerEntry.action == str(Event.DISBURSEMENT_CONFIRMED)),
        )
        .where(
            Disbursement.status == DisbursementStatus.CONFIRMED,
            Allocation.resource == ResourceKind.CASH,
            *filters,
        )
    ).all()
    flags = []
    for row in rows:
        receipt_amount = str((row.payload or {}).get("amount") or "")
        signed_amount = f"{row.amount:.2f}" if row.amount is not None else ""
        if receipt_amount != signed_amount:
            flags.append(
                AnomalyFlag(
                    kind="AMOUNT_MISMATCH",
                    subject_type="disbursement",
                    subject_id=row.id,
                    detail=(
                        f"receipt records {receipt_amount or 'nothing'} against a "
                        f"signed {signed_amount or 'nothing'}"
                    ),
                )
            )
    return flags


def _unconfirmed(session: Session, filters, now: datetime) -> list[AnomalyFlag]:
    """Money that left and never arrived, and households owed a state change.

    Two shapes. A disbursement stuck mid-execution is a payment nobody can
    account for. A confirmed disbursement whose Storm File never reached
    SETTLED is a household that was paid and still reads as waiting, which is
    what someone will call the office about.

    The first shape is unreachable today and deliberately kept anyway. This
    release's executor returns its confirmation from the same call that
    executes, so EXECUTING never outlives a transaction and no row can sit in
    it. A real rail (PAY-03) separates the two, and that is exactly when a
    payment can leave and never arrive — writing the check afterwards would
    mean writing it during the incident it exists to catch.
    """
    cutoff = now - STUCK_AFTER
    stuck = session.scalars(
        select(Disbursement.id)
        .join(Allocation, Allocation.id == Disbursement.allocation_id)
        .join(Claim, Claim.id == Allocation.claim_id)
        .where(
            Disbursement.status == DisbursementStatus.EXECUTING,
            Disbursement.executed_at.is_not(None),
            Disbursement.executed_at < cutoff,
            *filters,
        )
    ).all()
    flags = [
        AnomalyFlag(
            kind="UNCONFIRMED",
            subject_type="disbursement",
            subject_id=disbursement_id,
            detail=f"executing with no confirmation for over {STUCK_AFTER}",
        )
        for disbursement_id in stuck
    ]

    unsettled = session.scalars(
        select(StormFile.id)
        .join(Claim, Claim.storm_file_id == StormFile.id)
        .join(Allocation, Allocation.claim_id == Claim.id)
        .join(Disbursement, Disbursement.allocation_id == Allocation.id)
        .where(
            Disbursement.status == DisbursementStatus.CONFIRMED,
            StormFile.state != StormFileState.SETTLED,
            *filters,
        )
        .distinct()
    ).all()
    flags.extend(
        AnomalyFlag(
            kind="UNCONFIRMED",
            subject_type="storm_file",
            subject_id=storm_file_id,
            detail="confirmed disbursement exists but the household is not SETTLED",
        )
        for storm_file_id in unsettled
    )
    return flags


def _already_flagged(session: Session, flag: AnomalyFlag) -> bool:
    """A standing finding is not news twice.

    Re-flagging every run would bury the new finding under the old ones, and
    the agent cannot clear anything, so the flag persists until a human acts.
    """
    return bool(
        session.scalar(
            select(LedgerEntry.seq)
            .where(
                LedgerEntry.action == str(Event.ANOMALY_FLAGGED),
                LedgerEntry.subject_id == flag.subject_id,
                LedgerEntry.payload["kind"].astext == flag.kind,
            )
            .limit(1)
        )
    )


def reconcile(
    session: Session,
    hazard_event_id: uuid.UUID,
    *,
    since: datetime | None = None,
    channel: DisbursementChannel | None = None,
    now: datetime | None = None,
) -> ReconciliationRun:
    """Audit one event's money path and flag what does not add up."""
    current = now or datetime.now(UTC)
    filters = _scope(hazard_event_id, since, channel)

    anomalies = [
        *_duplicates(session, filters),
        *_orphans(session, hazard_event_id),
        *_amount_mismatches(session, filters),
        *_unconfirmed(session, filters, current),
    ]

    reconciled = session.scalar(
        select(func.count(Disbursement.id))
        .join(Allocation, Allocation.id == Disbursement.allocation_id)
        .join(Claim, Claim.id == Allocation.claim_id)
        .where(Disbursement.status == DisbursementStatus.CONFIRMED, *filters)
    )

    chain_valid = ledger.verify_chain(session)
    flagged = 0
    for flag in anomalies:
        if _already_flagged(session, flag):
            continue
        ledger.append(
            session,
            action=str(Event.ANOMALY_FLAGGED),
            subject_type=flag.subject_type,
            subject_id=flag.subject_id,
            payload={
                "kind": flag.kind,
                "subject_type": flag.subject_type,
                "subject_id": str(flag.subject_id),
                "detail": flag.detail,
                "hazard_event_id": str(hazard_event_id),
                # Said plainly: this agent does not fix what it finds.
                "resolution": "REQUIRES_HUMAN",
            },
            actor_kind=ActorKind.AGENT,
            agent=AgentName.LEDGER_AGENT,
        )
        flagged += 1

    if not chain_valid:
        # The most serious finding this agent can make, and the only one that
        # invalidates every other number in the report.
        ledger.append(
            session,
            action=str(Event.ANOMALY_FLAGGED),
            subject_type="ledger",
            subject_id=hazard_event_id,
            payload={
                "kind": "CHAIN_BROKEN",
                "detail": "verify_chain() failed; stop and investigate",
                "hazard_event_id": str(hazard_event_id),
                "resolution": "REQUIRES_HUMAN",
            },
            actor_kind=ActorKind.AGENT,
            agent=AgentName.LEDGER_AGENT,
        )
        flagged += 1

    output = LedgerAgentOutput(
        reconciled_count=reconciled or 0,
        anomalies=anomalies,
        chain_valid=chain_valid,
        rationale=(
            f"{reconciled or 0} confirmed disbursement(s) reconciled; "
            f"{len(anomalies)} anomaly(ies) found, {flagged} newly flagged; "
            f"hash chain {'intact' if chain_valid else 'BROKEN'}"
        ),
    )
    return ReconciliationRun(output=output, flagged=flagged)


__all__ = [
    "STUCK_AFTER",
    "LedgerAgentError",
    "ReconciliationRun",
    "reconcile",
]
