"""Build the structures tileset: every building, coloured by the storm.

The basemap already carries building footprints, and for a while that looked
like enough. It is not, for two reasons that are properties of the archive
rather than of the styling:

* Protomaps carries buildings from about **z14**. Below that there are none, so
  the wide view — a whole parish of settlement at once — cannot be drawn from
  it at all.
* Its buildings carry no attribute we can join on. MapLibre cannot do
  point-in-polygon in a style expression, so a tile-sourced building can never
  be coloured by which wind band it stands in.

So we build our own. 1,844,379 footprints, each carrying the advisory at which
it first enters each wind band, which is what lets the console colour a
building by the storm and animate that colour as the replay scrubs — with one
`setPaintProperty` per frame rather than per building.

    cd apps/api && uv run python -m app.registry.building_tiles

Requires tippecanoe and the pmtiles CLI (`brew install tippecanoe pmtiles`) and
the cached parquet from data/buildings/fetch_footprints.py. Publish the result
with data/tiles/upload_basemap.py.

**Cumulative, and the console has to say so.** `f64 <= now` means "has been in
hurricane-force wind by this advisory", not "is in it right now". A storm passes
over: a building enters the 64 kt field and leaves it, and an operations room
asking "who has been hit" is better served by the cumulative reading than by a
colour that clears itself once the eye moves on. It is a different claim from
the wind band drawn over it, so the legend names it.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
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
OUT = REPO / "data" / "tiles" / "cache" / "structures-z15.pmtiles"
#: Kept rather than written to a temp dir. It is 625 MB and takes minutes to
#: produce, and the first tippecanoe invocation died on a bad flag and took the
#: whole thing with it. Regenerate by deleting it.
GEOJSON = REPO / "data" / "buildings" / "cache" / "structures.geojsonl"
#: Centroids of the same buildings, same properties.
#:
#: A polygon cannot be seen at wide zoom and no styling fixes that: at z10 a
#: 47 m² footprint is four hundredths of a pixel, and a fill layer has no
#: minimum size. The tiles contain it, the screen shows nothing, and the view
#: reads as "no buildings here" rather than "too small to draw".
#:
#: A circle layer does have a minimum size. So the wide view is points at a
#: fixed radius — which is what made the damage viewer legible, where every
#: building was a 2×2 block — and the close view is the real footprints.
POINTS = REPO / "data" / "buildings" / "cache" / "structure-points.geojsonl"

#: Where points hand over to footprints. Below this a building is sub-pixel;
#: above it the shape is worth the bytes. They overlap by a level so the
#: handover has no gap.
POINT_MAX_ZOOM, POLY_MIN_ZOOM = 14, 13

ADMIN3 = "jam_admin3"
BANDS = (64, 50, 34)

#: z9 is where a parish fills the frame — the wide shot the basemap cannot draw.
#: z15 matches the island archive, so the two hand over without a visible seam.
MIN_ZOOM, MAX_ZOOM = 9, 15


def _require(*tools: str) -> None:
    missing = [t for t in tools if shutil.which(t) is None]
    if missing:
        sys.exit(f"missing {', '.join(missing)} — brew install {' '.join(missing)}")


def _duckdb():
    try:
        import duckdb
    except ModuleNotFoundError:
        sys.exit("duckdb is not installed:  cd apps/api && uv add --dev duckdb")
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    return con


def _advisories() -> list[tuple[int, str | None, str | None, str | None]]:
    with get_engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT ST_AsText(wind_field_34::geometry), "
                "       ST_AsText(wind_field_50::geometry), "
                "       ST_AsText(wind_field_64::geometry) "
                "FROM advisory WHERE observed = false "
                "ORDER BY (advisory_number)::int"
            )
        ).all()
    return [(i, a, b, c) for i, (a, b, c) in enumerate(rows)]


def write_geojson(path: Path) -> int:
    """Newline-delimited GeoJSON, which is what tippecanoe reads fastest."""
    if not PARQUET.exists():
        sys.exit(f"{PARQUET} is missing — run data/buildings/fetch_footprints.py first")

    advisories = _advisories()
    print(f"{len(advisories)} advisories")

    con = _duckdb()
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(BOUNDARIES) as z:
            z.extractall(tmp)
        shp = next(Path(tmp).rglob(f"{ADMIN3}.shp"))

        # The footprint polygon, not its centroid — this is the tileset that
        # makes a town look like a town, and a dot per building does not.
        con.execute(
            f"""
            CREATE TABLE b AS
            SELECT p.geometry AS geom, ST_Centroid(p.geometry) AS pt,
                   a.adm2_name AS district, a.adm3_name AS community
            FROM read_parquet('{PARQUET.as_posix()}') p
            LEFT JOIN ST_Read('{shp.as_posix()}') a
                   ON ST_Within(ST_Centroid(p.geometry), a.geom)
            """
        )
        print("  placed", flush=True)

        # First entry per band, as a running minimum over the advisories. One
        # pass per advisory rather than a 41-way join, which DuckDB plans badly.
        for kt in BANDS:
            con.execute(f"ALTER TABLE b ADD COLUMN f{kt} INTEGER")
        for index, w34, w50, w64 in advisories:
            for kt, wkt in ((64, w64), (50, w50), (34, w34)):
                if not wkt:
                    continue
                con.execute(
                    f"""
                    UPDATE b SET f{kt} = {index}
                    WHERE f{kt} IS NULL
                      AND ST_Within(pt, ST_GeomFromText('{wkt}'))
                    """
                )
            print(f"  advisory {index + 1}/{len(advisories)}", end="\r", flush=True)
        print()

        rows = con.execute(
            "SELECT ST_AsGeoJSON(geom), district, community, f64, f50, f34 FROM b"
        )
        n = 0
        with path.open("w") as fh:
            while True:
                batch = rows.fetchmany(50_000)
                if not batch:
                    break
                for geom, district, community, f64, f50, f34 in batch:
                    props: dict[str, object] = {}
                    # Absent rather than a sentinel: tippecanoe drops missing
                    # keys, and 1.8M copies of "never" is pure tile weight.
                    if f64 is not None:
                        props["f64"] = f64
                    if f50 is not None:
                        props["f50"] = f50
                    if f34 is not None:
                        props["f34"] = f34
                    if district:
                        props["d"] = district
                    if community:
                        props["c"] = community
                    fh.write(
                        json.dumps(
                            {"type": "Feature", "properties": props, "geometry": json.loads(geom)},
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                    n += 1
    return n


def write_points(polygons: Path, out: Path) -> int:
    """Centroid per building, from the polygon file rather than the database.

    The expensive half — placing 1.8M footprints and walking 41 advisories over
    them — is already done and cached. Re-deriving points from that file costs
    one pass; re-running DuckDB costs ten minutes for the same answer.
    """
    n = 0
    with polygons.open() as src, out.open("w") as dst:
        for line in src:
            feature = json.loads(line)
            rings = feature["geometry"]["coordinates"]
            ring = rings[0][0] if feature["geometry"]["type"] == "MultiPolygon" else rings[0]
            # Mean of the ring, which for a building-sized quadrilateral is the
            # centroid to well within the metre this is drawn at.
            lon = sum(c[0] for c in ring) / len(ring)
            lat = sum(c[1] for c in ring) / len(ring)
            dst.write(
                json.dumps(
                    {
                        "type": "Feature",
                        "properties": feature["properties"],
                        "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
            n += 1
    return n


def build() -> None:
    _require("tippecanoe", "pmtiles")
    started = time.monotonic()
    OUT.parent.mkdir(parents=True, exist_ok=True)

    if GEOJSON.exists():
        print(f"reusing {GEOJSON.name} ({GEOJSON.stat().st_size / 1e6:.0f} MB) — delete it to rebuild")
    else:
        n = write_geojson(GEOJSON)
        print(f"  {n:,} features, {GEOJSON.stat().st_size / 1e6:.0f} MB of GeoJSON")

    if not POINTS.exists():
        print("deriving centroids …", flush=True)
        n = write_points(GEOJSON, POINTS)
        print(f"  {n:,} points, {POINTS.stat().st_size / 1e6:.0f} MB")

    with tempfile.TemporaryDirectory() as tmp:
        mbtiles = Path(tmp) / "structures.mbtiles"
        subprocess.run(
            [
                "tippecanoe", "-o", str(mbtiles),
                "-Z", str(MIN_ZOOM), "-z", str(MAX_ZOOM),
                # Two layers, two geometries, one archive. The console picks by
                # zoom: circles where a footprint would be invisible, footprints
                # where the shape is legible.
                "-L", json.dumps({
                    "file": str(POINTS), "layer": "structure_points",
                    "minzoom": MIN_ZOOM, "maxzoom": POINT_MAX_ZOOM,
                }),
                "-L", json.dumps({
                    "file": str(GEOJSON), "layer": "structures",
                    "minzoom": POLY_MIN_ZOOM, "maxzoom": MAX_ZOOM,
                }),
                # Keep every building. Tippecanoe's default thinning is built
                # for label legibility, and it takes the density texture with
                # it — a town stops looking like a town, which is the one thing
                # this layer exists to show.
                "--no-feature-limit", "--no-tile-size-limit",
                # Below z14 a footprint is sub-pixel, so its shape costs bytes
                # nobody can see. Coordinates are dropped to the tile grid there
                # and kept exact where the shape is actually legible.
                "--simplification=4",
                # No --quiet. The first run of this died on an unrecognised
                # flag, tippecanoe printed its usage, and --quiet swallowed it
                # — leaving an exit code and no reason.
                "--force",
            ],
            check=True,
        )
        print(f"  mbtiles {mbtiles.stat().st_size / 1e6:.0f} MB", flush=True)

        subprocess.run(["pmtiles", "convert", str(mbtiles), str(OUT)], check=True)

    print(f"\n{OUT.relative_to(REPO)} — {OUT.stat().st_size / 1e6:.0f} MB "
          f"in {(time.monotonic() - started) / 60:.1f} min")
    print("publish with:  python3 data/tiles/upload_basemap.py")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.parse_args()
    build()


if __name__ == "__main__":
    main()
