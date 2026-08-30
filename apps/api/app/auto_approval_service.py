"""Settle the small, well-evidenced claims; leave the rest for a human.

A Director sets one standing authorization — a ceiling, an evidence bar, and
a funding source — and this agent applies it claim by claim. Everything it
declines it declines out loud: the reason is written to the ledger, so the
queue a human works is exactly the set of claims the machine would not touch,
each with the sentence explaining why.

Nothing here is trusted. The database re-checks the ceiling, the funding
source, and the authorization's validity when the row is written; this module
decides *whether to try*, and records what it decided.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from lighthouse_contracts import (
    ActorKind,
    AgentName,
    ClaimStatus,
    PayerRoute,
    ResourceKind,
    Verdict,
)

from app import ledger
from app.approvals import (
    AllocationApprovalRequest,
    ApprovalServiceError,
    approve_claim_allocation,
)
from app.models import (
    Allocation,
    AllocationPlan,
    AutoApprovalPolicy,
    Claim,
    DamageAssessment,
    DonationPool,
    Verification,
)

log = logging.getLogger(__name__)

AUTO_APPROVAL_JOB_TYPE = "auto_approval_agent"
DECIDED_ACTION = "allocation.auto_approved"
DEFERRED_ACTION = "allocation.deferred_to_human"


@dataclass(frozen=True, slots=True)
class AutoApprovalDecision:
    """What the agent concluded, and the sentence a human will read."""

    approved: bool
    reason: str
    amount: Decimal | None = None
    policy_id: uuid.UUID | None = None
    allocation_id: uuid.UUID | None = None


def active_policy(
    session: Session, hazard_event_id: uuid.UUID
) -> AutoApprovalPolicy | None:
    """The newest authorization still in force for this event."""
    return session.scalar(
        select(AutoApprovalPolicy)
        .where(
            AutoApprovalPolicy.hazard_event_id == hazard_event_id,
            AutoApprovalPolicy.revoked_at.is_(None),
        )
        .order_by(AutoApprovalPolicy.created_at.desc(), AutoApprovalPolicy.id.desc())
        .limit(1)
    )


def _latest_verification(session: Session, claim_id: uuid.UUID) -> Verification | None:
    return session.scalar(
        select(Verification)
        .where(Verification.claim_id == claim_id)
        .order_by(Verification.created_at.desc(), Verification.id.desc())
        .limit(1)
    )


def _latest_assessment(
    session: Session, claim_id: uuid.UUID
) -> DamageAssessment | None:
    return session.scalar(
        select(DamageAssessment)
        .where(DamageAssessment.claim_id == claim_id)
        .order_by(DamageAssessment.created_at.desc(), DamageAssessment.id.desc())
        .limit(1)
    )


def _signals_present(signals: object) -> int:
    if not isinstance(signals, dict):
        return 0
    return sum(
        1
        for value in signals.values()
        if isinstance(value, dict) and value.get("present") is True
    )


def evaluate(session: Session, claim: Claim) -> AutoApprovalDecision:
    """Decide without writing anything. Every ``no`` carries its reason."""
    policy = active_policy(session, claim.hazard_event_id)
    if policy is None:
        return AutoApprovalDecision(False, "no standing authorization is in force")

    if claim.status is not ClaimStatus.VERIFIED:
        return AutoApprovalDecision(
            False, f"claim is {claim.status} and only VERIFIED claims are covered",
            policy_id=policy.id,
        )

    existing = session.scalar(
        select(Allocation.id)
        .join(AllocationPlan, AllocationPlan.id == Allocation.plan_id)
        .where(
            Allocation.claim_id == claim.id,
            AllocationPlan.approval_id.is_not(None),
        )
        .limit(1)
    )
    if existing is not None:
        return AutoApprovalDecision(
            False, "an approved allocation already exists", policy_id=policy.id
        )

    verification = _latest_verification(session, claim.id)
    if verification is None or verification.verdict not in {
        Verdict.AUTO_VERIFIED,
        Verdict.APPROVED,
    }:
        return AutoApprovalDecision(
            False, "no standing verification supports this claim", policy_id=policy.id
        )

    confidence = Decimal(str(float(verification.confidence)))
    if confidence < policy.min_confidence:
        return AutoApprovalDecision(
            False,
            f"confidence {confidence:.2f} is below the authorized {policy.min_confidence:.2f}",
            policy_id=policy.id,
        )

    present = _signals_present(verification.signals)
    if present < policy.min_signals:
        return AutoApprovalDecision(
            False,
            f"{present} of 5 signals scored; the authorization requires {policy.min_signals}",
            policy_id=policy.id,
        )

    assessment = _latest_assessment(session, claim.id)
    if policy.requires_assessment and assessment is None:
        return AutoApprovalDecision(
            False, "no damage assessment has been recorded", policy_id=policy.id
        )

    # The estimate sizes the grant. Without one there is no defensible figure
    # for an agent to choose, so the claim goes to a human who can ask.
    if assessment is None:
        return AutoApprovalDecision(
            False,
            "no estimate exists to size a grant from",
            policy_id=policy.id,
        )
    amount = Decimal(assessment.estimate_high).quantize(Decimal("0.01"))
    if amount <= 0:
        return AutoApprovalDecision(
            False,
            "the assessment found no priceable damage",
            policy_id=policy.id,
        )
    if amount > policy.max_amount:
        return AutoApprovalDecision(
            False,
            f"estimate of J${amount:,.2f} is above the authorized ceiling of "
            f"J${policy.max_amount:,.2f}",
            policy_id=policy.id,
            amount=amount,
        )

    if policy.payer_route is PayerRoute.DONOR_POOL:
        pool = session.get(DonationPool, policy.pool_id)
        balance = Decimal("0.00") if pool is None else (pool.balance or Decimal("0.00"))
        if balance < amount:
            return AutoApprovalDecision(
                False,
                f"the authorized pool holds J${balance:,.2f} against a "
                f"J${amount:,.2f} grant",
                policy_id=policy.id,
                amount=amount,
            )

    return AutoApprovalDecision(
        True,
        f"within the authorization: J${amount:,.2f} at confidence {confidence:.2f} "
        f"with {present} of 5 signals",
        amount=amount,
        policy_id=policy.id,
    )


def run_auto_approval(session: Session, claim_id: uuid.UUID) -> AutoApprovalDecision:
    """Apply the standing authorization to one claim, and say what happened."""
    claim = session.get(Claim, claim_id)
    if claim is None:
        raise LookupError("claim no longer exists")

    decision = evaluate(session, claim)
    if not decision.approved:
        # A deferral is a queue entry for a human, not a failure. Recorded so
        # the operator sees the reason beside the claim rather than guessing.
        ledger.append(
            session,
            action=DEFERRED_ACTION,
            subject_type="claim",
            subject_id=claim.id,
            actor_kind=ActorKind.AGENT,
            agent=AgentName.LOGISTICS_AGENT,
            payload={
                "claim_ref": claim.claim_ref,
                "reason": decision.reason,
                "policy_id": str(decision.policy_id) if decision.policy_id else None,
                "money_movement": "NOT_INITIATED",
            },
        )
        return decision

    policy = session.get(AutoApprovalPolicy, decision.policy_id)
    request = AllocationApprovalRequest(
        resource=ResourceKind.CASH,
        amount=decision.amount,
        currency="JMD",
        payer_route=policy.payer_route,
        pool_id=policy.pool_id,
        note=f"Auto-approved under standing authorization: {decision.reason}.",
    )
    try:
        outcome = approve_claim_allocation(
            session,
            claim_id=claim.id,
            request=request,
            # Deterministic per claim and policy: a retried job re-presents the
            # same intent rather than signing a second grant.
            idempotency_key=f"auto-{policy.id}-{claim.id}",
            policy=policy,
        )
    except ApprovalServiceError as exc:
        ledger.append(
            session,
            action=DEFERRED_ACTION,
            subject_type="claim",
            subject_id=claim.id,
            actor_kind=ActorKind.AGENT,
            agent=AgentName.LOGISTICS_AGENT,
            payload={
                "claim_ref": claim.claim_ref,
                "reason": f"refused at signing: {exc.detail}",
                "policy_id": str(policy.id),
                "money_movement": "NOT_INITIATED",
            },
        )
        return AutoApprovalDecision(
            False, f"refused at signing: {exc.detail}", policy_id=policy.id
        )

    ledger.append(
        session,
        action=DECIDED_ACTION,
        subject_type="claim",
        subject_id=claim.id,
        actor_kind=ActorKind.AGENT,
        agent=AgentName.LOGISTICS_AGENT,
        payload={
            "claim_ref": claim.claim_ref,
            "allocation_id": str(outcome.allocation.id),
            "policy_id": str(policy.id),
            "authorized_by": str(policy.created_by),
            "amount": f"{decision.amount:.2f}",
            "currency": "JMD",
            "reason": decision.reason,
            "money_movement": "NOT_INITIATED_AT_APPROVAL",
        },
    )
    return AutoApprovalDecision(
        True,
        decision.reason,
        amount=decision.amount,
        policy_id=policy.id,
        allocation_id=outcome.allocation.id,
    )


__all__ = [
    "AUTO_APPROVAL_JOB_TYPE",
    "DECIDED_ACTION",
    "DEFERRED_ACTION",
    "AutoApprovalDecision",
    "active_policy",
    "evaluate",
    "run_auto_approval",
]
