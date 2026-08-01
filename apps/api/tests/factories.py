"""Minimal builders for test data. Synthetic only, always."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from lighthouse_contracts import (
    AppRole,
    DisbursementChannel,
    DisbursementStatus,
    GateKind,
    PayerRoute,
    ResourceKind,
    StormFileState,
)

from app.models import (
    Allocation,
    AppUser,
    Approval,
    Claim,
    Disbursement,
    DisbursementBatch,
    HazardEvent,
    StormFile,
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


def settle_with_signature(
    session: Session, claim: Claim, finance: AppUser, amount: float = 45000.0
) -> Disbursement:
    """Walk the money path properly: allocate, sign, disburse, confirm.

    Written the long way on purpose. The signature is not a formality to be
    stubbed — it is the thing under test, and a shortcut here would hide the
    exact bug this schema exists to make impossible.
    """
    allocation = Allocation(
        claim_id=claim.id,
        resource=ResourceKind.CASH,
        amount=amount,
        payer_route=PayerRoute.GOV_RELIEF,
    )
    session.add(allocation)
    session.flush()

    batch = DisbursementBatch(channel=DisbursementChannel.BANK, total=amount)
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
