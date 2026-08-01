"""The Storm File state machine.

This module is the product. Everything else is plumbing around it.

``transition()`` is the **only** place ``storm_file.state`` is written. Not an
agent, not a route handler, not a migration. A single writer is what makes the
ledger a complete record rather than a mostly-complete one, and "mostly" is not
a thing you can tell an Auditor General.

Each transition does three things in one transaction:

1. changes the state,
2. appends the ledger entry,
3. enqueues the follow-on agent job.

All three or none. That atomicity is why the queue is a Postgres table
(PRD 11.6) — with a separate queue, 1 and 2 could commit while 3 was lost, and
a household's claim would sit in a new state with nothing coming for it.

The legal transitions are frozen in ``packages/contracts/transitions.md``. This
table must match it exactly; the markdown is what the team agreed to and this is
the executable copy.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from lighthouse_contracts import (
    DEFAULT_PRIORITY,
    SOL_PRIORITY,
    ActorKind,
    AgentName,
    ClaimStatus,
    DisbursementStatus,
    Event,
    GateKind,
    StormFileState,
)
from lighthouse_contracts.events import FOLLOW_ON

from . import ledger, queue
from .models import Allocation, Claim, Disbursement, LedgerEntry, StormFile


class IllegalTransition(Exception):
    """Attempted a transition that is not in the frozen table."""


class GateNotSatisfied(Exception):
    """A human signature that this transition requires is missing."""


@dataclass(frozen=True, slots=True)
class Transition:
    id: str
    src: StormFileState | None  # None = creation
    dst: StormFileState
    agent: AgentName | None
    event: Event
    gate: GateKind | None = None


#: The eight legal Storm File transitions. See transitions.md.
TRANSITIONS: tuple[Transition, ...] = (
    Transition("T1", None, StormFileState.REGISTERED,
               AgentName.INTAKE_AGENT, Event.HOUSEHOLD_REGISTERED),
    Transition("T2", StormFileState.REGISTERED, StormFileState.AT_RISK,
               AgentName.RISK_MAPPER, Event.HOUSEHOLD_AT_RISK),
    Transition("T3", StormFileState.AT_RISK, StormFileState.REGISTERED,
               AgentName.FORECAST_SENTINEL, Event.HOUSEHOLD_STOOD_DOWN),
    Transition("T4", StormFileState.AT_RISK, StormFileState.AFFECTED,
               AgentName.INTAKE_AGENT, Event.CLAIM_CREATED),
    Transition("T4b", StormFileState.REGISTERED, StormFileState.AFFECTED,
               AgentName.INTAKE_AGENT, Event.CLAIM_CREATED),
    Transition("T5", None, StormFileState.AFFECTED,
               AgentName.INTAKE_AGENT, Event.CLAIM_CREATED),
    Transition("T6", StormFileState.AFFECTED, StormFileState.VERIFIED,
               AgentName.VERIFICATION_AGENT, Event.CLAIM_VERIFIED),
    Transition("T8", StormFileState.VERIFIED, StormFileState.SETTLED,
               AgentName.LEDGER_AGENT, Event.HOUSEHOLD_SETTLED,
               GateKind.DISBURSEMENT_BATCH),
)

_BY_PAIR: dict[tuple[StormFileState | None, StormFileState], Transition] = {
    (t.src, t.dst): t for t in TRANSITIONS
}


def find_transition(
    src: StormFileState | None, dst: StormFileState
) -> Transition | None:
    return _BY_PAIR.get((src, dst))


def _gate_satisfied(session: Session, storm_file_id: uuid.UUID, gate: GateKind) -> bool:
    """Is there a confirmed, signed money movement for this household?

    For G3 we do not merely look for an approval row — we look for a *confirmed
    disbursement*, which by schema cannot exist without one (``approval_id`` is
    NOT NULL). Checking the outcome rather than the paperwork means this cannot
    pass on a signature that was never acted on.
    """
    if gate is not GateKind.DISBURSEMENT_BATCH:
        return True

    found = session.execute(
        select(Disbursement.id)
        .join(Allocation, Allocation.id == Disbursement.allocation_id)
        .join(Claim, Claim.id == Allocation.claim_id)
        .where(
            Claim.storm_file_id == storm_file_id,
            Disbursement.status == DisbursementStatus.CONFIRMED,
        )
        .limit(1)
    ).scalar_one_or_none()
    return found is not None


def transition(
    session: Session,
    storm_file: StormFile,
    dst: StormFileState,
    *,
    actor_kind: ActorKind = ActorKind.AGENT,
    actor_id: uuid.UUID | None = None,
    agent: AgentName | None = None,
    payload: dict | None = None,
    enqueue_follow_on: bool = True,
) -> LedgerEntry:
    """Move a Storm File. Raises rather than guessing.

    Caller owns the transaction so that the state change, the ledger entry and
    the follow-on job commit together or not at all.
    """
    src = storm_file.state
    t = find_transition(src, dst)
    if t is None:
        raise IllegalTransition(
            f"{src} -> {dst} is not a legal transition (see transitions.md)"
        )

    if t.gate and not _gate_satisfied(session, storm_file.id, t.gate):
        raise GateNotSatisfied(
            f"{t.id} requires gate {t.gate}: no confirmed disbursement for "
            f"storm_file {storm_file.id}"
        )

    storm_file.state = dst
    storm_file.updated_at = datetime.now(UTC)
    session.flush()

    entry = ledger.append(
        session,
        action=str(t.event),
        subject_type="storm_file",
        subject_id=storm_file.id,
        payload={
            "transition": t.id,
            "previous": str(src) if src else None,
            "current": str(dst),
            **(payload or {}),
        },
        actor_kind=actor_kind,
        actor_id=actor_id,
        agent=agent or t.agent,
    )

    if enqueue_follow_on and (next_agent := FOLLOW_ON.get(t.event)):
        queue.enqueue(
            session,
            job_type=next_agent,
            payload={"storm_file_id": str(storm_file.id), "trigger": str(t.event)},
        )

    return entry


def record_creation(
    session: Session,
    storm_file: StormFile,
    *,
    actor_kind: ActorKind = ActorKind.AGENT,
    actor_id: uuid.UUID | None = None,
    payload: dict | None = None,
) -> LedgerEntry:
    """Ledger the birth of a Storm File — T1, or T5 for an unregistered reporter.

    Creation is not a transition between two states, so it does not go through
    ``transition()``. It still has to reach the ledger: a household that appears
    with no recorded origin is a hole in the audit trail.
    """
    t = "T5" if storm_file.state is StormFileState.AFFECTED else "T1"
    return ledger.append(
        session,
        action=str(Event.HOUSEHOLD_REGISTERED),
        subject_type="storm_file",
        subject_id=storm_file.id,
        payload={
            "transition": t,
            "previous": None,
            "current": str(storm_file.state),
            "thin": storm_file.thin,
            "synthetic": storm_file.synthetic,
            **(payload or {}),
        },
        actor_kind=actor_kind,
        actor_id=actor_id,
        agent=AgentName.INTAKE_AGENT,
    )


# ---------------------------------------------------------------------------
# Claim status
# ---------------------------------------------------------------------------

#: C1-C6 in transitions.md. A claim is per-event; a Storm File is long-lived.
CLAIM_TRANSITIONS: dict[tuple[ClaimStatus | None, ClaimStatus], str] = {
    (None, ClaimStatus.FILED): "C1",
    (ClaimStatus.FILED, ClaimStatus.VERIFIED): "C2",
    (ClaimStatus.FILED, ClaimStatus.REJECTED): "C3",
    (ClaimStatus.FILED, ClaimStatus.WITHDRAWN): "C4",
    (ClaimStatus.REJECTED, ClaimStatus.FILED): "C5",
    (ClaimStatus.VERIFIED, ClaimStatus.SETTLED): "C6",
}


def _enqueue_verification(session: Session, claim: Claim) -> None:
    """Send a claim to verification.

    INT-04: an SOL claim rides at priority 100. That changes *ordering* only —
    it still gets verified, and still needs a signature before money moves.
    """
    queue.enqueue(
        session,
        job_type=AgentName.VERIFICATION_AGENT,
        payload={"claim_id": str(claim.id), "sol": claim.sol},
        priority=SOL_PRIORITY if claim.sol else DEFAULT_PRIORITY,
    )


def record_claim_creation(
    session: Session,
    claim: Claim,
    *,
    actor_kind: ActorKind = ActorKind.AGENT,
    actor_id: uuid.UUID | None = None,
    payload: dict | None = None,
) -> LedgerEntry:
    """C1 — a claim is filed.

    Like Storm File creation, this is not a transition between two states: a
    claim is born ``FILED``. It gets its own function so the ledger entry and the
    verification job cannot be forgotten, which is what would happen if callers
    were expected to remember two separate calls.
    """
    entry = ledger.append(
        session,
        action=str(Event.CLAIM_CREATED),
        subject_type="claim",
        subject_id=claim.id,
        payload={
            "transition": "C1",
            "claim_ref": claim.claim_ref,
            "previous": None,
            "current": str(claim.status),
            "channel": claim.channel,
            "sol": claim.sol,
            "partial": claim.partial,
            **(payload or {}),
        },
        actor_kind=actor_kind,
        actor_id=actor_id,
        agent=AgentName.INTAKE_AGENT,
    )
    _enqueue_verification(session, claim)
    return entry


def transition_claim(
    session: Session,
    claim: Claim,
    dst: ClaimStatus,
    *,
    actor_kind: ActorKind = ActorKind.AGENT,
    actor_id: uuid.UUID | None = None,
    agent: AgentName | None = None,
    payload: dict | None = None,
) -> LedgerEntry:
    """Move a claim, stamping the T2R clock as it goes."""
    src = claim.status
    tid = CLAIM_TRANSITIONS.get((src, dst))
    if tid is None:
        raise IllegalTransition(f"claim {src} -> {dst} is not legal (see transitions.md)")

    claim.status = dst
    now = datetime.now(UTC)
    if dst is ClaimStatus.VERIFIED:
        claim.verified_at = now
    elif dst is ClaimStatus.SETTLED:
        claim.settled_at = now  # T2R clock stops (PAY-04)
    session.flush()

    action = {
        ClaimStatus.FILED: Event.CLAIM_CREATED,
        ClaimStatus.VERIFIED: Event.CLAIM_VERIFIED,
        ClaimStatus.REJECTED: Event.CLAIM_REJECTED,
        ClaimStatus.WITHDRAWN: Event.CLAIM_WITHDRAWN,
        ClaimStatus.SETTLED: Event.HOUSEHOLD_SETTLED,
    }[dst]

    entry = ledger.append(
        session,
        action=str(action),
        subject_type="claim",
        subject_id=claim.id,
        payload={
            "transition": tid,
            "claim_ref": claim.claim_ref,
            "previous": str(src) if src else None,
            "current": str(dst),
            **(payload or {}),
        },
        actor_kind=actor_kind,
        actor_id=actor_id,
        agent=agent,
    )

    if dst is ClaimStatus.FILED:
        # C5 — a rejected claim reopened on appeal (VER-06) goes back through
        # verification exactly like a new one.
        _enqueue_verification(session, claim)

    return entry


def time_to_relief_hours(claim: Claim) -> float | None:
    """T2R for one claim: filed -> first relief confirmed.

    The clock starts when the household speaks, not when we finish verifying.
    Returns None until the claim settles.
    """
    if claim.settled_at is None:
        return None
    return (claim.settled_at - claim.filed_at).total_seconds() / 3600.0
