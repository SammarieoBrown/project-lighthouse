"""National readiness posture: the rules, and the transition they drive.

This is the decision Forecast Sentinel owns. It began life under
``app/replay/`` because the replay driver could not demonstrate anything
without it, on the understanding that the agent would consume it rather than
reinvent the judgement when it landed. The agent has landed, so the rules
moved to where the agent lives — there is still exactly one definition of what
READY means, and now exactly one place that acts on it.

It is deliberately not a model: it is four rules a Director can read, disagree
with, and overrule.

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
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from lighthouse_contracts import ActorKind, AgentName, Event, Posture
from lighthouse_contracts.agents import ForecastSentinelOutput

from app import ledger, queue
from app.models import Advisory, HazardEvent
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


# ---------------------------------------------------------------------------
# The transition. Everything above decides what the posture *is*; this decides
# what happens when it changes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PostureDecision:
    """One advisory's effect on national posture."""

    posture: Posture
    previous: Posture
    changed: bool
    output: ForecastSentinelOutput


def _affected_parishes(session: Session, advisory: Advisory) -> list[str]:
    """Parishes with registered households inside the 34 kt wind field.

    The same containment test Risk Mapper uses, asked of the registry rather
    than of a single household. Empty is a real answer — an advisory whose
    wind field misses the registry entirely affects nobody we know about, and
    saying so is more useful than naming every parish the storm threatens.
    """
    if advisory.wind_field_34 is None:
        return []
    rows = session.execute(
        text(
            """
            SELECT DISTINCT sf.parish
              FROM storm_file sf
              JOIN advisory adv ON adv.id = :advisory_id
             WHERE sf.parish IS NOT NULL
               AND sf.location IS NOT NULL
               AND adv.wind_field_34 IS NOT NULL
               AND ST_Intersects(adv.wind_field_34, sf.location)
             ORDER BY sf.parish
            """
        ),
        {"advisory_id": advisory.id},
    ).scalars()
    return [parish for parish in rows if parish]


def evaluate_posture(
    session: Session, event: HazardEvent, advisory: Advisory
) -> PostureDecision:
    """Set national posture from one advisory, and record it if it moved.

    Idempotent by construction: posture is derived from the advisory, so
    running this twice on the same advisory computes the same answer, finds it
    already current the second time, and writes nothing. That is what lets the
    replay driver call it synchronously — it needs the answer in hand to report
    what an advisory did — while the worker calls it from a queued job on the
    live feed path, without the two racing to double-record anything.

    A posture change is a ledger event (HAZ-03) and not merely a column write.
    For most of this project's life it was only the column: the driver moved
    ``current_posture`` and enqueued the alert job, so the fact that the
    country went to READY at 03:40 existed nowhere anyone could audit. A
    posture change is the moment people start acting, and an unrecorded one
    makes every action that follows unexplainable.
    """
    previous = event.current_posture
    posture = posture_for(session, advisory)
    changed = posture != previous
    parishes = _affected_parishes(session, advisory)

    if changed:
        event.current_posture = posture

    output = ForecastSentinelOutput(
        posture=posture,
        posture_changed=changed,
        affected_parishes=parishes,
        advisory_number=advisory.advisory_number,
        # ACT is the level at which a Director is expected to be awake and
        # deciding, so reaching it asks for a human even though the posture
        # itself is set autonomously.
        escalate_to_human=changed and posture is Posture.ACT,
        rationale=(
            f"Advisory {advisory.advisory_number}: posture {previous} -> {posture}"
            if changed
            else f"Advisory {advisory.advisory_number}: posture holds at {posture}"
        ),
    )

    if not changed:
        return PostureDecision(posture, previous, False, output)

    ledger.append(
        session,
        action=str(Event.HAZARD_POSTURE_CHANGED),
        subject_type="hazard_event",
        subject_id=event.id,
        payload={
            "hazard_event_id": str(event.id),
            "advisory_id": str(advisory.id),
            "advisory_number": advisory.advisory_number,
            "issued_at": advisory.issued_at.isoformat(),
            "previous_posture": str(previous),
            "posture": str(posture),
            "affected_parishes": parishes,
            "escalate_to_human": output.escalate_to_human,
        },
        actor_kind=ActorKind.AGENT,
        agent=AgentName.FORECAST_SENTINEL,
    )

    # Alert cascades hang off the posture change, not off the advisory
    # arriving. No handler is registered for this yet, so the worker parks it
    # and the backlog waits for the Alert Agent.
    queue.enqueue(
        session,
        job_type=AgentName.ALERT_AGENT,
        payload={
            "hazard_event_id": str(event.id),
            "advisory_id": str(advisory.id),
            "advisory_number": advisory.advisory_number,
            "issued_at": advisory.issued_at.isoformat(),
            "event": str(Event.HAZARD_POSTURE_CHANGED),
            "previous_posture": str(previous),
            "posture": str(posture),
        },
    )
    return PostureDecision(posture, previous, True, output)


__all__ = [
    "PostureDecision",
    "SIMPLIFY_DEG",
    "arrival_hours",
    "evaluate_posture",
    "posture_for",
    "posture_from",
    "replay_area_wkb",
    "warning_codes_here",
]
