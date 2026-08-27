"""Alert: drafting a cascade, and the signature that stands between it and a
household's phone.

This is the one agent output that reaches a person directly, so what these
tests hold hardest is the negative: it drafts, it records, and it does not
send.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from lighthouse_contracts import (
    AgentName,
    AppRole,
    DamageBand,
    Event,
    GateKind,
    Posture,
    StormFileState,
)

from app import alert_approvals
from app.agents.alert_agent import handle as alert_handle
from app.alert_service import NothingToAlert, build_cascade, propose_cascade
from app.approval_credentials import issue_human_credential, set_human_password
from app.models import Advisory, Approval, HazardEvent, LedgerEntry, RiskAssessment
from app.web import app
from app.worker import load_handlers

from factories import make_event, make_storm_file, make_user


def _advisory(session, event, number="12"):
    advisory = Advisory(
        hazard_event_id=event.id,
        advisory_number=number,
        issued_at=datetime(2025, 10, 27, 15, 0, tzinfo=UTC),
    )
    session.add(advisory)
    session.flush()
    return advisory


def _at_risk(session, advisory, *, band=DamageBand.MAJOR, community="Newmarket",
             state=StormFileState.REGISTERED):
    sf = make_storm_file(session, state=state)
    sf.community = community
    session.add(
        RiskAssessment(
            storm_file_id=sf.id,
            advisory_id=advisory.id,
            p34=0.8,
            predicted_band=band,
            confidence=0.7,
            method="parametric_lookup_v1",
            model_version="test-v1",
        )
    )
    session.flush()
    return sf


def _event_at(session, posture: Posture) -> HazardEvent:
    event = make_event(session)
    event.current_posture = posture
    session.flush()
    return event


def _client(monkeypatch, session) -> TestClient:
    @contextmanager
    def scoped():
        yield session
        session.flush()

    monkeypatch.setattr(alert_approvals, "session_scope", scoped)
    return TestClient(app)


def _director(session):
    user = make_user(session, AppRole.DIRECTOR)
    password = "correct horse lighthouse"
    set_human_password(session, email=user.email, password=password)
    return user, issue_human_credential(session, email=user.email, password=password)


# -- who gets drafted for --------------------------------------------------


def test_a_cascade_covers_only_households_in_an_alertable_band(session):
    """Alerting a whole parish because a storm threatens the island is how
    people learn to ignore alerts."""
    event = _event_at(session, Posture.READY)
    advisory = _advisory(session, event)
    _at_risk(session, advisory, band=DamageBand.MAJOR, community="Newmarket")
    _at_risk(session, advisory, band=DamageBand.NONE, community="Siloah")

    output = build_cascade(session, event, advisory)

    assert {d.community for d in output.drafts} == {"Newmarket"}
    assert sum(d.recipient_count for d in output.drafts) == 1


def test_drafts_are_grouped_by_community_with_a_recipient_count(session):
    event = _event_at(session, Posture.ACT)
    advisory = _advisory(session, event)
    for _ in range(3):
        _at_risk(session, advisory, community="Newmarket")
    _at_risk(session, advisory, community="Siloah")

    output = build_cascade(session, event, advisory)

    counts = {d.community: d.recipient_count for d in output.drafts}
    assert counts == {"Newmarket": 3, "Siloah": 1}


def test_a_quiet_posture_drafts_nothing(session):
    """An agent that drafts a cascade with nothing coming is an agent that
    will eventually send one."""
    event = _event_at(session, Posture.QUIET)
    advisory = _advisory(session, event)
    _at_risk(session, advisory)

    with pytest.raises(NothingToAlert, match="QUIET"):
        build_cascade(session, event, advisory)


def test_nobody_at_risk_drafts_nothing(session):
    event = _event_at(session, Posture.ACT)
    advisory = _advisory(session, event)

    with pytest.raises(NothingToAlert, match="alertable band"):
        build_cascade(session, event, advisory)


# -- what the message says --------------------------------------------------


def test_every_draft_carries_both_languages_and_a_voice_script(session):
    """A household reading the Patois line first should not be reading a worse
    message."""
    event = _event_at(session, Posture.ACT)
    advisory = _advisory(session, event)
    _at_risk(session, advisory)

    draft = build_cascade(session, event, advisory).drafts[0]

    assert draft.text_en and draft.text_patois
    assert draft.text_en != draft.text_patois
    assert draft.voice_script_patois == draft.text_patois
    assert "Newmarket" in draft.text_en and "Newmarket" in draft.text_patois
    assert draft.preparation_steps


def test_the_message_escalates_with_the_posture(session):
    event = _event_at(session, Posture.WATCH)
    advisory = _advisory(session, event)
    _at_risk(session, advisory)
    watch = build_cascade(session, event, advisory).drafts[0]

    event.current_posture = Posture.ACT
    session.flush()
    act = build_cascade(session, event, advisory).drafts[0]

    assert "time to prepare" in watch.text_en
    assert "move to safety now" in act.text_en
    assert len(act.preparation_steps) >= len(watch.preparation_steps)


def test_no_shelter_is_named_because_there_is_no_shelter_registry(session):
    """LGX-04 is unbuilt. Naming a shelter we cannot confirm is open would be
    the single most dangerous thing this agent could do."""
    event = _event_at(session, Posture.ACT)
    advisory = _advisory(session, event)
    _at_risk(session, advisory)

    output = build_cascade(session, event, advisory)

    assert all(d.nearest_shelter is None for d in output.drafts)
    assert "shelter registry" in output.rationale


# -- propose only -----------------------------------------------------------


def test_proposing_records_the_cascade_raw_and_sends_nothing(session):
    event = _event_at(session, Posture.ACT)
    advisory = _advisory(session, event)
    _at_risk(session, advisory)

    proposal = propose_cascade(session, event, advisory)
    session.flush()

    entry = session.scalar(
        select(LedgerEntry).where(LedgerEntry.id == proposal.ledger_entry_id)
    )
    assert entry.action == str(Event.ALERT_CASCADE_PROPOSED)
    assert entry.agent_name == str(AgentName.ALERT_AGENT)
    assert entry.payload["requires_approval"] is True
    assert entry.payload["shelter_registry_available"] is False
    assert entry.payload["cascade"]["drafts"]
    # Proposing is not approving.
    assert session.scalar(select(func.count()).select_from(Approval)) == 0


def test_the_handler_reads_the_posture_now_not_the_posture_when_queued(session):
    """The payload records what was true when the job was queued. By the time
    it runs the storm may have moved, and a household should be warned about
    where things actually stand."""
    event = _event_at(session, Posture.WATCH)
    advisory = _advisory(session, event)
    _at_risk(session, advisory)

    event.current_posture = Posture.ACT
    session.flush()
    alert_handle(session, {"advisory_id": str(advisory.id), "posture": "WATCH"})
    session.flush()

    entry = session.scalar(
        select(LedgerEntry)
        .where(LedgerEntry.action == str(Event.ALERT_CASCADE_PROPOSED))
        .order_by(LedgerEntry.seq.desc())
        .limit(1)
    )
    assert entry.payload["posture"] == "ACT"


# -- gate G1 ----------------------------------------------------------------


def test_a_director_signature_authorises_a_cascade_without_sending_it(
    session, monkeypatch
):
    event = _event_at(session, Posture.ACT)
    advisory = _advisory(session, event)
    _at_risk(session, advisory)
    proposal = propose_cascade(session, event, advisory)
    director, issued = _director(session)
    client = _client(monkeypatch, session)

    response = client.post(
        f"/v1/hazard-events/{event.id}/alerts/approve",
        json={
            "proposal_id": str(proposal.ledger_entry_id),
            "note": "Wording checked against the 5pm advisory before signing.",
        },
        headers={"Authorization": f"Bearer {issued.token}"},
    )

    assert response.status_code == 201, response.text
    assert response.json()["cascade"]["delivery"] == "NOT_SENT_AT_APPROVAL"
    approval = session.scalar(select(Approval))
    assert approval.gate is GateKind.ALERT_CASCADE
    assert approval.approved_by == director.id
    receipt = session.scalar(
        select(LedgerEntry).where(
            LedgerEntry.action == str(Event.ALERT_CASCADE_APPROVED)
        )
    )
    assert receipt.payload["delivery"] == "NOT_SENT_AT_APPROVAL"


def test_only_a_director_may_sign_a_cascade(session, monkeypatch):
    event = _event_at(session, Posture.ACT)
    advisory = _advisory(session, event)
    _at_risk(session, advisory)
    proposal = propose_cascade(session, event, advisory)
    clerk = make_user(session, AppRole.REVIEW_CLERK)
    set_human_password(session, email=clerk.email, password="correct horse lighthouse")
    issued = issue_human_credential(
        session, email=clerk.email, password="correct horse lighthouse"
    )
    client = _client(monkeypatch, session)

    response = client.post(
        f"/v1/hazard-events/{event.id}/alerts/approve",
        json={
            "proposal_id": str(proposal.ledger_entry_id),
            "note": "A Review Clerk should not be able to do this.",
        },
        headers={"Authorization": f"Bearer {issued.token}"},
    )

    assert response.status_code == 403
    assert session.scalar(select(func.count()).select_from(Approval)) == 0


def test_a_cascade_is_signed_once(session, monkeypatch):
    """A second signature is a second authorisation to reach the same
    households with the same words."""
    event = _event_at(session, Posture.ACT)
    advisory = _advisory(session, event)
    _at_risk(session, advisory)
    proposal = propose_cascade(session, event, advisory)
    director, issued = _director(session)
    client = _client(monkeypatch, session)
    body = {
        "proposal_id": str(proposal.ledger_entry_id),
        "note": "Wording checked against the 5pm advisory before signing.",
    }
    headers = {"Authorization": f"Bearer {issued.token}"}

    first = client.post(f"/v1/hazard-events/{event.id}/alerts/approve", json=body, headers=headers)
    second = client.post(f"/v1/hazard-events/{event.id}/alerts/approve", json=body, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["idempotent_replay"] is True
    assert session.scalar(select(func.count()).select_from(Approval)) == 1


def test_signing_a_proposal_from_another_event_is_refused(session, monkeypatch):
    event = _event_at(session, Posture.ACT)
    advisory = _advisory(session, event)
    _at_risk(session, advisory)
    proposal = propose_cascade(session, event, advisory)
    other = _event_at(session, Posture.ACT)
    director, issued = _director(session)
    client = _client(monkeypatch, session)

    response = client.post(
        f"/v1/hazard-events/{other.id}/alerts/approve",
        json={
            "proposal_id": str(proposal.ledger_entry_id),
            "note": "This proposal belongs to a different storm entirely.",
        },
        headers={"Authorization": f"Bearer {issued.token}"},
    )

    assert response.status_code == 404


def test_worker_registers_the_alert_agent():
    assert str(AgentName.ALERT_AGENT) in load_handlers()
