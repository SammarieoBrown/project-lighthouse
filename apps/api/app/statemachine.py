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

import math
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
    AppRole,
    ClaimStatus,
    DisbursementStatus,
    Event,
    GateKind,
    StormFileState,
)
from lighthouse_contracts.events import FOLLOW_ON

from . import ledger, queue
from .models import (
    Allocation,
    AppUser,
    Claim,
    Disbursement,
    LedgerEntry,
    StormFile,
    Verification,
)


class IllegalTransition(Exception):
    """Attempted a transition that is not in the frozen table."""


class GateNotSatisfied(Exception):
    """A human signature that this transition requires is missing."""


class VerificationNotSatisfied(Exception):
    """A VERIFIED transition is not bound to an eligible immutable verdict."""


@dataclass(frozen=True, slots=True)
class Transition:
    id: str
    src: StormFileState | None  # None = creation
    dst: StormFileState
    agent: AgentName | None
    event: Event
    gate: GateKind | None = None


#: The legal Storm File transitions. T6 and T7 share a state pair but differ
#: by actor: automatic agent verdict versus Review Clerk adjudication.
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
    Transition("T7", StormFileState.AFFECTED, StormFileState.VERIFIED,
               None, Event.CLAIM_VERIFIED),
    Transition("T8", StormFileState.VERIFIED, StormFileState.SETTLED,
               AgentName.LEDGER_AGENT, Event.HOUSEHOLD_SETTLED,
               GateKind.DISBURSEMENT_BATCH),
)

_BY_CONTEXT: dict[
    tuple[StormFileState | None, StormFileState, ActorKind], Transition
] = {
    (t.src, t.dst, ActorKind.HUMAN if t.id == "T7" else ActorKind.AGENT): t
    for t in TRANSITIONS
}


def find_transition(
    src: StormFileState | None,
    dst: StormFileState,
    *,
    actor_kind: ActorKind = ActorKind.AGENT,
) -> Transition | None:
    return _BY_CONTEXT.get((src, dst, actor_kind))


_VERIFICATION_SIGNALS = frozenset(
    {
        "hazard_sufficiency",
        "satellite_change",
        "neighbour_corroboration",
        "registry_match",
        "media_integrity",
    }
)


def _agent_signals_eligible(signals: object) -> bool:
    if not isinstance(signals, dict) or set(signals) != _VERIFICATION_SIGNALS:
        return False
    for signal in signals.values():
        if not isinstance(signal, dict) or signal.get("present") is not True:
            return False
        score = signal.get("score")
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(float(score))
            or not 0.0 <= float(score) <= 1.0
        ):
            return False
    return True


def _verification_authorizes_claim(
    session: Session,
    claim: Claim,
    *,
    verification_id: object,
    actor_kind: ActorKind,
    actor_id: uuid.UUID | None,
) -> Verification:
    try:
        parsed_id = uuid.UUID(str(verification_id))
    except (TypeError, ValueError) as exc:
        raise VerificationNotSatisfied(
            "VERIFIED transition requires an immutable verification id"
        ) from exc

    verification = session.get(Verification, parsed_id)
    latest_id = session.execute(
        select(Verification.id)
        .where(Verification.claim_id == claim.id)
        .order_by(Verification.created_at.desc(), Verification.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if verification is None or verification.claim_id != claim.id or latest_id != parsed_id:
        raise VerificationNotSatisfied(
            "VERIFIED transition must bind the claim's latest immutable verification"
        )

    if actor_kind is ActorKind.AGENT:
        confidence = float(verification.confidence)
        eligible = (
            verification.actor_kind is ActorKind.AGENT
            and verification.actor_id is None
            and verification.agent_name == str(AgentName.VERIFICATION_AGENT)
            and verification.verdict.value == "AUTO_VERIFIED"
            and math.isfinite(confidence)
            and confidence >= 0.85
            and not verification.capped
            and _agent_signals_eligible(verification.signals)
        )
    elif actor_kind is ActorKind.HUMAN:
        clerk = session.get(AppUser, actor_id) if actor_id is not None else None
        eligible = (
            verification.actor_kind is ActorKind.HUMAN
            and verification.actor_id == actor_id
            and verification.verdict.value == "APPROVED"
            and verification.overrides_id is not None
            and clerk is not None
            and clerk.active
            and clerk.role in {AppRole.REVIEW_CLERK, AppRole.DIRECTOR}
        )
    else:
        eligible = False
    if not eligible:
        raise VerificationNotSatisfied(
            "immutable verification does not authorize this VERIFIED transition"
        )
    return verification


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
    t = find_transition(src, dst, actor_kind=actor_kind)
    if t is None:
        raise IllegalTransition(
            f"{src} -> {dst} is not a legal transition (see transitions.md)"
        )

    if t.gate and not _gate_satisfied(session, storm_file.id, t.gate):
        raise GateNotSatisfied(
            f"{t.id} requires gate {t.gate}: no confirmed disbursement for "
            f"storm_file {storm_file.id}"
        )

    if dst is StormFileState.VERIFIED:
        verification_id = (payload or {}).get("verification_id")
        try:
            parsed_verification_id = uuid.UUID(str(verification_id))
        except (TypeError, ValueError):
            parsed_verification_id = None
        verification = (
            session.get(Verification, parsed_verification_id)
            if parsed_verification_id is not None
            else None
        )
        claim = (
            session.get(Claim, verification.claim_id)
            if verification is not None
            else None
        )
        if claim is None or claim.storm_file_id != storm_file.id:
            raise VerificationNotSatisfied(
                "VERIFIED Storm File transition must bind a claim on that file"
            )
        _verification_authorizes_claim(
            session,
            claim,
            verification_id=verification_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
        )

    if actor_kind is ActorKind.AGENT:
        if agent is not None and agent is not t.agent:
            raise IllegalTransition(
                f"{agent} cannot perform transition {t.id}; authority belongs to {t.agent}"
            )
        resolved_agent = agent or t.agent
    else:
        if agent is not None:
            raise IllegalTransition("human transitions cannot assert agent authority")
        resolved_agent = None

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
        agent=resolved_agent,
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


def enqueue_claim_verification(session: Session, claim: Claim) -> None:
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
    enqueue_verification: bool = True,
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
    if enqueue_verification:
        enqueue_claim_verification(session, claim)
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

    if dst is ClaimStatus.VERIFIED:
        _verification_authorizes_claim(
            session,
            claim,
            verification_id=(payload or {}).get("verification_id"),
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        if actor_kind is ActorKind.AGENT and agent is not AgentName.VERIFICATION_AGENT:
            raise IllegalTransition("C2 agent authority belongs to verification_agent")
        if actor_kind is ActorKind.HUMAN and agent is not None:
            raise IllegalTransition("human C2 transitions cannot assert agent authority")

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
        enqueue_claim_verification(session, claim)

    return entry


def time_to_relief_hours(claim: Claim) -> float | None:
    """T2R for one claim: filed -> first relief confirmed.

    The clock starts when the household speaks, not when we finish verifying.
    Returns None until the claim settles.
    """
    if claim.settled_at is None:
        return None
    return (claim.settled_at - claim.filed_at).total_seconds() / 3600.0
