"""Replay driver tests.

The exit criterion these build toward is "the full replay runs unattended start
to finish without a manual intervention", so the tests walk the real storm
rather than a two-advisory fixture.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, text

from lighthouse_contracts import AgentName, Posture

from app.models import Advisory, AgentJob, HazardEvent
from app.nhc.ingest import ingest_storm
from app.replay import ReplayDriver, posture_for
from app.forecast_sentinel_service import warning_codes_here


@pytest.fixture(scope="module")
def driver(session_module):
    ingest_storm(session_module)
    session_module.flush()
    return ReplayDriver(session_module)


def test_a_driver_without_an_ingested_storm_says_so(session):
    with pytest.raises(LookupError, match="run app.nhc.ingest"):
        ReplayDriver(session, external_ref="al999999")


def test_the_storm_starts_quiet_and_unapplied(driver):
    state = driver.state()
    assert state.total == 41
    assert state.applied == 0
    assert state.posture is Posture.QUIET
    assert state.position is None
    assert state.next_advisory_at == datetime(2025, 10, 21, 15, 0, tzinfo=UTC)
    assert state.ends_at == datetime(2025, 10, 31, 15, 0, tzinfo=UTC)
    assert not state.finished


def test_stepping_applies_one_advisory_and_enqueues_its_agents(driver, session_module):
    before = session_module.scalar(select(func.count()).select_from(AgentJob))
    applied = driver.step()

    assert applied is not None
    assert applied.advisory_number == "1"
    assert applied.jobs_enqueued >= 1

    after = session_module.scalar(select(func.count()).select_from(AgentJob))
    assert after == before + applied.jobs_enqueued

    state = driver.state()
    assert state.applied == 1
    assert state.position == applied.issued_at


def test_advance_to_replays_the_gap_rather_than_skipping_it(driver, session_module):
    """Scrubbing forward must still walk every advisory in between."""
    target = datetime(2025, 10, 25, 3, 0, tzinfo=UTC)  # advisory 15
    applied = driver.advance_to(target)

    numbers = [a.advisory_number for a in applied]
    assert numbers == [str(n) for n in range(2, 16)], "advisories were skipped"
    assert all(a.issued_at <= target for a in applied)
    assert driver.state().applied == 15


def test_posture_escalates_through_every_level_as_the_storm_approaches(session_module):
    """The Act 1 curve, and the reason posture is worth having at all.

    Melissa gave five days of warning, and the whole argument for anticipatory
    action is that those five days are usable. A posture that jumps QUIET to ACT
    has thrown them away; one that sits on READY from day one has too, in the
    other direction. Both were real bugs here — the first from collapsing time
    by reading the merged wind field, which has no time in it, and the second
    from reading watch codes without their geometry and picking up Hispaniola's.

    Reads the rule directly rather than through the driver, because the driver
    is a cursor and only moves forward: by the time this runs, earlier tests
    have already consumed part of the storm.
    """
    advisories = session_module.scalars(
        select(Advisory)
        .where(Advisory.observed.is_(False))
        .order_by(Advisory.issued_at)
    ).all()
    curve = [(a.advisory_number, posture_for(session_module, a)) for a in advisories]
    postures = [p for _, p in curve]

    assert len(curve) == 41

    # Every level appears, in order, with real gaps between them.
    first: dict[Posture, int] = {}
    for index, posture in enumerate(postures):
        first.setdefault(posture, index)
    assert set(first) == {Posture.QUIET, Posture.WATCH, Posture.READY, Posture.ACT}
    assert first[Posture.QUIET] < first[Posture.WATCH] < first[Posture.READY] < first[Posture.ACT]

    # ACT arrives days before landfall, not at it. Melissa came ashore on
    # 28 October; the hurricane warning that covered these parishes was issued
    # on the 25th, at advisory 15.
    act_at = next(int(num) for num, p in curve if p is Posture.ACT)
    assert act_at <= 15

    # And it stands down once the storm has gone, or the next event begins from
    # a country that never left ACT.
    assert postures[-1] is not Posture.ACT


def test_posture_reads_the_warning_for_here_not_the_one_for_hispaniola(session_module):
    """The bug that made the whole curve useless, pinned.

    A watch/warning bundle covers everywhere the storm threatens. At advisory 1
    the hurricane watch in the file is for Hispaniola, and reading bare codes
    put these parishes on READY five days out. Codes only count when their
    segment actually runs along this coast.
    """
    first = session_module.scalar(
        select(Advisory)
        .where(Advisory.observed.is_(False), Advisory.advisory_number == "1")
    )
    everywhere = {w["code"] for w in first.raw["watches_warnings"]}
    here = warning_codes_here(session_module, first)

    assert "HWA" in everywhere, "advisory 1 did carry a hurricane watch somewhere"
    assert "HWA" not in here, "and it was not for St Elizabeth or Westmoreland"
    assert posture_for(session_module, first) is Posture.QUIET


def test_a_posture_change_wakes_the_alert_agent(driver, session_module):
    """Alert cascades hang off the posture change, not off the advisory."""
    alerts = session_module.scalars(
        select(AgentJob).where(AgentJob.job_type == str(AgentName.ALERT_AGENT))
    ).all()
    assert alerts, "no posture change ever reached the alert agent"

    for job in alerts:
        assert job.payload["event"] == "hazard.posture_changed"
        assert job.payload["previous_posture"] != job.payload["posture"]


def test_every_advisory_woke_the_risk_mapper(driver, session_module):
    """One job per advisory — the backlog Risk Mapper consumes when it lands."""
    driver.run()  # the driver only moves forward; say so rather than assume it
    count = session_module.scalar(
        select(func.count())
        .select_from(AgentJob)
        .where(AgentJob.job_type == str(AgentName.RISK_MAPPER))
    )
    assert count == 41


def test_running_a_finished_replay_does_nothing(driver):
    """Idempotent at the end, so a rehearsal that overshoots is harmless."""
    driver.run()
    assert driver.step() is None
    assert driver.run() == []
    assert driver.state().applied == 41


def test_advance_to_before_the_storm_applies_nothing(driver, session_module):
    early = datetime(2025, 1, 1, tzinfo=UTC)
    assert driver.advance_to(early) == []


def test_reset_clears_the_replay_without_touching_the_record(driver, session_module):
    """Reset is for rehearsal. It must not erase what happened."""
    ledger_before = session_module.execute(
        text("SELECT count(*) FROM ledger_entry")
    ).scalar()

    removed = driver.reset()
    assert removed > 0

    state = driver.state()
    assert state.applied == 0
    assert state.posture is Posture.QUIET

    ledger_after = session_module.execute(
        text("SELECT count(*) FROM ledger_entry")
    ).scalar()
    assert ledger_after == ledger_before

    event = session_module.scalar(
        select(HazardEvent).where(HazardEvent.external_ref == "al132025")
    )
    assert event is not None, "reset must not delete the storm itself"


def test_the_whole_storm_replays_unattended(driver, session_module):
    """Phase 3 exit criterion, in miniature: start to finish, no intervention."""
    applied = driver.run()
    assert len(applied) == 41
    assert [a.advisory_number for a in applied] == [str(n) for n in range(1, 42)]

    issued = [a.issued_at for a in applied]
    assert issued == sorted(issued), "advisories replayed out of order"
    assert driver.state().finished
