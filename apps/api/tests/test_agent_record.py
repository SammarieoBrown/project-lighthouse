"""The agent record an operator reads, and the notice a household receives."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import ledger
from lighthouse_contracts import (
    ActorKind,
    AgentName,
    AppRole,
    ClaimStatus,
    DamageAssessmentVerdict,
    DisbursementChannel,
    DisbursementStatus,
    StormFileState,
)

from app.intake.claims import claim_agent_timeline
from app.models import DamageAssessment, Disbursement, LedgerEntry
from app.relief_notifications import (
    NOTICE_SENT_ACTION,
    notify_relief_confirmed,
    voucher_code,
)

from factories import make_claim, make_event, make_storm_file, make_verification


def _claim_with_history(session):
    storm_file = make_storm_file(session, state=StormFileState.VERIFIED)
    event = make_event(session)
    claim = make_claim(session, storm_file, event, status=ClaimStatus.VERIFIED)
    make_verification(session, claim, confidence=0.88)
    session.add(
        DamageAssessment(
            claim_id=claim.id,
            storm_file_id=storm_file.id,
            band="MAJOR",
            estimate_low=Decimal("20000.00"),
            estimate_high=Decimal("60000.00"),
            currency="JMD",
            confidence=0.75,
            findings=[
                {
                    "evidence_id": str(uuid.uuid4()),
                    "observed_damage": "roof sheeting torn away",
                    "band": "MAJOR",
                    "confidence": 0.8,
                }
            ],
            evidence_ids=[],
            location_source="claim",
            verdict=DamageAssessmentVerdict.PROPOSED,
            actor_kind=ActorKind.AGENT,
            agent_name="damage_assessment_agent",
            model_version="anthropic:test",
            rationale="The photos show a roof opened to the sky.",
        )
    )
    # The factories write rows, not history; the triage entry is what a real
    # claim would carry by this point and is what the ledger source reads.
    ledger.append(
        session,
        action="claim.triaged",
        subject_type="claim",
        subject_id=claim.id,
        actor_kind=ActorKind.AGENT,
        agent=AgentName.TRIAGE_AGENT,
        payload={"claim_id": str(claim.id), "severity": "HIGH", "rank": 12},
    )
    session.flush()
    return storm_file, claim


def test_the_timeline_carries_every_agent_that_touched_the_claim(session):
    _, claim = _claim_with_history(session)

    events = claim_agent_timeline(session, claim.id)

    sources = {event["source"] for event in events}
    assert {"verification", "damage_assessment", "ledger"} <= sources
    # Oldest first, so an operator reads it as a story rather than a stack.
    stamps = [event["at"] for event in events if event["at"] is not None]
    assert stamps == sorted(stamps)

    verification = next(e for e in events if e["source"] == "verification")
    assert verification["data"]["confidence"] == pytest.approx(0.88)
    assert verification["data"]["signals_scored"] == 5

    assessment = next(e for e in events if e["source"] == "damage_assessment")
    assert assessment["detail"] == "The photos show a roof opened to the sky."
    assert assessment["data"]["estimate_high"] == 60000.0
    assert assessment["data"]["findings"][0]["observed_damage"] == "roof sheeting torn away"


def test_the_timeline_never_carries_the_household_message(session):
    _, claim = _claim_with_history(session)
    claim.transcript = "Mi roof gone and mi need help"
    session.flush()

    import json

    serialized = json.dumps(claim_agent_timeline(session, claim.id), default=str)

    # The transcript has its own place on the claim pane; the agent record is
    # about what the machine did, and re-exporting the household's words here
    # would put them in a second, less obvious place.
    assert "Mi roof gone" not in serialized


def _confirmed_disbursement(reference: str) -> Disbursement:
    return Disbursement(
        id=uuid.uuid4(),
        allocation_id=uuid.uuid4(),
        batch_id=uuid.uuid4(),
        approval_id=uuid.uuid4(),
        channel=DisbursementChannel.BANK,
        status=DisbursementStatus.CONFIRMED,
        simulated=True,
        executor_provider="LIGHTHOUSE_DEMO_EXECUTOR_V1",
        external_ref=reference,
    )


def test_the_voucher_reference_is_derived_from_the_confirmation():
    confirmed = _confirmed_disbursement("DEMO-7C0D52656564EB5D9FAB7B23")
    assert voucher_code(confirmed) == "LH-9FAB7B23"
    # Nothing to quote before the rail has answered.
    assert voucher_code(_confirmed_disbursement("")) is None


def test_a_live_notice_says_it_is_simulated_and_is_ledgered_once(session, monkeypatch):
    storm_file, claim = _claim_with_history(session)
    disbursement = _confirmed_disbursement("DEMO-7C0D52656564EB5D9FAB7B23")
    sent: list[tuple[str, str]] = []

    from app import relief_notifications

    live = SimpleNamespace(relief_notice_mode="live")
    monkeypatch.setattr(relief_notifications, "get_settings", lambda: live)
    monkeypatch.setattr(
        relief_notifications,
        "_send",
        lambda *, to_phone, body: sent.append((to_phone, body)),
    )

    code = notify_relief_confirmed(
        session,
        claim=claim,
        storm_file=storm_file,
        disbursement=disbursement,
        amount="60,000.00",
    )

    assert code == "LH-9FAB7B23"
    body = sent[0][1]
    assert claim.claim_ref in body and "LH-9FAB7B23" in body
    # The one claim this system may never make.
    assert "no real funds have moved" in body

    # A replayed execution must not text the household twice.
    notify_relief_confirmed(
        session,
        claim=claim,
        storm_file=storm_file,
        disbursement=disbursement,
        amount="60,000.00",
    )
    assert len(sent) == 1
    entries = session.scalars(
        select(LedgerEntry).where(LedgerEntry.action == NOTICE_SENT_ACTION)
    ).all()
    assert len(entries) == 1


def test_a_failed_notice_never_fails_the_settlement(session, monkeypatch):
    storm_file, claim = _claim_with_history(session)
    disbursement = _confirmed_disbursement("DEMO-7C0D52656564EB5D9FAB7B23")

    from app import relief_notifications

    live = SimpleNamespace(relief_notice_mode="live")
    monkeypatch.setattr(relief_notifications, "get_settings", lambda: live)

    def explode(**_kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(relief_notifications, "_send", explode)

    assert (
        notify_relief_confirmed(
            session,
            claim=claim,
            storm_file=storm_file,
            disbursement=disbursement,
            amount="60,000.00",
        )
        is None
    )
    assert (
        session.scalar(
            select(LedgerEntry).where(LedgerEntry.action == NOTICE_SENT_ACTION)
        )
        is None
    )
