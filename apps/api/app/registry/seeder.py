"""Seed the synthetic household registry.

Roughly 500 Storm Files across St Elizabeth and Westmoreland, split between the
two parishes by their real published populations and placed inside real
community boundaries. Fixed seed, so the same run produces the same households
in the same places on every machine — a replay that reshuffles its own registry
is not a replay.

**Synthetic only, and that is a standing rule rather than a phase.** Every row
carries ``synthetic = true``. No real household data enters this system until a
data sharing agreement exists.

What is real here: the parish boundaries, the community boundaries and names,
and the parish population split. What is invented: every household, its phone
number, its roof, and the people in it. The distributions below are stated
assumptions, not census marginals — they are written down so they can be argued
with, and replaced the moment a real registry exists.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models import StormFile
from app.registry.geography import REPLAY_PARISHES, Community, load_communities, load_parishes

DEFAULT_COUNT = 500

# --------------------------------------------------------------------------
# Stated assumptions.
#
# Western rural Jamaica, approximated. Zinc sheeting over block-and-steel is the
# dominant rural construction, and it is also the combination that fails first
# in a category 5 — which is exactly why roof material has to be a field rather
# than a footnote. These weights are illustrative and documented as such; STATIN
# marginals replace them when there is a real registry to compare against.
# --------------------------------------------------------------------------
ROOF = {"zinc": 0.58, "concrete": 0.28, "shingle": 0.10, "tile": 0.04}
WALLS = {"block_steel": 0.66, "wood": 0.24, "brick_stone": 0.06, "other": 0.04}
BUILT = {"pre_1980": 0.28, "1980_1999": 0.34, "2000_2014": 0.26, "post_2015": 0.12}

#: Household sizes. Jamaica's mean household is a little over three people.
HOUSEHOLD_SIZE = {1: 0.18, 2: 0.21, 3: 0.20, 4: 0.17, 5: 0.12, 6: 0.07, 7: 0.03, 8: 0.02}

#: Conditions that change how urgently someone must be reached, not a diagnosis
#: list. Dialysis is here because missing a session is measured in days.
MEDICAL = ("dialysis", "insulin_dependent", "oxygen", "mobility_impaired", "pregnant")

# --------------------------------------------------------------------------
# Vulnerability. A transparent lookup, on purpose (schema: risk_assessment is
# "transparent parametric lookup in v1"). Every point is attributable to a field
# a human can see on the Storm File, so a Director asking "why is this household
# scored 70" gets an answer rather than a model.
# --------------------------------------------------------------------------
ROOF_POINTS = {"zinc": 30, "shingle": 20, "tile": 10, "concrete": 0}
WALL_POINTS = {"wood": 20, "other": 12, "brick_stone": 5, "block_steel": 0}
BUILT_POINTS = {"pre_1980": 15, "1980_1999": 10, "2000_2014": 5, "post_2015": 0}


@dataclass(frozen=True)
class SeedReport:
    created: int
    by_parish: dict[str, int]
    communities: int
    seed: int


def _weighted(rng: random.Random, weights: dict) -> object:
    return rng.choices(list(weights), weights=list(weights.values()), k=1)[0]


def vulnerability(structure: dict, people: dict) -> int:
    """0–100, and every point traceable to a field on the row."""
    score = ROOF_POINTS.get(structure["roof"], 0)
    score += WALL_POINTS.get(structure["walls"], 0)
    score += BUILT_POINTS.get(structure["built"], 0)
    if people["elderly"]:
        score += 10
    if people["children"]:
        score += 5
    if people["medical"]:
        score += 15
    return min(score, 100)


def _household(rng: random.Random) -> tuple[dict, dict]:
    structure = {
        "roof": _weighted(rng, ROOF),
        "walls": _weighted(rng, WALLS),
        "built": _weighted(rng, BUILT),
        "floors": 1 if rng.random() < 0.86 else 2,
    }

    total = _weighted(rng, HOUSEHOLD_SIZE)
    children = rng.randint(0, max(0, total - 1)) if total > 1 and rng.random() < 0.52 else 0
    elderly = rng.randint(1, min(2, total - children)) if rng.random() < 0.24 and total - children else 0
    medical = [m for m in MEDICAL if rng.random() < 0.05]

    people = {
        "total": total,
        "children": children,
        "elderly": elderly,
        "medical": medical,
    }
    return structure, people


def _allocate(count: int, parishes: dict) -> dict[str, int]:
    """Split households between parishes by real population.

    Largest-remainder rather than rounding each share independently, so the
    parts add up to the whole. Rounding separately loses or gains a household
    and makes the registry size depend on the parish mix.
    """
    total_pop = sum(p.population for p in parishes.values())
    exact = {name: count * p.population / total_pop for name, p in parishes.items()}
    floors = {name: int(v) for name, v in exact.items()}
    for name in sorted(exact, key=lambda n: exact[n] - floors[n], reverse=True):
        if sum(floors.values()) >= count:
            break
        floors[name] += 1
    return floors


def _points_in(session: Session, community: Community, n: int, seed: int) -> list[tuple[float, float]]:
    """Random points inside a community boundary, via PostGIS.

    ``ST_GeneratePoints`` takes a seed, so this is reproducible, and it does the
    rejection sampling against the real polygon — households land inside the
    community they are labelled with rather than inside its bounding box, which
    for a coastal parish would put a good number of them in the sea.
    """
    if n <= 0:
        return []
    rows = session.execute(
        text(
            """
            SELECT ST_X(g.geom) AS lon, ST_Y(g.geom) AS lat
            FROM (
              SELECT (ST_Dump(ST_GeneratePoints(ST_GeomFromGeoJSON(:g), :n, :seed))).geom
            ) AS g
            """
        ),
        {"g": json.dumps(community.geometry), "n": n, "seed": seed},
    ).all()
    return [(r.lat, r.lon) for r in rows]


def seed_registry(
    session: Session,
    *,
    count: int = DEFAULT_COUNT,
    seed: int | None = None,
    parishes: tuple[str, ...] = REPLAY_PARISHES,
) -> SeedReport:
    """Create synthetic Storm Files. Idempotent — does nothing if any exist.

    Deliberately not "replace what is there": the registry is referenced by
    claims, risk assessments and ledger entries the moment the replay runs, and
    silently rebuilding it underneath them would orphan every one of those.
    Clear the table explicitly if you want a different registry.
    """
    from app.config import get_settings

    seed = get_settings().replay_seed if seed is None else seed
    rng = random.Random(seed)

    existing = session.scalar(select(func.count()).select_from(StormFile))
    if existing:
        return SeedReport(created=0, by_parish={}, communities=0, seed=seed)

    parish_data = load_parishes(parishes)
    communities = load_communities(parishes)
    per_parish = _allocate(count, parish_data)

    by_parish: dict[str, int] = {}
    created = 0

    for parish_name, wanted in per_parish.items():
        in_parish = [c for c in communities if c.parish == parish_name]
        if not in_parish:
            continue

        # Spread evenly across communities, remainder to the first few. Even
        # rather than area-weighted because population is not published below
        # parish level, and area is a poor proxy — the largest communities here
        # are the emptiest.
        base, extra = divmod(wanted, len(in_parish))
        placed = 0

        for index, community in enumerate(in_parish):
            n = base + (1 if index < extra else 0)
            # Per-community seed, derived so the whole registry still follows
            # from the one replay seed.
            points = _points_in(session, community, n, seed + index)

            for lat, lon in points:
                structure, people = _household(rng)
                phone = f"+1876{rng.randint(2_000_000, 9_999_999)}"
                session.add(
                    StormFile(
                        phone=phone,
                        phone_hash=hashlib.sha256(phone.encode()).hexdigest(),
                        location=f"SRID=4326;POINT({lon} {lat})",
                        parish=parish_name,
                        community=community.name,
                        structure=structure,
                        people=people,
                        vuln_score=vulnerability(structure, people),
                        synthetic=True,
                    )
                )
                placed += 1
                created += 1

        by_parish[parish_name] = placed

    session.flush()
    return SeedReport(
        created=created,
        by_parish=by_parish,
        communities=len(communities),
        seed=seed,
    )
