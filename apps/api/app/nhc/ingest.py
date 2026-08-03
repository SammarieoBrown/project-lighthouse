"""Load a cached storm into ``hazard_event`` and ``advisory``.

Reads the committed cache, never the network. The replay depends on this being
reproducible, so nothing here reaches outside ``data/replay/cache``.

The unions are done by PostGIS rather than in Python. We are already running the
database that will be asked "is this household inside the 64 kt field", and
having a second geometry engine produce the answer we store would mean two
things that can disagree about what a polygon means.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import MELISSA
from app.models import Advisory, HazardEvent
from app.nhc.fstadv import ForecastAdvisory, parse_fstadv
from app.nhc.geometry import quadrant_polygon_wkt
from app.nhc.shapes import read_layer
from app.nhc.wndprb import WindProbabilities, parse_wndprb

THRESHOLDS = (34, 50, 64)


def _union_geography(session: Session, wkts: list[str]) -> str | None:
    """Union polygons into one MultiPolygon.

    ``ST_Union`` is a geometry operation, so the cast to geography happens after
    the merge. Doing it the other way round would union on the spheroid, which
    is slower and, at the size of a wind field, identical.
    """
    if not wkts:
        return None
    arrays = ", ".join(f"ST_GeomFromText(:w{i}, 4326)" for i in range(len(wkts)))
    # Returned as WKT because a geography column binds through ST_GeogFromText.
    # Handing it the EWKB hex that ::geography produces makes PostGIS try to
    # read binary as text and fail on the second character, which is a long way
    # from where the mistake was made.
    row = session.execute(
        text(f"SELECT ST_AsText(ST_Multi(ST_Union(ARRAY[{arrays}]))) AS g"),
        {f"w{i}": w for i, w in enumerate(wkts)},
    ).one()
    return row.g


def _wind_fields(session: Session, advisory: ForecastAdvisory) -> dict[str, str | None]:
    """One MultiPolygon per threshold: the union across every forecast hour.

    The union is the point. A single forecast hour is where the wind is at that
    moment; what a household needs to know is whether it falls inside the area
    the storm is expected to sweep at all, at any hour of the forecast.
    """
    fields: dict[str, str | None] = {}
    for kt in THRESHOLDS:
        polygons = []
        for position in advisory.positions:
            radii = position.radius(kt)
            if radii is None or radii.is_empty:
                continue
            wkt = quadrant_polygon_wkt(
                position.lat, position.lon,
                ne=radii.ne, se=radii.se, sw=radii.sw, nw=radii.nw,
            )
            if wkt:
                polygons.append(wkt)
        fields[f"wind_field_{kt}"] = _union_geography(session, polygons)
    return fields


def _geojson_geography(session: Session, geometry: dict | None) -> str | None:
    if not geometry:
        return None
    row = session.execute(
        text("SELECT ST_AsText(ST_GeomFromGeoJSON(:g)) AS g"), {"g": json.dumps(geometry)}
    ).one()
    return row.g


def _raw_payload(
    advisory: ForecastAdvisory,
    probabilities: WindProbabilities | None,
    watch_codes: list[dict],
    sources: dict[str, str],
) -> dict:
    """Everything parsed, kept whole.

    Storing derived geometry without the source it came from makes every later
    question about how a number was reached unanswerable — the same reason every
    agent output is stored raw.
    """
    return {
        "storm_type": advisory.storm_type,
        "movement_deg": advisory.movement_deg,
        "movement_kt": advisory.movement_kt,
        "pressure_mb": advisory.pressure_mb,
        "eye_diameter_nm": advisory.eye_diameter_nm,
        "positions": [
            {
                **{k: v for k, v in asdict(p).items() if k != "valid_at" and k != "radii"},
                "valid_at": p.valid_at.isoformat(),
                "radii": [asdict(r) for r in p.radii],
            }
            for p in advisory.positions
        ],
        "probabilities": {
            location: {
                str(row.threshold_kt): {
                    "incremental": row.incremental,
                    "cumulative": row.cumulative,
                }
                for row in probabilities.for_location(location)
            }
            for location in (probabilities.locations if probabilities else ())
        },
        # Distinct codes for a quick read, and the segments with their geometry
        # so "is there a hurricane warning *here*" is answerable at all.
        "watch_codes": sorted({w["code"] for w in watch_codes}),
        "watches_warnings": watch_codes,
        "source": sources,
    }


def ingest_storm(
    session: Session,
    *,
    cache: Path = MELISSA,
    name: str = "Hurricane Melissa",
    external_ref: str = "al132025",
    replay: bool = True,
) -> HazardEvent:
    """Load every cached advisory for one storm. Idempotent.

    Re-running replaces the geometry on existing rows rather than inserting
    duplicates, because the seeder will be run over and over during rehearsal
    and a replay that accumulates a second copy of the storm each time is a
    replay that stops being deterministic on the second run.
    """
    event = session.scalar(select(HazardEvent).where(HazardEvent.external_ref == external_ref))
    if event is None:
        event = HazardEvent(name=name, external_ref=external_ref, replay=replay)
        session.add(event)
        session.flush()

    existing = {
        (a.advisory_number, a.observed): a
        for a in session.scalars(select(Advisory).where(Advisory.hazard_event_id == event.id))
    }

    for path in sorted((cache / "text" / "fstadv").glob("*.fstadv.*.txt")):
        advisory = parse_fstadv(path.read_text())
        number = advisory.advisory_number

        # NHC prints "25" in the product and writes "025" in every filename.
        # The sibling files are found by the stem of the file we are already
        # holding rather than by re-deriving a zero-padded form from the parsed
        # number — the filesystem is the authority on its own names, and getting
        # this wrong silently drops every cone and track to NULL rather than
        # failing.
        file_number = path.name.rsplit(".", 2)[-2]

        probabilities = None
        prob_path = cache / "text" / "wndprb" / f"{external_ref}.wndprb.{file_number}.txt"
        if prob_path.exists():
            probabilities = parse_wndprb(prob_path.read_text())

        cone = track = None
        watch_codes: list[str] = []
        gis = cache / "gis" / "5day" / f"{external_ref}_5day_{file_number}.zip"
        if gis.exists():
            cone = _geojson_geography(session, read_layer(gis, "_5day_pgn")[0].geometry)
            lines = read_layer(gis, "_5day_lin")
            track = _geojson_geography(session, lines[0].geometry) if lines else None
            # Absent once every watch has been discontinued — an empty list is
            # the correct reading of "no warnings in effect".
            #
            # Kept **with their geometry**. A watch/warning bundle covers the
            # whole storm, so at advisory 1 the hurricane watch is for
            # Hispaniola and has nothing to say about Jamaica. Storing bare
            # codes throws away the only thing that makes them answerable — it
            # put the replay on READY five days out because somewhere, someone
            # was under a watch.
            watch_codes = [
                {"code": f.attributes["TCWW"], "geometry": f.geometry}
                for f in read_layer(gis, "_ww_wwlin", required=False)
                if f.attributes.get("TCWW") and f.geometry
            ]

        row = existing.get((number, False))
        if row is None:
            row = Advisory(hazard_event_id=event.id, advisory_number=number, observed=False)
            session.add(row)

        row.issued_at = advisory.issued_at
        row.cone = cone
        row.track = track
        for field, value in _wind_fields(session, advisory).items():
            setattr(row, field, value)
        row.raw = _raw_payload(
            advisory,
            probabilities,
            watch_codes,
            {
                "fstadv": path.name,
                "wndprb": prob_path.name if probabilities else None,
                "gis": gis.name if gis.exists() else None,
            },
        )

    _ingest_best_track(session, event, cache=cache, external_ref=external_ref)
    session.flush()
    return event


def _ingest_best_track(
    session: Session, event: HazardEvent, *, cache: Path, external_ref: str
) -> None:
    """The post-season reanalysis: what actually happened.

    Stored as a single ``observed`` row rather than one per advisory, because
    the best track is not an advisory cycle — it is one description of the whole
    storm, published months later. Verification asks it whether a household was
    inside the wind that arrived, which is a different question from whether it
    was inside the wind that was forecast.
    """
    archive = cache / "gis" / "best_track" / f"{external_ref}_best_track.zip"
    if not archive.exists():
        return

    swath = read_layer(archive, "_windswath")
    lines = read_layer(archive, "_lin")

    row = session.scalar(
        select(Advisory).where(
            Advisory.hazard_event_id == event.id,
            Advisory.observed.is_(True),
        )
    )
    if row is None:
        row = Advisory(
            hazard_event_id=event.id,
            advisory_number="best_track",
            observed=True,
        )
        session.add(row)

    # The swath layer holds several polygons per threshold, which is why the
    # column is a MultiPolygon: a wind field that breaks apart as the storm
    # weakens and re-intensifies is genuinely several disjoint areas.
    for kt in THRESHOLDS:
        parts = [f for f in swath if int(f.attributes.get("RADII", 0)) == kt and f.geometry]
        merged = None
        if parts:
            merged = session.execute(
                text(
                    "SELECT ST_AsText(ST_Multi(ST_Union(ARRAY["
                    + ", ".join(f"ST_GeomFromGeoJSON(:g{i})" for i in range(len(parts)))
                    + "]))) AS g"
                ),
                {f"g{i}": json.dumps(f.geometry) for i, f in enumerate(parts)},
            ).one().g
        setattr(row, f"wind_field_{kt}", merged)

    row.issued_at = datetime.now(UTC)
    row.track = _geojson_geography(session, lines[0].geometry) if lines else None
    row.raw = {
        "source": {"gis": archive.name},
        "note": "post-season best track reanalysis — observed, not forecast",
        "swath_thresholds": sorted({int(f.attributes.get("RADII", 0)) for f in swath}),
    }
