"""Delegated approval: bounded, revocable, and refused by the database.

The agent decides whether to try; these tests care most about what happens
when it tries something it was never authorized to do, because that is the
only part a demo would not show.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from lighthouse_contracts import (
    ActorKind,
    AppRole,
    ClaimStatus,
    DamageAssessmentVerdict,
    PayerRoute,
    ResourceKind,
    StormFileState,
    Verdict,
)

from app.approvals import AllocationApprovalRequest, approve_claim_allocation
from app.auto_approval_service import (
    DECIDED_ACTION,
    DEFERRED_ACTION,
    evaluate,
    run_auto_approval,
)
from app.donations_service import create_pool, record_donation
from app.models import (
    Allocation,
    AutoApprovalPolicy,
    DamageAssessment,
    DonationPool,
    LedgerEntry,
)

from factories import (
    make_claim,
    make_event,
    make_storm_file,
    make_user,
    make_verification,
)


def _policy(
    session,
    event,
    *,
    director,
    max_amount="60000.00",
    min_confidence="0.85",
    min_signals=4,
    requires_assessment=True,
    pool=None,
) -> AutoApprovalPolicy:
    policy = AutoApprovalPolicy(
        hazard_event_id=event.id,
        max_amount=Decimal(max_amount),
        min_confidence=Decimal(min_confidence),
        min_signals=min_signals,
        requires_assessment=requires_assessment,
        payer_route=PayerRoute.DONOR_POOL if pool else PayerRoute.GOV_RELIEF,
        pool_id=pool.id if pool else None,
        created_by=director.id,
        role_at_time=AppRole.DIRECTOR,
        reauth_at=datetime.now(UTC),
    )
    session.add(policy)
    session.flush()
    return policy


def _assessed_claim(session, event, *, high="40000.00", confidence=0.9, signals=5):
    storm_file = make_storm_file(session, state=StormFileState.VERIFIED)
    claim = make_claim(session, storm_file, event, status=ClaimStatus.VERIFIED)
    present = {
        name: {"present": index < signals, "score": 0.9 if index < signals else None,
               "evidence": {}}
        for index, name in enumerate(
            (
                "hazard_sufficiency",
                "satellite_change",
                "neighbour_corroboration",
                "registry_match",
                "media_integrity",
            )
        )
    }
    make_verification(session, claim, confidence=confidence, signals=present)
    assessment = DamageAssessment(
        claim_id=claim.id,
        storm_file_id=storm_file.id,
        band="MAJOR",
        estimate_low=Decimal("20000.00"),
        estimate_high=Decimal(high),
        currency="JMD",
        confidence=0.8,
        findings=[],
        evidence_ids=[],
        location_source="claim",
        verdict=DamageAssessmentVerdict.PROPOSED,
        actor_kind=ActorKind.AGENT,
        agent_name="damage_assessment_agent",
        model_version="test",
    )
    session.add(assessment)
    session.flush()
    return storm_file, claim


def test_a_small_well_evidenced_claim_is_approved_under_the_authorization(session):
    event = make_event(session)
    director = make_user(session, role=AppRole.DIRECTOR)
    pool = create_pool(session, name="St Elizabeth pool", scope_kind="EVENT")
    record_donation(session, pool_id=pool.id, donor_handle="d", amount=Decimal("90000.00"))
    _policy(session, event, director=director, pool=pool)
    _, claim = _assessed_claim(session, event, high="40000.00")

    decision = run_auto_approval(session, claim.id)

    assert decision.approved is True
    allocation = session.scalar(select(Allocation))
    assert allocation.amount == Decimal("40000.00")
    assert allocation.payer_route is PayerRoute.DONOR_POOL
    # The pool actually paid for it.
    assert session.get(DonationPool, pool.id).balance == Decimal("50000.00")
    entry = session.scalar(
        select(LedgerEntry).where(LedgerEntry.action == DECIDED_ACTION)
    )
    assert entry.payload["amount"] == "40000.00"


def test_a_claim_above_the_ceiling_is_left_for_a_human_with_its_reason(session):
    event = make_event(session)
    director = make_user(session, role=AppRole.DIRECTOR)
    _policy(session, event, director=director, max_amount="60000.00")
    _, claim = _assessed_claim(session, event, high="150000.00")

    decision = run_auto_approval(session, claim.id)

    assert decision.approved is False
    assert "above the authorized ceiling" in decision.reason
    assert session.scalar(select(func.count()).select_from(Allocation)) == 0
    entry = session.scalar(
        select(LedgerEntry).where(LedgerEntry.action == DEFERRED_ACTION)
    )
    assert "ceiling" in entry.payload["reason"]


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"confidence": 0.6}, "below the authorized"),
        ({"signals": 2}, "signals scored"),
    ],
)
def test_thin_evidence_is_left_for_a_human(session, kwargs, expected):
    event = make_event(session)
    director = make_user(session, role=AppRole.DIRECTOR)
    _policy(session, event, director=director)
    _, claim = _assessed_claim(session, event, **kwargs)

    decision = run_auto_approval(session, claim.id)

    assert decision.approved is False
    assert expected in decision.reason
    assert session.scalar(select(func.count()).select_from(Allocation)) == 0


def test_an_underfunded_pool_is_left_for_a_human(session):
    event = make_event(session)
    director = make_user(session, role=AppRole.DIRECTOR)
    pool = create_pool(session, name="Thin pool", scope_kind="EVENT")
    record_donation(session, pool_id=pool.id, donor_handle="d", amount=Decimal("1000.00"))
    _policy(session, event, director=director, pool=pool)
    _, claim = _assessed_claim(session, event, high="40000.00")

    decision = run_auto_approval(session, claim.id)

    assert decision.approved is False
    assert "holds J$1,000.00" in decision.reason
    assert session.get(DonationPool, pool.id).balance == Decimal("1000.00")


def test_a_revoked_authorization_stops_covering_claims(session):
    event = make_event(session)
    director = make_user(session, role=AppRole.DIRECTOR)
    policy = _policy(session, event, director=director)
    policy.revoked_at = datetime.now(UTC)
    policy.revoked_by = director.id
    session.flush()
    _, claim = _assessed_claim(session, event)

    decision = run_auto_approval(session, claim.id)

    assert decision.approved is False
    assert decision.reason == "no standing authorization is in force"


def test_the_database_refuses_an_agent_signature_above_the_ceiling(session):
    """The bound is the database's, not the agent's — so break the agent."""
    event = make_event(session)
    director = make_user(session, role=AppRole.DIRECTOR)
    policy = _policy(session, event, director=director, max_amount="10000.00")
    _, claim = _assessed_claim(session, event)

    request = AllocationApprovalRequest(
        resource=ResourceKind.CASH,
        amount=Decimal("500000.00"),
        currency="JMD",
        payer_route=PayerRoute.GOV_RELIEF,
        note="a policy-signed request that exceeds what was authorized",
    )
    with pytest.raises(DBAPIError, match="exceeds the authorized ceiling"):
        approve_claim_allocation(
            session,
            claim_id=claim.id,
            request=request,
            idempotency_key=str(uuid.uuid4()),
            policy=policy,
        )


def test_the_database_refuses_an_agent_signature_on_a_revoked_authorization(session):
    event = make_event(session)
    director = make_user(session, role=AppRole.DIRECTOR)
    policy = _policy(session, event, director=director)
    policy.revoked_at = datetime.now(UTC)
    policy.revoked_by = director.id
    session.flush()
    _, claim = _assessed_claim(session, event)

    request = AllocationApprovalRequest(
        resource=ResourceKind.CASH,
        amount=Decimal("40000.00"),
        currency="JMD",
        payer_route=PayerRoute.GOV_RELIEF,
        note="drawing on authority that was withdrawn",
    )
    with pytest.raises(DBAPIError, match="revoked authorization"):
        approve_claim_allocation(
            session,
            claim_id=claim.id,
            request=request,
            idempotency_key=str(uuid.uuid4()),
            policy=policy,
        )


def test_an_auto_approval_still_names_the_authorizing_director(session):
    event = make_event(session)
    director = make_user(session, role=AppRole.DIRECTOR)
    policy = _policy(session, event, director=director)
    _, claim = _assessed_claim(session, event, high="30000.00")

    run_auto_approval(session, claim.id)

    from app.models import Approval

    approval = session.scalar(select(Approval))
    assert approval.approved_by == director.id
    assert approval.role_at_time is AppRole.DIRECTOR
    assert approval.policy_id == policy.id
    # The reauthentication is the one the Director actually performed.
    assert approval.reauth_at == policy.reauth_at


def test_a_replayed_job_does_not_sign_a_second_grant(session):
    event = make_event(session)
    director = make_user(session, role=AppRole.DIRECTOR)
    _policy(session, event, director=director)
    _, claim = _assessed_claim(session, event, high="30000.00")

    first = run_auto_approval(session, claim.id)
    second = run_auto_approval(session, claim.id)

    assert first.approved is True
    assert second.approved is False
    assert session.scalar(select(func.count()).select_from(Allocation)) == 1
