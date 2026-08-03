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

import re
from dataclasses import replace
from datetime import timedelta
from statistics import median

from sqlalchemy import select, text
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
_EXTERNAL_REF = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")

QUADRANTS = ("ne", "se", "sw", "nw")

# HURDAT2's status is source evidence, not a label to re-derive from wind.  In
# particular, a 25 kt extratropical cyclone and a 25 kt tropical depression are
# materially different systems even though their intensity is identical.
HURDAT_STORM_TYPES = {
    "TD": "TROPICAL DEPRESSION",
    "TS": "TROPICAL STORM",
    "HU": "HURRICANE",
    "EX": "EXTRATROPICAL CYCLONE",
    "SD": "SUBTROPICAL DEPRESSION",
    "SS": "SUBTROPICAL STORM",
    "LO": "LOW",
    "WV": "TROPICAL WAVE",
    "DB": "DISTURBANCE",
}


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
    reach = []
    for position in track.positions:
        for radii in position.radii:
            known = [
                value
                for value in (radii.ne, radii.se, radii.sw, radii.nw)
                if value is not None
            ]
            if radii.threshold_kt == 34 and any(known):
                reach.append(max(known))
    return median(reach) if reach else None


def _fill_radii(
    track: StormTrack,
) -> tuple[list[Position], list[dict[int, dict[str, str]]]]:
    """Fill only missing radii and retain evidence at quadrant resolution.

    A source can publish 34 kt but omit 50/64 kt, or analyse only two quadrants
    in a row.  Treating that position as wholly measured silently deletes
    expected fields; treating it as wholly modelled overwrites evidence.  This
    merges at the smallest unit the archive provides and returns the provenance
    that must travel beside each value.
    """
    typical = _typical_r34(track)
    filled: list[Position] = []
    provenance: list[dict[int, dict[str, str]]] = []

    for index, position in enumerate(track.positions):
        vmax = max(0, position.max_wind_kt or 0)
        source = {r.threshold_kt: r for r in position.radii}
        # A modelled threshold exactly equal to vmax is a zero-area contour and
        # cannot honestly become a quadrant polygon.  A source-published row at
        # equality is still preserved below because the archive, not the model,
        # is evidence that the field had measurable extent.
        eligible = tuple(threshold for threshold in THRESHOLDS if threshold < vmax)
        missing = tuple(
            threshold
            for threshold in eligible
            if threshold not in source or not source[threshold].is_complete
        )
        speed_kt, heading = _translation(track, index)
        rmw = track.rmw_nm[index] if index < len(track.rmw_nm) else None
        pressure = track.pressure_mb[index] if index < len(track.pressure_mb) else None
        local_r34 = source.get(34)
        local_r34_values = (
            [
                value
                for value in (local_r34.ne, local_r34.se, local_r34.sw, local_r34.nw)
                if value is not None and value > 0
            ]
            if local_r34
            else []
        )
        modelled = {
            radii.threshold_kt: radii
            for radii in (
                radii_for(
                    vmax_kt=vmax,
                    pressure_mb=pressure,
                    lat=position.lat,
                    rmw_nm=rmw,
                    r34_nm=max(local_r34_values) if local_r34_values else typical,
                    translation_kt=speed_kt,
                    heading_deg=heading,
                    thresholds=missing,
                )
                if missing
                else ()
            )
        }

        position_radii: list[Radii] = []
        position_source: dict[int, dict[str, str]] = {}
        for threshold in THRESHOLDS:
            measured = source.get(threshold)
            if threshold > vmax:
                # A stale radius must not override the authoritative maximum
                # wind.  Keep the rejection in provenance rather than drawing
                # a threshold the source says the storm did not reach.
                if measured is not None and not measured.is_empty:
                    position_source[threshold] = {
                        quadrant: "source_rejected_above_vmax" for quadrant in QUADRANTS
                    }
                continue

            if threshold == vmax and measured is None:
                position_source[threshold] = {
                    quadrant: "model_zero_area_at_vmax" for quadrant in QUADRANTS
                }
                continue
            if threshold == vmax and measured is not None and not measured.is_complete:
                position_source[threshold] = {
                    quadrant: "source_incomplete_at_vmax" for quadrant in QUADRANTS
                }
                continue

            inferred = modelled.get(threshold)
            if measured is None:
                if inferred is None:
                    raise ValueError(
                        f"{track.storm_id} {position.valid_at.isoformat()} is missing "
                        f"the expected {threshold} kt radius"
                    )
                position_radii.append(inferred)
                position_source[threshold] = {
                    quadrant: "modelled" for quadrant in QUADRANTS
                }
                continue

            values: dict[str, int] = {}
            sources: dict[str, str] = {}
            for quadrant in QUADRANTS:
                observed = getattr(measured, quadrant)
                if observed is not None:
                    values[quadrant] = observed
                    sources[quadrant] = "measured"
                    continue
                if inferred is None:
                    raise ValueError(
                        f"{track.storm_id} {position.valid_at.isoformat()} is missing "
                        f"the expected {threshold} kt {quadrant.upper()} radius"
                    )
                values[quadrant] = getattr(inferred, quadrant)
                sources[quadrant] = "modelled"

            position_radii.append(Radii(threshold_kt=threshold, **values))
            position_source[threshold] = sources

        filled.append(replace(position, radii=tuple(position_radii)))
        provenance.append(position_source)

    return filled, provenance


def _storm_type(position: Position) -> str:
    if position.status:
        return HURDAT_STORM_TYPES.get(position.status, position.status)
    return "HURRICANE" if (position.max_wind_kt or 0) >= 64 else "TROPICAL STORM"


def _track_wkt(positions: tuple[Position, ...]) -> str | None:
    """The disclosed perfect-foresight path for a historical advisory.

    A hindcast has no uncertainty cone, but omitting its line entirely makes
    the map less truthful rather than more: the positions already disclose the
    path the storm actually took. Keep the geometry and let the console label
    it as a historical path rather than a forecast track.
    """
    if len(positions) < 2:
        return None
    coordinates = ", ".join(
        f"{position.lon:.6f} {position.lat:.6f}" for position in positions
    )
    return f"LINESTRING({coordinates})"


def advisories_from_track(
    track: StormTrack,
    *,
    forecast_hours: int = FORECAST_HOURS,
    _filled_positions: list[Position] | None = None,
) -> list[ForecastAdvisory]:
    """One advisory per track point, each looking forward along the real track.

    Numbered from 1 in track order, because `advisory.advisory_number` is text
    that the console validates as `^\\d+[A-Z]?$` and sorts as an integer. A
    label like `t+06` would be rejected by the browser before it drew anything.
    """
    positions = _filled_positions
    if positions is None:
        positions, _ = _fill_radii(track)
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
                storm_type=_storm_type(current),
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
    replace_existing: bool = False,
) -> HazardEvent:
    """Write a historical storm as a replayable hazard event.

    Reuses `_wind_fields` and `_raw_payload` so the rows are indistinguishable
    in shape from Melissa's, which is the point: everything that already works
    for one storm works for this one without knowing it exists.

    Replacement is explicit and identity-stable. The hazard event and matching
    advisory UUIDs survive while derived risk/exposure rows are invalidated and
    rebuilt by the pipeline. That keeps claims and allocation plans attached to
    the same event and prevents queued work for unchanged advisory numbers from
    pointing at deleted rows. Authoritative NHC advisory events are never
    replaced with a perfect-foresight hindcast under the same external identity.
    """
    ref = (external_ref or track.storm_id).strip().lower()
    if _EXTERNAL_REF.fullmatch(ref) is None:
        raise ValueError(
            "external_ref must be 3-64 lowercase letters, digits, underscores or hyphens"
        )
    positions, radii_sources = _fill_radii(track)
    source_by_time = {
        position.valid_at: source
        for position, source in zip(positions, radii_sources, strict=True)
    }

    matches = list(
        session.scalars(
            select(HazardEvent).where(HazardEvent.external_ref == ref).with_for_update()
        )
    )
    if len(matches) > 1:
        raise RuntimeError(
            f"hazard event reference {ref!r} is ambiguous ({len(matches)} rows); "
            "apply the external_ref uniqueness migration after reconciling duplicates"
        )
    existing = matches[0] if matches else None
    if existing is not None and not replace_existing:
        raise ValueError(f"hazard event {ref!r} already exists")

    event_name = name or f"Hurricane {track.name.title()} {track.year}"
    if existing is None:
        event = HazardEvent(name=event_name, external_ref=ref, replay=True)
        session.add(event)
        session.flush()
        existing_advisories: list[Advisory] = []
    else:
        event = existing
        existing_advisories = list(
            session.scalars(
                select(Advisory)
                .where(
                    Advisory.hazard_event_id == event.id,
                    Advisory.observed.is_(False),
                )
                .with_for_update()
            )
        )
        authoritative = [
            advisory.advisory_number
            for advisory in existing_advisories
            if not advisory.raw.get("synthesized", False)
        ]
        if authoritative:
            raise ValueError(
                f"hazard event {ref!r} contains authoritative advisories "
                f"{authoritative[:5]}; ingest the hindcast under a different external_ref"
            )

        event.name = event_name
        event.replay = True
        # A stable event identity means the event-scoped completion marker does
        # not cascade away. Remove it first so an interrupted rebuild is
        # unavailable rather than appearing complete against stale advisories.
        session.execute(
            text("DELETE FROM place_exposure_build WHERE hazard_event_id = :event"),
            {"event": event.id},
        )
        session.execute(
            text(
                "DELETE FROM risk_assessment r USING advisory a "
                "WHERE r.advisory_id = a.id "
                "AND a.hazard_event_id = :event AND a.observed = false"
            ),
            {"event": event.id},
        )
        session.execute(
            text(
                "DELETE FROM place_exposure e USING advisory a "
                "WHERE e.advisory_id = a.id "
                "AND a.hazard_event_id = :event AND a.observed = false"
            ),
            {"event": event.id},
        )

    advisory_by_number = {
        advisory.advisory_number: advisory for advisory in existing_advisories
    }
    if len(advisory_by_number) != len(existing_advisories):
        raise RuntimeError(
            f"hazard event {ref!r} has duplicate forecast advisory numbers"
        )

    for advisory in advisories_from_track(track, _filled_positions=positions):
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
        evidence: set[str] = set()
        incomplete_source = False
        for raw_position, position in zip(
            raw["positions"], advisory.positions, strict=True
        ):
            position_source = source_by_time[position.valid_at]
            raw_position["radii_source"] = {
                str(threshold): quadrants
                for threshold, quadrants in position_source.items()
            }
            evidence.update(
                source
                for quadrants in position_source.values()
                for source in quadrants.values()
                if source in {"measured", "modelled"}
            )
            incomplete_source = incomplete_source or any(
                source == "source_incomplete_at_vmax"
                for quadrants in position_source.values()
                for source in quadrants.values()
            )
        if evidence == {"measured", "modelled"}:
            raw["size_source"] = "mixed"
        elif "modelled" in evidence:
            raw["size_source"] = "modelled"
        elif "measured" in evidence:
            raw["size_source"] = "measured"
        elif incomplete_source:
            raw["size_source"] = "unavailable"
        else:
            raw["size_source"] = "not_applicable"
        raw["hindcast"] = True

        row = advisory_by_number.pop(advisory.advisory_number, None)
        if row is None:
            row = Advisory(
                hazard_event_id=event.id,
                advisory_number=advisory.advisory_number,
                observed=False,
            )
            session.add(row)
        row.issued_at = advisory.issued_at
        row.raw = raw
        row.track = _track_wkt(advisory.positions)
        row.cone = None
        # WKT assigned straight to the geography column, as the NHC path does —
        # GeoAlchemy converts on the way in and an explicit ST_GeogFromText here
        # would be a second, divergent way of writing the same column.
        for field, value in fields.items():
            setattr(row, field, value)

    for stale in advisory_by_number.values():
        session.delete(stale)

    session.flush()
    return event
