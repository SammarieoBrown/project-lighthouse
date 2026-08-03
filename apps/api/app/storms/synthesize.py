"""Turn a storm track into advisories the rest of the system already reads.

This is the whole integration, and it is deliberately small. Nothing downstream
of `advisory.raw` knows where a storm came from — the replay driver, the posture
engine, the risk mapper, the exposure builder and the console exporter all read
that JSONB column and the three wind field geometries beside it. So a historical
storm does not need a new pipeline. It needs to arrive in the same shape.

The trick is that it arrives as a `ForecastAdvisory`, the same dataclass the NHC
teletype parser produces, which lets `_wind_fields` and `_raw_payload` be reused
verbatim rather than reimplemented. One storm format, one code path, and no
second implementation to drift.

**A hindcast is not a forecast, and the screen must not confuse them.** A real
advisory's forecast track is what forecasters believed at the time, and it was
often wrong. Here the "forecast" positions are the track the storm actually
took, because the question this product asks is *what would this storm do to
Jamaica*, not *what did the National Hurricane Center believe on Tuesday*. That
is perfect foresight, it makes the swath exact rather than probabilistic, and it
is a different claim from anything NHC published. `event.kind` carries it.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from statistics import median

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import Advisory, HazardEvent
from app.nhc.fstadv import ForecastAdvisory, Position, Radii

# Private by name, shared by design. These two functions define the contract
# every consumer of `advisory.raw` depends on; importing them is what keeps a
# modelled storm and a parsed one byte-comparable, and reimplementing them is
# how the two would quietly diverge.
from app.nhc.ingest import _raw_payload, _wind_fields
from app.storms.tracks import THRESHOLDS, StormTrack
from app.storms.wind import bearing_between, radii_for

#: How far ahead each advisory looks. NHC forecasts to 120 hours, and posture
#: reads arrival times out to 72, so anything shorter would silently stop the
#: escalation ladder from ever reaching READY.
FORECAST_HOURS = 120

#: How far ahead the *wind field* claims, which is a different question.
#:
#: A hindcast forecasts perfectly — the track it projects is the track the storm
#: took. Union that across 120 hours and Gilbert's wind field covers Jamaica
#: from three days out, so every advisory from the second one reports 1,314
#: homes destroyed and the escalation the console exists to show is a flat line.
#:
#: The two horizons are separate because they answer separate questions. The
#: full track stays in `raw["positions"]`, so posture still sees a 64 kt arrival
#: at 72 hours and escalates on schedule. The wind field unions only the next
#: two days, which is the window an operations room can act inside and the range
#: over which NHC's own radii forecasts are worth much.
WIND_FIELD_HOURS = 48

#: Track points are six-hourly, so this is the whole storm at advisory cadence.
#: Landfall records at odd times are kept — they are the most important points
#: in the track and dropping them would smooth over the exact hour of impact.
ADVISORY_EVERY_HOURS = 6


def _translation(track: StormTrack, index: int) -> tuple[float, float]:
    """Forward speed in knots and heading in degrees, from the track itself.

    Derived rather than read: HURDAT2 does not publish either, and computing
    them from consecutive positions is exact for a track that is already a
    sequence of fixes.
    """
    positions = track.positions
    if index + 1 >= len(positions):
        if index == 0:
            return 0.0, 0.0
        a, b = positions[index - 1], positions[index]
    else:
        a, b = positions[index], positions[index + 1]

    hours = (b.valid_at - a.valid_at).total_seconds() / 3600.0
    if hours <= 0:
        return 0.0, 0.0

    from app.storms.catalogue import _haversine

    km = _haversine((a.lat, a.lon), (b.lat, b.lon))
    return (km / 1.852) / hours, bearing_between((a.lat, a.lon), (b.lat, b.lon))


def _typical_r34(track: StormTrack) -> float | None:
    """The storm's own outer size, when any of its points were measured.

    Better than a climatological guess for filling this storm's gaps: a
    hurricane's extent is fairly persistent over a day or two, so its own
    median is a closer prior than the Atlantic average.
    """
    reach = [
        max(r.ne, r.se, r.sw, r.nw)
        for p in track.positions
        for r in p.radii
        if r.threshold_kt == 34 and not r.is_empty
    ]
    return median(reach) if reach else None


def _fill_radii(track: StormTrack) -> tuple[list[Position], list[bool]]:
    """Every position given radii, and a flag for which ones we invented.

    Measured wins outright. Only where a source published nothing does the
    parametric model run, and the flag travels with the position so the export
    can say which is which rather than presenting them as equivalent.
    """
    typical = _typical_r34(track)
    filled: list[Position] = []
    modelled: list[bool] = []

    for index, position in enumerate(track.positions):
        usable = [r for r in position.radii if not r.is_empty]
        if usable:
            filled.append(replace(position, radii=tuple(usable)))
            modelled.append(False)
            continue

        speed_kt, heading = _translation(track, index)
        rmw = track.rmw_nm[index] if index < len(track.rmw_nm) else None
        pressure = track.pressure_mb[index] if index < len(track.pressure_mb) else None
        radii: tuple[Radii, ...] = radii_for(
            vmax_kt=position.max_wind_kt or 0,
            pressure_mb=pressure,
            lat=position.lat,
            rmw_nm=rmw,
            r34_nm=typical,
            translation_kt=speed_kt,
            heading_deg=heading,
            thresholds=THRESHOLDS,
        )
        filled.append(replace(position, radii=radii))
        modelled.append(bool(radii))

    return filled, modelled


def advisories_from_track(
    track: StormTrack, *, forecast_hours: int = FORECAST_HOURS
) -> list[ForecastAdvisory]:
    """One advisory per track point, each looking forward along the real track.

    Numbered from 1 in track order, because `advisory.advisory_number` is text
    that the console validates as `^\\d+[A-Z]?$` and sorts as an integer. A
    label like `t+06` would be rejected by the browser before it drew anything.
    """
    positions, modelled = _fill_radii(track)
    horizon = timedelta(hours=forecast_hours)
    out: list[ForecastAdvisory] = []

    for index, current in enumerate(positions):
        # Only points the storm has not yet reached, and only within the
        # horizon. The union of their wind fields is the swath this advisory
        # claims — which for a hindcast is the swath the storm actually cut.
        ahead = tuple(
            replace(p, kind="forecast")
            for p in positions[index + 1 :]
            if p.valid_at - current.valid_at <= horizon
        )
        speed_kt, heading = _translation(track, index)
        pressure = track.pressure_mb[index] if index < len(track.pressure_mb) else None

        out.append(
            ForecastAdvisory(
                storm_id=track.storm_id,
                storm_name=track.name.title(),
                storm_type="HURRICANE" if (current.max_wind_kt or 0) >= 64 else "TROPICAL STORM",
                advisory_number=str(index + 1),
                issued_at=current.valid_at,
                current=replace(current, kind="observed"),
                forecasts=ahead,
                movement_deg=int(heading),
                movement_kt=int(speed_kt),
                pressure_mb=pressure,
            )
        )
    return out


def ingest_track(
    session: Session,
    track: StormTrack,
    *,
    external_ref: str | None = None,
    name: str | None = None,
    replace_existing: bool = True,
) -> HazardEvent:
    """Write a historical storm as a replayable hazard event.

    Reuses `_wind_fields` and `_raw_payload` so the rows are indistinguishable
    in shape from Melissa's, which is the point: everything that already works
    for one storm works for this one without knowing it exists.
    """
    ref = (external_ref or track.storm_id).lower()
    positions, modelled = _fill_radii(track)
    any_modelled = any(modelled)

    existing = session.execute(
        text("SELECT id FROM hazard_event WHERE external_ref = :r"), {"r": ref}
    ).scalar()
    if existing and not replace_existing:
        raise ValueError(f"hazard event {ref!r} already exists")
    if existing:
        # Cascades to advisories, risk assessments and exposure. Rebuilding a
        # storm must not leave last run's advisories interleaved with this one.
        session.execute(text("DELETE FROM hazard_event WHERE external_ref = :r"), {"r": ref})
        session.flush()

    event = HazardEvent(
        name=name or f"Hurricane {track.name.title()} {track.year}",
        external_ref=ref,
        replay=True,
    )
    session.add(event)
    session.flush()

    for advisory in advisories_from_track(track):
        # Wind fields from the near horizon, raw from the full one. Handing
        # `_wind_fields` a truncated copy keeps that function untouched — it
        # unions whatever positions it is given, and the choice of which
        # positions belongs here.
        horizon = timedelta(hours=WIND_FIELD_HOURS)
        near = replace(
            advisory,
            forecasts=tuple(
                p
                for p in advisory.forecasts
                if p.valid_at - advisory.current.valid_at <= horizon
            ),
        )
        fields = _wind_fields(session, near)
        raw = _raw_payload(advisory, None, [], {"archive": "hurdat2+ebtrk"})
        # Two claims the NHC path never has to make, because NHC published the
        # numbers. Here some of them are ours, and the row says so.
        raw["synthesized"] = True
        raw["size_source"] = "modelled" if any_modelled else "measured"
        raw["hindcast"] = True

        row = Advisory(
            hazard_event_id=event.id,
            advisory_number=advisory.advisory_number,
            issued_at=advisory.issued_at,
            observed=False,
            raw=raw,
        )
        # WKT assigned straight to the geography column, as the NHC path does —
        # GeoAlchemy converts on the way in and an explicit ST_GeogFromText here
        # would be a second, divergent way of writing the same column.
        for field, value in fields.items():
            setattr(row, field, value)
        session.add(row)

    session.flush()
    return event
