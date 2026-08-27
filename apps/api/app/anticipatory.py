"""The pre-landfall list of vulnerable households (ALT-04).

Director-only, and that is a privacy decision rather than a permissions
convenience. This is a ranked register of vulnerable people and where they
live — the single most sensitive object the platform produces. Publishing it
would invert the privacy posture every other surface argues for, so it is
never on the public portal, never in an aggregate, and never reachable by any
role but DIRECTOR (PRD §11.4).

Ranked by vulnerability times probability, which is the ordering ALT-04 names.
Neither term alone is useful: a fragile household the storm will miss does not
need pre-positioning, and a sturdy one in the eye can usually wait.

Generated when posture reaches READY. Before that there is nothing to
anticipate and the list would be a directory with no purpose, which is exactly
what it must not become.
"""

from __future__ import annotations

import csv
import io
import uuid

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from lighthouse_contracts import Posture, StormFileState

from app.models import Advisory, HazardEvent

#: Enough to pre-position against, few enough to act on. ALT-04 says "top N by
#: vulnerability x probability" and leaves N open; this is the number a parish
#: coordinator can actually work through before landfall.
DEFAULT_TOP_N = 100


class AnticipatoryListUnavailable(RuntimeError):
    """Safe, non-PII failure."""


def _latest_advisory(session: Session, event: HazardEvent) -> Advisory | None:
    return session.scalar(
        select(Advisory)
        .where(Advisory.hazard_event_id == event.id)
        .order_by(Advisory.issued_at.desc(), Advisory.id.desc())
        .limit(1)
    )


def build_list(
    session: Session, hazard_event_id: uuid.UUID, *, limit: int = DEFAULT_TOP_N
) -> list[dict]:
    """Rank registered households by vulnerability times wind probability."""
    event = session.get(HazardEvent, hazard_event_id)
    if event is None:
        raise AnticipatoryListUnavailable("hazard event does not exist")
    if event.current_posture in {Posture.QUIET, Posture.WATCH}:
        # Not a permissions error and not an empty list: at WATCH there is
        # genuinely nothing to anticipate yet, and saying so is different from
        # saying nobody is at risk.
        raise AnticipatoryListUnavailable(
            f"posture is {event.current_posture}; the anticipatory list is "
            "generated from READY"
        )
    advisory = _latest_advisory(session, event)
    if advisory is None:
        raise AnticipatoryListUnavailable("the event has no advisory to rank against")

    rows = session.execute(
        text(
            """
            SELECT sf.id, sf.parish, sf.community, sf.vuln_score,
                   sf.people, sf.structure,
                   ra.p34, ra.predicted_band::text AS predicted_band,
                   COALESCE(sf.vuln_score, 50) * COALESCE(ra.p34, 0) AS rank_score
              FROM storm_file sf
              JOIN risk_assessment ra ON ra.storm_file_id = sf.id
             WHERE ra.advisory_id = :advisory_id
               AND sf.state <> :settled
             ORDER BY rank_score DESC, sf.vuln_score DESC NULLS LAST, sf.id
             LIMIT :limit
            """
        ),
        {
            "advisory_id": advisory.id,
            "settled": StormFileState.SETTLED.value,
            "limit": limit,
        },
    ).all()

    return [
        {
            "storm_file_id": str(row.id),
            "parish": row.parish,
            "community": row.community,
            "vulnerability": row.vuln_score,
            "wind_probability_34kt": float(row.p34) if row.p34 is not None else None,
            "predicted_band": row.predicted_band,
            "rank_score": round(float(row.rank_score), 2),
            # People counts, not people. Enough to size a delivery, not enough
            # to identify anyone, and the name is deliberately absent even on
            # a Director-only surface.
            "household_size": (row.people or {}).get("total"),
            "elderly": (row.people or {}).get("elderly"),
            "children": (row.people or {}).get("children"),
            "roof": (row.structure or {}).get("roof"),
        }
        for row in rows
    ]


def to_csv(rows: list[dict]) -> str:
    """Exportable, because pre-positioning happens on paper in a truck."""
    columns = [
        "storm_file_id",
        "parish",
        "community",
        "vulnerability",
        "wind_probability_34kt",
        "predicted_band",
        "rank_score",
        "household_size",
        "elderly",
        "children",
        "roof",
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column) for column in columns})
    return buffer.getvalue()


__all__ = [
    "DEFAULT_TOP_N",
    "AnticipatoryListUnavailable",
    "build_list",
    "to_csv",
]
