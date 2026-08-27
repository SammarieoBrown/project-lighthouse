"""Triage: the ordering rule, and the fact that it only ever annotates.

TRI-01 gives the ordering in words — medical urgency first, then habitability,
then property, vulnerability breaking ties, safety-of-life pinned above all of
it. These tests are that sentence, one clause at a time.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from lighthouse_contracts import (
    SOL_PRIORITY,
    AgentName,
    ClaimStatus,
    Event,
    Severity,
    StormFileState,
)

from app.agents.triage_agent import handle as triage_handle
from app.models import AgentJob, LedgerEntry
from app.triage_service import (
    DEFAULT_VULN_SCORE,
    ClaimNotFound,
    TriageNotRunnable,
    run_triage,
    score_claim,
)
from app.worker import load_handlers

from factories import make_claim, make_event, make_storm_file


def _claim(session, *, vuln: int | None = 78, **kw):
    event = make_event(session)
    sf = make_storm_file(session, state=StormFileState.VERIFIED)
    sf.vuln_score = vuln
    session.flush()
    kw.setdefault("status", ClaimStatus.VERIFIED)
    return sf, make_claim(session, sf, event, **kw)


def _score(session, **kw):
    sf, claim = _claim(session, **kw)
    return score_claim(claim, sf, verification_confidence=0.8)


# -- the ordering, clause by clause ----------------------------------------


def test_safety_of_life_outranks_everything(session):
    """INT-04. A SOL claim with the least urgent possible needs still sorts
    above a medical emergency that is not flagged."""
    sol = _score(session, sol=True, damage_type=None, reported_needs=["food"])
    medical = _score(session, sol=False, reported_needs=["medical_support"])

    assert sol.severity is Severity.URGENT
    assert medical.severity is Severity.URGENT
    assert sol.rank < medical.rank
    assert "safety_of_life" in sol.drivers


def test_medical_urgency_comes_before_habitability(session):
    medical = _score(session, reported_needs=["insulin"], damage_type=None)
    habitability = _score(session, reported_needs=["shelter"], damage_type=None)

    assert medical.severity is Severity.URGENT
    assert habitability.severity is Severity.HIGH
    assert medical.rank < habitability.rank


def test_habitability_comes_before_property(session):
    habitability = _score(session, damage_type="structural_damage", reported_needs=[])
    property_only = _score(session, damage_type=None, reported_needs=["food", "water"])

    assert habitability.severity is Severity.HIGH
    assert property_only.severity is Severity.MED
    assert habitability.rank < property_only.rank


def test_vulnerability_breaks_ties_inside_a_tier(session):
    """TRI-01's tiebreak. Two households with identical damage and needs are
    ordered by how badly a hurricane was always going to hurt them."""
    fragile = _score(session, vuln=95, damage_type="roof_damage", reported_needs=[])
    sturdy = _score(session, vuln=20, damage_type="roof_damage", reported_needs=[])

    assert fragile.severity is sturdy.severity
    assert fragile.rank < sturdy.rank


def test_an_unknown_vulnerability_sits_at_the_midpoint(session):
    """A thin SMS-tier registration (REG-06) has no score yet. Treating that
    as zero would sink exactly the households least able to register fully."""
    unknown = _score(session, vuln=None, damage_type="roof_damage", reported_needs=[])
    midpoint = _score(
        session, vuln=DEFAULT_VULN_SCORE, damage_type="roof_damage", reported_needs=[]
    )

    assert unknown.rank == midpoint.rank
    assert "vuln:unknown" in unknown.drivers


def test_rank_stays_within_its_contract_at_both_extremes(session):
    """``TriageAgentOutput.rank`` is ge=0 and the console sorts on it, so the
    formula has to be bounded at both ends rather than merely usually positive.

    The absolute floor needs ten distinct needs and the extractor can only name
    seven, so no real claim reaches 0 — but the bound has to hold anyway, since
    the thing enforcing it is a frozen contract and not the extractor.
    """
    floor = _score(
        session,
        vuln=100,
        sol=True,
        reported_needs=[f"need-{n}" for n in range(12)],
    )
    realistic_floor = _score(
        session,
        vuln=100,
        sol=True,
        reported_needs=["water", "food", "shelter", "tarpaulin", "medicine"],
    )
    ceiling = _score(session, vuln=0, damage_type=None, reported_needs=[])

    assert floor.rank == 0
    assert realistic_floor.rank == 5
    assert ceiling.rank == 3510
    assert floor.rank < realistic_floor.rank < ceiling.rank


def test_confidence_is_recorded_but_does_not_move_the_queue(session):
    """Triage orders need, not certainty. A claim reaching triage is one
    verification already accepted."""
    sf, claim = _claim(session)
    shaky = score_claim(claim, sf, verification_confidence=0.51)
    solid = score_claim(claim, sf, verification_confidence=0.99)

    assert shaky.rank == solid.rank
    assert shaky.severity is solid.severity
    assert "0.51" in shaky.rationale


# -- what running it actually does ------------------------------------------


def test_triage_annotates_the_claim_and_never_moves_it(session):
    sf, claim = _claim(session, damage_type="structural_damage")

    run = run_triage(session, claim.id, verification_confidence=0.9)
    session.flush()

    assert run.created is True
    assert claim.severity is Severity.HIGH
    assert claim.triage_rank == run.output.rank
    assert claim.status is ClaimStatus.VERIFIED  # untouched
    assert sf.state is StormFileState.VERIFIED  # untouched


def test_triage_writes_a_ledger_entry_and_hands_on_to_logistics(session):
    sf, claim = _claim(session, sol=True)

    run = run_triage(session, claim.id, verification_confidence=0.9)
    session.flush()

    entry = session.scalar(
        select(LedgerEntry)
        .where(
            LedgerEntry.action == str(Event.CLAIM_TRIAGED),
            LedgerEntry.subject_id == claim.id,
        )
        .order_by(LedgerEntry.seq.desc())
        .limit(1)
    )
    assert entry is not None
    assert entry.payload["severity"] == str(run.output.severity)
    assert entry.payload["rank"] == run.output.rank
    assert entry.agent_name == str(AgentName.TRIAGE_AGENT)

    job = session.scalar(
        select(AgentJob).where(
            AgentJob.job_type == str(AgentName.LOGISTICS_AGENT),
            AgentJob.payload["claim_id"].astext == str(claim.id),
        )
    )
    assert job is not None
    assert job.priority == SOL_PRIORITY  # INT-04 rides the whole queue


def test_rerunning_unchanged_triage_adds_no_ledger_noise(session):
    sf, claim = _claim(session)

    first = run_triage(session, claim.id, verification_confidence=0.9)
    second = run_triage(session, claim.id, verification_confidence=0.9)
    session.flush()

    assert first.created is True
    assert second.created is False
    assert second.output.rank == first.output.rank
    assert session.scalar(
        select(func.count())
        .select_from(LedgerEntry)
        .where(
            LedgerEntry.action == str(Event.CLAIM_TRIAGED),
            LedgerEntry.subject_id == claim.id,
        )
    ) == 1


def test_an_unverified_claim_is_not_triaged(session):
    sf, claim = _claim(session, status=ClaimStatus.FILED)

    with pytest.raises(TriageNotRunnable, match="not verified"):
        run_triage(session, claim.id)


def test_a_missing_claim_is_refused(session):
    with pytest.raises(ClaimNotFound):
        run_triage(session, uuid.uuid4())


def test_the_handler_runs_from_the_payload_verification_enqueues(session):
    """The shape verification_service._enqueue_triage has been queuing since
    Act 2 landed, which the worker parked until now."""
    sf, claim = _claim(session)

    triage_handle(
        session,
        {
            "claim_id": str(claim.id),
            "storm_file_id": str(sf.id),
            "verification_confidence": 0.83,
            "sol": False,
        },
    )
    session.flush()

    assert claim.severity is not None
    assert claim.triage_rank is not None


def test_worker_registers_the_triage_agent():
    assert str(AgentName.TRIAGE_AGENT) in load_handlers()
