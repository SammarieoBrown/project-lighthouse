"""Parser tests, run against the real cached advisories.

Not against handcrafted samples. The bug worth catching here is "the published
format has a case we did not imagine", and a sample we wrote cannot contain a
case we did not imagine. All 41 of Melissa's advisories are in the repo, so the
parsers are held to every one of them — a tropical storm with no hurricane-force
field, a category 5 with all three, forecasts that roll into the next month, and
long-range outlooks that carry no radii at all.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.config import MELISSA
from app.nhc import parse_fstadv, parse_wndprb, quadrant_polygon_wkt
from app.nhc.geometry import destination
from app.nhc.wndprb import FORECAST_HOURS

FSTADV = sorted((MELISSA / "text" / "fstadv").glob("*.txt"))
WNDPRB = sorted((MELISSA / "text" / "wndprb").glob("*.txt"))


def test_the_cache_is_present():
    """If this fails, nothing below means anything."""
    assert len(FSTADV) == 41, "expected 41 forecast advisories in the committed cache"
    assert len(WNDPRB) == 41


# --------------------------------------------------------------------------
# Forecast/Advisory
# --------------------------------------------------------------------------


def test_advisory_25_parses_to_the_published_numbers():
    """Melissa at her peak, checked field by field against the printed product."""
    advisory = parse_fstadv((MELISSA / "text" / "fstadv" / "al132025.fstadv.025.txt").read_text())

    assert advisory.storm_id == "al132025"
    assert advisory.storm_name == "Melissa"
    assert advisory.advisory_number == "25"
    assert advisory.issued_at == datetime(2025, 10, 27, 15, 0, tzinfo=UTC)

    assert advisory.current.lat == pytest.approx(16.4)
    assert advisory.current.lon == pytest.approx(-78.2)  # west is negative
    assert advisory.current.max_wind_kt == 145
    assert advisory.current.gust_kt == 175
    assert advisory.pressure_mb == 908
    assert advisory.eye_diameter_nm == 10
    assert advisory.movement_deg == 270
    assert advisory.movement_kt == 3

    hurricane_force = advisory.current.radius(64)
    assert hurricane_force is not None
    assert (hurricane_force.ne, hurricane_force.se, hurricane_force.sw, hurricane_force.nw) == (
        25, 20, 20, 25,
    )

    # The asymmetry that makes a circle the wrong model: tropical-storm wind
    # reached more than three times further northeast than southwest.
    ts_force = advisory.current.radius(34)
    assert ts_force is not None
    assert (ts_force.ne, ts_force.se, ts_force.sw, ts_force.nw) == (170, 130, 50, 80)


def test_sea_state_is_not_mistaken_for_a_wind_field():
    """The seas line has the same quadrant shape as a radii line.

    "4 M SEAS....240NE 210SE  90SW 120NW." would parse as a 4 kt wind radius
    under a looser pattern, inventing a wind field the size of the sea state and
    roughly ten times the real one.
    """
    advisory = parse_fstadv((MELISSA / "text" / "fstadv" / "al132025.fstadv.025.txt").read_text())
    assert {r.threshold_kt for r in advisory.current.radii} <= {34, 50, 64}
    assert advisory.current.radius(4) is None


def test_a_tropical_storm_has_no_hurricane_force_field():
    """Advisory 1 — 45 kt. Absent thresholds must be absent, not zero."""
    advisory = parse_fstadv((MELISSA / "text" / "fstadv" / "al132025.fstadv.001.txt").read_text())
    assert advisory.current.max_wind_kt == 45
    assert advisory.current.radius(64) is None
    assert advisory.current.radius(50) is None
    assert advisory.current.radius(34) is not None


def test_forecast_days_roll_into_the_next_month():
    """Advisory 25 is issued 27 October and forecasts out to 1 November.

    NHC gives a day of month and never a month. Read naively, ``01/1200Z``
    becomes 1 October — four weeks in the past. The replay would still run.
    """
    advisory = parse_fstadv((MELISSA / "text" / "fstadv" / "al132025.fstadv.025.txt").read_text())
    november = [p for p in advisory.forecasts if p.valid_at.month == 11]
    assert november, "expected forecast points in November"
    assert all(p.valid_at.year == 2025 for p in november)

    # The observed position is valid at the issue time, not after it.
    assert advisory.current.valid_at == advisory.issued_at
    assert all(p.valid_at > advisory.issued_at for p in advisory.forecasts)


def test_every_advisory_parses_and_stays_ordered():
    """All 41, with time strictly increasing across the forecast."""
    for path in FSTADV:
        advisory = parse_fstadv(path.read_text())
        assert advisory.storm_id == "al132025", path.name
        assert advisory.current.max_wind_kt, path.name
        assert -90 <= advisory.current.lat <= 90, path.name
        assert -180 <= advisory.current.lon <= 180, path.name
        assert advisory.forecasts, path.name

        times = [p.valid_at for p in advisory.positions]
        assert times == sorted(times), f"{path.name}: forecast times out of order"


def test_intensity_peaks_where_the_record_says_it_did():
    """Melissa peaked at 160 kt and 892 mb — the strongest ever to hit Jamaica.

    Checked against the historical record rather than against our own output, so
    a parser that drifts on units or picks up the gust line instead of the
    sustained one fails here rather than quietly reshaping the demo.
    """
    peaks = [
        (a.current.max_wind_kt or 0, a.advisory_number, a.pressure_mb)
        for a in (parse_fstadv(p.read_text()) for p in FSTADV)
    ]
    max_wind, advisory_number, pressure = max(peaks)
    assert max_wind == 160
    assert advisory_number == "29"
    assert pressure == 892


def test_a_product_that_is_not_an_advisory_is_refused():
    with pytest.raises(ValueError, match="not a Forecast/Advisory"):
        parse_fstadv("TROPICAL WEATHER OUTLOOK\nNOTHING USEFUL HERE\n")


# --------------------------------------------------------------------------
# Wind speed probabilities
# --------------------------------------------------------------------------


def test_advisory_25_probabilities_match_the_printed_table():
    """MONTEGO BAY    64  X  29(29)  34(63)   1(64)   X(64)   X(64)   X(64)"""
    product = parse_wndprb((MELISSA / "text" / "wndprb" / "al132025.wndprb.025.txt").read_text())

    montego = {row.threshold_kt: row for row in product.for_location("MONTEGO BAY")}
    hurricane = montego[64]
    assert hurricane.incremental[12] == 0  # printed as X — below 0.5%, not impossible
    assert hurricane.incremental[24] == 29
    assert hurricane.cumulative[24] == 29
    assert hurricane.incremental[36] == 34
    assert hurricane.cumulative[36] == 63

    # The comparison that decides where relief stages.
    kingston = {row.threshold_kt: row for row in product.for_location("KINGSTON")}
    assert kingston[64].cumulative_at(36) == 27
    assert hurricane.cumulative_at(36) > kingston[64].cumulative_at(36)


def test_cumulative_probability_never_decreases():
    """A cumulative series that falls means the columns were misread."""
    for path in WNDPRB:
        product = parse_wndprb(path.read_text())
        for row in product.rows:
            series = [row.cumulative[h] for h in FORECAST_HOURS]
            assert series == sorted(series), f"{path.name}: {row.location} {row.threshold_kt}kt"
            assert all(0 <= v <= 100 for v in series), f"{path.name}: {row.location}"


def test_jamaica_is_reported_until_the_storm_has_passed_it():
    """NHC drops locations once they stop being at risk, and that is not a gap.

    Kingston and Montego Bay appear in advisories 1 through 31 and vanish from
    32 onward, by which point Melissa had cleared Jamaica for Cuba. The risk
    model has to treat a missing location as "no longer forecast" rather than as
    a parse failure or, worse, as zero probability — those are three different
    claims and only one of them is true.
    """
    with_jamaica, without = [], []
    for path in WNDPRB:
        product = parse_wndprb(path.read_text())
        number = int(product.advisory_number)
        if product.for_location("KINGSTON"):
            assert product.for_location("MONTEGO BAY"), path.name
            with_jamaica.append(number)
        else:
            without.append(number)

    assert with_jamaica == list(range(1, 32))
    assert without == list(range(32, 42))
    # Once dropped, a location does not come back — so a later advisory can
    # never silently overwrite an earlier probability with nothing.
    assert max(with_jamaica) < min(without)


def test_every_row_carries_all_seven_windows():
    for path in WNDPRB:
        for row in parse_wndprb(path.read_text()).rows:
            assert set(row.incremental) == set(FORECAST_HOURS), path.name
            assert set(row.cumulative) == set(FORECAST_HOURS), path.name


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------


def test_a_quadrant_polygon_reaches_the_published_distance():
    """60 nm due north of the centre is one degree of latitude, near enough."""
    lat, lon = 16.4, -78.2
    north_lat, north_lon = destination(lat, lon, 0, 60)
    assert north_lat == pytest.approx(lat + 1.0, abs=0.01)
    assert north_lon == pytest.approx(lon, abs=0.001)

    east_lat, east_lon = destination(lat, lon, 90, 60)
    # A degree of longitude shrinks with the cosine of latitude.
    assert east_lon == pytest.approx(lon + 1.0 / 0.9592, abs=0.02)
    assert east_lat == pytest.approx(lat, abs=0.01)


def test_the_polygon_is_lopsided_the_way_the_storm_was():
    """Advisory 25's 34 kt field: 170 nm NE against 50 nm SW."""
    wkt = quadrant_polygon_wkt(16.4, -78.2, ne=170, se=130, sw=50, nw=80)
    assert wkt is not None and wkt.startswith("POLYGON((")

    coords = [
        tuple(float(v) for v in pair.split())
        for pair in wkt[len("POLYGON((") : -2].split(", ")
    ]
    northeast = max(lat for lon, lat in coords if lon > -78.2)
    southwest = min(lat for lon, lat in coords if lon < -78.2)
    assert northeast - 16.4 > 16.4 - southwest


def test_a_threshold_the_storm_does_not_reach_has_no_geometry():
    """Absent is the honest representation of an area that does not exist."""
    assert quadrant_polygon_wkt(16.4, -78.2, ne=0, se=0, sw=0, nw=0) is None


def test_every_radius_in_the_cache_produces_a_closed_ring():
    for path in FSTADV:
        advisory = parse_fstadv(path.read_text())
        for position in advisory.positions:
            for radii in position.radii:
                wkt = quadrant_polygon_wkt(
                    position.lat, position.lon,
                    ne=radii.ne, se=radii.se, sw=radii.sw, nw=radii.nw,
                )
                if radii.is_empty:
                    assert wkt is None
                    continue
                assert wkt is not None
                ring = wkt[len("POLYGON((") : -2].split(", ")
                assert ring[0] == ring[-1], f"{path.name}: unclosed ring"
                assert len(ring) > 40, f"{path.name}: ring too coarse"
