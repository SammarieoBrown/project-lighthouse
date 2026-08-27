"""Sending an approved cascade (ALT-02).

This is the only code that talks to a household, so the tests that matter most
are the ones that stop it: nothing without a signature, nothing for real, and
nothing recorded by phone number.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from lighthouse_contracts import (
    AlertChannel,
    AlertDeliveryStatus,
    AppRole,
    Event,
    GateKind,
    Posture,
)

from app.alert_dispatch_service import (
    FALLBACK_AFTER,
    CascadeNotApproved,
    confirm_delivery,
    dispatch_cascade,
    sweep_fallbacks,
)
from app.alert_service import propose_cascade
from app.models import AlertDelivery, Approval, LedgerEntry

from factories import make_user
from test_alert_agent import _advisory, _at_risk, _event_at


def _signed_cascade(session, *, households=1):
    event = _event_at(session, Posture.ACT)
    advisory = _advisory(session, event)
    files = [_at_risk(session, advisory) for _ in range(households)]
    proposal = propose_cascade(session, event, advisory)
    director = make_user(session, AppRole.DIRECTOR)
    approval = Approval(
        gate=GateKind.ALERT_CASCADE,
        subject_type="alert_cascade",
        subject_id=proposal.ledger_entry_id,
        approved_by=director.id,
        role_at_time=director.role,
        reauth_at=datetime.now(UTC),
        note="signed for dispatch",
    )
    session.add(approval)
    session.flush()
    return approval, files


def _deliveries(session, channel=None):
    stmt = select(AlertDelivery)
    if channel is not None:
        stmt = stmt.where(AlertDelivery.channel == channel)
    return list(session.scalars(stmt.order_by(AlertDelivery.attempted_at)))


# -- nothing sends without a signature --------------------------------------


def test_dispatch_requires_a_signed_cascade(session):
    """G1 made structural: alert_delivery.approval_id is NOT NULL and the
    approval has to be a real cascade signature."""
    with pytest.raises(CascadeNotApproved):
        dispatch_cascade(session, uuid.uuid4())


def test_an_approval_for_another_gate_cannot_send_an_alert(session):
    director = make_user(session, AppRole.DIRECTOR)
    approval = Approval(
        gate=GateKind.ALLOCATION_PLAN,
        subject_type="allocation_plan",
        subject_id=uuid.uuid4(),
        approved_by=director.id,
        role_at_time=director.role,
        reauth_at=datetime.now(UTC),
    )
    session.add(approval)
    session.flush()

    with pytest.raises(CascadeNotApproved):
        dispatch_cascade(session, approval.id)


# -- what a send records ----------------------------------------------------


def test_a_send_records_one_row_per_household_and_names_no_number(session):
    """ALT-02's acceptance criterion, and the no-PII rule is not suspended
    because the table is operational."""
    approval, files = _signed_cascade(session, households=2)

    result = dispatch_cascade(session, approval.id)

    assert result.queued == 2
    assert result.simulated is True
    rows = _deliveries(session)
    assert len(rows) == 2
    assert {row.channel for row in rows} == {AlertChannel.WHATSAPP}
    assert all(row.status is AlertDeliveryStatus.SENT for row in rows)
    for row, storm_file in zip(sorted(rows, key=lambda r: r.phone_hash),
                               sorted(files, key=lambda f: f.phone_hash)):
        assert row.phone_hash == storm_file.phone_hash
        assert storm_file.phone not in str(row.__dict__)


def test_nothing_leaves_the_process(session):
    """The registry is synthetic and so are its numbers. A real message to a
    real number is the one mistake here that cannot be taken back."""
    approval, _ = _signed_cascade(session)

    dispatch_cascade(session, approval.id)

    row = _deliveries(session)[0]
    assert row.simulated is True
    assert row.provider_ref.startswith("simulated:")


def test_dispatching_twice_does_not_message_anyone_twice(session):
    approval, _ = _signed_cascade(session)

    first = dispatch_cascade(session, approval.id)
    second = dispatch_cascade(session, approval.id)

    assert first.queued == 1
    assert second.queued == 0
    assert second.already_sent == 1
    assert len(_deliveries(session)) == 1


def test_the_send_is_a_ledger_event(session):
    approval, _ = _signed_cascade(session)

    dispatch_cascade(session, approval.id)
    session.flush()

    entry = session.scalar(
        select(LedgerEntry)
        .where(LedgerEntry.action == str(Event.ALERT_CASCADE_APPROVED))
        .order_by(LedgerEntry.seq.desc())
        .limit(1)
    )
    assert entry.payload["recipients"] == 1
    assert entry.payload["simulated"] is True
    assert entry.payload["channel"] == str(AlertChannel.WHATSAPP)


# -- the fallback -----------------------------------------------------------


def test_sms_follows_a_whatsapp_that_never_confirmed(session):
    approval, _ = _signed_cascade(session)
    dispatch_cascade(session, approval.id)

    later = datetime.now(UTC) + FALLBACK_AFTER + timedelta(minutes=1)
    created = sweep_fallbacks(session, now=later)

    assert len(created) == 1
    assert created[0].channel is AlertChannel.SMS
    whatsapp = _deliveries(session, AlertChannel.WHATSAPP)[0]
    # Not a failure. An attempt that did not confirm, which is the fact that
    # justifies sending a second message to somebody in a storm.
    assert whatsapp.status is AlertDeliveryStatus.SUPERSEDED


def test_a_confirmed_whatsapp_gets_no_sms(session):
    approval, _ = _signed_cascade(session)
    dispatch_cascade(session, approval.id)
    confirm_delivery(session, _deliveries(session)[0].id)

    later = datetime.now(UTC) + FALLBACK_AFTER + timedelta(minutes=1)
    created = sweep_fallbacks(session, now=later)

    assert created == []
    assert session.scalar(select(func.count()).select_from(AlertDelivery)) == 1


def test_the_fallback_waits_the_full_window(session):
    """Ten minutes is long enough that a phone in a blackout has a real chance
    to come back."""
    approval, _ = _signed_cascade(session)
    dispatch_cascade(session, approval.id)

    too_soon = datetime.now(UTC) + FALLBACK_AFTER - timedelta(minutes=1)
    assert sweep_fallbacks(session, now=too_soon) == []
    assert FALLBACK_AFTER == timedelta(minutes=10)


def test_the_sweep_does_not_send_a_second_sms(session):
    approval, _ = _signed_cascade(session)
    dispatch_cascade(session, approval.id)
    later = datetime.now(UTC) + FALLBACK_AFTER + timedelta(minutes=1)

    sweep_fallbacks(session, now=later)
    again = sweep_fallbacks(session, now=later + timedelta(hours=1))

    assert again == []
    assert len(_deliveries(session, AlertChannel.SMS)) == 1


def test_the_database_refuses_two_attempts_on_one_channel(session):
    """Resending an identical message to a phone that never confirmed is
    noise; the fallback exists because the first channel may be down."""
    approval, files = _signed_cascade(session)
    dispatch_cascade(session, approval.id)
    row = _deliveries(session)[0]

    with pytest.raises(Exception, match="alert_delivery_attempt_uidx"):
        with session.begin_nested():
            session.add(
                AlertDelivery(
                    approval_id=row.approval_id,
                    storm_file_id=row.storm_file_id,
                    phone_hash=row.phone_hash,
                    channel=AlertChannel.WHATSAPP,
                    status=AlertDeliveryStatus.SENT,
                )
            )
            session.flush()
