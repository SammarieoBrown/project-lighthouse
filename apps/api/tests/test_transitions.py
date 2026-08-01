"""Phase 0 exit criterion #2.

    "An integration test drives one synthetic StormFile through all five states
     and the ledger hash chain validates."

Plus the negative cases, which matter more. A system that can walk the happy
path proves very little; a system that *refuses* to settle without a signature
is the one that can be trusted with public money.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from lighthouse_contracts import (
    ActorKind,
    AgentName,
    AppRole,
    ClaimStatus,
    Event,
    JobStatus,
    StormFileState,
)

from app import ledger, statemachine
from app.models import AgentJob, LedgerEntry
from app.statemachine import GateNotSatisfied, IllegalTransition

from factories import (
    make_claim,
    make_event,
    make_storm_file,
    make_user,
    settle_with_signature,
)


def test_storm_file_walks_all_five_states_and_chain_validates(session):
    """The spine, end to end: REGISTERED -> AT_RISK -> AFFECTED -> VERIFIED -> SETTLED."""
    finance = make_user(session, AppRole.FINANCE_OFFICER)
    event = make_event(session)

    # T1 — registration
    sf = make_storm_file(session)
    statemachine.record_creation(session, sf)
    assert sf.state is StormFileState.REGISTERED

    # T2 — a storm is forecast to affect this household
    statemachine.transition(
        session, sf, StormFileState.AT_RISK, agent=AgentName.RISK_MAPPER
    )
    assert sf.state is StormFileState.AT_RISK

    # T4/C1 — the household reports damage
    claim = make_claim(session, sf, event)
    statemachine.record_claim_creation(session, claim)
    statemachine.transition(
        session, sf, StormFileState.AFFECTED, agent=AgentName.INTAKE_AGENT
    )
    assert sf.state is StormFileState.AFFECTED

    # T6/C2 — evidence checks out
    statemachine.transition_claim(
        session, claim, ClaimStatus.VERIFIED, agent=AgentName.VERIFICATION_AGENT
    )
    statemachine.transition(
        session, sf, StormFileState.VERIFIED, agent=AgentName.VERIFICATION_AGENT
    )
    assert sf.state is StormFileState.VERIFIED
    assert claim.verified_at is not None

    # A Finance Officer signs, money moves, the payment is confirmed
    settle_with_signature(session, claim, finance)

    # T8/C6 — and only now can the file settle
    statemachine.transition(
        session, sf, StormFileState.SETTLED, agent=AgentName.LEDGER_AGENT
    )
    statemachine.transition_claim(
        session, claim, ClaimStatus.SETTLED, agent=AgentName.LEDGER_AGENT
    )

    assert sf.state is StormFileState.SETTLED
    assert claim.settled_at is not None

    # The whole journey is on the chain, and the chain is intact
    assert ledger.verify_chain(session) is True

    actions = session.execute(
        select(LedgerEntry.action).order_by(LedgerEntry.seq)
    ).scalars().all()
    assert str(Event.HOUSEHOLD_REGISTERED) in actions
    assert str(Event.HOUSEHOLD_AT_RISK) in actions
    assert str(Event.CLAIM_CREATED) in actions
    assert str(Event.CLAIM_VERIFIED) in actions
    assert str(Event.HOUSEHOLD_SETTLED) in actions

    # T2R is measured filed -> settled, and is a real positive number
    t2r = statemachine.time_to_relief_hours(claim)
    assert t2r is not None and t2r >= 0


def test_cannot_settle_without_a_confirmed_disbursement(session):
    """The rule the whole platform rests on. No signature, no settlement."""
    event = make_event(session)
    sf = make_storm_file(session, state=StormFileState.VERIFIED)
    make_claim(session, sf, event)

    with pytest.raises(GateNotSatisfied):
        statemachine.transition(
            session, sf, StormFileState.SETTLED, agent=AgentName.LEDGER_AGENT
        )

    assert sf.state is StormFileState.VERIFIED


def test_database_blocks_settled_even_if_the_state_machine_is_bypassed(session):
    """Belt and braces.

    ``transition()`` refuses, but the guarantee cannot depend on everyone
    remembering to call it. Writing the column directly must fail too.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    sf = make_storm_file(session, state=StormFileState.VERIFIED)
    session.flush()

    with pytest.raises(DBAPIError) as exc:
        session.execute(
            text("UPDATE storm_file SET state = 'SETTLED' WHERE id = :i"),
            {"i": sf.id},
        )
    assert "PAY-04" in str(exc.value)


def test_illegal_transitions_are_refused(session):
    """Skipping verification is the shortcut that would matter most, so it is
    the one most worth being unable to take."""
    sf = make_storm_file(session, state=StormFileState.AFFECTED)

    with pytest.raises(IllegalTransition):
        statemachine.transition(session, sf, StormFileState.SETTLED)

    sf2 = make_storm_file(session, state=StormFileState.REGISTERED)
    with pytest.raises(IllegalTransition):
        statemachine.transition(session, sf2, StormFileState.VERIFIED)


def test_transition_enqueues_the_follow_on_agent_atomically(session):
    """A state change and the job that reacts to it land together or not at all."""
    sf = make_storm_file(session)
    statemachine.record_creation(session, sf)
    statemachine.transition(session, sf, StormFileState.AT_RISK)
    statemachine.transition(session, sf, StormFileState.AFFECTED)

    jobs = session.execute(
        select(AgentJob).where(AgentJob.status == JobStatus.QUEUED)
    ).scalars().all()

    # CLAIM_CREATED chains to the Verification Agent (events.FOLLOW_ON)
    assert any(j.job_type == str(AgentName.VERIFICATION_AGENT) for j in jobs)


def test_safety_of_life_claims_jump_the_queue(session):
    """INT-04. Ordering changes; authority does not."""
    from lighthouse_contracts import SOL_PRIORITY

    event = make_event(session)
    sf = make_storm_file(session, state=StormFileState.AFFECTED)
    claim = make_claim(session, sf, event, sol=True)
    statemachine.record_claim_creation(session, claim)

    job = session.execute(
        select(AgentJob).where(AgentJob.priority == SOL_PRIORITY)
    ).scalars().first()
    assert job is not None
    assert job.job_type == str(AgentName.VERIFICATION_AGENT)


def test_agents_never_hold_money_authority(session):
    """transitions.md: Triage and Alert hold no transition authority at all.

    An agent that cannot move a file cannot lose one.
    """
    holders = {t.agent for t in statemachine.TRANSITIONS}
    assert AgentName.TRIAGE_AGENT not in holders
    assert AgentName.ALERT_AGENT not in holders
    assert AgentName.LOGISTICS_AGENT not in holders

    settle = next(t for t in statemachine.TRANSITIONS if t.id == "T8")
    assert settle.gate is not None
