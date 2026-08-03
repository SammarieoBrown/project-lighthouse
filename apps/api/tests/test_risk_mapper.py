"""Risk Mapper tests.

The claim being checked is not "the numbers are right" — nobody can check that
until there is observed damage to compare against. It is that the model is the
one we documented: a transparent lookup, applied to the households we hold, with
every prediction attributable to a roof and a wind band.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from lighthouse_contracts import DamageBand

from app.agents.risk_mapper import (
    IMPACT,
    METHOD,
    MODEL_VERSION,
    PROBABILITY_POINTS,
    assess,
)
from app.models import Advisory
from app.nhc.ingest import ingest_storm
from app.registry import seed_registry


@pytest.fixture(scope="module")
def assessed(session_module):
    ingest_storm(session_module)
    seed_registry(session_module, count=200)
    session_module.flush()

    advisories = {
        a.advisory_number: a
        for a in session_module.scalars(
            select(Advisory).where(Advisory.observed.is_(False))
        )
    }
    runs = {num: assess(session_module, advisories[num]) for num in ("9", "15", "25", "29")}
    session_module.flush()
    return advisories, runs


# --------------------------------------------------------------------------
# The model itself
# --------------------------------------------------------------------------


def test_the_impact_table_is_monotonic_in_wind():
    """More wind cannot mean less damage. The table is small enough to check."""
    order = [DamageBand.NONE, DamageBand.MINOR, DamageBand.MAJOR, DamageBand.DESTROYED]
    for roof, bands in IMPACT.items():
        severity = [order.index(bands[kt]) for kt in (0, 34, 50, 64)]
        assert severity == sorted(severity), f"{roof} gets better in stronger wind"


def test_a_zinc_roof_fares_worse_than_concrete_in_the_same_wind():
    """The whole physical argument of the model, asserted."""
    order = [DamageBand.NONE, DamageBand.MINOR, DamageBand.MAJOR, DamageBand.DESTROYED]
    for kt in (34, 50, 64):
        assert order.index(IMPACT["zinc"][kt]) > order.index(IMPACT["concrete"][kt])


def test_calm_air_damages_nothing():
    for roof, bands in IMPACT.items():
        assert bands[0] is DamageBand.NONE, roof


# --------------------------------------------------------------------------
# What lands in the table
# --------------------------------------------------------------------------


def test_every_household_is_assessed_for_every_advisory_run(assessed):
    _, runs = assessed
    assert all(run.assessed == 200 for run in runs.values())


def test_risk_climbs_as_the_storm_closes(assessed):
    """Act 1's second curve — not posture, but who is actually in the way."""
    _, runs = assessed
    destroyed = {num: run.by_band.get("DESTROYED", 0) for num, run in runs.items()}
    assert destroyed["9"] == 0, "no household is written off four days out"
    assert destroyed["29"] > destroyed["15"] > destroyed["9"]


def test_every_prediction_records_how_it_was_made(assessed, session_module):
    """Without method and model_version a prediction cannot be explained later.

    This is also what makes the learning loop possible: refitting against
    observed damage needs to know which version produced which number.
    """
    rows = session_module.execute(
        text("SELECT DISTINCT method, model_version FROM risk_assessment")
    ).all()
    assert rows == [(METHOD, MODEL_VERSION)]
    assert METHOD == "parametric_lookup_v1", "if this becomes a model, say so here"


def test_a_prediction_can_be_traced_back_to_a_roof_and_a_wind_band(
    assessed, session_module
):
    """Pick a real row and re-derive it by hand from the table."""
    advisories, _ = assessed
    advisory = advisories["29"]

    row = session_module.execute(
        text(
            """
            SELECT sf.structure->>'roof' AS roof,
                   ra.predicted_band::text AS band,
                   CASE
                     WHEN ST_Intersects(a.wind_field_64, sf.location) THEN 64
                     WHEN ST_Intersects(a.wind_field_50, sf.location) THEN 50
                     WHEN ST_Intersects(a.wind_field_34, sf.location) THEN 34
                     ELSE 0
                   END AS wind_kt
            FROM risk_assessment ra
            JOIN storm_file sf ON sf.id = ra.storm_file_id
            JOIN advisory a ON a.id = ra.advisory_id
            WHERE ra.advisory_id = :id
            ORDER BY sf.id
            LIMIT 1
            """
        ),
        {"id": advisory.id},
    ).one()

    expected = IMPACT[row.roof][row.wind_kt]
    assert row.band == str(expected), (
        f"a {row.roof} roof in {row.wind_kt} kt should be {expected}, got {row.band}"
    )


def test_probabilities_are_fractions_not_percentages(assessed, session_module):
    """The contract says 0 to 1. NHC publishes 0 to 100.

    A hundred-fold error here renders as a perfectly plausible map, which is
    why it is worth a test rather than a comment.
    """
    row = session_module.execute(
        text("SELECT max(p34) AS p34, max(p50) AS p50, max(p64) AS p64 FROM risk_assessment")
    ).one()
    assert 0 <= row.p34 <= 1 and 0 <= row.p50 <= 1 and 0 <= row.p64 <= 1
    assert row.p64 > 0, "some household should carry a hurricane-force probability"


def test_confidence_falls_off_with_distance_from_a_sample_point(assessed, session_module):
    """NHC publishes probabilities for two places in Jamaica, and no others.

    A household 80 km from both is an extrapolation and has to say so.
    """
    rows = session_module.execute(
        text(
            """
            SELECT ra.confidence,
                   least(
                     ST_Distance(sf.location, ST_SetSRID(ST_MakePoint(:kgn_lon, :kgn_lat),4326)::geography),
                     ST_Distance(sf.location, ST_SetSRID(ST_MakePoint(:mbj_lon, :mbj_lat),4326)::geography)
                   ) / 1000.0 AS km
            FROM risk_assessment ra JOIN storm_file sf ON sf.id = ra.storm_file_id
            """
        ),
        {
            "kgn_lat": PROBABILITY_POINTS["KINGSTON"][0],
            "kgn_lon": PROBABILITY_POINTS["KINGSTON"][1],
            "mbj_lat": PROBABILITY_POINTS["MONTEGO BAY"][0],
            "mbj_lon": PROBABILITY_POINTS["MONTEGO BAY"][1],
        },
    ).all()

    nearest = min(rows, key=lambda r: r.km)
    furthest = max(rows, key=lambda r: r.km)
    assert furthest.km - nearest.km > 20, "expected a real spread of distances"
    assert furthest.confidence < nearest.confidence
    assert all(0 <= r.confidence <= 1 for r in rows)


def test_reassessing_the_same_advisory_replaces_rather_than_duplicates(
    assessed, session_module
):
    """Rehearsal replays the same advisory. The unique constraint is not a bug."""
    advisories, _ = assessed
    advisory = advisories["25"]

    before = session_module.execute(
        text("SELECT count(*) FROM risk_assessment WHERE advisory_id = :id"),
        {"id": advisory.id},
    ).scalar()

    assess(session_module, advisory)
    session_module.flush()

    after = session_module.execute(
        text("SELECT count(*) FROM risk_assessment WHERE advisory_id = :id"),
        {"id": advisory.id},
    ).scalar()
    assert before == after == 200


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


def test_an_unhandled_job_waits_rather_than_dying(session):
    """The replay enqueues work for agents that do not exist yet, on purpose.

    That only works if an unhandled job stays queued. Running it through the
    retry path burns five attempts against a handler that was never going to
    answer and then marks it DEAD — so the backlog the driver carefully built up
    is gone by the time the agent lands. Found by draining the queue for real:
    41 risk_mapper jobs DONE, and 4 alert_agent jobs DEAD.
    """
    from lighthouse_contracts import AgentName, JobStatus

    from app import queue

    job = queue.enqueue(session, job_type=AgentName.ALERT_AGENT, payload={"x": 1})
    attempts_before = job.attempts

    queue.park(session, job, "no handler registered for alert_agent")

    assert job.status is JobStatus.QUEUED
    assert job.attempts == attempts_before, "parking must not spend a retry"
    assert job.finished_at is None
    assert job.locked_by is None
    assert "no handler" in job.last_error


def test_the_worker_can_actually_find_this_handler():
    """The failure mode is silent: the worker starts, claims jobs, parks them all.

    Handlers register by import side effect, and the worker imports them inside
    main() to dodge a circular import — which is exactly the kind of wiring that
    works on the machine where it was written and nowhere else.
    """
    from lighthouse_contracts import AgentName

    from app.worker import load_handlers

    handlers = load_handlers()
    assert str(AgentName.RISK_MAPPER) in handlers
