"""National readiness posture from an advisory.

This is the decision Forecast Sentinel will own. It lives here because the
replay driver cannot demonstrate anything without it, and because it is
deliberately not a model: it is four rules a Director can read, disagree with,
and overrule. When the agent lands it consumes this function rather than
reinventing the judgement, so there is one definition of what READY means.

    ACT     a hurricane warning covering the replay area, or hurricane-force
            wind forecast to arrive within 36 hours
    READY   a hurricane watch covering the area, or damaging (50 kt) wind
            within 48 hours, or hurricane-force wind within 72
    WATCH   tropical-storm-force wind forecast to arrive at all
    QUIET   none of the above

Two things this gets right that a simpler version got wrong, both of which
produced a posture curve that looked plausible and was useless:

**Geography.** The rules are evaluated against the replay area — the parishes we
hold a registry for — not against Jamaica and not against the storm. A watch and
warning bundle covers everywhere the storm threatens, so at advisory 1 the
hurricane watch in the file is for Hispaniola. Reading bare codes put the
country on READY five days out because somebody, somewhere, was under a watch.

**Time.** ``advisory.wind_field_*`` is the union across the whole forecast
period, so "does 50 kt wind reach here" is true from the first advisory that
points at Jamaica and stays true. Posture has to ask *when*, which means going
back to the per-forecast-hour radii in ``raw`` rather than the merged geometry.
Collapse time and posture saturates on day one, which is the same as having no
posture at all.
"""

from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from lighthouse_contracts import Posture

from app.models import Advisory
from app.nhc.geometry import quadrant_polygon_wkt
from app.registry.geography import REPLAY_PARISHES, load_parishes

HURRICANE_WARNING = "HWR"
HURRICANE_WATCH = "HWA"

#: How close a warning segment has to run to count as covering us. The segments
#: are coastline linework and our parish outline is that same coast from the
#: other side, so exact intersection is a coin toss on rounding. Ten kilometres
#: is comfortably inside "this warning is about this parish".
_WARNING_NEAR_M = 10_000

#: Lead times, in hours. These are the thresholds a Director is agreeing to when
#: they sign an alert cascade, so they are written here as numbers rather than
#: buried in a query. NHC issues a hurricane warning about 36 hours before onset
#: and a watch about 48, which is where these come from.
ACT_WITHIN_H = 36
READY_50_WITHIN_H = 48
READY_64_WITHIN_H = 72

#: Simplification tolerance for the replay area, in degrees — roughly 200 m.
#: The parish outlines carry tens of thousands of vertices and the question is
#: whether a wind field hundreds of kilometres across touches them. Verified,
#: not assumed: a test compares every advisory against the unsimplified outline.
#:
#: The console export reduces the same outlines to the same tolerance, and
#: imports this rather than restating it: a map drawn at one resolution from a
#: posture decided at another would disagree with itself on screen.
SIMPLIFY_DEG = 0.002

_AREA_WKB: str | None = None


def replay_area_wkb(session: Session, *, simplify: bool = True) -> str:
    """The parishes we hold a registry for, as one geometry in WKB hex.

    Cached because the alternative is re-parsing a fifth of a megabyte of
    GeoJSON on every threshold of every advisory — measured at half a second a
    call, which turned an eleven-test file into an eight-minute one.
    """
    global _AREA_WKB
    if simplify and _AREA_WKB is not None:
        return _AREA_WKB

    parishes = load_parishes(REPLAY_PARISHES)
    collection = json.dumps(
        {
            "type": "GeometryCollection",
            "geometries": [p.geometry for p in parishes.values()],
        }
    )
    geom = "ST_Union(ST_GeomFromGeoJSON(:area))"
    if simplify:
        geom = f"ST_SimplifyPreserveTopology({geom}, {SIMPLIFY_DEG})"

    wkb = session.execute(
        text(f"SELECT encode(ST_AsBinary({geom}), 'hex') AS wkb"), {"area": collection}
    ).scalar()

    if simplify:
        _AREA_WKB = wkb
    return wkb


_AREA_CTE = """
WITH area AS (
  SELECT ST_GeomFromWKB(decode(:wkb, 'hex'), 4326)::geography AS g
)
"""


def warning_codes_here(session: Session, advisory: Advisory, *, simplify: bool = True) -> set[str]:
    """Watch and warning codes whose segments actually cover the replay area."""
    segments = [w for w in (advisory.raw.get("watches_warnings") or ()) if isinstance(w, dict)]
    if not segments:
        return set()

    rows = session.execute(
        text(
            _AREA_CTE
            + """
            SELECT DISTINCT s.code
            FROM area, jsonb_to_recordset(:segments) AS s(code text, geometry jsonb)
            WHERE ST_DWithin(area.g, ST_GeomFromGeoJSON(s.geometry)::geography, :near)
            """
        ),
        {
            "wkb": replay_area_wkb(session, simplify=simplify),
            "segments": json.dumps(segments),
            "near": _WARNING_NEAR_M,
        },
    ).scalars()
    return set(rows)


def arrival_hours(session: Session, advisory: Advisory, *, simplify: bool = True) -> dict[int, float]:
    """Hours until each wind threshold first reaches the area, per this advisory.

    Built from the per-forecast-hour radii rather than the merged wind field,
    because the merged field has no time in it. A threshold that never reaches
    the area is absent from the result rather than present with a large number —
    "not forecast" and "forecast in five days" are different claims.
    """
    positions = advisory.raw.get("positions") or []
    rows: list[dict] = []
    for position in positions:
        for radii in position.get("radii", []):
            wkt = quadrant_polygon_wkt(
                position["lat"],
                position["lon"],
                ne=radii["ne"], se=radii["se"], sw=radii["sw"], nw=radii["nw"],
            )
            if wkt:
                rows.append(
                    {"kt": radii["threshold_kt"], "valid_at": position["valid_at"], "wkt": wkt}
                )
    if not rows:
        return {}

    found = session.execute(
        text(
            _AREA_CTE
            + """
            SELECT p.kt, min(p.valid_at::timestamptz) AS first_at
            FROM area, jsonb_to_recordset(:rows) AS p(kt int, valid_at text, wkt text)
            WHERE ST_Intersects(area.g, ST_GeomFromText(p.wkt, 4326)::geography)
            GROUP BY p.kt
            """
        ),
        {"wkb": replay_area_wkb(session, simplify=simplify), "rows": json.dumps(rows)},
    ).all()

    issued = advisory.issued_at
    return {
        int(r.kt): max((r.first_at - issued) / timedelta(hours=1), 0.0)
        for r in found
    }


def posture_from(codes: set[str], arrival: dict[int, float]) -> Posture:
    """Four rules, in order of severity. Nothing here is a model.

    Split out from ``posture_for`` so a caller that already holds the codes and
    the arrival times — the console export wants both on the frame anyway — can
    reach the same verdict without asking the database for them twice. There is
    still one definition of what READY means, and it is this function.
    """

    def within(kt: int, hours: float) -> bool:
        return kt in arrival and arrival[kt] <= hours

    if HURRICANE_WARNING in codes or within(64, ACT_WITHIN_H):
        return Posture.ACT
    if (
        HURRICANE_WATCH in codes
        or within(50, READY_50_WITHIN_H)
        or within(64, READY_64_WITHIN_H)
    ):
        return Posture.READY
    if 34 in arrival:
        return Posture.WATCH
    return Posture.QUIET


def posture_for(session: Session, advisory: Advisory, *, simplify: bool = True) -> Posture:
    """The rules above, against one advisory."""
    return posture_from(
        warning_codes_here(session, advisory, simplify=simplify),
        arrival_hours(session, advisory, simplify=simplify),
    )
