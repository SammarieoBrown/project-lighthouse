"""Risk Mapper — hazard times exposure, per household, per advisory.

IMP-01. Version 1 is a transparent parametric lookup keyed on wind band by roof
type, and that is a decision rather than a shortcut: this is the number that
decides who gets warned first, so a Director has to be able to ask "why is this
household MAJOR" and get a sentence back rather than a model. ``method`` and
``model_version`` go on every row so the answer stays available, and so the
learning loop (IMP-04) has provenance when it refits against observed damage.

What it does *not* do is more interesting than what it does:

- No walls, no building age, no household composition. Those feed ``vuln_score``
  on the Storm File, which drives triage — how urgently to reach someone — not
  the damage prediction. Mixing them in would make the lookup untraceable and
  would double-count the same evidence in two places.
- No confidence from the model's own agreement with itself. Confidence here is
  about the *inputs*: how far out the forecast is, and how far the household
  sits from the nearest place NHC actually publishes a probability for.

The whole thing runs as one statement per advisory. Five hundred households
against forty-one advisories is twenty thousand assessments per replay, and a
row-at-a-time loop would make the demo wait on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from lighthouse_contracts import AgentName, DamageBand

from app.models import Advisory
from app.worker import register

METHOD = "parametric_lookup_v1"
MODEL_VERSION = "risk-lookup-2026.08.02"

#: The two Jamaican locations NHC publishes wind speed probabilities for. There
#: are no others — the product reports 26 places across the whole basin. Every
#: household probability is interpolated between these two, which is why
#: ``method`` says lookup and ``confidence`` falls off with distance from them.
PROBABILITY_POINTS = {
    "KINGSTON": (17.9714, -76.7931),
    "MONTEGO BAY": (18.4762, -77.8939),
}

# --------------------------------------------------------------------------
# The impact function.
#
# Rows are roof material, columns are the strongest wind band the household
# falls inside. Zinc sheeting over timber battens is the dominant rural
# construction in western Jamaica and it is also what leaves first: the fixings
# fail, the sheet lifts, and the building is open to the rain before the walls
# are touched. Concrete slab stays on and the damage moves to openings and
# contents. That is the entire physical argument, and it is the whole model.
#
# Deliberately coarse. Four bands is what a lookup this simple can honestly
# support, and pretending to more resolution than the inputs carry is the
# failure this project keeps guarding against.
# --------------------------------------------------------------------------
IMPACT: dict[str, dict[int, DamageBand]] = {
    "zinc": {0: DamageBand.NONE, 34: DamageBand.MINOR, 50: DamageBand.MAJOR, 64: DamageBand.DESTROYED},
    "shingle": {0: DamageBand.NONE, 34: DamageBand.MINOR, 50: DamageBand.MAJOR, 64: DamageBand.DESTROYED},
    "tile": {0: DamageBand.NONE, 34: DamageBand.NONE, 50: DamageBand.MINOR, 64: DamageBand.MAJOR},
    "concrete": {0: DamageBand.NONE, 34: DamageBand.NONE, 50: DamageBand.MINOR, 64: DamageBand.MAJOR},
}

#: Anything not in the table. Unknown construction is treated as the weaker
#: case, because under-warning a household costs more than over-warning one.
UNKNOWN_ROOF = "zinc"

# --------------------------------------------------------------------------
# Confidence. A stated heuristic, not a calibrated probability — which is
# exactly why it is written here in three numbers rather than buried in a query.
#
#   base            what we claim at zero lead, standing next to a sample point
#   per hour        forecast skill decays with lead time
#   per kilometre   our probability surface is two points for a whole island
# --------------------------------------------------------------------------
CONFIDENCE_BASE = 0.95
CONFIDENCE_PER_LEAD_HOUR = 0.004
CONFIDENCE_PER_KM = 0.0015
CONFIDENCE_FLOOR = 0.30


@dataclass(frozen=True)
class RiskRun:
    advisory_number: str
    assessed: int
    at_risk: int
    by_band: dict[str, int]


def _probabilities(advisory: Advisory) -> dict[str, dict[int, float]]:
    """Cumulative probability at each Jamaican sample point, as 0–1.

    Takes the 48-hour cumulative figure: far enough out to be actionable, close
    enough that the forecast means something. NHC publishes percentages; the
    contract wants fractions, and mixing the two is the kind of hundred-fold
    error that renders as a plausible-looking map.
    """
    published = advisory.raw.get("probabilities") or {}
    out: dict[str, dict[int, float]] = {}
    for location in PROBABILITY_POINTS:
        rows = published.get(location) or {}
        out[location] = {
            kt: (rows.get(str(kt), {}).get("cumulative", {}).get("48", 0) or 0) / 100.0
            for kt in (34, 50, 64)
        }
    return out


def _known_roofs_sql() -> str:
    return ", ".join(f"'{roof}'" for roof in IMPACT)


def _band_case_sql() -> str:
    """Build the impact lookup as SQL, from the table above.

    Generated rather than hand-written so IMPACT stays the only place the
    mapping lives. A second copy in a query is a second copy to forget when the
    curves are refitted against observed damage.
    """
    for roof, bands in IMPACT.items():
        assert roof.isalpha(), f"roof key {roof!r} is not a bare identifier"
        assert all(isinstance(kt, int) for kt in bands)

    whens = " ".join(
        f"WHEN w.roof = '{roof}' AND w.wind_kt = {kt} THEN '{band}'"
        for roof, bands in IMPACT.items()
        for kt, band in bands.items()
    )
    return f"CASE {whens} ELSE 'NONE' END"


_UPSERT = """
WITH adv AS (
  SELECT id, wind_field_34, wind_field_50, wind_field_64, issued_at
  FROM advisory WHERE id = :advisory_id
),
scored AS (
  SELECT
    sf.id AS storm_file_id,
    CASE
      WHEN adv.wind_field_64 IS NOT NULL AND ST_Intersects(adv.wind_field_64, sf.location) THEN 64
      WHEN adv.wind_field_50 IS NOT NULL AND ST_Intersects(adv.wind_field_50, sf.location) THEN 50
      WHEN adv.wind_field_34 IS NOT NULL AND ST_Intersects(adv.wind_field_34, sf.location) THEN 34
      ELSE 0
    END AS wind_kt,
    CASE WHEN sf.structure->>'roof' IN ({known_roofs})
         THEN sf.structure->>'roof' ELSE :unknown_roof END AS roof,
    -- Inverse-distance weighting between the only two points NHC publishes a
    -- probability for. +1 on the denominator keeps a household standing exactly
    -- on Kingston from dividing by zero.
    ST_Distance(sf.location, ST_SetSRID(ST_MakePoint(:kgn_lon, :kgn_lat), 4326)::geography) / 1000.0 AS km_kgn,
    ST_Distance(sf.location, ST_SetSRID(ST_MakePoint(:mbj_lon, :mbj_lat), 4326)::geography) / 1000.0 AS km_mbj
  FROM storm_file sf CROSS JOIN adv
),
weighted AS (
  SELECT
    scored.*,
    1.0 / (km_kgn + 1) AS w_kgn,
    1.0 / (km_mbj + 1) AS w_mbj,
    least(km_kgn, km_mbj) AS km_nearest
  FROM scored
)
INSERT INTO risk_assessment
  (storm_file_id, advisory_id, p34, p50, p64, predicted_band, confidence, method, model_version)
SELECT
  w.storm_file_id,
  :advisory_id,
  (:p34_kgn * w.w_kgn + :p34_mbj * w.w_mbj) / (w.w_kgn + w.w_mbj),
  (:p50_kgn * w.w_kgn + :p50_mbj * w.w_mbj) / (w.w_kgn + w.w_mbj),
  (:p64_kgn * w.w_kgn + :p64_mbj * w.w_mbj) / (w.w_kgn + w.w_mbj),
  ({band_case})::damage_band,
  greatest(
    :floor,
    least(1.0, :base - :per_hour * :lead_hours - :per_km * w.km_nearest)
  ),
  :method,
  :model_version
FROM weighted w
ON CONFLICT (storm_file_id, advisory_id) DO UPDATE SET
  p34 = EXCLUDED.p34,
  p50 = EXCLUDED.p50,
  p64 = EXCLUDED.p64,
  predicted_band = EXCLUDED.predicted_band,
  confidence = EXCLUDED.confidence,
  method = EXCLUDED.method,
  model_version = EXCLUDED.model_version
"""


def assess(session: Session, advisory: Advisory, *, lead_hours: float = 0.0) -> RiskRun:
    """Write one assessment per household for this advisory.

    Upserts rather than inserts. A rehearsal that replays the same advisory
    twice must not fail on the unique constraint, and must not leave a stale
    prediction behind either.
    """
    probabilities = _probabilities(advisory)
    kingston, montego = probabilities["KINGSTON"], probabilities["MONTEGO BAY"]

    session.execute(
        text(_UPSERT.format(known_roofs=_known_roofs_sql(), band_case=_band_case_sql())),
        {
            "advisory_id": advisory.id,
            "unknown_roof": UNKNOWN_ROOF,
            "kgn_lat": PROBABILITY_POINTS["KINGSTON"][0],
            "kgn_lon": PROBABILITY_POINTS["KINGSTON"][1],
            "mbj_lat": PROBABILITY_POINTS["MONTEGO BAY"][0],
            "mbj_lon": PROBABILITY_POINTS["MONTEGO BAY"][1],
            "p34_kgn": kingston[34], "p50_kgn": kingston[50], "p64_kgn": kingston[64],
            "p34_mbj": montego[34], "p50_mbj": montego[50], "p64_mbj": montego[64],
            "base": CONFIDENCE_BASE,
            "per_hour": CONFIDENCE_PER_LEAD_HOUR,
            "per_km": CONFIDENCE_PER_KM,
            "floor": CONFIDENCE_FLOOR,
            "lead_hours": lead_hours,
            "method": METHOD,
            "model_version": MODEL_VERSION,
        },
    )
    return _summarise(session, advisory)


def _summarise(session: Session, advisory: Advisory) -> RiskRun:
    rows = session.execute(
        text(
            """
            SELECT predicted_band::text AS band, count(*) AS n
            FROM risk_assessment WHERE advisory_id = :id
            GROUP BY predicted_band
            """
        ),
        {"id": advisory.id},
    ).all()
    by_band = {r.band: r.n for r in rows}
    return RiskRun(
        advisory_number=advisory.advisory_number,
        assessed=sum(by_band.values()),
        at_risk=sum(n for band, n in by_band.items() if band != "NONE"),
        by_band=by_band,
    )


@register(AgentName.RISK_MAPPER)
def handle(session: Session, payload: dict) -> None:
    """Queue entry point. The driver enqueues one of these per advisory."""
    advisory = session.get(Advisory, UUID(payload["advisory_id"]))
    if advisory is None:
        raise LookupError(f"advisory {payload['advisory_id']} is gone")
    assess(session, advisory)
