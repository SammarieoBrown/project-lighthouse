"""Aggregate Jamaica's building footprints into the exposure inventory.

The registry is 2,000 synthetic households scattered inside community polygons.
This is the real denominator underneath them: every structure on the island,
counted per place, and counted again per wind band for every advisory — so the
console can say "N structures in the 64 kt band" instead of "413 of our 500
synthetic homes".

    cd apps/api && uv run python -m app.registry.buildings          # build both tables
    cd apps/api && uv run python -m app.registry.buildings --stats  # report only

Run ``data/buildings/fetch_footprints.py`` first; this never touches the network.

**Why the buildings never reach Postgres.** They were loaded there first, and
the measurements sent them back out: 610 MB of table and indexes against a
512 MB Neon limit, and 93.9 seconds for a single wind band on a single advisory,
because a geography predicate does spheroid math 1.8 million times. Nothing
needs a building row at query time — counting, exposure and the population
weight are all aggregates, and the map draws footprints from the basemap tiles.
So DuckDB does the planar spatial work against the cached parquet, and Postgres
stores the answers. 610 MB becomes kilobytes and hours become seconds.

**Exposure, not vulnerability.** A footprint says a structure is there and how
big it is. It says nothing about how it fails. Roof material stays modelled.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from sqlalchemy import text

from app.db import get_engine
from app.registry.geography import BOUNDARIES

REPO = Path(__file__).parents[4]
PARQUET = REPO / "data" / "buildings" / "cache" / "jamaica-buildings.parquet"

#: The admin-3 layer carries parish, district and community on every feature, so
#: one point-in-polygon pass resolves all three levels at once.
ADMIN3 = "jam_admin3"

#: Highest first. The bands are nested, so a structure must be counted once at
#: the strongest wind that reaches it — counting each band independently would
#: report the same building three times.
BANDS = (64, 50, 34)


def _duckdb():
    try:
        import duckdb
    except ModuleNotFoundError:
        sys.exit(
            "duckdb is not installed. It is a dev-time loader dependency:\n"
            "  cd apps/api && uv add --dev duckdb"
        )
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    return con


def _placed_buildings(con, tmp: str) -> None:
    """Materialise (point, area, parish, district, community) once.

    One spatial join for 1.84M footprints, reused by every advisory afterwards.
    Doing it per advisory would repeat the expensive half 41 times.
    """
    if not PARQUET.exists():
        sys.exit(f"{PARQUET} is missing — run data/buildings/fetch_footprints.py first")

    with zipfile.ZipFile(BOUNDARIES) as z:
        z.extractall(tmp)
    shp = next(Path(tmp).rglob(f"{ADMIN3}.shp"), None)
    if shp is None:
        sys.exit(f"{ADMIN3}.shp not found inside {BOUNDARIES}")

    # LEFT JOIN, deliberately. A centroid outside every published boundary is a
    # real outcome — coastal cays, boundary gaps — and an inner join would
    # quietly shrink the island's building count to whatever fell inside a
    # polygon. They are counted and reported instead.
    con.execute(
        f"""
        CREATE TABLE b AS
        SELECT ST_Centroid(p.geometry) AS pt,
               p.area_in_meters        AS area_m2,
               a.adm1_name             AS parish,
               a.adm2_name             AS district,
               a.adm3_name             AS community
        FROM read_parquet('{PARQUET.as_posix()}') p
        LEFT JOIN ST_Read('{shp.as_posix()}') a ON ST_Within(ST_Centroid(p.geometry), a.geom)
        """
    )


def _structures(con) -> list[tuple]:
    return con.execute(
        """
        SELECT parish, district, community, count(*), sum(area_m2)
        FROM b WHERE parish IS NOT NULL
        GROUP BY 1, 2, 3
        """
    ).fetchall()


def _exposure(con, wkt: dict[int, str | None]) -> list[tuple]:
    """Structures per community at the highest wind band reaching them."""
    present = [kt for kt in BANDS if wkt.get(kt)]
    if not present:
        return []
    ladder = "\n".join(
        f"WHEN ST_Within(pt, ST_GeomFromText('{wkt[kt]}')) THEN {kt}" for kt in present
    )
    return con.execute(
        f"""
        SELECT parish, district, community, band, count(*) FROM (
          SELECT parish, district, community, CASE {ladder} END AS band
          FROM b WHERE parish IS NOT NULL
        ) t
        WHERE band IS NOT NULL
        GROUP BY 1, 2, 3, 4
        """
    ).fetchall()


def build() -> None:
    engine = get_engine()

    # Wind fields out of Postgres as WKT, cast to geometry on the way. DuckDB
    # does planar math; at these scales the difference from spheroid is far
    # below the resolution of a forecast wind radius.
    with engine.connect() as conn:
        advisories = conn.execute(
            text(
                "SELECT id, advisory_number, "
                "  ST_AsText(wind_field_34::geometry), "
                "  ST_AsText(wind_field_50::geometry), "
                "  ST_AsText(wind_field_64::geometry) "
                "FROM advisory WHERE observed = false "
                "ORDER BY (advisory_number)::int"
            )
        ).fetchall()
    print(f"{len(advisories)} forecast advisories")

    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        con = _duckdb()
        print("placing 1,844,379 footprints …", flush=True)
        _placed_buildings(con, tmp)

        total, unplaced = con.execute(
            "SELECT count(*), count(*) FILTER (WHERE parish IS NULL) FROM b"
        ).fetchone()
        print(f"  {total:,} footprints, {unplaced:,} outside every boundary "
              f"({100 * unplaced / total:.2f}%)  [{time.monotonic() - started:.1f}s]")

        structures = _structures(con)
        print(f"  {len(structures):,} communities with structures")

        exposure: list[tuple] = []
        for aid, number, w34, w50, w64 in advisories:
            rows = _exposure(con, {34: w34, 50: w50, 64: w64})
            exposure.extend((aid, *r) for r in rows)
            print(f"  advisory {number:>3}: {sum(r[4] for r in rows):>9,} structures exposed",
                  flush=True)

    with engine.begin() as conn:
        conn.execute(text("TRUNCATE place_exposure"))
        conn.execute(text("TRUNCATE place_structures"))
        conn.execute(
            text(
                "INSERT INTO place_structures (parish, district, community, structures, built_m2) "
                "VALUES (:p, :d, :c, :n, :a)"
            ),
            [{"p": p, "d": d, "c": c, "n": n, "a": a} for p, d, c, n, a in structures],
        )
        if exposure:
            conn.execute(
                text(
                    "INSERT INTO place_exposure "
                    "(advisory_id, parish, district, community, band, structures) "
                    "VALUES (:aid, :p, :d, :c, :b, :n)"
                ),
                [
                    {"aid": aid, "p": p, "d": d, "c": c, "b": b, "n": n}
                    for aid, p, d, c, b, n in exposure
                ],
            )

    print(f"\nwrote {len(structures):,} place rows and {len(exposure):,} exposure rows "
          f"in {time.monotonic() - started:.1f}s")


def stats() -> None:
    with get_engine().connect() as conn:
        total, built = conn.execute(
            text("SELECT coalesce(sum(structures), 0), coalesce(sum(built_m2), 0) FROM place_structures")
        ).one()
        if not total:
            print("place_structures is empty — run without --stats first")
            return
        print(f"structures: {total:,}   built area: {built / 1e6:,.1f} km²")

        print("\nby parish")
        for parish, n, area in conn.execute(
            text(
                "SELECT parish, sum(structures), sum(built_m2) FROM place_structures "
                "GROUP BY 1 ORDER BY 2 DESC"
            )
        ):
            print(f"  {parish:<22} {n:>9,}   {area / 1e6:7.1f} km²")

        print("\nworst-exposed communities at peak (64 kt)")
        for parish, community, n, adv in conn.execute(
            text(
                "SELECT e.parish, e.community, e.structures, a.advisory_number "
                "FROM place_exposure e JOIN advisory a ON a.id = e.advisory_id "
                "WHERE e.band = 64 ORDER BY e.structures DESC LIMIT 8"
            )
        ):
            print(f"  {community:<26} {parish:<18} {n:>7,}  (advisory {adv})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stats", action="store_true", help="report on what is loaded, build nothing")
    args = ap.parse_args()

    if args.stats:
        stats()
        return
    build()
    print()
    stats()


if __name__ == "__main__":
    main()
