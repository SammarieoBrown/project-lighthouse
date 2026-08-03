"""Minimal builders for test data. Synthetic only, always."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from lighthouse_contracts import (
    ActorKind,
    AgentName,
    AppRole,
    DisbursementChannel,
    DisbursementStatus,
    GateKind,
    PayerRoute,
    ResourceKind,
    StormFileState,
    Verdict,
)

from app import ledger
from app.approvals import allocation_ledger_payload
from app.models import (
    Allocation,
    AllocationPlan,
    AppUser,
    Approval,
    Claim,
    Disbursement,
    DisbursementBatch,
    HazardEvent,
    StormFile,
    Verification,
)
from app.public_taxonomy import (
    canonical_public_need_category,
    canonical_public_parish,
)


def make_user(session: Session, role: AppRole = AppRole.FINANCE_OFFICER) -> AppUser:
    user = AppUser(
        email=f"{role.value.lower()}-{uuid.uuid4().hex[:6]}@example.test",
        display_name=f"Test {role.value}",
        role=role,
    )
    session.add(user)
    session.flush()
    return user


def make_event(session: Session, name: str = "Melissa (replay)") -> HazardEvent:
    event = HazardEvent(name=name, replay=True)
    session.add(event)
    session.flush()
    return event


def make_storm_file(
    session: Session, *, state: StormFileState = StormFileState.REGISTERED
) -> StormFile:
    suffix = uuid.uuid4().hex[:10]
    sf = StormFile(
        phone=f"+1876{suffix[:7]}",
        phone_hash=f"hash-{suffix}",
        head_name="Synthetic Household",
        parish="St Elizabeth",
        community="Newmarket",
        structure={"roof": "zinc", "walls": "timber", "floors": 1},
        people={"total": 5, "children": 2, "elderly": 1, "medical": ["insulin"]},
        vuln_score=78,
        state=state,
        synthetic=True,
    )
    session.add(sf)
    session.flush()
    return sf


def make_claim(session: Session, sf: StormFile, event: HazardEvent, **kw) -> Claim:
    claim = Claim(
        claim_ref=f"SE-{uuid.uuid4().int % 10000:04d}-{uuid.uuid4().hex[:4]}",
        storm_file_id=sf.id,
        hazard_event_id=event.id,
        damage_type=kw.pop("damage_type", "roof_loss"),
        reported_needs=kw.pop("reported_needs", ["tarpaulin", "water"]),
        transcript=kw.pop("transcript", "Mi roof gone, mi deh a Newmarket."),
        lang=kw.pop("lang", "jam"),
        channel=kw.pop("channel", "whatsapp"),
        **kw,
    )
    session.add(claim)
    session.flush()
    return claim


def make_verification(
    session: Session,
    claim: Claim,
    *,
    verdict: Verdict = Verdict.AUTO_VERIFIED,
    confidence: float = 0.90,
    capped: bool = False,
    actor: AppUser | None = None,
    signals: dict | None = None,
    created_at: datetime | None = None,
) -> Verification:
    """Insert real immutable evidence and refresh its database snapshot hash."""
    if signals is None:
        signals = {
            name: {"present": True, "score": 0.9, "evidence": {}}
            for name in (
                "hazard_sufficiency",
                "satellite_change",
                "neighbour_corroboration",
                "registry_match",
                "media_integrity",
            )
        }
    verification = Verification(
        claim_id=claim.id,
        signals=signals,
        confidence=confidence,
        verdict=verdict,
        actor_kind=ActorKind.HUMAN if actor is not None else ActorKind.AGENT,
        actor_id=actor.id if actor is not None else None,
        agent_name=None if actor is not None else str(AgentName.VERIFICATION_AGENT),
        model_version="test-verification-v1",
        threshold_version="test-threshold-v1",
        rationale="Synthetic test evidence met the configured threshold.",
        capped=capped,
        **({"created_at": created_at} if created_at is not None else {}),
    )
    session.add(verification)
    session.flush()
    # snapshot_hash is assigned by a BEFORE INSERT trigger. Refreshing is
    # intentional: relying on the identity map here would bind a stale None.
    session.refresh(verification)
    assert verification.snapshot_hash
    return verification


def settle_with_signature(
    session: Session, claim: Claim, finance: AppUser, amount: float = 45000.0
) -> Disbursement:
    """Walk the money path properly: allocate, sign, disburse, confirm.

    Written the long way on purpose. The signature is not a formality to be
    stubbed — it is the thing under test, and a shortcut here would hide the
    exact bug this schema exists to make impossible.
    """
    if Decimal(str(amount)) != Decimal("45000.00"):
        raise ValueError("the release fixture only supports the fixed JMD 45000 grant")

    director = make_user(session, AppRole.DIRECTOR)
    verification = make_verification(session, claim)
    session.execute(
        text(
            "SET CONSTRAINTS signed_plan_complete_trigger, "
            "allocation_ledger_complete_trigger DEFERRED"
        )
    )
    plan_id = uuid.uuid4()
    allocation_approval = Approval(
        gate=GateKind.ALLOCATION_PLAN,
        subject_type="allocation_plan",
        subject_id=plan_id,
        approved_by=director.id,
        role_at_time=AppRole.DIRECTOR,
        reauth_at=datetime.now(UTC),
        note="test allocation signature",
    )
    session.add(allocation_approval)
    session.flush()
    plan = AllocationPlan(
        id=plan_id,
        hazard_event_id=claim.hazard_event_id,
        proposed_by="test_fixture",
        approval_id=allocation_approval.id,
    )
    session.add(plan)
    session.flush()

    allocation = Allocation(
        plan_id=plan.id,
        claim_id=claim.id,
        resource=ResourceKind.CASH,
        amount=Decimal("45000.00"),
        currency="JMD",
        payer_route=PayerRoute.GOV_RELIEF,
        verification_id=verification.id,
        verification_snapshot_hash=verification.snapshot_hash,
    )
    session.add(allocation)
    session.flush()

    storm_file = session.get(StormFile, claim.storm_file_id)
    assert storm_file is not None
    ledger.append(
        session,
        action="allocation.approved",
        subject_type="allocation",
        subject_id=allocation.id,
        actor_kind=ActorKind.HUMAN,
        actor_id=director.id,
        payload=allocation_ledger_payload(
            approval=allocation_approval,
            plan=plan,
            allocation=allocation,
            claim=claim,
            verification=verification,
            parish=canonical_public_parish(storm_file.parish),
            need_category=canonical_public_need_category(claim.damage_type),
            synthetic=storm_file.synthetic,
        ),
    )
    session.execute(
        text(
            "SET CONSTRAINTS signed_plan_complete_trigger, "
            "allocation_ledger_complete_trigger IMMEDIATE"
        )
    )

    batch = DisbursementBatch(
        channel=DisbursementChannel.BANK, total=Decimal("45000.00")
    )
    session.add(batch)
    session.flush()

    approval = Approval(
        gate=GateKind.DISBURSEMENT_BATCH,
        subject_type="disbursement_batch",
        subject_id=batch.id,
        approved_by=finance.id,
        role_at_time=AppRole.FINANCE_OFFICER,
        reauth_at=datetime.now(UTC),
        note="test signature",
    )
    session.add(approval)
    session.flush()

    batch.approval_id = approval.id

    disb = Disbursement(
        allocation_id=allocation.id,
        batch_id=batch.id,
        approval_id=approval.id,
        channel=DisbursementChannel.BANK,
        status=DisbursementStatus.CONFIRMED,
        simulated=True,
        executed_at=datetime.now(UTC),
        confirmed_at=datetime.now(UTC),
    )
    session.add(disb)
    session.flush()
    return disb
