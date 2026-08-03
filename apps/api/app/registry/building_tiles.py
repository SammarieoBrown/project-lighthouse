"""Build the event-independent Jamaica structures tileset.

The archive is a reference inventory: footprints at close zoom and centroids at
wide zoom. It deliberately contains no advisory number, wind band, exposure,
damage, or "first hit" field. Those are event observations or forecasts and
belong in a separate overlay keyed to an explicit event and advisory.

The two source-layer names remain compatible with the console's existing tile
source:

* ``structure_points`` at z9-13 contains deterministic 0.005-degree grid
  aggregates. Its ``w`` property is the exact number of structures represented
  by that point; and
* ``structures`` at z14-15 emits one mapped source-footprint feature per source
  structure, with compact ``d`` / ``c`` district and community properties.

    cd apps/api && uv run python -m app.registry.building_tiles

Requires tippecanoe and the pmtiles CLI (``brew install tippecanoe pmtiles``)
and the cached VIDA GeoParquet from ``data/buildings/fetch_footprints.py``.

Large GeoJSON intermediates are reused only when their SHA-256 digests and a
fingerprint of the exact source inputs and inventory recipe match. The finished
PMTiles archive is pinned by ``data/tiles/structures.manifest.json``; the
publisher refuses to upload a local archive that does not match it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

from app.registry.geography import BOUNDARIES

REPO = Path(__file__).parents[4]
sys.path.insert(0, str(REPO / "data"))
import manifest as checksum_manifest  # noqa: E402

PARQUET = REPO / "data" / "buildings" / "cache" / "jamaica-buildings.parquet"
OUT = REPO / "data" / "tiles" / "cache" / "structures-z15.pmtiles"

# The large intermediates are fetch-cache neighbours because both are ignored
# build material. Their one-line state file shares the ``.geojsonl`` suffix so
# it is ignored too; it contains metadata, not a GeoJSON feature.
GEOJSON = REPO / "data" / "buildings" / "cache" / "structures.geojsonl"
POINTS = REPO / "data" / "buildings" / "cache" / "structure-points.geojsonl"
BUILD_STATE = REPO / "data" / "buildings" / "cache" / "structures-build.geojsonl"

# Committed provenance for the ignored derived archive. It is intentionally
# outside the cache's fetched-basemap manifest: fetching a new basemap must
# never certify whichever structures archive happens to be on disk.
ARTIFACT_MANIFEST = REPO / "data" / "tiles" / "structures.manifest.json"

POINT_MAX_ZOOM, POLY_MIN_ZOOM = 13, 14
MIN_ZOOM, MAX_ZOOM = 9, 15
ADMIN3 = "jam_admin3"

# About 530 m east-west and 556 m north-south at Jamaica's latitude. This keeps
# the whole-island layer to one truthful weighted mark per local settlement
# cell instead of forcing 1.84M points into a handful of low-zoom tiles.
POINT_GRID_DEGREES = 0.005
EXPECTED_STRUCTURE_COUNT = 1_844_379

INVENTORY_RECIPE_VERSION = 3
TILE_RECIPE_VERSION = 5
STATE_SCHEMA = "lighthouse.structure-intermediates.v1"
MANIFEST_SCHEMA = "lighthouse.structure-archive.v1"
TILESET_NAME = "Project Lighthouse structure inventory"
TILESET_DESCRIPTION = "Mapped Jamaica structure distribution and source footprints"


def _require(*tools: str) -> None:
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing:
        sys.exit(f"missing {', '.join(missing)} — brew install {' '.join(missing)}")


def _duckdb_module():
    try:
        import duckdb
    except ModuleNotFoundError:
        sys.exit("duckdb is not installed:  cd apps/api && uv add --dev duckdb")
    return duckdb


def _duckdb():
    duckdb = _duckdb_module()
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    return con


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _relative(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def inventory_recipe(
    *,
    parquet_digest: str | None = None,
    boundaries_digest: str | None = None,
    duckdb_version: str | None = None,
) -> dict[str, Any]:
    """Return the complete, canonical recipe for the reusable inventory files.

    Optional values make the function cheap to test. Production callers omit
    them, so the fingerprint is tied to the bytes actually present on disk.
    """
    missing = [path for path in (PARQUET, BOUNDARIES) if not path.exists()]
    if missing and (parquet_digest is None or boundaries_digest is None):
        sys.exit(
            f"{missing[0]} is missing — fetch the building footprints and replay geography first"
        )

    parquet_digest = parquet_digest or checksum_manifest.sha256_file(PARQUET)
    boundaries_digest = boundaries_digest or checksum_manifest.sha256_file(BOUNDARIES)
    duckdb_version = duckdb_version or _duckdb_module().__version__

    return {
        "version": INVENTORY_RECIPE_VERSION,
        "inputs": {
            _relative(PARQUET): parquet_digest,
            _relative(BOUNDARIES): boundaries_digest,
        },
        "toolchain": {"duckdb": duckdb_version},
        "transform": {
            "boundary_layer": ADMIN3,
            "footprint": "source geometry, EPSG:4326",
            "source_order": "GeoParquet file_row_number",
            "boundary_tie_breaker": "adm3_pcode, then OGC_FID; first match",
            "feature_zoom": {
                "structure_points": [MIN_ZOOM, POINT_MAX_ZOOM],
                "structures": [POLY_MIN_ZOOM, MAX_ZOOM],
            },
            "wide_zoom": {
                "method": "centroid grid aggregate",
                "cell_degrees": POINT_GRID_DEGREES,
                "coordinate": "mean source centroid in cell, rounded to 6 decimals",
                "coordinate_accumulator": "integer nanodegrees before division",
                "weight_property": "w (exact structures represented)",
            },
            "placement": "ST_Within(centroid, admin3 geometry)",
            # This allowlist is the contract. Event fields do not belong here.
            "properties": {
                "structures": {"d": "admin2 district", "c": "admin3 community"},
                "structure_points": {"w": "structures represented"},
            },
        },
    }


def inventory_fingerprint(recipe: dict[str, Any]) -> str:
    return _canonical_digest(recipe)


def _tool_version(tool: str) -> str:
    version_arg = "version" if tool == "pmtiles" else "--version"
    proc = subprocess.run(
        [tool, version_arg], check=True, capture_output=True, text=True
    )
    return (proc.stdout or proc.stderr).strip().splitlines()[0]


def _tool_digest(tool: str) -> str:
    """Pin executable bytes when a tool's self-reported version is ambiguous."""
    resolved = shutil.which(tool)
    if resolved is None:
        raise RuntimeError(f"cannot resolve executable for {tool}")
    return checksum_manifest.sha256_file(Path(resolved).resolve())


def tile_recipe(
    *,
    inventory_fingerprint_value: str,
    polygon_digest: str,
    point_digest: str,
    tippecanoe_version: str | None = None,
    pmtiles_version: str | None = None,
    pmtiles_digest: str | None = None,
) -> dict[str, Any]:
    """Canonical archive recipe, including tools that can change tile bytes."""
    return {
        "version": TILE_RECIPE_VERSION,
        "inventory_fingerprint": inventory_fingerprint_value,
        "inputs": {
            _relative(GEOJSON): polygon_digest,
            _relative(POINTS): point_digest,
        },
        "toolchain": {
            "tippecanoe": tippecanoe_version or _tool_version("tippecanoe"),
            "pmtiles": pmtiles_version or _tool_version("pmtiles"),
            "pmtiles_executable_sha256": pmtiles_digest or _tool_digest("pmtiles"),
        },
        "layers": {
            "structure_points": {"minzoom": MIN_ZOOM, "maxzoom": POINT_MAX_ZOOM},
            "structures": {"minzoom": POLY_MIN_ZOOM, "maxzoom": MAX_ZOOM},
        },
        "metadata": {"name": TILESET_NAME, "description": TILESET_DESCRIPTION},
        "tippecanoe": {
            "minzoom": MIN_ZOOM,
            "maxzoom": MAX_ZOOM,
            "base_zoom": MIN_ZOOM,
            "drop_rate": 1,
            "no_feature_limit": True,
            "no_tile_size_limit": True,
            "no_tiny_polygon_reduction": True,
            "preserve_input_order": True,
            "simplification": 4,
            # Tippecanoe honours per-feature zoom metadata when emitting tiles,
            # but advertises each layer with the archive-wide z9-15 range. We
            # correct its MBTiles TileJSON before PMTiles conversion so clients
            # do not fetch layers at zooms where they cannot exist.
            "correct_vector_layer_zoom_metadata": True,
        },
    }


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the suffix last so cache ignore rules also cover an interrupted write.
    partial = path.with_name(f"{path.stem}.partial{path.suffix}")
    partial.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    partial.replace(path)


def verified_intermediate_state(
    expected_fingerprint: str,
    *,
    state_path: Path = BUILD_STATE,
    polygons: Path = GEOJSON,
    points: Path = POINTS,
) -> dict[str, Any] | None:
    """Return state only if provenance and both file contents still match."""
    if not state_path.exists() or not polygons.exists() or not points.exists():
        return None
    try:
        state = json.loads(state_path.read_text())
        if state.get("schema") != STATE_SCHEMA:
            return None
        if state.get("inventory_fingerprint") != expected_fingerprint:
            return None
        outputs = state["outputs"]
        expected_paths = {"polygons": polygons, "points": points}
        for name, path in expected_paths.items():
            record = outputs[name]
            if record["sha256"] != checksum_manifest.sha256_file(path):
                return None
        if int(state["feature_count"]) <= 0 or int(state["point_count"]) <= 0:
            return None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
        return None
    return state


def _properties(district: str | None, community: str | None) -> dict[str, str]:
    """The complete footprint-property allowlist: geography, never storm state."""
    properties: dict[str, str] = {}
    if district:
        properties["d"] = district
    if community:
        properties["c"] = community
    return properties


def _footprint_line(
    geometry: str, district: str | None, community: str | None
) -> str:
    """Serialize one mapped source footprint with geography-only properties."""
    polygon = {
        "type": "Feature",
        "properties": _properties(district, community),
        "geometry": json.loads(geometry),
        # The min/max keys inside tippecanoe's -L JSON are silently ignored.
        # Per-feature metadata is what actually gates source geometry by zoom.
        "tippecanoe": {"minzoom": POLY_MIN_ZOOM, "maxzoom": MAX_ZOOM},
    }
    return json.dumps(polygon, separators=(",", ":"), sort_keys=True)


def _aggregate_point_line(lon: float, lat: float, weight: int) -> str:
    """Serialize a weighted settlement cell, never an apparent single building."""
    if weight <= 0:
        raise ValueError("aggregate structure weight must be positive")
    point = {
        "type": "Feature",
        "properties": {"w": int(weight)},
        "geometry": {
            "type": "Point",
            "coordinates": [round(float(lon), 6), round(float(lat), 6)],
        },
        "tippecanoe": {"minzoom": MIN_ZOOM, "maxzoom": POINT_MAX_ZOOM},
    }
    return json.dumps(point, separators=(",", ":"), sort_keys=True)


def write_inventory(polygons: Path, points: Path) -> tuple[int, int]:
    """Write source footprints plus deterministic weighted wide-zoom cells."""
    con = _duckdb()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            with zipfile.ZipFile(BOUNDARIES) as archive:
                archive.extractall(tmp)
            shape = next(Path(tmp).rglob(f"{ADMIN3}.shp"))

            con.execute(
                f"""
                CREATE TABLE b AS
                WITH source AS (
                  SELECT file_row_number AS source_order,
                         p.geometry AS geom,
                         ST_Centroid(p.geometry) AS pt
                  FROM read_parquet(
                    '{PARQUET.as_posix()}', file_row_number = true
                  ) p
                ), placed AS (
                  SELECT s.source_order, s.geom, s.pt,
                         a.adm2_name AS district,
                         a.adm3_name AS community,
                         row_number() OVER (
                           PARTITION BY s.source_order
                           ORDER BY a.adm3_pcode NULLS LAST,
                                    a.OGC_FID NULLS LAST
                         ) AS boundary_rank
                  FROM source s
                  LEFT JOIN ST_Read('{shape.as_posix()}') a
                         ON ST_Within(s.pt, a.geom)
                )
                SELECT source_order, geom, pt, district, community
                FROM placed
                WHERE boundary_rank = 1
                """
            )
            print("  placed structures in reference geography", flush=True)

            rows = con.execute(
                "SELECT ST_AsGeoJSON(geom), district, community "
                "FROM b ORDER BY source_order"
            )
            count = 0
            with polygons.open("w") as polygon_file:
                while True:
                    batch = rows.fetchmany(50_000)
                    if not batch:
                        break
                    for geometry, district, community in batch:
                        polygon_file.write(_footprint_line(geometry, district, community) + "\n")
                        count += 1

            # A static grid, rather than zoom-dependent thinning, makes every
            # mark's meaning stable. Ordering by the grid key makes rebuilds
            # deterministic even if DuckDB's group execution order changes.
            grid = POINT_GRID_DEGREES
            buckets = con.execute(
                f"""
                SELECT CAST(sum(CAST(round(ST_X(pt) * 1000000000) AS HUGEINT)) AS DOUBLE)
                           / count(*) / 1000000000 AS lon,
                       CAST(sum(CAST(round(ST_Y(pt) * 1000000000) AS HUGEINT)) AS DOUBLE)
                           / count(*) / 1000000000 AS lat,
                       count(*) AS weight
                FROM b
                GROUP BY floor(ST_X(pt) / {grid}), floor(ST_Y(pt) / {grid})
                ORDER BY floor(ST_X(pt) / {grid}), floor(ST_Y(pt) / {grid})
                """
            )
            point_count = 0
            represented = 0
            with points.open("w") as point_file:
                while True:
                    batch = buckets.fetchmany(50_000)
                    if not batch:
                        break
                    for lon, lat, weight in batch:
                        point_file.write(_aggregate_point_line(lon, lat, weight) + "\n")
                        point_count += 1
                        represented += int(weight)
            if represented != count:
                raise RuntimeError(
                    f"wide-zoom aggregates represent {represented:,} structures, expected {count:,}"
                )
            if count != EXPECTED_STRUCTURE_COUNT:
                raise RuntimeError(
                    f"inventory contains {count:,} structures, expected "
                    f"{EXPECTED_STRUCTURE_COUNT:,} from the pinned VIDA source"
                )
            return count, point_count
    finally:
        con.close()


def _write_intermediate_state(
    recipe: dict[str, Any], fingerprint: str, feature_count: int, point_count: int
) -> dict[str, Any]:
    state = {
        "schema": STATE_SCHEMA,
        "inventory_fingerprint": fingerprint,
        "inventory_recipe": recipe,
        "feature_count": feature_count,
        "point_count": point_count,
        "outputs": {
            "polygons": {
                "path": _relative(GEOJSON),
                "sha256": checksum_manifest.sha256_file(GEOJSON),
            },
            "points": {
                "path": _relative(POINTS),
                "sha256": checksum_manifest.sha256_file(POINTS),
            },
        },
    }
    _atomic_json(BUILD_STATE, state)
    return state


def _ensure_inventory() -> tuple[dict[str, Any], dict[str, Any]]:
    recipe = inventory_recipe()
    fingerprint = inventory_fingerprint(recipe)
    state = verified_intermediate_state(fingerprint)
    if state is not None:
        print(
            f"reusing {int(state['feature_count']):,} structures — "
            f"input, recipe and content hashes verified ({fingerprint[:12]})"
        )
        return recipe, state

    print("building event-independent structure inventory …", flush=True)
    GEOJSON.parent.mkdir(parents=True, exist_ok=True)
    polygon_partial = GEOJSON.with_name(f"{GEOJSON.stem}.partial{GEOJSON.suffix}")
    point_partial = POINTS.with_name(f"{POINTS.stem}.partial{POINTS.suffix}")
    polygon_partial.unlink(missing_ok=True)
    point_partial.unlink(missing_ok=True)
    count, point_count = write_inventory(polygon_partial, point_partial)
    polygon_partial.replace(GEOJSON)
    point_partial.replace(POINTS)
    state = _write_intermediate_state(recipe, fingerprint, count, point_count)
    print(
        f"  {count:,} structures · {point_count:,} weighted grid cells · "
        f"{GEOJSON.stat().st_size / 1e6:.0f} MB polygons · "
        f"{POINTS.stat().st_size / 1e6:.0f} MB aggregates"
    )
    return recipe, state


def _write_artifact_manifest(
    inventory_recipe_value: dict[str, Any],
    state: dict[str, Any],
    archive_recipe: dict[str, Any],
) -> None:
    document = {
        "schema": MANIFEST_SCHEMA,
        "artifact": {
            "path": OUT.relative_to(ARTIFACT_MANIFEST.parent).as_posix(),
            "bytes": OUT.stat().st_size,
            "sha256": checksum_manifest.sha256_file(OUT),
        },
        "build": {
            "feature_count": int(state["feature_count"]),
            "point_count": int(state["point_count"]),
            # Equal by assertion in write_inventory: each source structure is
            # represented in exactly one weighted wide-zoom cell.
            "represented_structures": int(state["feature_count"]),
            "inventory_fingerprint": inventory_fingerprint(inventory_recipe_value),
            "tile_fingerprint": _canonical_digest(archive_recipe),
            "inventory_recipe": inventory_recipe_value,
            "tile_recipe": archive_recipe,
        },
    }
    _atomic_json(ARTIFACT_MANIFEST, document)


def _correct_mbtiles_layer_zooms(mbtiles: Path) -> None:
    """Make TileJSON layer ranges agree with the actual per-feature ranges.

    Tippecanoe 2.79.0 applies each feature's ``tippecanoe.minzoom`` and
    ``tippecanoe.maxzoom`` while building tiles, but its generated
    ``vector_layers`` metadata still repeats the global archive range. PMTiles
    copies that metadata verbatim, so patch the disposable MBTiles before
    conversion and fail closed if Tippecanoe's layer set changed unexpectedly.
    """
    expected = {
        "structure_points": {"minzoom": MIN_ZOOM, "maxzoom": POINT_MAX_ZOOM},
        "structures": {"minzoom": POLY_MIN_ZOOM, "maxzoom": MAX_ZOOM},
    }
    with sqlite3.connect(mbtiles) as connection:
        row = connection.execute(
            "SELECT value FROM metadata WHERE name = 'json'"
        ).fetchone()
        if row is None:
            raise RuntimeError("tippecanoe MBTiles has no TileJSON metadata")
        try:
            metadata = json.loads(row[0])
            layers = metadata["vector_layers"]
            actual_ids = {layer["id"] for layer in layers}
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise RuntimeError("tippecanoe MBTiles has invalid vector_layers metadata") from error
        if actual_ids != set(expected):
            raise RuntimeError(
                "unexpected tippecanoe layers: "
                f"expected {sorted(expected)}, found {sorted(actual_ids)}"
            )
        for layer in layers:
            layer.update(expected[layer["id"]])
        connection.execute(
            "UPDATE metadata SET value = ? WHERE name = 'json'",
            (json.dumps(metadata, sort_keys=True, separators=(",", ":")),),
        )
        connection.commit()


def _validate_archive_metadata(metadata: dict[str, Any]) -> None:
    """Fail if PMTiles advertises a layer contract different from its recipe."""
    expected = {
        "structure_points": {
            "minzoom": MIN_ZOOM,
            "maxzoom": POINT_MAX_ZOOM,
            "fields": {"w": "Number"},
        },
        "structures": {
            "minzoom": POLY_MIN_ZOOM,
            "maxzoom": MAX_ZOOM,
            "fields": {"c": "String", "d": "String"},
        },
    }
    try:
        actual = {
            layer["id"]: {
                "minzoom": int(layer["minzoom"]),
                "maxzoom": int(layer["maxzoom"]),
                "fields": layer["fields"],
            }
            for layer in metadata["vector_layers"]
        }
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("PMTiles has invalid vector_layers metadata") from error
    if actual != expected:
        raise RuntimeError(
            f"PMTiles layer metadata does not match recipe: {actual!r} != {expected!r}"
        )
    if metadata.get("name") != TILESET_NAME:
        raise RuntimeError(f"PMTiles name is not {TILESET_NAME!r}")
    if metadata.get("description") != TILESET_DESCRIPTION:
        raise RuntimeError(f"PMTiles description is not {TILESET_DESCRIPTION!r}")


def _verify_archive(archive: Path) -> None:
    subprocess.run(
        ["pmtiles", "verify", _relative(archive)], check=True, cwd=REPO
    )
    result = subprocess.run(
        ["pmtiles", "show", "--metadata", _relative(archive)],
        check=True,
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    try:
        metadata = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("pmtiles show returned invalid JSON metadata") from error
    _validate_archive_metadata(metadata)


def build() -> None:
    _require("tippecanoe", "pmtiles")
    started = time.monotonic()
    OUT.parent.mkdir(parents=True, exist_ok=True)

    recipe, state = _ensure_inventory()
    archive_recipe = tile_recipe(
        inventory_fingerprint_value=state["inventory_fingerprint"],
        polygon_digest=state["outputs"]["polygons"]["sha256"],
        point_digest=state["outputs"]["points"]["sha256"],
    )

    # Stable relative paths keep random TemporaryDirectory names out of PMTiles
    # name, description and generator_options metadata. The MBTiles file is an
    # exact disposable target and is removed even if conversion fails.
    mbtiles = REPO / "data" / "tiles" / "cache" / "structures-build.mbtiles"
    archive_partial = OUT.with_name(f"{OUT.stem}.partial{OUT.suffix}")
    mbtiles.unlink(missing_ok=True)
    archive_partial.unlink(missing_ok=True)
    try:
        subprocess.run(
            [
                "tippecanoe",
                "-o",
                _relative(mbtiles),
                f"--name={TILESET_NAME}",
                f"--description={TILESET_DESCRIPTION}",
                "-Z",
                str(MIN_ZOOM),
                "-z",
                str(MAX_ZOOM),
                "-L",
                json.dumps(
                    {
                        "file": _relative(POINTS),
                        "layer": "structure_points",
                    }
                ),
                "-L",
                json.dumps(
                    {
                        "file": _relative(GEOJSON),
                        "layer": "structures",
                    }
                ),
                f"--base-zoom={MIN_ZOOM}",
                "--drop-rate=1",
                "--no-feature-limit",
                "--no-tile-size-limit",
                "--no-tiny-polygon-reduction",
                "--preserve-input-order",
                "--simplification=4",
                "--force",
            ],
            check=True,
            cwd=REPO,
        )
        _correct_mbtiles_layer_zooms(mbtiles)
        print(f"  mbtiles {mbtiles.stat().st_size / 1e6:.0f} MB", flush=True)

        subprocess.run(
            [
                "pmtiles",
                "convert",
                _relative(mbtiles),
                _relative(archive_partial),
            ],
            check=True,
            cwd=REPO,
        )
        _verify_archive(archive_partial)
        archive_partial.replace(OUT)
    finally:
        mbtiles.unlink(missing_ok=True)
        archive_partial.unlink(missing_ok=True)

    _write_artifact_manifest(recipe, state, archive_recipe)
    print(
        f"\n{OUT.relative_to(REPO)} — {OUT.stat().st_size / 1e6:.0f} MB "
        f"in {(time.monotonic() - started) / 60:.1f} min"
    )
    print(f"checksum and provenance: {ARTIFACT_MANIFEST.relative_to(REPO)}")
    print("publish with:  python3 data/tiles/upload_basemap.py")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    build()


if __name__ == "__main__":
    main()
