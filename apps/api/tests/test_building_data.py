"""Focused contracts for the structure inventory and event exposure build."""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from sqlalchemy import text

from app.registry import building_tiles, buildings
from app.registry.buildings import (
    BuildingInventoryError,
    _delete_event_exposure,
    _forecast_advisories,
    _require_build_schema,
    advisory_fingerprint,
    advisory_key,
    exposure_rows_sha256,
    structure_rows_sha256,
)

DATA = Path(__file__).resolve().parents[3] / "data"
sys.path.insert(0, str(DATA))
import manifest as checksum_manifest  # noqa: E402
from buildings import fetch_footprints  # noqa: E402
from tiles import fetch_basemap  # noqa: E402
from tiles import upload_basemap  # noqa: E402


def test_structure_tile_properties_are_event_independent():
    footprint = json.loads(
        building_tiles._footprint_line(
            '{"type":"Polygon","coordinates":[[[0,0],[1,0],[0,0]]]}',
            "Black River",
            "Luana",
        )
    )
    aggregate = json.loads(building_tiles._aggregate_point_line(-77.2, 18.0, 37))

    assert footprint["properties"] == {"c": "Luana", "d": "Black River"}
    assert aggregate["properties"] == {"w": 37}
    assert footprint["tippecanoe"] == {"minzoom": 14, "maxzoom": 15}
    assert aggregate["tippecanoe"] == {"minzoom": 9, "maxzoom": 13}
    forbidden = {"f34", "f50", "f64", "advisory", "event", "exposure", "damage"}
    assert forbidden.isdisjoint(footprint["properties"])
    assert forbidden.isdisjoint(aggregate["properties"])

    recipe = building_tiles.inventory_recipe(
        parquet_digest="a" * 64,
        boundaries_digest="b" * 64,
        duckdb_version="test",
    )
    assert "advisory" not in json.dumps(recipe).lower()
    assert recipe["transform"]["wide_zoom"] == {
        "method": "centroid grid aggregate",
        "cell_degrees": 0.005,
        "coordinate": "mean source centroid in cell, rounded to 6 decimals",
        "coordinate_accumulator": "integer nanodegrees before division",
        "weight_property": "w (exact structures represented)",
    }
    assert recipe["transform"]["feature_zoom"] == {
        "structure_points": [9, 13],
        "structures": [14, 15],
    }
    assert recipe["transform"]["source_order"] == "GeoParquet file_row_number"
    assert recipe["transform"]["boundary_tie_breaker"] == (
        "adm3_pcode, then OGC_FID; first match"
    )
    assert recipe["transform"]["wide_zoom"]["coordinate_accumulator"] == (
        "integer nanodegrees before division"
    )


def test_tile_recipe_disables_implicit_thinning_and_records_layer_zooms():
    recipe = building_tiles.tile_recipe(
        inventory_fingerprint_value="i" * 64,
        polygon_digest="p" * 64,
        point_digest="q" * 64,
        tippecanoe_version="test-tippecanoe",
        pmtiles_version="test-pmtiles",
        pmtiles_digest="m" * 64,
    )
    assert recipe["layers"] == {
        "structure_points": {"minzoom": 9, "maxzoom": 13},
        "structures": {"minzoom": 14, "maxzoom": 15},
    }
    assert recipe["toolchain"]["pmtiles_executable_sha256"] == "m" * 64
    assert recipe["metadata"] == {
        "name": building_tiles.TILESET_NAME,
        "description": building_tiles.TILESET_DESCRIPTION,
    }
    assert recipe["tippecanoe"] == {
        "minzoom": 9,
        "maxzoom": 15,
        "base_zoom": 9,
        "drop_rate": 1,
        "no_feature_limit": True,
        "no_tile_size_limit": True,
        "no_tiny_polygon_reduction": True,
        "preserve_input_order": True,
        "simplification": 4,
        "correct_vector_layer_zoom_metadata": True,
    }


def test_mbtiles_metadata_advertises_actual_layer_zoom_ranges(tmp_path: Path):
    mbtiles = tmp_path / "structures.mbtiles"
    with sqlite3.connect(mbtiles) as connection:
        connection.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
        connection.execute(
            "INSERT INTO metadata VALUES ('json', ?)",
            (
                json.dumps(
                    {
                        "vector_layers": [
                            {
                                "id": "structure_points",
                                "minzoom": 9,
                                "maxzoom": 15,
                                "fields": {"w": "Number"},
                            },
                            {
                                "id": "structures",
                                "minzoom": 9,
                                "maxzoom": 15,
                                "fields": {"c": "String", "d": "String"},
                            },
                        ]
                    }
                ),
            ),
        )

    building_tiles._correct_mbtiles_layer_zooms(mbtiles)

    with sqlite3.connect(mbtiles) as connection:
        value = connection.execute(
            "SELECT value FROM metadata WHERE name = 'json'"
        ).fetchone()[0]
    layers = {layer["id"]: layer for layer in json.loads(value)["vector_layers"]}
    assert (layers["structure_points"]["minzoom"], layers["structure_points"]["maxzoom"]) == (
        9,
        13,
    )
    assert (layers["structures"]["minzoom"], layers["structures"]["maxzoom"]) == (
        14,
        15,
    )

    building_tiles._validate_archive_metadata(
        {
            "name": building_tiles.TILESET_NAME,
            "description": building_tiles.TILESET_DESCRIPTION,
            "vector_layers": [
                {
                    "id": "structure_points",
                    "minzoom": 9,
                    "maxzoom": 13,
                    "fields": {"w": "Number"},
                },
                {
                    "id": "structures",
                    "minzoom": 14,
                    "maxzoom": 15,
                    "fields": {"c": "String", "d": "String"},
                },
            ]
        }
    )
    with pytest.raises(RuntimeError, match="does not match recipe"):
        building_tiles._validate_archive_metadata(
            {
                "name": building_tiles.TILESET_NAME,
                "description": building_tiles.TILESET_DESCRIPTION,
                "vector_layers": [
                    {
                        "id": "structure_points",
                        "minzoom": 9,
                        "maxzoom": 15,
                        "fields": {"w": "Number"},
                    },
                    {
                        "id": "structures",
                        "minzoom": 9,
                        "maxzoom": 15,
                        "fields": {"c": "String", "d": "String"},
                    },
                ]
            }
        )


def test_inventory_fingerprint_changes_when_an_input_changes():
    common = {"boundaries_digest": "b" * 64, "duckdb_version": "test"}
    first = building_tiles.inventory_recipe(parquet_digest="a" * 64, **common)
    second = building_tiles.inventory_recipe(parquet_digest="c" * 64, **common)
    assert building_tiles.inventory_fingerprint(first) != building_tiles.inventory_fingerprint(
        second
    )


def test_intermediates_are_reused_only_when_their_content_hashes_match(tmp_path: Path):
    polygons = tmp_path / "structures.geojsonl"
    points = tmp_path / "structure-points.geojsonl"
    state_path = tmp_path / "structures-build.geojsonl"
    polygons.write_text("polygon\n")
    points.write_text("point-a\n")
    state = {
        "schema": building_tiles.STATE_SCHEMA,
        "inventory_fingerprint": "recipe-fingerprint",
        "feature_count": 1,
        "point_count": 1,
        "outputs": {
            "polygons": {"sha256": checksum_manifest.sha256_file(polygons)},
            "points": {"sha256": checksum_manifest.sha256_file(points)},
        },
    }
    state_path.write_text(json.dumps(state))

    assert building_tiles.verified_intermediate_state(
        "recipe-fingerprint",
        state_path=state_path,
        polygons=polygons,
        points=points,
    ) == state

    # Same byte count, different bytes: a size-only gate would accept this.
    points.write_text("point-b\n")
    assert (
        building_tiles.verified_intermediate_state(
            "recipe-fingerprint",
            state_path=state_path,
            polygons=polygons,
            points=points,
        )
        is None
    )


def test_source_cache_manifest_can_exclude_derived_build_outputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    (tmp_path / ".manifestignore").write_text("*.geojsonl\n")
    (tmp_path / "source.parquet").write_bytes(b"source")
    derived = tmp_path / "structures.geojsonl"
    derived.write_bytes(b"derived-a")
    (tmp_path / "manifest.sha256.partial").write_text("interrupted prior write")

    checksum_manifest.write(tmp_path)
    assert not (tmp_path / "manifest.sha256.partial").exists()
    entries = checksum_manifest.read(checksum_manifest.manifest_path(tmp_path))
    assert set(entries) == {".manifestignore", "source.parquet"}
    assert checksum_manifest.verify(tmp_path) == 0
    capsys.readouterr()

    derived.write_bytes(b"derived-b")
    assert checksum_manifest.verify(tmp_path) == 0


def test_footprint_fetch_refuses_same_size_corrupt_cached_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target = tmp_path / "jamaica-buildings.parquet"
    target.write_bytes(b"trusted")
    checksum_manifest.write(tmp_path)
    target.write_bytes(b"changed")  # same byte count, different digest

    monkeypatch.setattr(fetch_footprints, "CACHE", tmp_path)
    monkeypatch.setattr(fetch_footprints, "TARGET", target)
    monkeypatch.setattr(
        fetch_footprints.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("corrupt cached bytes must fail before network"),
    )

    with pytest.raises(SystemExit, match="refusing to rewrite the manifest"):
        fetch_footprints.fetch(force=False)


def test_basemap_fetch_refuses_same_size_corrupt_cached_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target = tmp_path / "island.pmtiles"
    target.write_bytes(b"trusted")
    checksum_manifest.write(tmp_path)
    target.write_bytes(b"changed")

    monkeypatch.setattr(fetch_basemap, "CACHE", tmp_path)
    monkeypatch.setattr(
        fetch_basemap,
        "ARCHIVES",
        {"island.pmtiles": {"bbox": "0,0,1,1", "maxzoom": 1}},
    )
    monkeypatch.setattr(
        fetch_basemap,
        "latest_build",
        lambda: pytest.fail("corrupt cached bytes must fail before network"),
    )

    with pytest.raises(RuntimeError, match="refusing to rewrite the manifest"):
        fetch_basemap.extract_tiles(force=False)


def test_basemap_extract_promotes_only_a_complete_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target = tmp_path / "island.pmtiles"
    target.write_bytes(b"old-valid-target")
    monkeypatch.setattr(fetch_basemap, "CACHE", tmp_path)
    monkeypatch.setattr(
        fetch_basemap,
        "ARCHIVES",
        {"island.pmtiles": {"bbox": "0,0,1,1", "maxzoom": 1}},
    )
    monkeypatch.setattr(fetch_basemap, "latest_build", lambda: "test-build.pmtiles")
    monkeypatch.setattr(fetch_basemap.shutil, "which", lambda _tool: "/usr/bin/pmtiles")
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command[1] == "extract":
            partial = Path(command[3])
            assert partial.name == "island.partial.pmtiles"
            assert target.read_bytes() == b"old-valid-target"
            partial.write_bytes(b"complete-new-target")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(fetch_basemap.subprocess, "run", fake_run)

    assert fetch_basemap.extract_tiles(force=True) == [target]
    assert target.read_bytes() == b"complete-new-target"
    assert not (tmp_path / "island.partial.pmtiles").exists()
    assert [command[1] for command in calls] == ["extract", "verify"]


def test_committed_structure_manifest_matches_build_contract():
    document = json.loads((DATA / "tiles" / "structures.manifest.json").read_text())
    artifact = document["artifact"]
    build = document["build"]
    inventory = build["inventory_recipe"]
    tiles = build["tile_recipe"]

    assert document["schema"] == building_tiles.MANIFEST_SCHEMA
    assert inventory["version"] == building_tiles.INVENTORY_RECIPE_VERSION
    assert tiles["version"] == building_tiles.TILE_RECIPE_VERSION
    assert build["inventory_fingerprint"] == building_tiles.inventory_fingerprint(inventory)
    assert build["tile_fingerprint"] == building_tiles._canonical_digest(tiles)
    assert build["feature_count"] == build["represented_structures"]
    assert build["feature_count"] == building_tiles.EXPECTED_STRUCTURE_COUNT
    assert 0 < build["point_count"] < build["feature_count"]
    assert tiles["layers"] == {
        layer: {"minzoom": zooms[0], "maxzoom": zooms[1]}
        for layer, zooms in inventory["transform"]["feature_zoom"].items()
    }
    assert artifact["path"] == "cache/structures-z15.pmtiles"
    assert artifact["bytes"] > 0
    assert upload_basemap._valid_digest(artifact["sha256"])
    assert upload_basemap._valid_digest(
        tiles["toolchain"]["pmtiles_executable_sha256"]
    )

    archive = DATA / "tiles" / artifact["path"]
    if archive.exists():
        assert archive.stat().st_size == artifact["bytes"]
        assert checksum_manifest.sha256_file(archive) == artifact["sha256"]


def test_upload_skip_requires_remote_checksum_not_just_matching_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    local = tmp_path / "asset.json"
    local.write_bytes(b"local-data")
    artifact = upload_basemap.Artifact(
        "asset.json", local, checksum_manifest.sha256_file(local), local.stat().st_size
    )
    monkeypatch.setattr(
        upload_basemap,
        "head",
        lambda _url: upload_basemap.RemoteHead(
            200, local.stat().st_size, upload_basemap.CACHE_CONTROL
        ),
    )
    monkeypatch.setattr(
        upload_basemap,
        "remote_sha256",
        lambda _url: upload_basemap.RemoteDigest(
            200, "0" * 64, local.stat().st_size
        ),
    )
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(upload_basemap.subprocess, "run", fake_run)
    result = upload_basemap.upload(
        "https://tiles.example", {"CLOUDFLARE_API_TOKEN": "test"}, False, [artifact]
    )
    assert result == 0
    assert calls, "same-sized content with a different digest must be replaced"
    assert f"--cache-control={upload_basemap.CACHE_CONTROL}" in calls[0]


def test_upload_skips_content_only_after_remote_checksum_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    local = tmp_path / "asset.json"
    local.write_bytes(b"local-data")
    digest = checksum_manifest.sha256_file(local)
    artifact = upload_basemap.Artifact("asset.json", local, digest, local.stat().st_size)
    monkeypatch.setattr(
        upload_basemap,
        "head",
        lambda _url: upload_basemap.RemoteHead(
            200, local.stat().st_size, "MUST-REVALIDATE, PUBLIC, MAX-AGE = 0"
        ),
    )
    monkeypatch.setattr(
        upload_basemap,
        "remote_sha256",
        lambda _url: upload_basemap.RemoteDigest(200, digest, local.stat().st_size),
    )
    monkeypatch.setattr(
        upload_basemap.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("verified content should not upload"),
    )
    assert (
        upload_basemap.upload(
            "https://tiles.example",
            {"CLOUDFLARE_API_TOKEN": "test"},
            False,
            [artifact],
        )
        == 0
    )


def test_upload_replaces_matching_bytes_when_cache_control_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    local = tmp_path / "asset.json"
    local.write_bytes(b"local-data")
    digest = checksum_manifest.sha256_file(local)
    artifact = upload_basemap.Artifact("asset.json", local, digest, local.stat().st_size)
    monkeypatch.setattr(
        upload_basemap,
        "head",
        lambda _url: upload_basemap.RemoteHead(
            200, local.stat().st_size, "public, max-age=31536000, immutable"
        ),
    )
    monkeypatch.setattr(
        upload_basemap,
        "remote_sha256",
        lambda _url: pytest.fail("stale metadata must bypass checksum skip"),
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(
        upload_basemap.subprocess,
        "run",
        lambda command, **_kwargs: (
            calls.append(command) or SimpleNamespace(returncode=0, stderr="")
        ),
    )

    assert (
        upload_basemap.upload(
            "https://tiles.example",
            {"CLOUDFLARE_API_TOKEN": "test"},
            False,
            [artifact],
        )
        == 0
    )
    assert len(calls) == 1
    assert f"--cache-control={upload_basemap.CACHE_CONTROL}" in calls[0]


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("MUST-REVALIDATE, public, MAX-AGE = 0", 0),
        (None, 1),
        ("public, max-age=31536000, immutable", 1),
    ],
)
def test_public_verification_requires_revalidating_cache_control(
    header: str | None,
    expected: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    local = tmp_path / "asset.json"
    local.write_bytes(b"local-data")
    digest = checksum_manifest.sha256_file(local)
    artifact = upload_basemap.Artifact("asset.json", local, digest, local.stat().st_size)
    monkeypatch.setattr(
        upload_basemap,
        "head",
        lambda _url: upload_basemap.RemoteHead(200, local.stat().st_size, header),
    )
    monkeypatch.setattr(
        upload_basemap,
        "remote_sha256",
        lambda _url: upload_basemap.RemoteDigest(
            200, digest, local.stat().st_size
        ),
    )

    assert upload_basemap.verify("https://tiles.example", [artifact]) == expected
    capsys.readouterr()


class _Rows:
    def __init__(self, rows: list[tuple]):
        self.rows = rows

    def fetchall(self) -> list[tuple]:
        return self.rows

    def fetchone(self) -> tuple | None:
        return self.rows[0] if self.rows else None


class _Connection:
    def __init__(self, rows: list[tuple] | None = None):
        self.rows = rows or []
        self.calls: list[tuple[str, dict[str, str]]] = []

    def execute(self, statement, parameters=None):
        self.calls.append((str(statement), parameters))
        return _Rows(self.rows)


def test_exposure_advisories_are_event_scoped_and_suffix_safe():
    connection = _Connection(
        [
            ("id-16", "16", None, None, None),
            ("id-15a", "15A", None, None, None),
            ("id-9", "9", None, None, None),
            ("id-15", "15", None, None, None),
        ]
    )
    rows = _forecast_advisories(connection, "event-melissa")
    assert [row[1] for row in rows] == ["9", "15", "15A", "16"]

    sql, parameters = connection.calls[0]
    assert "a.hazard_event_id = :event" in sql
    assert "::int" not in sql
    assert parameters == {"event": "event-melissa"}


def test_replacing_exposure_deletes_only_the_selected_event():
    connection = _Connection()
    _delete_event_exposure(connection, "event-melissa")
    sql, parameters = connection.calls[0]
    assert sql.startswith("DELETE FROM place_exposure")
    assert "hazard_event_id = :event" in sql
    assert "TRUNCATE place_exposure" not in sql
    assert parameters == {"event": "event-melissa"}

    marker_sql, marker_parameters = connection.calls[1]
    assert marker_sql.startswith("DELETE FROM place_exposure_build")
    assert marker_parameters == {"event": "event-melissa"}


def test_advisory_key_rejects_non_forecast_identifiers():
    assert advisory_key("15") < advisory_key("15A") < advisory_key("16")
    with pytest.raises(BuildingInventoryError, match="does not start with a number"):
        advisory_key("best_track")


def test_advisory_fingerprint_covers_identity_order_and_wind_geometry():
    baseline = [
        ("id-1", "1", "wind-34", None, None),
        ("id-2", "2", "wind-34", "wind-50", None),
    ]
    digest = advisory_fingerprint(baseline)

    assert len(digest) == 64
    assert advisory_fingerprint(list(baseline)) == digest
    assert advisory_fingerprint(list(reversed(baseline))) != digest
    assert (
        advisory_fingerprint(
            [baseline[0], ("id-2", "2", "changed-wind-34", "wind-50", None)]
        )
        != digest
    )


def test_derived_row_digests_cover_every_material_field_and_ignore_input_order():
    structures = [
        ("Saint A", "District A", "Community A", 7, 125.5),
        ("Saint B", "District B", "Community B", 11, 250.25),
    ]
    structure_digest = structure_rows_sha256(structures)
    assert structure_rows_sha256(list(reversed(structures))) == structure_digest
    for replacement in (
        ("Changed", "District A", "Community A", 7, 125.5),
        ("Saint A", "Changed", "Community A", 7, 125.5),
        ("Saint A", "District A", "Changed", 7, 125.5),
        ("Saint A", "District A", "Community A", 8, 125.5),
        ("Saint A", "District A", "Community A", 7, 125.75),
    ):
        assert structure_rows_sha256([replacement, structures[1]]) != structure_digest
    assert structure_rows_sha256([("P\u0000é", "D", "C", 1, 0.0)]) != (
        structure_rows_sha256([("P\\u0000e\u0301", "D", "C", 1, 0.0)])
    )
    assert structure_rows_sha256([("P", "D", "C", 1, 0.0)]) != (
        structure_rows_sha256([("P", "D", "C", 1, -0.0)])
    )

    first_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    second_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    exposure = [
        (first_id, "Saint A", "District A", "Community A", 34, 4),
        (second_id, "Saint B", "District B", "Community B", 64, 6),
    ]
    event_id = uuid.UUID("00000000-0000-0000-0000-000000000010")
    exposure_digest = exposure_rows_sha256(event_id, exposure)
    assert exposure_rows_sha256(event_id, list(reversed(exposure))) == exposure_digest
    assert (
        exposure_rows_sha256(
            uuid.UUID("00000000-0000-0000-0000-000000000011"),
            exposure,
        )
        != exposure_digest
    )
    for replacement in (
        (second_id, "Saint A", "District A", "Community A", 34, 4),
        (first_id, "Changed", "District A", "Community A", 34, 4),
        (first_id, "Saint A", "Changed", "Community A", 34, 4),
        (first_id, "Saint A", "District A", "Changed", 34, 4),
        (first_id, "Saint A", "District A", "Community A", 50, 4),
        (first_id, "Saint A", "District A", "Community A", 34, 5),
    ):
        assert exposure_rows_sha256(event_id, [replacement, exposure[1]]) != exposure_digest

    assert exposure_rows_sha256(event_id, []) != exposure_rows_sha256(second_id, [])
    for nonfinite in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(BuildingInventoryError, match="finite"):
            structure_rows_sha256([("P", "D", "C", 1, nonfinite)])
    with pytest.raises(BuildingInventoryError, match="canonical UUID"):
        exposure_rows_sha256("not-an-event", [])


def test_derived_row_digest_v1_golden_vectors_pin_canonical_bytes():
    event_id = uuid.UUID("00000000-0000-0000-0000-000000000010")
    advisory_id = uuid.UUID("00000000-0000-0000-0000-000000000001")

    assert structure_rows_sha256([("P\u0000é", "D", "C", 1, -0.0)]) == (
        "73aee1c47aa95f52ee4877d68aa373f514563b3eb2dfda5189d088e2bd89c8ac"
    )
    assert exposure_rows_sha256(event_id, []) == (
        "9a363908c6bb967121695e953fa4a2e1c52e962373cf0c93d2c17ce9196d5490"
    )
    assert exposure_rows_sha256(
        event_id,
        [(advisory_id, "P\u0000é", "D", "C", 34, 4)],
    ) == "5aa668d9f1be862f3da810d678b052015a542f05344822fa1a7eb74075b786e9"


def test_build_preflight_requires_both_completion_tables_before_expensive_work():
    ready = _Connection([(True, True, True, True, True)])
    _require_build_schema(ready)
    assert "information_schema.tables" in ready.calls[0][0]

    for state in (
        (False, False, False, False, False),
        (True, False, True, False, False),
        (False, True, False, True, True),
        (True, True, False, False, False),
    ):
        connection = _Connection([state])
        with pytest.raises(BuildingInventoryError, match="alembic upgrade head"):
            _require_build_schema(connection)


class _FastBuildDuck:
    def execute(self, statement):
        assert "count(*)" in statement
        return _Rows([(2, 0)])


def _seed_build_event(engine, external_ref: str) -> tuple[uuid.UUID, uuid.UUID]:
    event_id = uuid.uuid4()
    advisory_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO hazard_event (id, name, external_ref, replay) "
                "VALUES (:id, 'Digest test storm', :external_ref, true)"
            ),
            {"id": event_id, "external_ref": external_ref},
        )
        connection.execute(
            text(
                "INSERT INTO advisory "
                "(id, hazard_event_id, advisory_number, issued_at, observed, raw) "
                "VALUES (:id, :event, '1', :issued, false, '{}'::jsonb)"
            ),
            {
                "id": advisory_id,
                "event": event_id,
                "issued": datetime(2026, 8, 3, tzinfo=UTC),
            },
        )
    return event_id, advisory_id


def _install_fast_build(monkeypatch, engine) -> None:
    source = "a" * 64
    boundaries = "b" * 64
    provenance = {
        "source_sha256": source,
        "boundaries_sha256": boundaries,
        "recipe_version": buildings.INVENTORY_RECIPE_VERSION,
        "inventory_fingerprint": buildings.inventory_fingerprint(
            source_sha256=source,
            boundaries_sha256=boundaries,
            recipe_version=buildings.INVENTORY_RECIPE_VERSION,
        ),
    }
    monkeypatch.setattr(buildings, "get_engine", lambda: engine)
    monkeypatch.setattr(buildings, "inventory_provenance", lambda: provenance)
    monkeypatch.setattr(buildings, "_duckdb", _FastBuildDuck)
    monkeypatch.setattr(buildings, "_placed_buildings", lambda _con, _tmp: None)
    monkeypatch.setattr(
        buildings,
        "_structures",
        lambda _con: [
            ("Test Parish", "Test District", "Place A", 5, 100.25),
            ("Test Parish", "Test District", "Place B", 7, 140.5),
        ],
    )
    monkeypatch.setattr(
        buildings,
        "_exposure",
        lambda _con, _wkt: [
            ("Test Parish", "Test District", "Place A", 34, 3),
        ],
    )


def _clean_build_events(engine, event_ids: list[uuid.UUID]) -> None:
    with engine.begin() as connection:
        for event_id in event_ids:
            connection.execute(
                text("DELETE FROM hazard_event WHERE id = :event"),
                {"event": event_id},
            )
        connection.execute(text("TRUNCATE place_structures"))
        connection.execute(text("DELETE FROM place_structure_build"))


def _event_derived_snapshot(connection, event_id: uuid.UUID) -> dict[str, tuple[tuple, ...]]:
    return {
        "exposure": tuple(
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT e.advisory_id, e.parish, e.district, e.community, "
                    "       e.band, e.structures "
                    "FROM place_exposure e JOIN advisory a ON a.id = e.advisory_id "
                    "WHERE a.hazard_event_id = :event "
                    "ORDER BY e.advisory_id, e.parish, e.district, e.community, e.band"
                ),
                {"event": event_id},
            )
        ),
        "exposure_marker": tuple(
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT hazard_event_id, inventory_fingerprint, structure_rows_sha256, "
                    "       advisory_fingerprint, advisory_count, exposure_row_count, "
                    "       exposed_structure_count, exposure_rows_sha256, completed_at "
                    "FROM place_exposure_build WHERE hazard_event_id = :event"
                ),
                {"event": event_id},
            )
        ),
    }


def _complete_build_snapshot(connection) -> dict[str, tuple[tuple, ...]]:
    statements = {
        "advisory": (
            "SELECT id, hazard_event_id, advisory_number, issued_at, observed, "
            "       track::text, cone::text, wind_field_34::text, "
            "       wind_field_50::text, wind_field_64::text, "
            "       raw::text, ingested_at "
            "FROM advisory ORDER BY id"
        ),
        "place_structures": (
            "SELECT parish, district, community, structures, built_m2 "
            "FROM place_structures ORDER BY parish, district, community"
        ),
        "place_structure_build": (
            "SELECT singleton, inventory_fingerprint, source_sha256, boundaries_sha256, "
            "       recipe_version, structure_count, place_count, "
            "       structure_rows_sha256, completed_at "
            "FROM place_structure_build ORDER BY singleton"
        ),
        "place_exposure": (
            "SELECT advisory_id, parish, district, community, band, structures "
            "FROM place_exposure ORDER BY advisory_id, parish, district, community, band"
        ),
        "place_exposure_build": (
            "SELECT hazard_event_id, inventory_fingerprint, structure_rows_sha256, "
            "       advisory_fingerprint, advisory_count, exposure_row_count, "
            "       exposed_structure_count, exposure_rows_sha256, completed_at "
            "FROM place_exposure_build ORDER BY hazard_event_id"
        ),
    }
    return {
        name: tuple(tuple(row) for row in connection.execute(text(statement)))
        for name, statement in statements.items()
    }


def test_real_build_transaction_preserves_two_events_and_their_markers(
    engine, monkeypatch
):
    first_ref = f"digest-a-{uuid.uuid4().hex}"
    second_ref = f"digest-b-{uuid.uuid4().hex}"
    first_event, _ = _seed_build_event(engine, first_ref)
    second_event, _ = _seed_build_event(engine, second_ref)
    _install_fast_build(monkeypatch, engine)

    try:
        buildings.build(external_ref=second_ref)
        with engine.connect() as connection:
            second_before = _event_derived_snapshot(connection, second_event)

        buildings.build(external_ref=first_ref)

        with engine.connect() as connection:
            assert _event_derived_snapshot(connection, second_event) == second_before
            exposure_events = set(
                connection.execute(
                    text(
                        "SELECT DISTINCT a.hazard_event_id "
                        "FROM place_exposure e JOIN advisory a ON a.id = e.advisory_id"
                    )
                ).scalars()
            )
            marker_events = set(
                connection.execute(
                    text("SELECT hazard_event_id FROM place_exposure_build")
                ).scalars()
            )
            assert exposure_events == {first_event, second_event}
            assert marker_events == {first_event, second_event}
            assert connection.execute(text("SELECT count(*) FROM place_exposure")).scalar_one() == 2
            assert connection.execute(
                text("SELECT count(*) FROM place_structure_build")
            ).scalar_one() == 1
    finally:
        _clean_build_events(engine, [first_event, second_event])


class _FailingConnection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, statement, parameters=None):
        if "INSERT INTO place_exposure_build" in str(statement):
            raise RuntimeError("injected completion-marker failure")
        return self.connection.execute(statement, parameters)


class _FailingEngine:
    def __init__(self, engine):
        self.engine = engine

    def connect(self):
        return self.engine.connect()

    @contextmanager
    def begin(self):
        with self.engine.begin() as connection:
            yield _FailingConnection(connection)


class _AdvisoryMutatingEngine:
    def __init__(self, engine, event_id: uuid.UUID):
        self.engine = engine
        self.event_id = event_id

    def connect(self):
        return self.engine.connect()

    @contextmanager
    def begin(self):
        with self.engine.begin() as mutation:
            mutation.execute(
                text(
                    "INSERT INTO advisory "
                    "(id, hazard_event_id, advisory_number, issued_at, observed, raw) "
                    "VALUES (:id, :event, '2', :issued, false, '{}'::jsonb)"
                ),
                {
                    "id": uuid.uuid4(),
                    "event": self.event_id,
                    "issued": datetime(2026, 8, 3, 6, tzinfo=UTC),
                },
            )
        with self.engine.begin() as connection:
            yield connection


def test_real_build_transaction_aborts_if_advisories_change_during_spatial_work(
    engine, monkeypatch
):
    external_ref = f"digest-race-{uuid.uuid4().hex}"
    event_id, _ = _seed_build_event(engine, external_ref)
    _install_fast_build(monkeypatch, engine)
    monkeypatch.setattr(
        buildings,
        "get_engine",
        lambda: _AdvisoryMutatingEngine(engine, event_id),
    )

    try:
        with pytest.raises(BuildingInventoryError, match="changed during"):
            buildings.build(external_ref=external_ref)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM place_structures")).scalar_one() == 0
            assert connection.execute(
                text("SELECT count(*) FROM place_structure_build")
            ).scalar_one() == 0
            assert connection.execute(text("SELECT count(*) FROM place_exposure")).scalar_one() == 0
            assert connection.execute(
                text("SELECT count(*) FROM place_exposure_build")
            ).scalar_one() == 0
    finally:
        _clean_build_events(engine, [event_id])


def test_real_build_transaction_rolls_back_every_row_when_completion_marker_fails(
    engine, monkeypatch
):
    first_ref = f"digest-rollback-a-{uuid.uuid4().hex}"
    second_ref = f"digest-rollback-b-{uuid.uuid4().hex}"
    first_event, _ = _seed_build_event(engine, first_ref)
    second_event, _ = _seed_build_event(engine, second_ref)
    _install_fast_build(monkeypatch, engine)

    try:
        buildings.build(external_ref=second_ref)
        buildings.build(external_ref=first_ref)
        with engine.connect() as connection:
            before = _complete_build_snapshot(connection)

        monkeypatch.setattr(
            buildings,
            "_structures",
            lambda _con: [
                ("Changed Parish", "Changed District", "Changed Place", 12, 999.5),
            ],
        )
        monkeypatch.setattr(
            buildings,
            "_exposure",
            lambda _con, _wkt: [
                ("Changed Parish", "Changed District", "Changed Place", 64, 9),
            ],
        )
        monkeypatch.setattr(buildings, "get_engine", lambda: _FailingEngine(engine))
        with pytest.raises(RuntimeError, match="injected completion-marker failure"):
            buildings.build(external_ref=first_ref)

        with engine.connect() as connection:
            assert _complete_build_snapshot(connection) == before
    finally:
        _clean_build_events(engine, [first_event, second_event])
