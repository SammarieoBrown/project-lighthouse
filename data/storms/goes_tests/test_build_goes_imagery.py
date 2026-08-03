from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "build_goes_imagery.py"
SPEC = importlib.util.spec_from_file_location("build_goes_imagery", MODULE_PATH)
assert SPEC and SPEC.loader
goes = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = goes
SPEC.loader.exec_module(goes)


REAL_KEY = (
    "ABI-L2-MCMIPF/2025/301/15/"
    "OR_ABI-L2-MCMIPF-M6_G19_s20253011500205_e20253011509525_c20253011509583.nc"
)


def valid_config() -> dict:
    return {
        "schema": "lighthouse.goes-scenes.v1",
        "bucket": "noaa-goes19",
        "product": "ABI-L2-MCMIPF",
        "satellite": "G19",
        "bbox": [-81.5, 14.0, -73.0, 21.0],
        "minzoom": 4,
        "maxzoom": 8,
        "storms": [
            {
                "id": "al132025",
                "source": "NOAA GOES-19",
                "frames": [{"target_at": "2025-10-28T15:00:00Z"}],
            }
        ],
    }


class GoesObjectTests(unittest.TestCase):
    def test_actual_noaa_key_uses_scene_start_not_last_modified(self) -> None:
        observed = goes.parse_goes_key_start(REAL_KEY)
        self.assertEqual(
            observed,
            dt.datetime(2025, 10, 28, 15, 0, 20, 500000, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(goes.format_utc(observed), "2025-10-28T15:00:20.500Z")

    def test_s3_xml_fixture_preserves_etag_size_and_scene_time(self) -> None:
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
        <ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
          <Contents>
            <Key>{REAL_KEY}</Key>
            <LastModified>2025-10-28T15:11:36.000Z</LastModified>
            <ETag>&quot;71a295f69915397baabfd94bca366366&quot;</ETag>
            <Size>358471437</Size>
          </Contents>
        </ListBucketResult>""".encode()
        objects, token = goes.parse_s3_listing(xml)
        self.assertIsNone(token)
        self.assertEqual(len(objects), 1)
        self.assertEqual(objects[0].etag, "71a295f69915397baabfd94bca366366")
        self.assertEqual(objects[0].size, 358471437)
        self.assertNotEqual(
            objects[0].last_modified, goes.format_utc(objects[0].start_at)
        )

    def test_nearest_selection_is_deterministic_and_bounded(self) -> None:
        target = dt.datetime(2025, 10, 28, 15, 0, tzinfo=dt.timezone.utc)
        before = goes.S3Object("before", target - dt.timedelta(minutes=1), "a", 1, "")
        after = goes.S3Object("after", target + dt.timedelta(minutes=1), "b", 1, "")
        self.assertEqual(
            goes.nearest_object(
                target, [after, before], max_offset=dt.timedelta(minutes=2)
            ),
            before,
        )
        with self.assertRaises(goes.PipelineError):
            goes.nearest_object(target, [after], max_offset=dt.timedelta(seconds=30))

    def test_hour_prefixes_cross_year_boundary(self) -> None:
        target = dt.datetime(2025, 1, 1, 0, 2, tzinfo=dt.timezone.utc)
        self.assertEqual(
            goes.hour_prefixes(target, window_minutes=5),
            ["ABI-L2-MCMIPF/2024/366/23/", "ABI-L2-MCMIPF/2025/001/00/"],
        )


class TileAndContractTests(unittest.TestCase):
    def test_tile_math_and_mbtiles_row_flip(self) -> None:
        x_range, y_range = goes.tile_range_for_bbox([-81.5, 14.0, -73.0, 21.0], 8)
        self.assertGreaterEqual(len(x_range), 1)
        self.assertGreaterEqual(len(y_range), 1)
        for y in y_range:
            self.assertEqual(goes.xyz_to_tms_row(goes.xyz_to_tms_row(y, 8), 8), y)
        left, bottom, right, top = goes.tile_bounds_mercator(
            x_range.start, y_range.start, 8
        )
        self.assertLess(left, right)
        self.assertLess(bottom, top)

    def test_unconfigured_gilbert_does_not_claim_goes19_coverage(self) -> None:
        config = goes.validate_config(valid_config())
        with self.assertRaisesRegex(goes.PipelineError, "no coverage is assumed"):
            goes.select_storms(config, "al081988")

    def test_public_manifest_has_only_runtime_contract_fields(self) -> None:
        artifacts = {
            "storms": [
                {
                    "id": "al132025",
                    "source": "NOAA GOES-19",
                    "frames": [
                        {
                            "at": "2025-10-28T15:00:20.500Z",
                            "tiles": "pmtiles://https://tiles.example/storm.pmtiles/{z}/{x}/{y}",
                            "source_object": {"key": REAL_KEY},
                        }
                    ],
                }
            ]
        }
        public = goes.public_index_from_artifacts(artifacts)
        self.assertEqual(set(public), {"storms"})
        self.assertEqual(set(public["storms"][0]), {"id", "source", "frames"})
        self.assertEqual(set(public["storms"][0]["frames"][0]), {"at", "tiles"})

    def test_local_verify_checks_hashes_and_manifest_with_tiny_files(self) -> None:
        config = goes.validate_config(valid_config())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.nc"
            mbtiles = root / "frame.mbtiles"
            pmtiles = root / "frame.pmtiles"
            source.write_bytes(b"tiny synthetic netcdf stand-in")
            mbtiles.write_bytes(b"tiny synthetic mbtiles stand-in")
            pmtiles.write_bytes(b"tiny synthetic pmtiles stand-in")

            def record(path: Path) -> dict:
                return {
                    "path": str(path),
                    "bytes": path.stat().st_size,
                    "sha256": goes.sha256_file(path),
                }

            frame = {
                "target_at": "2025-10-28T15:00:00Z",
                "at": "2025-10-28T15:00:20.500Z",
                "tiles": "pmtiles://https://tiles.example/storm.pmtiles/{z}/{x}/{y}",
                "source_object": {
                    **record(source),
                    "bucket": "noaa-goes19",
                    "key": REAL_KEY,
                    "etag": "71a295f69915397baabfd94bca366366",
                },
                "mbtiles": record(mbtiles),
                "pmtiles": {
                    **record(pmtiles),
                    "remote_key": "storm-imagery/al132025/frame.pmtiles",
                },
            }
            artifacts = {
                "schema": "lighthouse.goes-artifacts.v1",
                "config_sha256": goes.sha256_json(config),
                "storms": [
                    {"id": "al132025", "source": "NOAA GOES-19", "frames": [frame]}
                ],
            }
            artifacts_path = root / "artifacts.json"
            index_path = root / "index.json"
            artifacts_path.write_text(json.dumps(artifacts))
            index_path.write_text(
                json.dumps(goes.public_index_from_artifacts(artifacts))
            )
            fake_pmtiles = root / "pmtiles"
            fake_pmtiles.write_text("#!/bin/sh\nexit 0\n")
            os.chmod(fake_pmtiles, 0o755)

            report = goes.verify(
                config,
                artifacts_path=artifacts_path,
                public_index_path=index_path,
                pmtiles_bin=str(fake_pmtiles),
            )
            self.assertEqual(report["verified_frames"], 1)
            pmtiles.write_bytes(b"tampered")
            with self.assertRaisesRegex(goes.PipelineError, "checksum/size mismatch"):
                goes.verify(
                    config,
                    artifacts_path=artifacts_path,
                    public_index_path=index_path,
                    pmtiles_bin=str(fake_pmtiles),
                )


if __name__ == "__main__":
    unittest.main()
