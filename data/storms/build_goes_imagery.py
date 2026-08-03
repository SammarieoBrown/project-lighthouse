#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "certifi>=2025.1.31",
#   "h5netcdf>=1.6.1",
#   "h5py>=3.13.0",
#   "numpy>=2.2.0",
#   "Pillow>=11.1.0",
#   "pyproj>=3.7.0",
#   "rasterio>=1.4.3",
#   "xarray>=2025.1.2",
# ]
# ///
"""Build a small, provenance-pinned GOES-19 imagery set for storm replay.

Listing and verification use only the Python standard library. NetCDF and
geospatial dependencies are imported lazily for ``--build`` so unit tests and
``--dry-run`` do not need the large rendering stack.

This program never uploads data. It writes PMTiles locally and updates the
browser manifest only after every requested frame has built and verified.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import dataclasses
import datetime as dt
import hashlib
import io
import json
import math
import os
import re
import shutil
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence


UTC = dt.timezone.utc
SCRIPT_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = SCRIPT_DIR.parents[1]
DEFAULT_CONFIG = SCRIPT_DIR / "goes_scenes.json"
DEFAULT_CACHE = SCRIPT_DIR / "goes_cache"
DEFAULT_ARTIFACTS = SCRIPT_DIR / "goes_artifacts.json"
DEFAULT_PUBLIC_INDEX = (
    REPOSITORY_ROOT / "apps" / "console" / "public" / "storm-imagery" / "index.json"
)
S3_XML_NAMESPACE = "http://s3.amazonaws.com/doc/2006-03-01/"
WEB_MERCATOR_LIMIT = 85.0511287798066
WEB_MERCATOR_ORIGIN = 20_037_508.342789244
TILE_SIZE = 256

GOES_KEY_RE = re.compile(
    r"^ABI-L2-MCMIPF/(?P<year>\d{4})/(?P<doy>\d{3})/(?P<hour>\d{2})/"
    r"OR_ABI-L2-MCMIPF-M\d+_G19_"
    r"s(?P<start_year>\d{4})(?P<start_doy>\d{3})(?P<start_hour>\d{2})"
    r"(?P<start_minute>\d{2})(?P<start_second>\d{2})(?P<start_fraction>\d+)"
    r"_e\d+_c\d+\.nc$"
)


class PipelineError(RuntimeError):
    """A safe, user-actionable pipeline failure."""


@dataclasses.dataclass(frozen=True, slots=True)
class S3Object:
    key: str
    start_at: dt.datetime
    etag: str
    size: int
    last_modified: str

    def public_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "start_at": format_utc(self.start_at),
            "etag": self.etag,
            "bytes": self.size,
            "last_modified": self.last_modified,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class ResolvedFrame:
    storm_id: str
    source: str
    target_at: dt.datetime
    object: S3Object

    def public_dict(self) -> dict[str, Any]:
        return {
            "storm_id": self.storm_id,
            "source": self.source,
            "target_at": format_utc(self.target_at),
            "offset_seconds": round(
                abs((self.object.start_at - self.target_at).total_seconds()), 3
            ),
            "object": self.object.public_dict(),
        }


def parse_utc(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PipelineError(f"Invalid timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        raise PipelineError(f"Timestamp must include a timezone: {value!r}")
    return parsed.astimezone(UTC)


def format_utc(value: dt.datetime) -> str:
    value = value.astimezone(UTC)
    if value.microsecond:
        rendered = value.isoformat(timespec="milliseconds")
    else:
        rendered = value.isoformat(timespec="seconds")
    return rendered.replace("+00:00", "Z")


def _from_year_doy(
    year: int,
    doy: int,
    hour: int,
    minute: int,
    second: int,
    fraction: str = "",
) -> dt.datetime:
    if not 1 <= doy <= 366:
        raise ValueError(f"Invalid day-of-year {doy}")
    microsecond = int((fraction + "000000")[:6]) if fraction else 0
    return dt.datetime(year, 1, 1, tzinfo=UTC) + dt.timedelta(
        days=doy - 1,
        hours=hour,
        minutes=minute,
        seconds=second,
        microseconds=microsecond,
    )


def parse_goes_key_start(key: str) -> dt.datetime:
    """Read the observation start from an actual ABI MCMIPF object key."""

    match = GOES_KEY_RE.fullmatch(key)
    if match is None:
        raise ValueError(f"Not a GOES-19 ABI-L2-MCMIPF key: {key}")
    parts = match.groupdict()
    start = _from_year_doy(
        int(parts["start_year"]),
        int(parts["start_doy"]),
        int(parts["start_hour"]),
        int(parts["start_minute"]),
        int(parts["start_second"]),
        parts["start_fraction"],
    )
    if (
        int(parts["year"]),
        int(parts["doy"]),
        int(parts["hour"]),
    ) != (start.year, int(start.strftime("%j")), start.hour):
        raise ValueError(f"GOES key prefix and start timestamp disagree: {key}")
    return start


def parse_s3_listing(xml_bytes: bytes) -> tuple[list[S3Object], str | None]:
    """Parse one public S3 ListObjectsV2 page, ignoring unrelated objects."""

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise PipelineError("NOAA returned malformed S3 listing XML") from exc
    namespace = {"s3": S3_XML_NAMESPACE}
    objects: list[S3Object] = []
    for content in root.findall("s3:Contents", namespace):
        key = content.findtext("s3:Key", default="", namespaces=namespace)
        try:
            start_at = parse_goes_key_start(key)
        except ValueError:
            continue
        etag = content.findtext("s3:ETag", default="", namespaces=namespace).strip('"')
        size_text = content.findtext("s3:Size", default="", namespaces=namespace)
        modified = content.findtext("s3:LastModified", default="", namespaces=namespace)
        if not etag or not size_text.isdigit():
            raise PipelineError(f"Incomplete S3 metadata for {key}")
        objects.append(
            S3Object(
                key=key,
                start_at=start_at,
                etag=etag,
                size=int(size_text),
                last_modified=modified,
            )
        )
    token = root.findtext(
        "s3:NextContinuationToken", default=None, namespaces=namespace
    )
    return sorted(objects, key=lambda item: (item.start_at, item.key)), token


def hour_prefixes(
    target: dt.datetime,
    *,
    window_minutes: int,
    product: str = "ABI-L2-MCMIPF",
) -> list[str]:
    """Return every hourly S3 prefix intersecting the selection window."""

    if window_minutes < 0:
        raise ValueError("window_minutes must be non-negative")
    target = target.astimezone(UTC)
    start = target - dt.timedelta(minutes=window_minutes)
    end = target + dt.timedelta(minutes=window_minutes)
    cursor = start.replace(minute=0, second=0, microsecond=0)
    prefixes: list[str] = []
    while cursor <= end:
        prefixes.append(
            f"{product}/{cursor.year}/{cursor.strftime('%j')}/{cursor.hour:02d}/"
        )
        cursor += dt.timedelta(hours=1)
    return prefixes


def nearest_object(
    target: dt.datetime,
    candidates: Sequence[S3Object],
    *,
    max_offset: dt.timedelta,
) -> S3Object:
    if not candidates:
        raise PipelineError(f"No GOES objects found near {format_utc(target)}")
    selected = min(
        candidates,
        key=lambda item: (
            abs((item.start_at - target).total_seconds()),
            item.start_at,
            item.key,
        ),
    )
    offset = abs(selected.start_at - target)
    if offset > max_offset:
        raise PipelineError(
            f"Nearest GOES object is {offset.total_seconds():.1f}s from "
            f"{format_utc(target)}, beyond the {max_offset.total_seconds():.0f}s limit"
        )
    return selected


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi  # type: ignore[import-not-found]

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def list_noaa_prefix(
    bucket: str,
    prefix: str,
    *,
    timeout: float = 30.0,
) -> list[S3Object]:
    """List an hourly NOAA public-bucket prefix without AWS credentials."""

    endpoint = f"https://{bucket}.s3.amazonaws.com/"
    token: str | None = None
    result: list[S3Object] = []
    while True:
        query: dict[str, str] = {"list-type": "2", "prefix": prefix}
        if token:
            query["continuation-token"] = token
        url = endpoint + "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Project-Lighthouse-GOES-builder/1"},
        )
        try:
            with urllib.request.urlopen(
                request, timeout=timeout, context=_ssl_context()
            ) as response:
                page, token = parse_s3_listing(response.read())
        except OSError as exc:
            raise PipelineError(f"Could not list NOAA prefix {prefix}: {exc}") from exc
        result.extend(page)
        if not token:
            break
    return sorted(result, key=lambda item: (item.start_at, item.key))


def list_noaa_prefixes(
    bucket: str,
    prefixes: Iterable[str],
    *,
    timeout: float = 30.0,
) -> dict[str, list[S3Object]]:
    unique = sorted(set(prefixes))
    if not unique:
        return {}
    results: dict[str, list[S3Object]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(unique))) as pool:
        futures = {
            pool.submit(list_noaa_prefix, bucket, prefix, timeout=timeout): prefix
            for prefix in unique
        }
        for future in concurrent.futures.as_completed(futures):
            prefix = futures[future]
            results[prefix] = future.result()
    return {prefix: results[prefix] for prefix in unique}


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PipelineError(f"Missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PipelineError(f"Invalid JSON in {path}: {exc}") from exc


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(value, indent=2, sort_keys=False) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def validate_config(config: Any) -> dict[str, Any]:
    if (
        not isinstance(config, dict)
        or config.get("schema") != "lighthouse.goes-scenes.v1"
    ):
        raise PipelineError("GOES config must use schema lighthouse.goes-scenes.v1")
    if config.get("bucket") != "noaa-goes19":
        raise PipelineError(
            "This curated pipeline is locked to the official noaa-goes19 bucket"
        )
    if config.get("product") != "ABI-L2-MCMIPF" or config.get("satellite") != "G19":
        raise PipelineError("Only GOES-19 ABI-L2-MCMIPF is supported")
    bbox = config.get("bbox")
    if (
        not isinstance(bbox, list)
        or len(bbox) != 4
        or not all(isinstance(value, (int, float)) for value in bbox)
        or not (-180 <= bbox[0] < bbox[2] <= 180)
        or not (-90 <= bbox[1] < bbox[3] <= 90)
    ):
        raise PipelineError("bbox must be [west, south, east, north]")
    minzoom = config.get("minzoom")
    maxzoom = config.get("maxzoom")
    if (
        not isinstance(minzoom, int)
        or not isinstance(maxzoom, int)
        or not 0 <= minzoom <= maxzoom <= 14
    ):
        raise PipelineError(
            "minzoom/maxzoom must be ordered integers from 0 through 14"
        )
    storms = config.get("storms")
    if not isinstance(storms, list) or not storms:
        raise PipelineError("GOES config must contain at least one curated storm")
    seen_ids: set[str] = set()
    for storm in storms:
        if not isinstance(storm, dict):
            raise PipelineError("Each storm config must be an object")
        storm_id = storm.get("id")
        if not isinstance(storm_id, str) or not re.fullmatch(r"al\d{6}", storm_id):
            raise PipelineError(f"Invalid Atlantic storm id: {storm_id!r}")
        if storm_id in seen_ids:
            raise PipelineError(f"Duplicate storm id: {storm_id}")
        seen_ids.add(storm_id)
        if storm.get("source") != "NOAA GOES-19":
            raise PipelineError(f"{storm_id} must identify its source as NOAA GOES-19")
        frames = storm.get("frames")
        if not isinstance(frames, list) or not frames:
            raise PipelineError(f"{storm_id} must contain at least one frame")
        parsed_times = [parse_utc(frame.get("target_at", "")) for frame in frames]
        if parsed_times != sorted(set(parsed_times)):
            raise PipelineError(
                f"{storm_id} frame times must be unique and chronological"
            )
    return config


def select_storms(
    config: Mapping[str, Any], storm_id: str | None
) -> list[dict[str, Any]]:
    storms = list(config["storms"])
    if storm_id is None:
        return storms
    selected = [storm for storm in storms if storm["id"].lower() == storm_id.lower()]
    if not selected:
        raise PipelineError(
            f"Storm {storm_id!r} is not in the curated GOES-19 set; no coverage is assumed"
        )
    return selected


def resolve_frames(
    config: Mapping[str, Any],
    storms: Sequence[Mapping[str, Any]],
    *,
    window_minutes: int,
    timeout: float,
) -> tuple[list[ResolvedFrame], dict[str, list[S3Object]]]:
    targets = [
        parse_utc(frame["target_at"]) for storm in storms for frame in storm["frames"]
    ]
    prefixes = [
        prefix
        for target in targets
        for prefix in hour_prefixes(
            target,
            window_minutes=window_minutes,
            product=config["product"],
        )
    ]
    listings = list_noaa_prefixes(config["bucket"], prefixes, timeout=timeout)
    resolved: list[ResolvedFrame] = []
    for storm in storms:
        for frame in storm["frames"]:
            target = parse_utc(frame["target_at"])
            relevant_prefixes = hour_prefixes(
                target,
                window_minutes=window_minutes,
                product=config["product"],
            )
            candidates = [
                item
                for prefix in relevant_prefixes
                for item in listings.get(prefix, [])
            ]
            selected = nearest_object(
                target,
                candidates,
                max_offset=dt.timedelta(minutes=window_minutes),
            )
            resolved.append(
                ResolvedFrame(
                    storm_id=storm["id"],
                    source=storm["source"],
                    target_at=target,
                    object=selected,
                )
            )
    return resolved, listings


def _portable_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError:
        return str(path.resolve())


def _resolve_recorded_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPOSITORY_ROOT / path


def _recorded_file(path: Path) -> dict[str, Any]:
    return {
        "path": _portable_path(path),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _matching_source_record(
    artifacts: Mapping[str, Any], obj: S3Object
) -> Mapping[str, Any] | None:
    for storm in artifacts.get("storms", []):
        for frame in storm.get("frames", []):
            source = frame.get("source_object", {})
            if (
                source.get("key") == obj.key
                and source.get("etag") == obj.etag
                and source.get("bytes") == obj.size
            ):
                return source
    return None


def _record_is_valid_file(record: Mapping[str, Any]) -> bool:
    path_value = record.get("path")
    expected_sha = record.get("sha256")
    expected_bytes = record.get("bytes")
    if not isinstance(path_value, str) or not isinstance(expected_sha, str):
        return False
    path = _resolve_recorded_path(path_value)
    return (
        path.is_file()
        and path.stat().st_size == expected_bytes
        and sha256_file(path) == expected_sha
    )


def download_source(
    bucket: str,
    obj: S3Object,
    destination: Path,
    *,
    prior_record: Mapping[str, Any] | None,
    timeout: float,
    force: bool,
) -> dict[str, Any]:
    if not force and prior_record and _record_is_valid_file(prior_record):
        return dict(prior_record)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".partial")
    with contextlib.suppress(FileNotFoundError):
        temporary.unlink()
    url = f"https://{bucket}.s3.amazonaws.com/{urllib.parse.quote(obj.key, safe='/')}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Project-Lighthouse-GOES-builder/1"},
    )
    digest = hashlib.sha256()
    byte_count = 0
    try:
        with urllib.request.urlopen(
            request, timeout=timeout, context=_ssl_context()
        ) as response, temporary.open("wb") as output:
            response_etag = response.headers.get("ETag", "").strip('"')
            if response_etag and response_etag != obj.etag:
                raise PipelineError(
                    f"NOAA ETag changed for {obj.key}: {obj.etag} -> {response_etag}"
                )
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                digest.update(chunk)
                byte_count += len(chunk)
            output.flush()
            os.fsync(output.fileno())
        if byte_count != obj.size:
            raise PipelineError(
                f"NOAA size mismatch for {obj.key}: listed {obj.size}, downloaded {byte_count}"
            )
        os.replace(temporary, destination)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
    return {
        "bucket": bucket,
        "key": obj.key,
        "etag": obj.etag,
        "bytes": byte_count,
        "sha256": digest.hexdigest(),
        "path": _portable_path(destination),
        "observed_at": format_utc(obj.start_at),
    }


def lonlat_to_tile(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    lat = max(-WEB_MERCATOR_LIMIT, min(WEB_MERCATOR_LIMIT, lat))
    scale = 1 << zoom
    x = (lon + 180.0) / 360.0 * scale
    latitude_radians = math.radians(lat)
    y = (1.0 - math.asinh(math.tan(latitude_radians)) / math.pi) / 2.0 * scale
    return x, y


def tile_range_for_bbox(bbox: Sequence[float], zoom: int) -> tuple[range, range]:
    west, south, east, north = bbox
    northwest_x, northwest_y = lonlat_to_tile(west, north, zoom)
    southeast_x, southeast_y = lonlat_to_tile(east, south, zoom)
    scale = 1 << zoom
    x_start = max(0, min(scale - 1, math.floor(northwest_x)))
    x_stop = max(0, min(scale - 1, math.ceil(southeast_x) - 1))
    y_start = max(0, min(scale - 1, math.floor(northwest_y)))
    y_stop = max(0, min(scale - 1, math.ceil(southeast_y) - 1))
    return range(x_start, x_stop + 1), range(y_start, y_stop + 1)


def xyz_to_tms_row(y: int, zoom: int) -> int:
    return (1 << zoom) - 1 - y


def tile_bounds_mercator(
    x: int, y: int, zoom: int
) -> tuple[float, float, float, float]:
    scale = 1 << zoom
    span = 2 * WEB_MERCATOR_ORIGIN / scale
    left = -WEB_MERCATOR_ORIGIN + x * span
    right = left + span
    top = WEB_MERCATOR_ORIGIN - y * span
    bottom = top - span
    return left, bottom, right, top


def _bbox_perimeter(
    bbox: Sequence[float], points_per_edge: int = 33
) -> Iterator[tuple[float, float]]:
    west, south, east, north = bbox
    for index in range(points_per_edge):
        ratio = index / (points_per_edge - 1)
        longitude = west + (east - west) * ratio
        latitude = south + (north - south) * ratio
        yield longitude, south
        yield longitude, north
        yield west, latitude
        yield east, latitude


def _render_rgba(red: Any, blue: Any, veggie: Any, infrared: Any, np: Any) -> Any:
    """NOAA-style true colour by day, channel-13 cloud brightness by night."""

    red_f = np.clip(np.asarray(red, dtype="float32"), 0.0, 1.0)
    blue_f = np.clip(np.asarray(blue, dtype="float32"), 0.0, 1.0)
    veggie_f = np.clip(np.asarray(veggie, dtype="float32"), 0.0, 1.0)
    green_f = np.clip(0.45 * red_f + 0.10 * veggie_f + 0.45 * blue_f, 0.0, 1.0)
    visible = np.stack((red_f, green_f, blue_f), axis=-1)
    visible = np.sqrt(visible)

    infrared_f = np.asarray(infrared, dtype="float32")
    cloud = np.clip((305.0 - infrared_f) / 115.0, 0.0, 1.0)
    infrared_rgb = np.stack((cloud * 0.88, cloud * 0.93, cloud), axis=-1)
    visible_signal = np.nanmean(np.stack((red_f, green_f, blue_f)), axis=0)
    night_weight = np.clip((0.055 - visible_signal) / 0.055, 0.0, 1.0)[..., None]
    rgb = visible * (1.0 - night_weight) + infrared_rgb * night_weight

    valid = np.isfinite(infrared_f) | np.any(np.isfinite(visible), axis=-1)
    rgb = np.nan_to_num(rgb, nan=0.0, posinf=1.0, neginf=0.0)
    rgba = np.empty((*infrared_f.shape, 4), dtype="uint8")
    rgba[..., :3] = np.rint(np.clip(rgb, 0.0, 1.0) * 255.0).astype("uint8")
    rgba[..., 3] = np.where(valid, 255, 0).astype("uint8")
    return rgba


def load_geostationary_rgba(
    source_path: Path, bbox: Sequence[float]
) -> tuple[Any, Any, Any]:
    """Load and crop MCMIPF channels, returning RGBA, affine, and GEOS CRS."""

    try:
        import numpy as np  # type: ignore[import-not-found]
        import xarray as xr  # type: ignore[import-not-found]
        from pyproj import CRS, Transformer  # type: ignore[import-not-found]
        from rasterio import Affine  # type: ignore[import-not-found]
    except ImportError as exc:
        raise PipelineError(
            "Rendering dependencies are missing; run this script with `uv run --script`"
        ) from exc

    with xr.open_dataset(
        source_path,
        engine="h5netcdf",
        decode_cf=True,
        mask_and_scale=True,
    ) as dataset:
        required = {
            "CMI_C01",
            "CMI_C02",
            "CMI_C03",
            "CMI_C13",
            "x",
            "y",
            "goes_imager_projection",
        }
        missing = sorted(required.difference(dataset.variables))
        if missing:
            raise PipelineError(
                f"{source_path.name} lacks GOES variables: {', '.join(missing)}"
            )
        projection_attrs = dict(dataset["goes_imager_projection"].attrs)
        try:
            source_crs = CRS.from_cf(projection_attrs)
            height = float(projection_attrs["perspective_point_height"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PipelineError(
                f"Invalid GOES projection metadata in {source_path}"
            ) from exc
        transformer = Transformer.from_crs("EPSG:4326", source_crs, always_xy=True)
        projected = [
            transformer.transform(lon, lat) for lon, lat in _bbox_perimeter(bbox)
        ]
        finite = [(x, y) for x, y in projected if math.isfinite(x) and math.isfinite(y)]
        if not finite:
            raise PipelineError("Configured bbox is outside the GOES-19 view")
        projected_x = [point[0] for point in finite]
        projected_y = [point[1] for point in finite]
        all_x = np.asarray(dataset["x"].values, dtype="float64") * height
        all_y = np.asarray(dataset["y"].values, dtype="float64") * height
        if all_x.ndim != 1 or all_y.ndim != 1 or len(all_x) < 2 or len(all_y) < 2:
            raise PipelineError("Unexpected GOES x/y coordinate arrays")
        margin_x = abs(float(np.median(np.diff(all_x)))) * 2
        margin_y = abs(float(np.median(np.diff(all_y)))) * 2
        x_indices = np.flatnonzero(
            (all_x >= min(projected_x) - margin_x)
            & (all_x <= max(projected_x) + margin_x)
        )
        y_indices = np.flatnonzero(
            (all_y >= min(projected_y) - margin_y)
            & (all_y <= max(projected_y) + margin_y)
        )
        if not len(x_indices) or not len(y_indices):
            raise PipelineError("GOES grid does not intersect the configured bbox")
        x_slice = slice(
            max(0, int(x_indices[0]) - 1), min(len(all_x), int(x_indices[-1]) + 2)
        )
        y_slice = slice(
            max(0, int(y_indices[0]) - 1), min(len(all_y), int(y_indices[-1]) + 2)
        )
        selection = {"x": x_slice, "y": y_slice}
        red = dataset["CMI_C02"].isel(selection).values
        blue = dataset["CMI_C01"].isel(selection).values
        veggie = dataset["CMI_C03"].isel(selection).values
        infrared = dataset["CMI_C13"].isel(selection).values
        selected_x = all_x[x_slice]
        selected_y = all_y[y_slice]

    rgba = _render_rgba(red, blue, veggie, infrared, np)
    if selected_x[1] < selected_x[0]:
        selected_x = selected_x[::-1]
        rgba = rgba[:, ::-1, :]
    if selected_y[1] > selected_y[0]:
        selected_y = selected_y[::-1]
        rgba = rgba[::-1, :, :]
    pixel_width = float(np.median(np.diff(selected_x)))
    pixel_height = float(np.median(np.diff(selected_y)))
    transform = Affine(
        pixel_width,
        0.0,
        float(selected_x[0] - pixel_width / 2),
        0.0,
        pixel_height,
        float(selected_y[0] - pixel_height / 2),
    )
    return rgba, transform, source_crs


def _png_bytes(rgba: Any) -> bytes:
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError as exc:
        raise PipelineError("Pillow is required to encode raster tiles") from exc
    output = io.BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(
        output, format="PNG", optimize=False, compress_level=9
    )
    return output.getvalue()


def build_mbtiles(
    source_path: Path,
    destination: Path,
    *,
    bbox: Sequence[float],
    minzoom: int,
    maxzoom: int,
    name: str,
) -> int:
    try:
        import numpy as np  # type: ignore[import-not-found]
        from rasterio.enums import Resampling  # type: ignore[import-not-found]
        from rasterio.transform import from_bounds  # type: ignore[import-not-found]
        from rasterio.warp import reproject  # type: ignore[import-not-found]
    except ImportError as exc:
        raise PipelineError(
            "Rendering dependencies are missing; run this script with `uv run --script`"
        ) from exc

    source_rgba, source_transform, source_crs = load_geostationary_rgba(
        source_path, bbox
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.stem + ".partial.mbtiles")
    with contextlib.suppress(FileNotFoundError):
        temporary.unlink()
    connection = sqlite3.connect(temporary)
    tile_count = 0
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA page_size=4096")
        connection.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
        connection.execute(
            "CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB, PRIMARY KEY (zoom_level, tile_column, tile_row))"
        )
        metadata = {
            "attribution": "NOAA/NESDIS/STAR GOES-19",
            "bounds": ",".join(f"{float(value):.6f}" for value in bbox),
            "description": "Curated GOES-19 ABI multichannel imagery for Project Lighthouse",
            "format": "png",
            "maxzoom": str(maxzoom),
            "minzoom": str(minzoom),
            "name": name,
            "type": "overlay",
            "version": "1.0",
        }
        connection.executemany(
            "INSERT INTO metadata(name, value) VALUES (?, ?)", sorted(metadata.items())
        )
        for zoom in range(minzoom, maxzoom + 1):
            x_range, y_range = tile_range_for_bbox(bbox, zoom)
            for x in x_range:
                for y in y_range:
                    bounds = tile_bounds_mercator(x, y, zoom)
                    destination_transform = from_bounds(*bounds, TILE_SIZE, TILE_SIZE)
                    rgb = np.zeros((3, TILE_SIZE, TILE_SIZE), dtype="uint8")
                    alpha = np.zeros((TILE_SIZE, TILE_SIZE), dtype="uint8")
                    reproject(
                        source=source_rgba[..., :3].transpose(2, 0, 1),
                        destination=rgb,
                        src_transform=source_transform,
                        src_crs=source_crs,
                        dst_transform=destination_transform,
                        dst_crs="EPSG:3857",
                        resampling=Resampling.bilinear,
                    )
                    reproject(
                        source=source_rgba[..., 3],
                        destination=alpha,
                        src_transform=source_transform,
                        src_crs=source_crs,
                        dst_transform=destination_transform,
                        dst_crs="EPSG:3857",
                        resampling=Resampling.nearest,
                    )
                    if not alpha.any():
                        continue
                    tile = np.empty((TILE_SIZE, TILE_SIZE, 4), dtype="uint8")
                    tile[..., :3] = rgb.transpose(1, 2, 0)
                    tile[..., 3] = alpha
                    connection.execute(
                        "INSERT INTO tiles VALUES (?, ?, ?, ?)",
                        (zoom, x, xyz_to_tms_row(y, zoom), _png_bytes(tile)),
                    )
                    tile_count += 1
        connection.commit()
        connection.execute("VACUUM")
    except BaseException:
        connection.close()
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        raise
    else:
        connection.close()
        os.replace(temporary, destination)
    if tile_count == 0:
        raise PipelineError(f"No visible tiles were rendered from {source_path}")
    return tile_count


def pmtiles_identity(executable: str) -> dict[str, str]:
    resolved = (
        shutil.which(executable) if not Path(executable).is_file() else executable
    )
    if not resolved:
        raise PipelineError("The current pmtiles CLI is required on PATH")
    result = subprocess.run(
        [resolved, "version"], text=True, capture_output=True, check=False
    )
    version = (result.stdout or result.stderr).strip()
    if result.returncode != 0:
        raise PipelineError(f"Could not identify pmtiles CLI: {version}")
    return {
        "path": str(Path(resolved).resolve()),
        "version": version,
        "sha256": sha256_file(Path(resolved)),
    }


def convert_pmtiles(mbtiles: Path, destination: Path, executable: str) -> None:
    resolved = (
        shutil.which(executable) if not Path(executable).is_file() else executable
    )
    if not resolved:
        raise PipelineError("The current pmtiles CLI is required on PATH")
    temporary = destination.with_name(destination.stem + ".partial.pmtiles")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        temporary.unlink()
    convert = subprocess.run(
        [resolved, "convert", str(mbtiles), str(temporary)],
        text=True,
        capture_output=True,
        check=False,
    )
    if convert.returncode != 0:
        raise PipelineError(
            f"pmtiles convert failed for {mbtiles}: {(convert.stderr or convert.stdout).strip()}"
        )
    verify = subprocess.run(
        [resolved, "verify", str(temporary)],
        text=True,
        capture_output=True,
        check=False,
    )
    if verify.returncode != 0:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        raise PipelineError(
            f"pmtiles verify failed for {temporary}: {(verify.stderr or verify.stdout).strip()}"
        )
    os.replace(temporary, destination)


def _frame_stamp(value: dt.datetime) -> str:
    value = value.astimezone(UTC)
    base = value.strftime("%Y%m%dT%H%M%S")
    if value.microsecond:
        base += f"{value.microsecond // 1000:03d}"
    return base + "Z"


def public_index_from_artifacts(artifacts: Mapping[str, Any]) -> dict[str, Any]:
    storms = []
    for storm in artifacts.get("storms", []):
        frames = [
            {"at": frame["at"], "tiles": frame["tiles"]}
            for frame in sorted(storm.get("frames", []), key=lambda item: item["at"])
        ]
        storms.append({"id": storm["id"], "source": storm["source"], "frames": frames})
    return {"storms": sorted(storms, key=lambda item: item["id"])}


def build(
    config: Mapping[str, Any],
    resolved_frames: Sequence[ResolvedFrame],
    *,
    cache_dir: Path,
    artifacts_path: Path,
    public_index_path: Path,
    tiles_base: str,
    timeout: float,
    force: bool,
    pmtiles_bin: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"https://[^\s/]+(?:/[^\s]*)?", tiles_base):
        raise PipelineError("--tiles-base must be an absolute HTTPS public base URL")
    tiles_base = tiles_base.rstrip("/")
    existing: Mapping[str, Any] = {"storms": []}
    if artifacts_path.exists():
        loaded = read_json(artifacts_path)
        if isinstance(loaded, dict):
            existing = loaded
    cli = pmtiles_identity(pmtiles_bin)
    storm_records: dict[str, dict[str, Any]] = {}
    for frame in resolved_frames:
        source_name = Path(frame.object.key).name
        source_path = cache_dir / "source" / source_name
        source_record = download_source(
            config["bucket"],
            frame.object,
            source_path,
            prior_record=_matching_source_record(existing, frame.object),
            timeout=timeout,
            force=force,
        )
        stamp = _frame_stamp(frame.object.start_at)
        output_dir = cache_dir / "artifacts" / frame.storm_id
        mbtiles = output_dir / f"{stamp}.mbtiles"
        pmtiles = output_dir / f"{stamp}.pmtiles"
        tile_count = build_mbtiles(
            source_path,
            mbtiles,
            bbox=config["bbox"],
            minzoom=config["minzoom"],
            maxzoom=config["maxzoom"],
            name=f"{frame.storm_id} {format_utc(frame.object.start_at)}",
        )
        convert_pmtiles(mbtiles, pmtiles, pmtiles_bin)
        remote_key = f"storm-imagery/{frame.storm_id}/{pmtiles.name}"
        record = {
            "target_at": format_utc(frame.target_at),
            "at": format_utc(frame.object.start_at),
            "tiles": f"pmtiles://{tiles_base}/{remote_key}/{{z}}/{{x}}/{{y}}",
            "source_object": source_record,
            "render": {
                "bbox": list(config["bbox"]),
                "minzoom": config["minzoom"],
                "maxzoom": config["maxzoom"],
                "recipe": "C02/C01/C03 synthetic-green true-colour plus C13 night cloud brightness",
                "tile_count": tile_count,
            },
            "mbtiles": _recorded_file(mbtiles),
            "pmtiles": {**_recorded_file(pmtiles), "remote_key": remote_key},
        }
        storm_record = storm_records.setdefault(
            frame.storm_id,
            {"id": frame.storm_id, "source": frame.source, "frames": []},
        )
        storm_record["frames"].append(record)
    artifacts = {
        "schema": "lighthouse.goes-artifacts.v1",
        "config_sha256": sha256_json(config),
        "pmtiles_cli": cli,
        "storms": sorted(storm_records.values(), key=lambda item: item["id"]),
    }
    for storm in artifacts["storms"]:
        storm["frames"].sort(key=lambda item: item["at"])
    public_index = public_index_from_artifacts(artifacts)
    atomic_write_json(artifacts_path, artifacts)
    atomic_write_json(public_index_path, public_index)
    return artifacts


def verify(
    config: Mapping[str, Any],
    *,
    artifacts_path: Path,
    public_index_path: Path,
    pmtiles_bin: str,
) -> dict[str, Any]:
    artifacts = read_json(artifacts_path)
    if (
        not isinstance(artifacts, dict)
        or artifacts.get("schema") != "lighthouse.goes-artifacts.v1"
    ):
        raise PipelineError("Artifact manifest has the wrong schema")
    if artifacts.get("config_sha256") != sha256_json(config):
        raise PipelineError("Artifact manifest was built from a different GOES config")
    frames = [
        frame
        for storm in artifacts.get("storms", [])
        for frame in storm.get("frames", [])
    ]
    if not frames:
        raise PipelineError(
            "No GOES artifacts have been built; the empty public index is intentional"
        )
    resolved_pmtiles = (
        shutil.which(pmtiles_bin) if not Path(pmtiles_bin).is_file() else pmtiles_bin
    )
    if not resolved_pmtiles:
        raise PipelineError("The current pmtiles CLI is required on PATH")
    checked: list[str] = []
    for frame in frames:
        source = frame.get("source_object", {})
        if not source.get("key") or not source.get("etag") or not source.get("sha256"):
            raise PipelineError("Frame source provenance is incomplete")
        for field in ("source_object", "mbtiles", "pmtiles"):
            record = frame.get(field, {})
            if not _record_is_valid_file(record):
                raise PipelineError(
                    f"Artifact checksum/size mismatch: {record.get('path', field)}"
                )
        pmtiles_path = _resolve_recorded_path(frame["pmtiles"]["path"])
        result = subprocess.run(
            [resolved_pmtiles, "verify", str(pmtiles_path)],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise PipelineError(
                f"pmtiles verify failed for {pmtiles_path}: {(result.stderr or result.stdout).strip()}"
            )
        checked.append(frame["pmtiles"]["path"])
    expected_public = public_index_from_artifacts(artifacts)
    if read_json(public_index_path) != expected_public:
        raise PipelineError(
            "Browser imagery index does not match the artifact manifest"
        )
    return {"verified_frames": len(frames), "pmtiles": checked}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    modes = result.add_mutually_exclusive_group()
    modes.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve nearest objects; write nothing (default)",
    )
    modes.add_argument(
        "--list",
        action="store_true",
        help="show all candidate objects in each relevant hour",
    )
    modes.add_argument(
        "--build",
        action="store_true",
        help="download, render, package, and atomically write manifests",
    )
    modes.add_argument(
        "--verify",
        action="store_true",
        help="verify local checksums, PMTiles, and browser manifest",
    )
    result.add_argument("--storm", help="one curated Atlantic id, for example al132025")
    result.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    result.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    result.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS)
    result.add_argument("--public-index", type=Path, default=DEFAULT_PUBLIC_INDEX)
    result.add_argument(
        "--tiles-base", help="public HTTPS base used in generated pmtiles:// URLs"
    )
    result.add_argument("--max-offset-minutes", type=int, default=12)
    result.add_argument("--network-timeout", type=float, default=45.0)
    result.add_argument("--pmtiles-bin", default="pmtiles")
    result.add_argument(
        "--force", action="store_true", help="redownload sources before rebuilding"
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        config = validate_config(read_json(arguments.config))
        storms = select_storms(config, arguments.storm)
        if arguments.verify:
            report = verify(
                config,
                artifacts_path=arguments.artifacts,
                public_index_path=arguments.public_index,
                pmtiles_bin=arguments.pmtiles_bin,
            )
            print(json.dumps(report, indent=2))
            return 0
        if arguments.max_offset_minutes < 1:
            raise PipelineError("--max-offset-minutes must be positive")
        resolved, listings = resolve_frames(
            config,
            storms,
            window_minutes=arguments.max_offset_minutes,
            timeout=arguments.network_timeout,
        )
        if arguments.list:
            payload = {
                "bucket": config["bucket"],
                "prefixes": [
                    {
                        "prefix": prefix,
                        "objects": [item.public_dict() for item in objects],
                    }
                    for prefix, objects in listings.items()
                ],
            }
            print(json.dumps(payload, indent=2))
            return 0
        if arguments.build:
            if not arguments.tiles_base:
                raise PipelineError("--build requires --tiles-base")
            artifacts = build(
                config,
                resolved,
                cache_dir=arguments.cache,
                artifacts_path=arguments.artifacts,
                public_index_path=arguments.public_index,
                tiles_base=arguments.tiles_base,
                timeout=arguments.network_timeout,
                force=arguments.force,
                pmtiles_bin=arguments.pmtiles_bin,
            )
            print(json.dumps(artifacts, indent=2))
            return 0
        print(
            json.dumps(
                {
                    "mode": "dry-run",
                    "writes": False,
                    "estimated_source_bytes": sum(
                        frame.object.size for frame in resolved
                    ),
                    "frames": [frame.public_dict() for frame in resolved],
                },
                indent=2,
            )
        )
        return 0
    except PipelineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
