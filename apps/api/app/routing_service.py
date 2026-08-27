"""Who pays for a claim, and the consent that justified deciding so (RTE).

RTE-01 gives four outcomes and they are not four of a kind. Three describe who
carries the loss — the government's relief programme, a named insurer, or both
at once for a household insured on the structure and still needing water
tonight. The fourth, ``DONOR_POOL``, is a *funding source* for a claim already
on the relief path, and the PRD says so outright: "orthogonal to the other two".
So the router never returns it. Which pool funds a relief allocation is a
question asked at allocation time, where the pool balances are.

**Consent is the gate, and it is snapshotted rather than joined.** A claim goes
to an insurer only if the household agreed to that specific sharing, and the
agreement is copied onto the decision at the moment it is made. Consent can be
withdrawn afterwards, and when an auditor asks about a share that already
happened the question is not "may we share this now" but "what were we
permitted to do when we decided". A live join answers the wrong question.

**Nothing here is inferred from a transcript.** A household mentioning an
insurer while describing a collapsed roof has not consented to anything. The
only thing that routes a claim away from relief is a consent record.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from lighthouse_contracts import ActorKind, ClaimStatus, Event, PayerRoute

from app import ledger
from app.models import Claim, Consent, RoutingDecision, StormFile

#: The consent scope key that permits sending a claim to a named insurer.
#: Narrow on purpose: being contactable and being shareable are different
#: permissions and a household may grant one without the other.
INSURER_SHARING_SCOPE = "share_with_insurer"


class RoutingServiceError(RuntimeError):
    """Base class for safe, non-PII routing failures."""


class ClaimNotFound(RoutingServiceError):
    pass


class RoutingNotRunnable(RoutingServiceError):
    pass


@dataclass(frozen=True, slots=True)
class RoutingRun:
    decision: RoutingDecision
    created: bool


def active_insurer_consent(session: Session, storm_file_id: uuid.UUID) -> Consent | None:
    """The household's live permission to share with a named insurer.

    Newest wins, and a revoked record is not a permission. Both of those are
    the kind of thing that looks obvious written down and is easy to get wrong
    in a query that only checks the flag.
    """
    return session.scalar(
        select(Consent)
        .where(
            Consent.storm_file_id == storm_file_id,
            Consent.revoked_at.is_(None),
            Consent.scope[INSURER_SHARING_SCOPE].astext == "true",
        )
        .order_by(Consent.granted_at.desc(), Consent.id.desc())
        .limit(1)
    )


def _route_for(claim: Claim, consent: Consent | None) -> tuple[PayerRoute, str | None]:
    """RTE-01's outcomes, as the requirement words them."""
    insurer = None
    if consent is not None:
        insurer = (consent.scope or {}).get("insurer_name") or None
    if consent is None or not insurer:
        # The default, and the honest one. An uninsured household is the
        # common case and relief is what the programme exists to provide.
        return PayerRoute.GOV_RELIEF, None
    if claim.reported_needs:
        # "Insured for structure, relief for immediate needs." A household
        # waiting months for an adjuster still needs water tonight, and
        # routing them wholly to an insurer would be telling them to wait.
        return PayerRoute.BOTH, insurer
    return PayerRoute.INSURER, insurer


def route_claim(
    session: Session,
    claim_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> RoutingRun:
    """Decide who pays for one verified claim and record why (RTE-01, RTE-02).

    Decided once. A routing decision is the basis on which a claim may be sent
    outside the programme, so re-deciding it silently — on a replay, on a
    re-delivered job — would mean the record no longer says what was true when
    the sharing happened. A household changing their answer is a new consent
    record and an explicit re-route, not a quiet overwrite.
    """
    claim = session.scalar(select(Claim).where(Claim.id == claim_id).with_for_update())
    if claim is None:
        raise ClaimNotFound("claim does not exist")
    if claim.status not in {ClaimStatus.VERIFIED, ClaimStatus.SETTLED}:
        raise RoutingNotRunnable("claim is not verified")
    storm_file = session.get(StormFile, claim.storm_file_id)
    if storm_file is None:
        raise ClaimNotFound("claim Storm File does not exist")

    existing = session.scalar(
        select(RoutingDecision)
        .where(RoutingDecision.claim_id == claim.id)
        .order_by(RoutingDecision.decided_at.desc(), RoutingDecision.id.desc())
        .limit(1)
    )
    if existing is not None:
        return RoutingRun(existing, False)

    consent = active_insurer_consent(session, storm_file.id)
    route, insurer = _route_for(claim, consent)
    snapshot = _consent_snapshot(consent)

    decision = RoutingDecision(
        claim_id=claim.id,
        route=route,
        insurer_name=insurer,
        consent_id=consent.id if consent is not None else None,
        consent_snapshot=snapshot,
        decided_at=now or datetime.now(UTC),
    )
    session.add(decision)
    session.flush()

    ledger.append(
        session,
        action=str(Event.CLAIM_ROUTED),
        subject_type="claim",
        subject_id=claim.id,
        payload={
            "claim_id": str(claim.id),
            "routing_decision_id": str(decision.id),
            "route": str(route),
            # The insurer is named because the point of the record is that a
            # named third party may receive this household's claim.
            "insurer_name": insurer,
            "consent_id": str(consent.id) if consent is not None else None,
            "consent_snapshot": snapshot,
        },
        actor_kind=ActorKind.SYSTEM,
    )
    return RoutingRun(decision, True)


def _consent_snapshot(consent: Consent | None) -> dict:
    """What we were permitted to do, frozen at the moment we decided.

    No phone number and no name: the snapshot answers what was permitted, not
    who permitted it, and the Storm File already holds the identity behind a
    role-gated door.
    """
    if consent is None:
        return {
            "granted": False,
            "basis": "no active insurer-sharing consent on file",
        }
    return {
        "granted": True,
        "consent_version": consent.version,
        "granted_at": consent.granted_at.isoformat(),
        "channel": consent.channel,
        "scope": dict(consent.scope or {}),
    }


__all__ = [
    "INSURER_SHARING_SCOPE",
    "ClaimNotFound",
    "RoutingNotRunnable",
    "RoutingRun",
    "RoutingServiceError",
    "active_insurer_consent",
    "route_claim",
]
