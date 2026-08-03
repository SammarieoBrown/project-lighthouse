"""Jamaica's administrative geography, from the cached COD datasets.

Boundaries and parish populations both come from OCHA's Common Operational
Datasets — the geography a humanitarian response is expected to use, so a parish
list we hand to a partner agency means the same thing to them as to us.

Read from ``data/replay/cache/jamaica``, never the network.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import REPLAY_CACHE
from app.nhc.shapes import read_layer

JAMAICA = REPLAY_CACHE / "jamaica"
BOUNDARIES = JAMAICA / "jam_admin_boundaries.shp.zip"
POPULATION = JAMAICA / "jam_adm1_pop.csv"

#: The replay area. Melissa's landfall was in Westmoreland, and St Elizabeth
#: took the eastern side of the eyewall.
REPLAY_PARISHES = ("Saint Elizabeth", "Westmoreland")


class PcodeConflict(RuntimeError):
    """Raised if the two COD datasets ever start agreeing — or disagreeing worse."""


@dataclass(frozen=True)
class Parish:
    name: str
    pcode: str
    population: int
    area_sqkm: float
    geometry: dict[str, Any]


@dataclass(frozen=True)
class Community:
    """An admin-3 unit: the smallest published community boundary."""

    name: str
    parish: str
    pcode: str
    geometry: dict[str, Any]


def _strip(value: Any) -> str:
    return str(value).strip()


@lru_cache(maxsize=1)
def _parish_population() -> dict[str, int]:
    """Parish totals, keyed by name.

    **Keyed by name, and deliberately not by p-code.** The two OCHA COD datasets
    for Jamaica disagree about p-codes: 11 of 14 parishes carry a different code
    in cod-ab (boundaries) than in cod-ps (population). Saint Elizabeth is JM09
    in the boundaries and JM11 in the population table, while JM09 in the
    population table is Saint Ann.

    Joining on p-code therefore runs clean and silently gives St Elizabeth the
    population of a parish on the other side of the island — 172,362 instead of
    150,205, and Westmoreland 93,902 instead of 144,103. Every downstream number
    would be wrong and nothing would look broken.

    All 14 names match exactly once whitespace is stripped, so name is the join
    key, and ``load_parishes`` asserts the match rather than trusting it.
    """
    with POPULATION.open(newline="", encoding="utf-8-sig") as fh:
        return {_strip(row["ADM1_EN"]): int(row["T_TL"]) for row in csv.DictReader(fh)}


@lru_cache(maxsize=1)
def _parish_shapes() -> dict[str, dict[str, Any]]:
    return {
        _strip(f.attributes["adm1_name"]): {
            "pcode": _strip(f.attributes["adm1_pcode"]),
            "area_sqkm": float(f.attributes["area_sqkm"]),
            "geometry": f.geometry,
        }
        for f in read_layer(BOUNDARIES, "jam_admin1")
    }


def load_parishes(names: tuple[str, ...] = REPLAY_PARISHES) -> dict[str, Parish]:
    shapes, population = _parish_shapes(), _parish_population()

    unmatched = sorted(set(shapes) ^ set(population))
    if unmatched:
        raise PcodeConflict(
            "boundary and population parish names no longer line up: "
            f"{unmatched}. They are joined by name because the p-codes disagree, "
            "so a name change breaks the only key we have."
        )

    missing = [n for n in names if n not in shapes]
    if missing:
        raise KeyError(f"no such parish: {missing}. Known: {sorted(shapes)}")

    return {
        name: Parish(
            name=name,
            pcode=shapes[name]["pcode"],
            population=population[name],
            area_sqkm=shapes[name]["area_sqkm"],
            geometry=shapes[name]["geometry"],
        )
        for name in names
    }


def load_communities(parishes: tuple[str, ...] = REPLAY_PARISHES) -> list[Community]:
    """Admin-3 units inside the given parishes.

    Population is not published at this level, which is why the seeder spreads
    households evenly across communities rather than pretending to know the
    density inside a parish.
    """
    wanted = set(parishes)
    out = [
        Community(
            name=_strip(f.attributes["adm3_name"]),
            parish=_strip(f.attributes["adm1_name"]),
            pcode=_strip(f.attributes["adm3_pcode"]),
            geometry=f.geometry,
        )
        for f in read_layer(BOUNDARIES, "jam_admin3")
        if _strip(f.attributes["adm1_name"]) in wanted and f.geometry
    ]
    return sorted(out, key=lambda c: c.pcode)


def cache_is_present() -> bool:
    return BOUNDARIES.exists() and POPULATION.exists()


def missing_files() -> list[Path]:
    return [p for p in (BOUNDARIES, POPULATION) if not p.exists()]
