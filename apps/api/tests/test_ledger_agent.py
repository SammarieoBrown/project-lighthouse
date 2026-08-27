"""The Ledger Agent: the one agent that audits the others.

What matters most here is the negative. It finds things, it records what it
found, and it fixes nothing — an auditor that can close its own findings is
not an auditor, and that is exactly why this one is allowed to run without
asking anybody.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select, text

from lighthouse_contracts import (
    AgentName,
    AppRole,
    ClaimStatus,
    DisbursementStatus,
    Event,
    StormFileState,
)

from app.agents.ledger_agent import LedgerAgentNotRunnable
from app.agents.ledger_agent import handle as ledger_handle
from app.ledger_agent_service import reconcile
from app.models import Disbursement, LedgerEntry, StormFile
from app.worker import load_handlers

from factories import (
    make_claim,
    make_event,
    make_storm_file,
    make_user,
    make_verification,
    settle_with_signature,
)


def _settled_claim(session):
    """Walk the real demo path all the way to a confirmed payment."""
    event = make_event(session)
    sf = make_storm_file(session, state=StormFileState.VERIFIED)
    claim = make_claim(session, sf, event, status=ClaimStatus.VERIFIED)
    make_verification(session, claim)
    finance = make_user(session, AppRole.FINANCE_OFFICER)
    disbursement = settle_with_signature(session, claim, finance)
    session.flush()
    return event, sf, claim, disbursement


def _anomalies(session, kind: str) -> list[LedgerEntry]:
    return list(
        session.scalars(
            select(LedgerEntry)
            .where(
                LedgerEntry.action == str(Event.ANOMALY_FLAGGED),
                LedgerEntry.payload["kind"].astext == kind,
            )
            .order_by(LedgerEntry.seq)
        )
    )


def test_a_clean_event_reconciles_with_nothing_to_say(session):
    event, sf, claim, disbursement = _settled_claim(session)

    run = reconcile(session, event.id)

    assert run.output.reconciled_count == 1
    assert run.output.anomalies == []
    assert run.output.chain_valid is True
    assert run.flagged == 0
    assert "intact" in run.output.rationale


def test_a_paid_household_still_reading_as_waiting_is_flagged(session):
    """Confirmed money and an unsettled household is what somebody calls the
    office about."""
    event, sf, claim, disbursement = _settled_claim(session)
    # Reach past the state machine to create the disagreement it exists to
    # prevent, which is the only way to prove the audit would catch it.
    session.execute(
        text("UPDATE storm_file SET state = 'VERIFIED' WHERE id = :id"), {"id": sf.id}
    )
    session.flush()
    session.expire(sf)

    run = reconcile(session, event.id)

    kinds = {a.kind for a in run.output.anomalies}
    assert "UNCONFIRMED" in kinds
    flagged = _anomalies(session, "UNCONFIRMED")
    assert any(entry.payload["subject_id"] == str(sf.id) for entry in flagged)


def test_a_confirmed_payment_cannot_be_edited_to_fake_a_finding(session):
    """Worth stating: the reason the stuck-execution branch has no test is that
    the state is unreachable, not that it is untested by oversight.

    This release's executor confirms in the same call that executes, and a
    confirmed disbursement is immutable, so there is no way to produce a row
    sitting in EXECUTING. The branch is kept for PAY-03's real rail, where
    execution and confirmation genuinely separate.
    """
    event, sf, claim, disbursement = _settled_claim(session)

    with pytest.raises(Exception, match="illegal disbursement lifecycle transition"):
        with session.begin_nested():
            session.execute(
                text(
                    "UPDATE disbursement SET status = 'EXECUTING' WHERE id = :id"
                ),
                {"id": disbursement.id},
            )

    run = reconcile(session, event.id)
    assert run.output.reconciled_count == 1
    assert run.output.anomalies == []


def test_a_finding_is_recorded_once_not_every_run(session):
    """A standing finding is not news twice, and the agent cannot clear it —
    re-flagging every run would bury the new finding under the old ones."""
    event, sf, claim, disbursement = _settled_claim(session)
    session.execute(
        text("UPDATE storm_file SET state = 'VERIFIED' WHERE id = :id"), {"id": sf.id}
    )
    session.flush()

    first = reconcile(session, event.id)
    second = reconcile(session, event.id)
    session.flush()

    assert first.flagged >= 1
    assert second.flagged == 0
    assert second.output.anomalies  # still found, just not re-recorded
    assert len(_anomalies(session, "UNCONFIRMED")) == first.flagged


def test_the_agent_records_findings_and_fixes_nothing(session):
    event, sf, claim, disbursement = _settled_claim(session)
    session.execute(
        text("UPDATE storm_file SET state = 'VERIFIED' WHERE id = :id"), {"id": sf.id}
    )
    session.flush()

    reconcile(session, event.id)
    session.flush()
    session.expire_all()

    # The household it complained about is exactly as it found it.
    assert session.get(StormFile, sf.id).state is StormFileState.VERIFIED
    assert (
        session.get(Disbursement, disbursement.id).status
        is DisbursementStatus.CONFIRMED
    )
    entry = _anomalies(session, "UNCONFIRMED")[0]
    assert entry.payload["resolution"] == "REQUIRES_HUMAN"
    assert entry.agent_name == str(AgentName.LEDGER_AGENT)


def test_every_flag_is_itself_a_ledger_entry(session):
    """LGR-04. A missed payment and the discovery of a missed payment are both
    permanent."""
    event, sf, claim, disbursement = _settled_claim(session)
    session.execute(
        text("UPDATE storm_file SET state = 'VERIFIED' WHERE id = :id"), {"id": sf.id}
    )
    session.flush()
    before = session.scalar(select(func.count()).select_from(LedgerEntry))

    run = reconcile(session, event.id)
    session.flush()

    after = session.scalar(select(func.count()).select_from(LedgerEntry))
    assert after == before + run.flagged
    from app import ledger as ledger_module

    assert ledger_module.verify_chain(session) is True


def test_the_handler_resolves_the_event_from_a_payment(session):
    event, sf, claim, disbursement = _settled_claim(session)

    ledger_handle(session, {"disbursement_id": str(disbursement.id)})
    session.flush()

    # A clean event, so nothing flagged — the assertion is that it ran at all.
    assert session.scalar(
        select(func.count())
        .select_from(LedgerEntry)
        .where(LedgerEntry.action == str(Event.ANOMALY_FLAGGED))
    ) == 0


def test_a_job_naming_neither_an_event_nor_a_payment_is_refused(session):
    with pytest.raises(LedgerAgentNotRunnable, match="neither an event nor a payment"):
        ledger_handle(session, {})


def test_an_unknown_payment_is_refused(session):
    with pytest.raises(LedgerAgentNotRunnable, match="does not resolve"):
        ledger_handle(session, {"disbursement_id": str(uuid.uuid4())})


def test_worker_registers_the_ledger_agent():
    assert str(AgentName.LEDGER_AGENT) in load_handlers()
