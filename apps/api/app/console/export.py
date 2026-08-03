"""Rebuild the console's replay file from the database.

    uv run python -m app.console.export

The console is a static Next.js app. It has no database, no Python and no
network it can rely on — the venue wifi is a hope, not a dependency — so
everything the EOC screen knows about Melissa arrives as one generated file at
``apps/console/public/replay/replay.json``. This module writes it, and
``docs/engineering/replay-export-contract.md`` is the agreement about its shape.

**The output is committed on purpose.** Vercel builds `apps/console` with
``npm run build`` and nothing else: no Python, no ``uv``, no ``DATABASE_URL``.
A gitignored file would mean production ships with no data at all. So treat
``replay.json`` like a lockfile — derived, deterministic, and checked in, with
the command above as the way to regenerate it. It is not a stray artefact, and
it is not hand-edited.

Two runs against the same database produce byte-identical output, which is what
makes committing it safe: a stale file is a diff, not a silent divergence.

Three things this gets right that a first pass got wrong:

**Static geometry is emitted once.** Parish outlines and household positions do
not move between advisories. Repeating them per frame is the difference between
a file that fits in a browser and one that does not, and re-deriving them per
advisory is the difference between an export measured in seconds and one
measured in minutes — a naive version re-parsed a fifth of a megabyte of GeoJSON
per call and took 147 seconds to do it.

**Ordering is the join.** ``district_counts[i]`` belongs to ``districts[i]`` and
``household_bands[i]`` to ``households[i]``. Nothing carries a key, because 2,000
keys per frame across 41 frames is megabytes of repeated identifiers. The
lengths are asserted before anything is written: a silent off-by-one here
mislabels real homes.

**The best track is not a frame.** It is what happened, not what was forecast,
and folding it into the timeline would let the console claim foresight it did
not have.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from itertools import takewhile
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session, defer

from app.config import REPO_ROOT
from app.models import Advisory, HazardEvent
from app.registry.geography import community_districts, load_parishes, parish_names
from app.replay.posture import SIMPLIFY_DEG, arrival_hours, posture_from, warning_codes_here

#: Where the console looks for it. Committed — see the module docstring.
DEFAULT_OUTPUT = REPO_ROOT / "apps" / "console" / "public" / "replay" / "replay.json"

#: The replayed storm.
DEFAULT_EVENT = "al132025"

#: One character per household per frame. 2,000 characters rather than 2,000
#: objects is the difference between 82 KB and 3 MB across the whole storm.
BAND_CHAR = {"DESTROYED": "d", "MAJOR": "m", "MINOR": "n", "NONE": "o"}

#: Order of the four counts in ``totals`` and in every ``district_counts`` row.
BAND_ORDER = ("d", "m", "n", "o")
BAND_NAMES = ("destroyed", "major", "minor", "none")

#: Six decimal places is about 10 cm — far finer than a synthetic household
#: position or a wind radius published in whole nautical miles is known to.
#: Also the reason two runs agree byte for byte: the rounding happens on write.
COORD_DP = 6

#: The cumulative probability the console shows. NHC publishes 34, 50 and 64 kt;
#: 64 kt is hurricane force, which is the number a posture decision hangs on.
PROBABILITY_KT = "64"

_GEOMETRY_COLUMNS = (
    Advisory.cone,
    Advisory.track,
    Advisory.wind_field_34,
    Advisory.wind_field_50,
    Advisory.wind_field_64,
)


class ExportError(RuntimeError):
    """The payload would have been wrong. Nothing is written."""


# --------------------------------------------------------------------------
# Small conversions
# --------------------------------------------------------------------------


def _iso_z(when: datetime) -> str:
    """UTC, ``Z``-suffixed, seconds resolution.

    The console formats fixed to ``en-JM``. A naive local timestamp renders one
    way on the server and another in the browser and takes the whole tree down
    as a hydration mismatch, so the offset is never left implicit.
    """
    return when.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"


def advisory_key(number: str) -> tuple[int, str]:
    """Sort key for an advisory number, parsed as an integer.

    As a string ``"10"`` sorts before ``"9"`` and the timeline renders the storm
    out of order while looking entirely plausible. NHC also issues intermediate
    advisories numbered ``15A``, so the leading digits decide and the whole
    string breaks the tie — ``15A`` after ``15``, before ``16``.
    """
    digits = "".join(takewhile(str.isdigit, number))
    if not digits:
        raise ExportError(f"advisory number {number!r} does not start with a number")
    return int(digits), number


def _round(value: float) -> float:
    return round(value, COORD_DP)


def _band_case_sql() -> str:
    """The band-to-character mapping as SQL, generated from ``BAND_CHAR``.

    No ``ELSE``: an unmapped or missing band contributes NULL, ``string_agg``
    skips it, the frame comes out short, and the length assertion fails. A
    default would instead quietly call it NONE — reporting an unknown household
    as undamaged, which is the one direction this must never round.
    """
    whens = " ".join(f"WHEN '{band}' THEN '{char}'" for band, char in BAND_CHAR.items())
    return f"CASE ra.predicted_band::text {whens} END"


# --------------------------------------------------------------------------
# Static: emitted once, never varies by advisory
# --------------------------------------------------------------------------


def _parishes(session: Session, *, with_registry: set[str]) -> list[dict[str, Any]]:
    """All 14 parish outlines, simplified, in one round trip.

    ``registry`` says we hold Storm Files there, and is read off the households
    rather than off a constant: the flag has to mean "we can actually help
    someone here", and a hardcoded list would go on claiming that after the
    registry changed underneath it.
    """
    names = parish_names()
    parishes = load_parishes(names)
    geometries = json.dumps([parishes[name].geometry for name in names])

    rows = session.execute(
        text(
            """
            SELECT t.ord AS ord,
                   ST_AsGeoJSON(
                     ST_SimplifyPreserveTopology(ST_GeomFromGeoJSON(t.g::text), :tol),
                     :dp
                   ) AS geojson
            FROM jsonb_array_elements(CAST(:geometries AS jsonb))
                 WITH ORDINALITY AS t(g, ord)
            ORDER BY t.ord
            """
        ),
        {"geometries": geometries, "tol": SIMPLIFY_DEG, "dp": COORD_DP},
    ).all()

    if len(rows) != len(names):
        raise ExportError(f"asked for {len(names)} parish outlines, got {len(rows)}")

    return [
        {
            "name": name,
            "registry": name in with_registry,
            "geometry": json.loads(row.geojson),
        }
        for name, row in zip(names, rows, strict=True)
    ]


def _households(session: Session) -> list[dict[str, Any]]:
    """Every Storm File, ordered by primary key.

    Ordered by key rather than by anything meaningful because the order is a
    join, not a presentation: ``household_bands`` is built with the same
    ``ORDER BY`` in the database, so the two cannot drift apart. Sorting by
    parish here would need the band string sorted by parish there, in two
    places, agreeing forever.
    """
    rows = session.execute(
        text(
            """
            SELECT id,
                   ST_X(location::geometry) AS lon,
                   ST_Y(location::geometry) AS lat,
                   parish,
                   community,
                   structure->>'roof' AS roof
            FROM storm_file
            ORDER BY id
            """
        )
    ).all()

    out: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        household: dict[str, Any] = {"id": index}
        # Absent data is an absent key, never a null and never a zero. A
        # household with no location is a real state — a thin registration taken
        # over the phone — and the console can decline to plot it.
        if row.lon is not None and row.lat is not None:
            household["lon"] = _round(row.lon)
            household["lat"] = _round(row.lat)
        for key, value in (
            ("parish", row.parish),
            ("community", row.community),
            ("roof", row.roof),
        ):
            if value is not None:
                household[key] = value
        out.append(household)
    return out


def _districts(households: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[int]]:
    """The admin-2 districts that hold households, and each household's index.

    Returns the district list and a parallel-to-``households`` list of indices
    into it, which is what turns a frame's band string into district counts
    without another query.

    Districts with no households are dropped rather than emitted with ``n: 0``:
    their position is the mean of their households, and the mean of nothing is
    not a place.
    """
    lookup = community_districts()

    unknown = sorted(
        {
            (h.get("parish"), h.get("community"))
            for h in households
            if (h.get("parish"), h.get("community")) not in lookup
        }
    )
    if unknown:
        raise ExportError(
            "these households sit in communities the admin-3 dataset does not "
            f"know, so they belong to no district: {unknown[:5]}"
            f"{f' and {len(unknown) - 5} more' if len(unknown) > 5 else ''}"
        )

    named = [(h["parish"], lookup[(h["parish"], h["community"])]) for h in households]
    order = sorted(set(named))
    index_of = {key: i for i, key in enumerate(order)}

    #: households, then positioned households, then the summed coordinates. The
    #: two counts differ when a Storm File has no location — a thin registration
    #: taken over the phone. It still counts as a household in the district; it
    #: just cannot pull the district's marker towards the Gulf of Guinea.
    tally = [[0, 0, 0.0, 0.0] for _ in order]
    for key, household in zip(named, households, strict=True):
        bucket = tally[index_of[key]]
        bucket[0] += 1
        if "lon" in household and "lat" in household:
            bucket[1] += 1
            bucket[2] += household["lon"]
            bucket[3] += household["lat"]

    districts = []
    for i, (parish, district) in enumerate(order):
        total, placed, lon, lat = tally[i]
        entry: dict[str, Any] = {
            "id": i,
            "parish": parish,
            "district": district,
            "n": total,
        }
        if placed:
            entry["lon"] = _round(lon / placed)
            entry["lat"] = _round(lat / placed)
        districts.append(entry)

    return districts, [index_of[key] for key in named]


def _structures(session: Session) -> dict[tuple[str, str], int]:
    """Real structures per district, from the footprint inventory.

    The counts beside these are synthetic: 2,000 households dropped inside
    community polygons by a seeded generator, each sitting where nothing
    necessarily stands. These are 1,844,379 measured footprints. Where both
    exist the map should size itself on the measured one, because "413 of our
    500 synthetic homes" describes our random seed and "42,264 structures in
    Black River" describes Jamaica.
    """
    rows = session.execute(
        text(
            "SELECT parish, district, sum(structures)::int "
            "FROM place_structures GROUP BY 1, 2"
        )
    ).all()
    return {(parish, district): n for parish, district, n in rows}


def _exposure(session: Session) -> dict[UUID, dict[tuple[str, str], list[int]]]:
    """Structures per district inside each wind band, per advisory.

    ``[n64, n50, n34]``, mutually exclusive — a structure is counted once, at
    the strongest wind reaching it. The bands nest, so counting them
    independently would report the same building three times and inflate
    exposure by the width of the storm.

    Empty when the inventory has not been built. That is a legitimate state on
    a clone that has not run app.registry.buildings, so it degrades to absent
    keys rather than failing the export.
    """
    out: dict[UUID, dict[tuple[str, str], list[int]]] = {}
    rows = session.execute(
        text(
            "SELECT advisory_id, parish, district, band, sum(structures)::int "
            "FROM place_exposure GROUP BY 1, 2, 3, 4"
        )
    ).all()
    for advisory_id, parish, district, band, n in rows:
        slot = {64: 0, 50: 1, 34: 2}[band]
        entry = out.setdefault(advisory_id, {}).setdefault((parish, district), [0, 0, 0])
        entry[slot] = n
    return out


# --------------------------------------------------------------------------
# Per advisory
# --------------------------------------------------------------------------


def _advisories(session: Session, event: HazardEvent) -> list[Advisory]:
    """Forecast advisories, ascending by number. The best track is not one.

    Geometry is deferred: it is fetched once as GeoJSON in ``_geometry`` rather
    than pulled through the ORM as several megabytes of polygon per row.
    """
    advisories = list(
        session.scalars(
            select(Advisory)
            .options(*(defer(column) for column in _GEOMETRY_COLUMNS))
            .where(
                Advisory.hazard_event_id == event.id,
                Advisory.observed.is_(False),
            )
        )
    )
    return sorted(advisories, key=lambda a: advisory_key(a.advisory_number))


def _geometry(session: Session, event: HazardEvent) -> dict[UUID, dict[str, Any]]:
    """Every advisory's geometry as GeoJSON, in one round trip.

    Simplified to the same tolerance the posture rules use, so the wind field on
    screen is the one the decision was made against. The track is left alone:
    it is nine forecast positions, and simplifying it would drop forecasts
    rather than vertices.
    """
    rows = session.execute(
        text(
            """
            SELECT id,
                   ST_AsGeoJSON(
                     ST_SimplifyPreserveTopology(wind_field_34::geometry, :tol), :dp
                   ) AS wind34,
                   ST_AsGeoJSON(
                     ST_SimplifyPreserveTopology(wind_field_50::geometry, :tol), :dp
                   ) AS wind50,
                   ST_AsGeoJSON(
                     ST_SimplifyPreserveTopology(wind_field_64::geometry, :tol), :dp
                   ) AS wind64,
                   ST_AsGeoJSON(
                     ST_SimplifyPreserveTopology(cone::geometry, :tol), :dp
                   ) AS cone,
                   ST_AsGeoJSON(track::geometry, :dp) AS track
            FROM advisory
            WHERE hazard_event_id = :event AND observed = false
            """
        ),
        {"event": event.id, "tol": SIMPLIFY_DEG, "dp": COORD_DP},
    ).all()

    return {
        row.id: {
            key: json.loads(value)
            for key, value in (
                ("wind34", row.wind34),
                ("wind50", row.wind50),
                ("wind64", row.wind64),
                ("cone", row.cone),
                ("track", row.track),
            )
            # A tropical storm has no hurricane-force field. The honest
            # representation of an area that does not exist is no key.
            if value is not None
        }
        for row in rows
    }


def _bands(session: Session, event: HazardEvent) -> dict[UUID, str]:
    """One band character per household per advisory, ordered by household key.

    Built in the database as 41 strings rather than 82,000 rows: the ordering
    that makes ``household_bands`` a join is the same ``ORDER BY id`` that
    ``_households`` uses, and expressing it once in SQL is what keeps them
    honest.
    """
    rows = session.execute(
        text(
            f"""
            SELECT ra.advisory_id AS advisory_id,
                   count(*) AS assessed,
                   string_agg({_band_case_sql()}, '' ORDER BY ra.storm_file_id) AS bands
            FROM risk_assessment ra
            JOIN advisory a ON a.id = ra.advisory_id
            WHERE a.hazard_event_id = :event AND a.observed = false
            GROUP BY ra.advisory_id
            """
        ),
        {"event": event.id},
    ).all()

    for row in rows:
        if len(row.bands or "") != row.assessed:
            raise ExportError(
                f"advisory {row.advisory_id} has {row.assessed} assessments but "
                f"{len(row.bands or '')} readable bands — some predicted_band is "
                "null or outside the contract's four values"
            )
    return {row.advisory_id: row.bands for row in rows}


def _position(raw: dict[str, Any]) -> dict[str, Any]:
    """Where the storm is now, and how hard it is blowing there.

    The observed position, not a forecast one: everything else on the frame is
    what was expected, and the centre is what was measured.
    """
    positions = raw.get("positions") or []
    current = next((p for p in positions if p.get("kind") == "observed"), None)
    if current is None:
        current = positions[0] if positions else {}

    out: dict[str, Any] = {}
    if current.get("lon") is not None:
        out["lon"] = _round(current["lon"])
    if current.get("lat") is not None:
        out["lat"] = _round(current["lat"])
    for key in ("max_wind_kt", "gust_kt"):
        if current.get(key) is not None:
            out[key] = current[key]
    if raw.get("pressure_mb") is not None:
        out["pressure_mb"] = raw["pressure_mb"]
    return out


def _probabilities(raw: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Cumulative 64 kt probability, by location then forecast hour.

    NHC publishes no probability surface — only percentages at named locations,
    and the list of locations shrinks as the storm passes them. A location that
    has dropped out is absent here rather than zero: zero is a claim that the
    chance is nil, which is a different and false statement. Published zeros are
    kept, because NHC saying nil and NHC saying nothing are not the same thing.
    """
    published = raw.get("probabilities") or {}
    out: dict[str, dict[str, int]] = {}
    for location in sorted(published):
        block = (published[location] or {}).get(PROBABILITY_KT) or {}
        cumulative = block.get("cumulative") or {}
        if not cumulative:
            continue
        out[location] = {hour: cumulative[hour] for hour in sorted(cumulative, key=int)}
    return out


def _frame(
    advisory: Advisory,
    *,
    codes: set[str],
    arrival: dict[int, float],
    geometry: dict[str, Any],
    bands: str,
    district_index: list[int],
    district_count: int,
    exposed: list[list[int]] | None = None,
) -> dict[str, Any]:
    if len(bands) != len(district_index):
        raise ExportError(
            f"advisory {advisory.advisory_number}: {len(bands)} household bands "
            f"against {len(district_index)} households. The two are a positional "
            "join, so a mismatch mislabels real homes."
        )

    slot = {char: i for i, char in enumerate(BAND_ORDER)}
    counts = [[0, 0, 0, 0] for _ in range(district_count)]
    totals = [0, 0, 0, 0]
    for district, char in zip(district_index, bands, strict=True):
        index = slot[char]
        counts[district][index] += 1
        totals[index] += 1

    frame: dict[str, Any] = {
        "n": advisory.advisory_number,
        "at": _iso_z(advisory.issued_at),
        "posture": str(posture_from(codes, arrival)),
        "watch_codes": sorted(codes),
        "position": _position(advisory.raw or {}),
    }
    frame.update(geometry)
    frame["probabilities"] = _probabilities(advisory.raw or {})
    frame["totals"] = dict(zip(BAND_NAMES, totals, strict=True))
    frame["district_counts"] = counts
    frame["household_bands"] = bands
    # Absent, not zeroed, when the inventory has not been built — a clone that
    # has not run app.registry.buildings has no measurement, and zero would be
    # a claim that nothing is exposed.
    if exposed is not None:
        frame["district_exposed"] = exposed
    return frame


# --------------------------------------------------------------------------
# The payload
# --------------------------------------------------------------------------


def build_replay(
    session: Session,
    *,
    external_ref: str = DEFAULT_EVENT,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """The whole replay, as the console reads it.

    Pure: reads the database, returns a dict, writes nothing. ``generated_at``
    is injectable because it is the only value in the payload that is allowed to
    differ between two runs, and a test that could not pin it could not prove
    the rest is deterministic.
    """
    event = session.scalar(
        select(HazardEvent).where(HazardEvent.external_ref == external_ref)
    )
    if event is None:
        raise ExportError(
            f"no hazard event {external_ref!r} — run app.nhc.ingest.ingest_storm first"
        )

    households = _households(session)
    if not households:
        raise ExportError("no Storm Files — run app.registry.seed_registry first")

    districts, district_index = _districts(households)
    with_registry = {h["parish"] for h in households if h.get("parish")}

    advisories = _advisories(session, event)
    if not advisories:
        raise ExportError(f"hazard event {external_ref!r} has no forecast advisories")

    geometry = _geometry(session, event)
    bands = _bands(session, event)

    # Measured structures beside the synthetic households. Absent on a clone
    # that has not built the inventory, which the frames express by omitting the
    # key rather than by reporting zeros.
    structures = _structures(session)
    exposure = _exposure(session)
    for entry in districts:
        n = structures.get((entry["parish"], entry["district"]))
        if n is not None:
            entry["structures"] = n

    missing = [a.advisory_number for a in advisories if a.id not in bands]
    if missing:
        raise ExportError(
            f"no risk assessments for advisories {missing[:5]} — the replay has "
            "not been run against this registry, so the frames would be empty"
        )

    frames = [
        _frame(
            advisory,
            codes=warning_codes_here(session, advisory),
            arrival=arrival_hours(session, advisory),
            geometry=geometry.get(advisory.id, {}),
            bands=bands[advisory.id],
            district_index=district_index,
            district_count=len(districts),
            exposed=(
                [
                    exposure[advisory.id].get((d["parish"], d["district"]), [0, 0, 0])
                    for d in districts
                ]
                if advisory.id in exposure
                else None
            ),
        )
        for advisory in advisories
    ]

    for frame, advisory in zip(frames, advisories, strict=True):
        if len(frame["district_counts"]) != len(districts):
            raise ExportError(
                f"advisory {advisory.advisory_number}: {len(frame['district_counts'])} "
                f"district counts against {len(districts)} districts"
            )
        if len(frame["household_bands"]) != len(households):
            raise ExportError(
                f"advisory {advisory.advisory_number}: {len(frame['household_bands'])} "
                f"household bands against {len(households)} households"
            )

    return {
        "generated_at": _iso_z(generated_at or datetime.now(UTC)),
        "event": {
            # NHC's own identifier is upper case; the ingest stores the lower
            # case form it reads off the filenames.
            "id": (event.external_ref or "").upper(),
            "name": event.name,
            "advisory_count": len(frames),
        },
        "parishes": _parishes(session, with_registry=with_registry),
        "districts": districts,
        "households": households,
        "frames": frames,
    }


def serialise(payload: dict[str, Any]) -> bytes:
    """Compact, UTF-8, one trailing newline, and the same bytes every time.

    Compact rather than indented because this ships over a venue network:
    indentation is about a third of the file and none of it is read by anyone.
    Written as bytes so no platform gets to translate the newline.
    """
    text_out = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (text_out + "\n").encode("utf-8")


def write_replay(
    session: Session,
    path: Path | str | None = None,
    *,
    external_ref: str = DEFAULT_EVENT,
    generated_at: datetime | None = None,
) -> Path:
    """Build the payload and write it. Returns where it went."""
    payload = build_replay(
        session, external_ref=external_ref, generated_at=generated_at
    )
    destination = Path(path) if path is not None else DEFAULT_OUTPUT
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(serialise(payload))
    return destination


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.console.export",
        description=(
            "Regenerate apps/console/public/replay/replay.json from the database. "
            "The output is committed on purpose — the Vercel build has no Python."
        ),
    )
    parser.add_argument(
        "output",
        nargs="?",
        default=None,
        help=f"where to write it (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--event",
        default=DEFAULT_EVENT,
        help=f"hazard event external_ref (default: {DEFAULT_EVENT})",
    )
    args = parser.parse_args(argv)

    from app.db import session_scope

    started = datetime.now(UTC)
    with session_scope() as session:
        written = write_replay(session, args.output, external_ref=args.event)

    elapsed = (datetime.now(UTC) - started).total_seconds()
    size = written.stat().st_size
    print(f"{written} — {size / 1024:.1f} KB in {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
