"""Aggregate Jamaica's building footprints into the exposure inventory.

The registry is 2,000 synthetic households scattered inside community polygons.
This is the mapped-reference denominator underneath them: every footprint in
the pinned public-source inventory, counted per place and again per wind band
for every advisory — so the console can say "N mapped structures in the 64 kt
band" instead of "413 of our 500 synthetic homes".

    cd apps/api && uv run python -m app.registry.buildings          # build both tables
    cd apps/api && uv run python -m app.registry.buildings --stats  # report only

Run ``data/buildings/fetch_footprints.py`` first; this never touches the network.

**Why the buildings never reach Postgres.** They were loaded there first, and
the benchmarks sent them back out: 610 MB of table and indexes against a
512 MB Neon limit, and 93.9 seconds for a single wind band on a single advisory,
because a geography predicate does spheroid math 1.8 million times. Nothing
needs a building row at query time — counting, exposure and the population
weight are all aggregates, and the map draws source geometry from the dedicated
structure archive. So DuckDB does the planar spatial work against the cached
parquet, and Postgres stores the answers. 610 MB becomes kilobytes and hours
become seconds.

**Exposure, not vulnerability.** A footprint says a structure is there and how
big it is. It says nothing about how it fails. Roof material stays modelled.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import sys
import tempfile
import time
import zipfile
from itertools import takewhile
from pathlib import Path
from uuid import UUID

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
DEFAULT_EVENT = "al132025"

#: A fingerprint includes this value, the footprint source and the boundary
#: source. Bump it whenever placement or aggregation semantics change so an
#: event built by an older recipe fails closed instead of looking current.
INVENTORY_RECIPE_VERSION = "place-structures-v2"


class BuildingInventoryError(RuntimeError):
    pass


def _sha256_file(path: Path, *, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inventory_fingerprint(
    *, source_sha256: str, boundaries_sha256: str, recipe_version: str
) -> str:
    """Canonical identity stored beside the aggregate inventory."""
    provenance = {
        "recipe_version": recipe_version,
        "source_sha256": source_sha256,
        "boundaries_sha256": boundaries_sha256,
    }
    encoded = json.dumps(provenance, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def inventory_provenance() -> dict[str, str]:
    """Identity of the exact mapped inventory consumed by this build."""
    missing = [path for path in (PARQUET, BOUNDARIES) if not path.exists()]
    if missing:
        raise BuildingInventoryError(
            "inventory provenance cannot be calculated; missing "
            + ", ".join(str(path) for path in missing)
        )
    provenance = {
        "recipe_version": INVENTORY_RECIPE_VERSION,
        "source_sha256": _sha256_file(PARQUET),
        "boundaries_sha256": _sha256_file(BOUNDARIES),
    }
    return {
        **provenance,
        "inventory_fingerprint": inventory_fingerprint(**provenance),
    }


def advisory_fingerprint(advisories: list[tuple]) -> str:
    """Hash the complete ordered advisory set and its wind geometry."""
    document = [
        [str(advisory_id), str(number), wind34, wind50, wind64]
        for advisory_id, number, wind34, wind50, wind64 in advisories
    ]
    encoded = json.dumps(document, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


DERIVED_ROWS_DIGEST_VERSION = 1


def _canonical_text(value, field: str) -> str:
    if not isinstance(value, str):
        raise BuildingInventoryError(f"{field} must be text for row digest")
    return value


def _canonical_int(value, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BuildingInventoryError(f"{field} must be an integer for row digest")
    return value


def _canonical_uuid(value, field: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise BuildingInventoryError(f"{field} must be a canonical UUID for row digest") from exc


def _derived_rows_sha256(*, domain: str, rows: list[list], event_id=None) -> str:
    """Hash a versioned/domain-separated canonical UTF-8 JSON document."""
    document = {
        "domain": domain,
        "rows": sorted(rows),
        "version": DERIVED_ROWS_DIGEST_VERSION,
    }
    if event_id is not None:
        document["event_id"] = _canonical_uuid(event_id, "event_id")
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def structure_rows_sha256(rows) -> str:
    """Exact, order-independent identity of persisted ``place_structures`` rows.

    Counts alone cannot detect moving structures or built area between places.
    The floating-point value is finite and encoded as its exact big-endian
    IEEE-754 bits; canonical JSON frames exact UTF-8 place names unambiguously.
    """
    canonical = []
    for parish, district, community, structures, built_m2 in rows:
        area = float(built_m2)
        if not math.isfinite(area):
            raise BuildingInventoryError("built_m2 must be finite for row digest")
        canonical.append(
            [
                _canonical_text(parish, "parish"),
                _canonical_text(district, "district"),
                _canonical_text(community, "community"),
                _canonical_int(structures, "structures"),
                struct.pack(">d", area).hex(),
            ]
        )
    return _derived_rows_sha256(domain="place_structures", rows=canonical)


def exposure_rows_sha256(event_id, rows) -> str:
    """Exact identity of every material exposure row for one bound event."""
    canonical = [
        [
            _canonical_uuid(advisory_id, "advisory_id"),
            _canonical_text(parish, "parish"),
            _canonical_text(district, "district"),
            _canonical_text(community, "community"),
            _canonical_int(band, "band"),
            _canonical_int(structures, "structures"),
        ]
        for advisory_id, parish, district, community, band, structures in rows
    ]
    return _derived_rows_sha256(
        domain="place_exposure",
        event_id=event_id,
        rows=canonical,
    )


def stored_structure_rows(conn) -> list[tuple]:
    """All material columns used to certify the current global inventory."""
    return conn.execute(
        text(
            "SELECT parish, district, community, structures, built_m2 "
            "FROM place_structures"
        )
    ).fetchall()


def stored_event_exposure_rows(conn, event_id) -> list[tuple]:
    """All material exposure columns for exactly one event, observed included.

    The builder only emits forecast rows. Including any observed row here makes
    later accidental contamination change the digest and fail export closed.
    """
    return conn.execute(
        text(
            "SELECT e.advisory_id, e.parish, e.district, e.community, "
            "       e.band, e.structures "
            "FROM place_exposure e "
            "JOIN advisory a ON a.id = e.advisory_id "
            "WHERE a.hazard_event_id = :event"
        ),
        {"event": event_id},
    ).fetchall()


def _event_id(conn, external_ref: str):
    rows = conn.execute(
        text("SELECT id FROM hazard_event WHERE external_ref = :external_ref"),
        {"external_ref": external_ref},
    ).fetchall()
    if not rows:
        raise BuildingInventoryError(f"event {external_ref!r} does not exist")
    if len(rows) != 1:
        raise BuildingInventoryError(
            f"event reference {external_ref!r} is ambiguous ({len(rows)} rows)"
        )
    return rows[0][0]


def _require_build_schema(conn) -> None:
    """Fail before hashing or placing 1.84M footprints on an old schema."""
    rows = conn.execute(
        text(
            "SELECT EXISTS ("
            "         SELECT 1 FROM information_schema.tables "
            "         WHERE table_schema = current_schema() "
            "           AND table_name = 'place_structure_build'"
            "       ), "
            "       EXISTS ("
            "         SELECT 1 FROM information_schema.tables "
            "         WHERE table_schema = current_schema() "
            "           AND table_name = 'place_exposure_build'"
            "       ), "
            "       EXISTS ("
            "         SELECT 1 FROM information_schema.columns "
            "         WHERE table_schema = current_schema() "
            "           AND table_name = 'place_structure_build' "
            "           AND column_name = 'structure_rows_sha256'"
            "       ), "
            "       EXISTS ("
            "         SELECT 1 FROM information_schema.columns "
            "         WHERE table_schema = current_schema() "
            "           AND table_name = 'place_exposure_build' "
            "           AND column_name = 'structure_rows_sha256'"
            "       ), "
            "       EXISTS ("
            "         SELECT 1 FROM information_schema.columns "
            "         WHERE table_schema = current_schema() "
            "           AND table_name = 'place_exposure_build' "
            "           AND column_name = 'exposure_rows_sha256'"
            "       )"
        )
    ).fetchall()
    if len(rows) != 1 or not all(rows[0]):
        raise BuildingInventoryError(
            "building completion schema is missing or predates row digests; run "
            "`cd apps/api && uv run alembic upgrade head` before rebuilding inventory"
        )


def advisory_key(number: str) -> tuple[int, str]:
    """Order NHC numbers numerically while preserving intermediate advisories.

    NHC's ``15A`` follows ``15`` and precedes ``16``. A database cast to int
    rejects the suffix; a text sort puts ``10`` before ``9``.
    """
    digits = "".join(takewhile(str.isdigit, number))
    if not digits:
        raise BuildingInventoryError(
            f"advisory number {number!r} does not start with a number"
        )
    return int(digits), number


def _forecast_advisories(conn, event_id) -> list[tuple]:
    """Read one event's forecast geometry, explicitly excluding best track."""
    rows = conn.execute(
        text(
            "SELECT a.id, a.advisory_number, "
            "  ST_AsText(a.wind_field_34::geometry), "
            "  ST_AsText(a.wind_field_50::geometry), "
            "  ST_AsText(a.wind_field_64::geometry) "
            "FROM advisory a "
            "WHERE a.observed = false AND a.hazard_event_id = :event"
        ),
        {"event": event_id},
    ).fetchall()
    return sorted(rows, key=lambda row: advisory_key(row[1]))


def _delete_event_exposure(conn, event_id) -> None:
    """Replace only this event's derived exposure rows."""
    conn.execute(
        text(
            "DELETE FROM place_exposure WHERE advisory_id IN ("
            "  SELECT id FROM advisory WHERE hazard_event_id = :event"
            ")"
        ),
        {"event": event_id},
    )
    conn.execute(
        text(
            "DELETE FROM place_exposure_build WHERE hazard_event_id = :event"
        ),
        {"event": event_id},
    )


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
        WITH source AS (
          SELECT file_row_number AS source_order,
                 ST_Centroid(p.geometry) AS pt,
                 p.area_in_meters AS area_m2
          FROM read_parquet('{PARQUET.as_posix()}', file_row_number = true) p
        ), placed AS (
          SELECT s.source_order, s.pt, s.area_m2,
                 a.adm1_name AS parish,
                 a.adm2_name AS district,
                 a.adm3_name AS community,
                 row_number() OVER (
                   PARTITION BY s.source_order
                   ORDER BY a.adm3_pcode NULLS LAST, a.OGC_FID NULLS LAST
                 ) AS boundary_rank
          FROM source s
          LEFT JOIN ST_Read('{shp.as_posix()}') a ON ST_Within(s.pt, a.geom)
        )
        SELECT source_order, pt, area_m2, parish, district, community
        FROM placed
        WHERE boundary_rank = 1
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


def build(*, external_ref: str = DEFAULT_EVENT) -> None:
    engine = get_engine()

    # Wind fields out of Postgres as WKT, cast to geometry on the way. DuckDB
    # does planar math; at these scales the difference from spheroid is far
    # below the resolution of a forecast wind radius.
    with engine.connect() as conn:
        _require_build_schema(conn)
        event_id = _event_id(conn, external_ref)
        advisories = _forecast_advisories(conn, event_id)
    if not advisories:
        raise BuildingInventoryError(
            f"event {external_ref!r} has no forecast advisories"
        )
    print(f"{len(advisories)} forecast advisories for {external_ref}")
    provenance = inventory_provenance()

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

    structure_count = sum(row[3] for row in structures)
    exposure_count = sum(row[5] for row in exposure)
    advisories_fingerprint = advisory_fingerprint(advisories)

    with engine.begin() as conn:
        # The spatial work happens outside Postgres and can take seconds. Hold a
        # stable advisory set for the write and prove that nothing was ingested
        # or corrected since the WKT used above was read. Otherwise the marker
        # would commit successfully while already describing stale exposure.
        conn.execute(text("LOCK TABLE advisory IN SHARE MODE"))
        current_advisories = _forecast_advisories(conn, event_id)
        if advisory_fingerprint(current_advisories) != advisories_fingerprint:
            raise BuildingInventoryError(
                "forecast advisories changed during the exposure build; rebuild from the current set"
            )
        # Exposure belongs to one explicit event. Rebuilding Melissa must not
        # erase or mix another storm's advisory rows.
        _delete_event_exposure(conn, event_id)
        conn.execute(text("TRUNCATE place_structures"))
        conn.execute(text("DELETE FROM place_structure_build"))
        conn.execute(
            text(
                "INSERT INTO place_structures (parish, district, community, structures, built_m2) "
                "VALUES (:p, :d, :c, :n, :a)"
            ),
            [{"p": p, "d": d, "c": c, "n": n, "a": a} for p, d, c, n, a in structures],
        )
        persisted_structure_rows_sha256 = structure_rows_sha256(
            stored_structure_rows(conn)
        )
        conn.execute(
            text(
                "INSERT INTO place_structure_build "
                "(inventory_fingerprint, source_sha256, boundaries_sha256, "
                " recipe_version, structure_count, place_count, structure_rows_sha256) "
                "VALUES (:fingerprint, :source, :boundaries, :recipe, :structures, "
                " :places, :rows_sha256)"
            ),
            {
                "fingerprint": provenance["inventory_fingerprint"],
                "source": provenance["source_sha256"],
                "boundaries": provenance["boundaries_sha256"],
                "recipe": provenance["recipe_version"],
                "structures": structure_count,
                "places": len(structures),
                "rows_sha256": persisted_structure_rows_sha256,
            },
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
        # Written last in the same transaction: its presence certifies that all
        # selected advisories were processed, including advisories whose valid
        # result produced no sparse exposure rows.
        conn.execute(
            text(
                "INSERT INTO place_exposure_build "
                "(hazard_event_id, inventory_fingerprint, structure_rows_sha256, "
                " advisory_fingerprint, "
                " advisory_count, exposure_row_count, exposed_structure_count, "
                " exposure_rows_sha256) "
                "VALUES (:event, :inventory, :structure_rows, :advisories, :advisory_count, "
                " :row_count, :structure_count, :rows_sha256)"
            ),
            {
                "event": event_id,
                "inventory": provenance["inventory_fingerprint"],
                "structure_rows": persisted_structure_rows_sha256,
                "advisories": advisories_fingerprint,
                "advisory_count": len(advisories),
                "row_count": len(exposure),
                "structure_count": exposure_count,
                "rows_sha256": exposure_rows_sha256(
                    event_id,
                    stored_event_exposure_rows(conn, event_id),
                ),
            },
        )

    print(f"\nwrote {len(structures):,} place rows and {len(exposure):,} exposure rows "
          f"in {time.monotonic() - started:.1f}s")


def stats(*, external_ref: str = DEFAULT_EVENT) -> None:
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
                "FROM place_exposure e "
                "JOIN advisory a ON a.id = e.advisory_id "
                "JOIN hazard_event h ON h.id = a.hazard_event_id "
                "WHERE e.band = 64 AND h.external_ref = :external_ref "
                "ORDER BY e.structures DESC LIMIT 8"
            ),
            {"external_ref": external_ref},
        ):
            print(f"  {community:<26} {parish:<18} {n:>7,}  (advisory {adv})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stats", action="store_true", help="report on what is loaded, build nothing")
    ap.add_argument(
        "--event",
        default=DEFAULT_EVENT,
        help=f"hazard event external_ref (default: {DEFAULT_EVENT})",
    )
    args = ap.parse_args()

    if args.stats:
        stats(external_ref=args.event)
        return
    build(external_ref=args.event)
    print()
    stats(external_ref=args.event)


if __name__ == "__main__":
    main()
