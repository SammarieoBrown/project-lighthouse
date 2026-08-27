"""Severity and queue order for a verified claim (TRI-01, TRI-02).

Four ordered tiers, and the tier is the whole decision: medical urgency first,
then habitability, then property, with the vulnerability score breaking ties
inside a tier and a safety-of-life claim pinned above all of it. Those are
TRI-01's words, and the rules below are a lookup table a Director can read and
argue with rather than a model that has to be trusted.

**Why ``rank`` is a score and not a position.** ``TriageAgentInput`` carries one
claim and no view of its peers, so a rank computed here cannot be an index into
an ordering — the agent cannot see the ordering. It is a priority *key*: lower
sorts first, it is derived only from this claim, and it is stable, so the live
console queue (TRI-02) sorts by it without one claim's triage rewriting
another's row. Recomputing absolute positions on every claim would be both
racy and, on a queue that changes as fast as this one, wrong by the time it
rendered.

**What does not move the rank.** Verification confidence is carried into the
input because the contract asks for it, and it is recorded in the rationale,
but it is deliberately not a term in the score. Triage orders need, not
certainty. A claim reaching triage is a claim verification already accepted,
and letting confidence re-weight the queue would quietly re-litigate that
decision in a place nobody is looking.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from lighthouse_contracts import (
    SOL_PRIORITY,
    ActorKind,
    AgentName,
    ClaimStatus,
    Event,
    Severity,
)
from lighthouse_contracts.agents import TriageAgentOutput

from app import ledger, queue
from app.models import Claim, StormFile

#: A household saying it needs a clinician, insulin, or medication. These are
#: the extractor's canonical needs (intake/extraction.py) — not free text, so
#: the tier cannot drift as phrasing changes.
MEDICAL_NEEDS = frozenset({"medical_support", "insulin", "medicine"})

#: Needs that say the dwelling is not sheltering them: nowhere to stay, or a
#: roof that has to be covered before it can.
HABITABILITY_NEEDS = frozenset({"shelter", "tarpaulin"})

#: Every damage type the extractor can name is structural — a collapsed house,
#: a roof off, cracked walls, water inside. In a hurricane all four bear on
#: whether the household can stay where it is, so any of them reaches the
#: habitability tier and the property tier is what is left when a household
#: reported needs but named no damage at all.
TIER_SOL = 0
TIER_MEDICAL = 1
TIER_HABITABILITY = 2
TIER_PROPERTY = 3

#: A household with no recorded vulnerability score sits at the midpoint rather
#: than at either end. Scoring an unknown as 0 would sink every thin SMS-tier
#: registration (REG-06) to the bottom of the queue, which is the opposite of
#: what a thin registration usually means.
DEFAULT_VULN_SCORE = 50

_SEVERITY_BY_TIER = {
    TIER_SOL: Severity.URGENT,
    TIER_MEDICAL: Severity.URGENT,
    TIER_HABITABILITY: Severity.HIGH,
    TIER_PROPERTY: Severity.MED,
}


class TriageServiceError(RuntimeError):
    """Base class for safe, non-PII triage failures."""


class ClaimNotFound(TriageServiceError):
    pass


class TriageNotRunnable(TriageServiceError):
    pass


@dataclass(frozen=True, slots=True)
class TriageRun:
    claim: Claim
    output: TriageAgentOutput
    created: bool


def _tier_and_drivers(claim: Claim) -> tuple[int, list[str]]:
    needs = {str(need).strip().casefold() for need in (claim.reported_needs or [])}
    damage = str(claim.damage_type or "").strip().casefold()
    drivers: list[str] = []

    if claim.sol:
        # INT-04. Pinned above everything, and the reason is recorded so a
        # Director can see why this claim is at the top of their screen.
        drivers.append("safety_of_life")
        return TIER_SOL, drivers

    medical = sorted(needs & MEDICAL_NEEDS)
    if medical:
        drivers.extend(f"medical:{need}" for need in medical)
        return TIER_MEDICAL, drivers

    habitability = sorted(needs & HABITABILITY_NEEDS)
    if damage:
        drivers.append(f"damage:{damage}")
    drivers.extend(f"habitability:{need}" for need in habitability)
    if damage or habitability:
        return TIER_HABITABILITY, drivers

    drivers.extend(f"need:{need}" for need in sorted(needs))
    return TIER_PROPERTY, drivers


def score_claim(
    claim: Claim, storm_file: StormFile, *, verification_confidence: float
) -> TriageAgentOutput:
    """The whole ordering rule, as a pure function of one claim.

    Pure so that it can be argued with in a test rather than only in
    production, and so the console can explain any position in the queue by
    replaying the same inputs.
    """
    tier, drivers = _tier_and_drivers(claim)
    vuln = storm_file.vuln_score
    if vuln is None:
        vuln = DEFAULT_VULN_SCORE
        drivers.append("vuln:unknown")
    else:
        drivers.append(f"vuln:{vuln}")

    need_count = len(claim.reported_needs or [])
    # Tier dominates; vulnerability breaks ties within it (TRI-01); the number
    # of distinct unmet needs is a last nudge so two equally vulnerable
    # households in the same tier are not ordered arbitrarily. Every term is
    # bounded and non-negative, so ``rank`` satisfies the contract's ``ge=0``
    # at both extremes: 0 for a maximally vulnerable SOL claim, 3510 for the
    # least urgent claim this can produce.
    rank = (tier * 1000) + ((100 - vuln) * 5) + (10 - min(need_count, 10))

    severity = _SEVERITY_BY_TIER[tier]
    return TriageAgentOutput(
        severity=severity,
        rank=rank,
        drivers=drivers,
        rationale=(
            f"{severity} at rank {rank}: "
            f"{', '.join(drivers) if drivers else 'no needs or damage reported'}"
            f" (verification confidence {verification_confidence:.2f}, "
            "which does not affect ordering)"
        ),
    )


def run_triage(
    session: Session,
    claim_id: uuid.UUID,
    *,
    verification_confidence: float | None = None,
) -> TriageRun:
    """Annotate one verified claim with severity and queue order.

    Holds no transition authority (transitions.md): it never moves the claim,
    the Storm File, or any money. It writes two columns and one ledger entry,
    then hands on to Logistics.

    Re-running is a no-op when nothing has changed. Triage is cheap and
    deterministic, so unlike the vision agents there is no cost argument for
    refusing to recompute — the reason to short-circuit is that an unchanged
    re-run should not litter the ledger with identical entries.
    """
    claim = session.scalar(select(Claim).where(Claim.id == claim_id).with_for_update())
    if claim is None:
        raise ClaimNotFound("claim does not exist")
    if claim.status is not ClaimStatus.VERIFIED:
        # SETTLED is deliberately not accepted the way damage assessment
        # accepts it: re-ordering a queue a claim has already left changes
        # nothing and would only add noise to the ledger.
        raise TriageNotRunnable("claim is not verified")
    storm_file = session.get(StormFile, claim.storm_file_id)
    if storm_file is None:
        raise ClaimNotFound("claim Storm File does not exist")

    output = score_claim(
        claim, storm_file, verification_confidence=verification_confidence or 0.0
    )
    if claim.severity is output.severity and claim.triage_rank == output.rank:
        return TriageRun(claim, output, False)

    claim.severity = output.severity
    claim.triage_rank = output.rank
    session.flush()

    ledger.append(
        session,
        action=str(Event.CLAIM_TRIAGED),
        subject_type="claim",
        subject_id=claim.id,
        payload={
            "claim_id": str(claim.id),
            "severity": str(output.severity),
            "rank": output.rank,
            "drivers": output.drivers,
            "sol": claim.sol,
        },
        actor_kind=ActorKind.AGENT,
        agent=AgentName.TRIAGE_AGENT,
    )

    # FOLLOW_ON maps CLAIM_TRIAGED to the Logistics Agent. Triage does not
    # transition the claim, so the state machine will not enqueue that for us;
    # it is enqueued here for the same reason verification enqueues triage. No
    # handler is registered yet, so the worker parks it and the backlog waits.
    queue.enqueue(
        session,
        job_type=AgentName.LOGISTICS_AGENT,
        priority=SOL_PRIORITY if claim.sol else 0,
        payload={
            "claim_id": str(claim.id),
            "storm_file_id": str(claim.storm_file_id),
            "severity": str(output.severity),
            "rank": output.rank,
        },
    )
    return TriageRun(claim, output, True)


__all__ = [
    "ClaimNotFound",
    "DEFAULT_VULN_SCORE",
    "HABITABILITY_NEEDS",
    "MEDICAL_NEEDS",
    "TriageNotRunnable",
    "TriageRun",
    "TriageServiceError",
    "run_triage",
    "score_claim",
]
