"""The storm engine: track parsing, the wind model, and the advisory writer.

These tests exist because the failure modes here are quiet. A fixed-width
parser that reads one column wrong still returns numbers. A wind model with an
inverted sign still returns radii. A synthetic advisory in nearly the right
shape still inserts. Every one of those would produce a storm that draws
convincingly and is wrong, which is the failure this whole product is built to
refuse.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.nhc.fstadv import Position, Radii
from app.storms import tracks, wind
from app.storms.catalogue import JAMAICA, _haversine, summarise
from app.storms.synthesize import advisories_from_track, _translation
from app.storms.tracks import StormTrack

pytestmark = pytest.mark.skipif(
    not tracks.HURDAT2.exists() or not tracks.EBTRK.exists(),
    reason="storm archives not fetched — run data/storms/fetch_tracks.py",
)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_hurdat2_reads_the_whole_atlantic():
    storms = tracks.read_hurdat2()
    assert len(storms) > 1900, "the Atlantic archive runs from 1851 and should be large"
    gilbert = storms["AL081988"]
    assert gilbert.name == "GILBERT"
    assert gilbert.year == 1988
    assert gilbert.peak_wind_kt == 160
    assert gilbert.min_pressure_mb == 888


def test_hurdat2_alone_cannot_size_gilbert():
    """The gap that makes the second archive necessary, asserted rather than assumed.

    If HURDAT2 ever gains pre-2004 radii this test fails, which is the correct
    outcome: the merge would then be redundant and should be reconsidered
    rather than left in place doing nothing.
    """
    gilbert = tracks.read_hurdat2()["AL081988"]
    assert not gilbert.has_radii()
    assert not any(gilbert.rmw_nm)


def test_ebtrk_fills_what_hurdat2_lacks():
    merged = tracks.load_tracks()["AL081988"]
    assert merged.has_radii(), "EBTRK should supply Gilbert's quadrant radii"
    assert any(merged.rmw_nm), "EBTRK should supply Gilbert's radius of maximum wind"

    # Position and intensity must still come from the reanalysis, untouched.
    original = tracks.read_hurdat2()["AL081988"]
    assert merged.peak_wind_kt == original.peak_wind_kt
    assert [p.lat for p in merged.positions] == [p.lat for p in original.positions]


def test_ebtrk_block_parsing_is_positional_not_whitespace():
    """Four radii packed into twelve characters, with no separator.

    `250200250250` is 250/200/250/250. Splitting on whitespace works until a
    two-digit value pads to ` 50` and the block becomes several tokens — token
    counts across the real file run from 18 to 27, so any whitespace-based
    reader is wrong for some rows and right for others.
    """
    assert tracks._eb_block("250200250250") == [250, 200, 250, 250]
    assert tracks._eb_block("-99-99-99-99") == [None, None, None, None]
    assert tracks._eb_block(" 50 40 40 50") == [50, 40, 40, 50]


def test_missing_sentinels_are_none_not_zero():
    """-999 and -99 mean "never analysed", which is not "the wind was calm"."""
    assert tracks._int("-999") is None
    assert tracks._int("-99") is None
    assert tracks._int("9999") is None
    assert tracks._int("0") == 0


def test_a_threshold_with_no_data_is_omitted_not_zeroed():
    radii = tracks._radii_from([None] * 4 + [100, 80, 80, 100] + [None] * 4)
    assert [r.threshold_kt for r in radii] == [50]


def test_a_partially_analysed_quadrant_stays_missing_until_modelled():
    """Archive silence is not an observed zero-wind quadrant."""
    radii = tracks._radii_from([100, None, 0, 80] + [None] * 8)
    assert len(radii) == 1
    assert (radii[0].ne, radii[0].se, radii[0].sw, radii[0].nw) == (100, None, 0, 80)
    assert not radii[0].is_complete


def test_hurdat_status_is_preserved_separately_from_position_kind():
    gilbert = tracks.read_hurdat2()["AL081988"]
    assert gilbert.positions[0].kind == "observed"
    assert gilbert.positions[0].status == "TD"
    assert any(position.status == "HU" for position in gilbert.positions)
    assert gilbert.positions[-1].status == "EX"


# ---------------------------------------------------------------------------
# The wind model
# ---------------------------------------------------------------------------


def test_profile_peaks_at_the_radius_of_maximum_wind():
    kw = dict(rmw_km=40.0, b=1.5, delta_p_hpa=60.0, lat=18.0)
    peak = wind.gradient_wind_ms(40.0, **kw)
    assert peak > wind.gradient_wind_ms(15.0, **kw)
    assert peak > wind.gradient_wind_ms(120.0, **kw)


def test_holland_b_reproduces_the_observed_peak():
    """B inverted from vmax must put the profile's maximum back at vmax.

    This is the one property the derivation guarantees, so if it fails the
    equation has been transcribed wrong.
    """
    vmax_ms, delta_p, lat = 50.0, 60.0, 18.0
    b = wind.holland_b(vmax_ms=vmax_ms, delta_p_hpa=delta_p, lat=lat)
    modelled = wind.gradient_wind_ms(40.0, rmw_km=40.0, b=b, delta_p_hpa=delta_p, lat=lat)
    assert modelled == pytest.approx(vmax_ms, rel=0.02)


def test_forward_motion_makes_the_field_asymmetric():
    """A storm moving north-west is stronger on its north-east flank.

    This is the test that caught an inverted sign in the tangential direction.
    A mirrored circulation still produces a plausible-looking hurricane; it
    just puts the strong side, and therefore the largest quadrant radius, on
    the wrong side of the storm. Symmetry here would warn the wrong parish.
    """
    kw = dict(
        rmw_km=25.0 * wind.NM_TO_KM,
        b=1.4,
        delta_p_hpa=60.0,
        lat=18.0,
        translation_ms=8.0,
        heading_deg=315.0,
    )
    # The forward-right flank of a north-west-moving storm is its north-east
    # side; the rear-left is the south-west. Same distance from the eye.
    strong = wind.surface_wind_kt(150.0, 45.0, **kw)
    weak = wind.surface_wind_kt(150.0, 225.0, **kw)
    assert strong > weak

    # With the storm stationary the two sides must agree, which is what proves
    # the difference above came from translation and not from the inflow term.
    still = {**kw, "translation_ms": 0.0}
    assert wind.surface_wind_kt(150.0, 45.0, **still) == pytest.approx(
        wind.surface_wind_kt(150.0, 225.0, **still), rel=1e-6
    )


def test_quadrant_radii_are_sector_maxima_for_a_northwest_moving_storm():
    """NW motion aligns the strongest field inside NE, near 68 degrees.

    Testing ``radii_for`` closes the gap left by testing one surface point: the
    exported quadrant values, rather than merely the underlying vector, must
    put the maximum in the forward-right sector.
    """
    radii = wind.radii_for(
        vmax_kt=100,
        pressure_mb=950,
        lat=18.0,
        r34_nm=140,
        translation_kt=30,
        heading_deg=315,
    )
    for threshold in radii:
        assert threshold.ne == max(
            threshold.ne, threshold.se, threshold.sw, threshold.nw
        )
        assert threshold.ne > threshold.sw


def test_surface_field_is_calibrated_to_the_authoritative_vmax():
    """Independent grid benchmark of the full vector field's maximum."""
    vmax_kt = 100.0
    pressure_mb = 950.0
    lat = 18.0
    rmw_nm = 25.0
    heading = 315.0
    forward_kt = 20.0
    delta_p = wind.AMBIENT_MB - pressure_mb
    translation_ms = min(
        forward_kt * wind.KT_TO_MS * wind.TRANSLATION_ASYMMETRY_FACTOR,
        vmax_kt * wind.KT_TO_MS * wind.MAX_TRANSLATION_SHARE_OF_VMAX,
    )
    b = wind.fit_b_to_r34(
        120.0,
        rmw_nm=rmw_nm,
        vmax_kt=vmax_kt,
        delta_p_hpa=delta_p,
        lat=lat,
        translation_ms=translation_ms,
        heading_deg=heading,
    )
    scale = wind._intensity_scale(
        vmax_ms=vmax_kt * wind.KT_TO_MS,
        rmw_nm=rmw_nm,
        b=b,
        delta_p_hpa=delta_p,
        lat=lat,
        translation_ms=translation_ms,
    )
    aligned = wind._aligned_bearing(heading_deg=heading, northern=True)
    # This grid is deliberately independent of the bounded search used by the
    # implementation.  It includes the entire inner core and enough of the
    # outer profile to catch a misplaced maximum.
    modelled = max(
        wind.surface_wind_kt(
            radius_nm * wind.NM_TO_KM,
            aligned,
            rmw_km=rmw_nm * wind.NM_TO_KM,
            b=b,
            delta_p_hpa=delta_p,
            lat=lat,
            translation_ms=translation_ms,
            heading_deg=heading,
            intensity_scale=scale,
        )
        for radius_nm in (rmw_nm * index / 1000.0 for index in range(1, 3001))
    )
    assert modelled == pytest.approx(vmax_kt, rel=1e-4)


def test_radii_grow_with_the_size_target():
    """The authoring control must actually control something.

    An earlier version fitted only the shape parameter, which pinned at its
    floor and made 150 nm and 250 nm produce identical storms.
    """
    small = wind.radii_for(vmax_kt=120, pressure_mb=940, lat=18.0, r34_nm=90)
    large = wind.radii_for(vmax_kt=120, pressure_mb=940, lat=18.0, r34_nm=240)
    reach = lambda rs, kt: next(r.ne for r in rs if r.threshold_kt == kt)  # noqa: E731
    assert reach(large, 34) > reach(small, 34) * 1.5
    assert reach(large, 64) > reach(small, 64)


def test_a_hurricane_always_has_a_hurricane_force_field():
    """Fitting a wide outer radius must not flatten the core out of existence.

    Without a floor on the shape parameter the fit will happily report a
    120 kt storm with no 64 kt wind anywhere, which is not a storm.
    """
    radii = wind.radii_for(vmax_kt=120, pressure_mb=940, lat=18.0, r34_nm=250)
    assert any(r.threshold_kt == 64 and not r.is_empty for r in radii)


def test_a_tropical_depression_has_no_hurricane_field():
    radii = wind.radii_for(vmax_kt=25, pressure_mb=1005, lat=18.0)
    assert not any(r.threshold_kt == 64 for r in radii)


def test_fast_weak_or_stale_pressure_fix_cannot_invent_stronger_thresholds():
    """Translation and low pressure never outrank authoritative source vmax."""
    radii = wind.radii_for(
        vmax_kt=25,
        pressure_mb=960,
        lat=40.0,
        r34_nm=220,
        translation_kt=50,
        heading_deg=45,
    )
    assert radii == ()

    tropical_storm = wind.radii_for(
        vmax_kt=55,
        pressure_mb=940,
        lat=25.0,
        r34_nm=180,
        translation_kt=45,
        heading_deg=20,
    )
    assert {radius.threshold_kt for radius in tropical_storm} == {34, 50}
    assert all(radius.threshold_kt <= 55 for radius in tropical_storm)


def test_every_requested_threshold_below_vmax_is_emitted():
    radii = wind.radii_for(
        vmax_kt=80,
        pressure_mb=1005,
        lat=18.0,
        r34_nm=100,
        translation_kt=20,
        heading_deg=315,
    )
    assert {radius.threshold_kt for radius in radii} == {34, 50, 64}
    assert all(not radius.is_empty for radius in radii)


def test_threshold_equal_to_vmax_does_not_create_a_spurious_quadrant_wedge():
    """An equal-threshold contour is a point/ring, not a filled wind sector."""
    storm_50 = wind.radii_for(
        vmax_kt=50,
        pressure_mb=990,
        lat=18.0,
        r34_nm=90,
        translation_kt=15,
        heading_deg=315,
    )
    storm_64 = wind.radii_for(
        vmax_kt=64,
        pressure_mb=980,
        lat=18.0,
        r34_nm=110,
        translation_kt=15,
        heading_deg=315,
    )
    assert {radius.threshold_kt for radius in storm_50} == {34}
    assert {radius.threshold_kt for radius in storm_64} == {34, 50}


def test_equal_threshold_absence_is_explicit_in_provenance():
    from app.storms.synthesize import _fill_radii

    base = _toy_track()
    positions = tuple(
        replace(position, max_wind_kt=50, radii=()) for position in base.positions
    )
    _, provenance = _fill_radii(replace(base, positions=positions))
    assert set(provenance[0][34].values()) == {"modelled"}
    assert set(provenance[0][50].values()) == {"model_zero_area_at_vmax"}


def test_intense_storms_have_tighter_cores():
    tight = wind.estimate_rmw_nm(delta_p_hpa=110.0, lat=18.0)
    broad = wind.estimate_rmw_nm(delta_p_hpa=20.0, lat=18.0)
    assert tight < broad


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------


def test_gilbert_passed_over_jamaica():
    summary = summarise(tracks.load_tracks()["AL081988"])
    assert summary.closest_km < 80, "Gilbert crossed the island"
    assert summary.wind_at_closest_kt >= 100
    assert summary.radii_measured, "the EBTRK merge should make this measured"


def test_haversine_is_sane():
    assert _haversine(JAMAICA, JAMAICA) == pytest.approx(0.0, abs=1e-9)
    # Kingston to Montego Bay, straight line, is about 130 km.
    assert _haversine((17.97, -76.79), (18.47, -77.89)) == pytest.approx(130, rel=0.1)


# ---------------------------------------------------------------------------
# The advisory writer
# ---------------------------------------------------------------------------


def _toy_track() -> StormTrack:
    """Two days of a storm walking due west at a steady 120 kt."""
    positions = tuple(
        Position(
            valid_at=datetime(2020, 9, 1, tzinfo=UTC) + timedelta(hours=6 * i),
            lat=18.0,
            lon=-70.0 - 1.5 * i,
            kind="observed",
            max_wind_kt=120,
            radii=(Radii(threshold_kt=34, ne=120, se=100, sw=90, nw=110),),
        )
        for i in range(8)
    )
    return StormTrack(
        storm_id="AL992020",
        name="TESTER",
        year=2020,
        positions=positions,
        rmw_nm=(20.0,) * 8,
        pressure_mb=(950,) * 8,
    )


def test_advisory_numbers_satisfy_the_console_validator():
    """The browser rejects anything but `^\\d+[A-Z]?$` before it draws.

    A label like `t+06` would fail validation wholesale, and the console would
    show an empty screen rather than a bad storm — correct, but only findable
    here.
    """
    import re

    advisories = advisories_from_track(_toy_track())
    assert advisories
    for index, advisory in enumerate(advisories):
        assert re.fullmatch(r"\d+[A-Z]?", advisory.advisory_number)
        assert int(advisory.advisory_number) == index + 1


def test_each_advisory_looks_forward_and_the_last_one_does_not():
    advisories = advisories_from_track(_toy_track())
    assert advisories[0].forecasts, "an early advisory must project ahead"
    assert advisories[-1].forecasts == (), "the final fix has nothing left to project"
    assert advisories[0].current.kind == "observed"
    assert all(p.kind == "forecast" for p in advisories[0].forecasts)


def test_translation_is_derived_from_the_track():
    """HURDAT2 publishes neither speed nor heading, so both are computed."""
    speed_kt, heading = _translation(_toy_track(), 0)
    # 1.5 degrees of longitude at 18N is about 159 km, not 167 — the cos(lat)
    # factor is the whole reason this is computed rather than assumed.
    assert speed_kt == pytest.approx(14.3, rel=0.05)
    assert heading == pytest.approx(270, abs=2), "due west"


def test_measured_radii_are_never_overwritten_by_the_model():
    """Where a source published a number, that number survives.

    The model exists to fill silence, not to correct the archive.
    """
    from app.storms.synthesize import _fill_radii

    track = _toy_track()
    filled, provenance = _fill_radii(track)
    original = track.positions[0].radius(34)
    assert filled[0].radius(34) == original
    assert set(provenance[0][34].values()) == {"measured"}
    assert set(provenance[0][50].values()) == {"modelled"}
    assert set(provenance[0][64].values()) == {"modelled"}


def test_partial_quadrants_are_filled_without_overwriting_measurements():
    from app.storms.synthesize import _fill_radii

    base = _toy_track()
    first = replace(
        base.positions[0],
        radii=(Radii(threshold_kt=34, ne=123, se=None, sw=0, nw=None),),
    )
    track = replace(base, positions=(first, *base.positions[1:]))
    filled, provenance = _fill_radii(track)

    r34 = filled[0].radius(34)
    assert r34 is not None and r34.is_complete
    assert r34.ne == 123
    assert r34.sw == 0
    assert r34.se is not None and r34.nw is not None
    assert provenance[0][34] == {
        "ne": "measured",
        "se": "modelled",
        "sw": "measured",
        "nw": "modelled",
    }


def test_synthetic_advisories_represent_status_instead_of_inferring_from_wind():
    track = _toy_track()
    statuses = ("TD", "TS", "HU", "EX", "SD", "SS", "LO", "WV")
    positions = tuple(
        replace(position, status=status)
        for position, status in zip(track.positions, statuses, strict=True)
    )
    advisories = advisories_from_track(replace(track, positions=positions))
    assert [advisory.storm_type for advisory in advisories] == [
        "TROPICAL DEPRESSION",
        "TROPICAL STORM",
        "HURRICANE",
        "EXTRATROPICAL CYCLONE",
        "SUBTROPICAL DEPRESSION",
        "SUBTROPICAL STORM",
        "LOW",
        "TROPICAL WAVE",
    ]


def test_hindcast_advisory_keeps_its_disclosed_historical_path():
    from app.storms.synthesize import _track_wkt

    advisory = advisories_from_track(_toy_track())[0]
    track = _track_wkt(advisory.positions)
    assert track is not None
    assert track.startswith("LINESTRING(")
    assert f"{advisory.current.lon:.6f} {advisory.current.lat:.6f}" in track
