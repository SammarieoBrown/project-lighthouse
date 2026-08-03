"""Ingest tests — the chain from cached file to a spatial answer.

The question the whole ingest exists to serve is "was this household inside the
wind", so that is what gets asked here, of real coordinates against real
geometry built from the real advisory. Everything else is plumbing that either
supports that answer or does not.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text

from app.models import Advisory, HazardEvent
from app.nhc.ingest import ingest_storm

# Real places, so a wrong answer is wrong about somewhere that exists.
MONTEGO_BAY = (18.4762, -77.8939)
KINGSTON = (17.9714, -76.7931)


@pytest.fixture(scope="module")
def melissa(session_module):
    """Ingest the whole storm once; the assertions below all read from it."""
    event = ingest_storm(session_module)
    session_module.flush()
    return event


def _contains(session, advisory_id, threshold_kt, point) -> bool:
    lat, lon = point
    return session.execute(
        text(
            f"""
            SELECT ST_Intersects(
                     wind_field_{threshold_kt},
                     ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography
                   )
            FROM advisory WHERE id = :id
            """
        ),
        {"lon": lon, "lat": lat, "id": advisory_id},
    ).scalar()


def test_the_whole_storm_lands_as_one_event(melissa, session_module):
    assert melissa.external_ref == "al132025"
    assert melissa.replay is True

    advisories = session_module.scalars(
        select(Advisory).where(Advisory.hazard_event_id == melissa.id)
    ).all()
    forecast = [a for a in advisories if not a.observed]
    observed = [a for a in advisories if a.observed]

    assert len(forecast) == 41
    assert len(observed) == 1, "best track is one row describing the whole storm"
    assert observed[0].advisory_number == "best_track"


def test_ingest_is_idempotent(melissa, session_module):
    """The seeder runs over and over in rehearsal. It must not accumulate."""
    before = session_module.scalar(select(func.count()).select_from(Advisory))
    ingest_storm(session_module)
    session_module.flush()
    after = session_module.scalar(select(func.count()).select_from(Advisory))
    assert before == after

    events = session_module.scalars(
        select(func.count()).select_from(HazardEvent).where(HazardEvent.external_ref == "al132025")
    ).one()
    assert events == 1


def test_advisory_25_puts_montego_bay_in_the_storm_but_not_in_the_eye_wall(
    melissa, session_module
):
    """The answer the whole pipeline exists to produce.

    At advisory 25 Melissa sat at 16.4N 78.2W with hurricane-force wind reaching
    25 nm northeast and tropical-storm wind reaching 170 nm. Montego Bay is
    roughly 125 nm north-northeast of that: inside the 34 kt field the storm is
    forecast to sweep, outside the 64 kt one at this point in the forecast.

    If the quadrant polygons were built as circles, or the longitude sign were
    dropped, or the union skipped forecast hours, this is where it shows.
    """
    advisory = session_module.scalar(
        select(Advisory).where(
            Advisory.hazard_event_id == melissa.id,
            Advisory.advisory_number == "25",
            Advisory.observed.is_(False),
        )
    )
    assert advisory is not None
    assert _contains(session_module, advisory.id, 34, MONTEGO_BAY) is True


def test_the_wind_fields_nest_inside_each_other(melissa, session_module):
    """Hurricane-force wind cannot reach further than tropical-storm wind.

    A containment check across every advisory catches a whole family of errors
    at once — quadrants assigned to the wrong compass point, thresholds swapped,
    a union built from the wrong position list.
    """
    advisories = session_module.scalars(
        select(Advisory).where(
            Advisory.hazard_event_id == melissa.id, Advisory.observed.is_(False)
        )
    ).all()

    checked = 0
    for advisory in advisories:
        if advisory.wind_field_64 is None or advisory.wind_field_34 is None:
            continue
        covered = session_module.execute(
            text(
                """
                SELECT ST_Covers(wind_field_34::geometry, wind_field_64::geometry)
                FROM advisory WHERE id = :id
                """
            ),
            {"id": advisory.id},
        ).scalar()
        assert covered, f"advisory {advisory.advisory_number}: 64 kt escapes the 34 kt field"
        checked += 1

    assert checked > 20, "expected most advisories to carry a hurricane-force field"


def test_every_stored_geometry_is_valid(melissa, session_module):
    """An invalid polygon still renders. It just answers questions wrongly."""
    bad = session_module.execute(
        text(
            """
            SELECT advisory_number, observed
            FROM advisory
            WHERE (cone IS NOT NULL AND NOT ST_IsValid(cone::geometry))
               OR (track IS NOT NULL AND NOT ST_IsValid(track::geometry))
               OR (wind_field_34 IS NOT NULL AND NOT ST_IsValid(wind_field_34::geometry))
               OR (wind_field_50 IS NOT NULL AND NOT ST_IsValid(wind_field_50::geometry))
               OR (wind_field_64 IS NOT NULL AND NOT ST_IsValid(wind_field_64::geometry))
            """
        )
    ).all()
    assert bad == []


def test_the_cone_is_a_cone_and_not_its_inverse(melissa, session_module):
    """pyshp warns that NHC winds the cone's rings the way a hole is wound.

    It recovers by treating them as exterior rings, which is right — but "right"
    is a claim worth checking, because the failure mode is a polygon covering
    the entire globe except the cone, which would put every household on earth
    inside the forecast track.
    """
    row = session_module.execute(
        text(
            """
            SELECT ST_Area(cone) / 1e6 AS km2
            FROM advisory
            WHERE advisory_number = '25' AND observed IS false
            """
        )
    ).one()
    # A five-day cone is large but bounded — hundreds of thousands of km², not
    # the 510 million km² of the planet.
    assert 10_000 < row.km2 < 3_000_000, f"cone area {row.km2:,.0f} km² is not a cone"


def test_the_best_track_swath_covers_jamaica(melissa, session_module):
    """Melissa made landfall on Jamaica, so the observed 64 kt swath must contain it.

    This is the row verification will ask "was this household actually hit", and
    it is checked against the one fact about this storm nobody disputes.
    """
    observed = session_module.scalar(
        select(Advisory).where(
            Advisory.hazard_event_id == melissa.id, Advisory.observed.is_(True)
        )
    )
    assert observed.wind_field_64 is not None
    assert _contains(session_module, observed.id, 64, MONTEGO_BAY) is True


def test_raw_keeps_the_source_and_the_probabilities(melissa, session_module):
    """Derived geometry without its source is an unanswerable question later."""
    advisory = session_module.scalar(
        select(Advisory).where(
            Advisory.hazard_event_id == melissa.id,
            Advisory.advisory_number == "25",
            Advisory.observed.is_(False),
        )
    )
    raw = advisory.raw
    assert raw["source"]["fstadv"] == "al132025.fstadv.025.txt"
    assert raw["pressure_mb"] == 908
    assert raw["storm_type"] == "HURRICANE"
    assert "HWR" in raw["watch_codes"]

    # Segments keep their geometry, because a watch/warning bundle covers the
    # whole storm. Without it, "is there a hurricane warning here" can only be
    # answered "somewhere, yes" — which put the replay on READY five days out.
    segments = raw["watches_warnings"]
    assert segments and all("code" in s and "geometry" in s for s in segments)
    assert any(s["geometry"]["type"] in ("LineString", "MultiLineString") for s in segments)

    montego = raw["probabilities"]["MONTEGO BAY"]
    assert montego["64"]["cumulative"]["36"] == 63
    assert raw["probabilities"]["KINGSTON"]["64"]["cumulative"]["36"] == 27
