"""Which storms can be simulated, and how honestly.

1,991 Atlantic storms are in the archive. Most never came near Jamaica, and of
those that did, many predate the fields a wind field model needs. This module
answers two questions and refuses to blur them: *did this storm affect Jamaica*,
and *how much of what we would draw is measured rather than inferred*.

That second question is the whole reason this file exists. Gilbert 1988 has
measured wind radii, because CIRA digitised them. Allen 1980 does not, and
never will — a wind field for Allen would be entirely our model's invention.
Both can be simulated. Only one should say "measured" on the screen.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from math import asin, cos, radians, sin, sqrt
from pathlib import Path
from typing import Any

from app.storms.tracks import CACHE, EBTRK, HURDAT2, StormTrack, load_tracks

#: Jamaica's centre. Distances are to this point, not to the coastline — an
#: approximation worth about 80 km, which is inside the width of a wind field
#: and far inside the uncertainty of a 1950s track.
JAMAICA = (18.11, -77.30)

#: A storm passing within this distance had, or could have had, a wind field
#: over the island. The 34 kt radius of a large hurricane reaches 250 nm
#: (460 km) on its strong side, so anything closer than this is a candidate and
#: anything further is not.
NEAR_KM = 500.0

#: Below this the storm is not a hurricane and the impact lookup returns NONE
#: for every roof type, so it would replay as a flat line.
MIN_PEAK_KT = 64

EARTH_KM = 6371.0


def _haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(radians, (a[0], a[1], b[0], b[1]))
    h = (
        sin((lat2 - lat1) / 2) ** 2
        + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_KM * asin(sqrt(h))


@dataclass(frozen=True)
class StormSummary:
    """Enough to choose a storm, and enough to know what you are choosing."""

    storm_id: str
    name: str
    year: int
    peak_wind_kt: int
    min_pressure_mb: int | None
    closest_km: float
    #: Wind at the moment it was nearest Jamaica — the number that decides
    #: whether this storm is remembered here.
    wind_at_closest_kt: int
    points: int
    #: True when a source published quadrant radii for at least one point.
    #: False means every wind field we draw comes from the parametric model.
    radii_measured: bool
    #: Same question for radius of maximum wind.
    rmw_measured: bool
    #: Positions capable of producing at least a 34 kt wind field.
    wind_field_points: int
    #: Those positions with every applicable threshold complete in the archive.
    fully_measured_extent_points: int

    @property
    def label(self) -> str:
        return f"{self.name.title()} {self.year}"

    @property
    def provenance(self) -> str:
        """What the screen must say about this storm's size."""
        if self.wind_field_points == 0:
            return "unavailable"
        if self.fully_measured_extent_points == self.wind_field_points:
            return "measured"
        if self.radii_measured:
            return "mixed"
        return "modelled"


def _radii_coverage(track: StormTrack) -> tuple[int, int]:
    wind_field_points = 0
    complete_points = 0
    for position in track.positions:
        vmax = max(0, position.max_wind_kt or 0)
        expected = [threshold for threshold in (34, 50, 64) if threshold < vmax]
        if not expected:
            continue
        wind_field_points += 1
        source = {radii.threshold_kt: radii for radii in position.radii}
        if all(
            threshold in source and source[threshold].is_complete
            for threshold in expected
        ):
            complete_points += 1
    return wind_field_points, complete_points


def _closest(track: StormTrack) -> tuple[float, int]:
    best_km = float("inf")
    best_kt = 0
    for position in track.positions:
        km = _haversine((position.lat, position.lon), JAMAICA)
        if km < best_km:
            best_km, best_kt = km, position.max_wind_kt or 0
    return best_km, best_kt


def summarise(track: StormTrack) -> StormSummary:
    closest_km, wind_kt = _closest(track)
    wind_field_points, complete_points = _radii_coverage(track)
    return StormSummary(
        storm_id=track.storm_id,
        name=track.name,
        year=track.year,
        peak_wind_kt=track.peak_wind_kt,
        min_pressure_mb=track.min_pressure_mb,
        closest_km=round(closest_km, 1),
        wind_at_closest_kt=wind_kt,
        points=len(track.positions),
        radii_measured=track.has_radii(),
        rmw_measured=any(r for r in track.rmw_nm),
        wind_field_points=wind_field_points,
        fully_measured_extent_points=complete_points,
    )


@lru_cache(maxsize=1)
def _all() -> dict[str, StormTrack]:
    return load_tracks()


def catalogue(
    *, near_km: float = NEAR_KM, min_peak_kt: int = MIN_PEAK_KT
) -> list[StormSummary]:
    """Every storm that reached hurricane strength and passed near Jamaica.

    Ordered by proximity of the storm centre to Jamaica, then by the source
    wind at that closest fix. This is a transparent discovery order, not a
    claim about wind experienced on the island: local impact requires the wind
    field pipeline. The storm id is a final tie-breaker so pinned inputs produce
    stable bytes.
    """
    out = [
        summary
        for track in _all().values()
        if track.peak_wind_kt >= min_peak_kt
        for summary in (summarise(track),)
        if summary.closest_km <= near_km
    ]
    return sorted(
        out,
        key=lambda summary: (
            summary.closest_km,
            -summary.wind_at_closest_kt,
            summary.storm_id,
        ),
    )


def load(storm_id: str) -> StormTrack:
    track = _all().get(storm_id.upper())
    if track is None:
        raise KeyError(f"no storm {storm_id!r} in the Atlantic archive")
    return track


def find(name: str, year: int) -> StormTrack:
    """By the name a person would use. Storm names repeat across decades."""
    wanted = name.strip().upper()
    for track in _all().values():
        if track.name.upper() == wanted and track.year == year:
            return track
    raise KeyError(f"no Atlantic storm named {name!r} in {year}")


CATALOGUE_SCHEMA = "lighthouse.storm-catalogue.v1"
TRACKS_SCHEMA = "lighthouse.storm-track-library.v1"


class CatalogueError(RuntimeError):
    pass


def archive_provenance() -> list[dict[str, str]]:
    """Verify the two source bytes against the committed manifest."""
    manifest = CACHE / "manifest.sha256"
    try:
        expected = {
            name: digest
            for line in manifest.read_text().splitlines()
            if line.strip()
            for digest, name in [line.split(maxsplit=1)]
        }
    except (OSError, ValueError) as exc:
        raise CatalogueError(
            f"cannot read storm archive manifest {manifest}: {exc}"
        ) from exc

    sources: list[dict[str, str]] = []
    for path, authority in (
        (HURDAT2, "NHC HURDAT2"),
        (EBTRK, "CIRA Extended Best Track"),
    ):
        pinned = expected.get(path.name)
        if pinned is None:
            raise CatalogueError(f"{path.name} is not pinned in {manifest}")
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise CatalogueError(f"cannot read storm archive {path}: {exc}") from exc
        if actual != pinned:
            raise CatalogueError(
                f"storm archive {path.name} has SHA-256 {actual}, expected {pinned}"
            )
        sources.append(
            {
                "authority": authority,
                "file": path.name,
                "sha256": actual,
            }
        )
    return sources


def catalogue_document(
    *, near_km: float = NEAR_KM, min_peak_kt: int = MIN_PEAK_KT
) -> dict[str, Any]:
    """A deterministic, machine-readable catalogue contract.

    There is deliberately no generation timestamp: the pinned archive bytes
    and filters determine every byte of this document, making it suitable for
    source control, a static endpoint, or a build cache.
    """
    sources = archive_provenance()
    storms = catalogue(near_km=near_km, min_peak_kt=min_peak_kt)
    entries = [
        {
            "closest_km": summary.closest_km,
            "id": summary.storm_id.lower(),
            "label": summary.label,
            "measured_radii": summary.radii_measured,
            "measured_rmw": summary.rmw_measured,
            "fully_measured_extent_points": summary.fully_measured_extent_points,
            "name": summary.name.title(),
            "peak_wind_kt": summary.peak_wind_kt,
            "points": summary.points,
            "provenance": summary.provenance,
            "wind_at_closest_kt": summary.wind_at_closest_kt,
            "wind_field_points": summary.wind_field_points,
            "year": summary.year,
            **(
                {"minimum_pressure_mb": summary.min_pressure_mb}
                if summary.min_pressure_mb is not None
                else {}
            ),
        }
        for summary in storms
    ]
    return {
        "basin": "north-atlantic",
        "filters": {
            "jamaica_radius_km": near_km,
            "minimum_peak_wind_kt": min_peak_kt,
        },
        "ordering": [
            "closest_km:ascending",
            "wind_at_closest_kt:descending",
            "id:ascending",
        ],
        "schema": CATALOGUE_SCHEMA,
        "sources": sources,
        "storm_count": len(entries),
        "storms": entries,
    }


def serialise_catalogue(document: dict[str, Any]) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def track_library_document(
    *, near_km: float = NEAR_KM, min_peak_kt: int = MIN_PEAK_KT
) -> dict[str, Any]:
    """Compact source tracks for instant browser-side historical editing.

    The database pipeline remains the authoritative path for publication. This
    static bundle exists so selecting the 73rd storm does not require 98,000
    writes before a person can move a point or change a wind control.
    """
    summaries = catalogue(near_km=near_km, min_peak_kt=min_peak_kt)
    tracks = _all()
    entries: list[dict[str, Any]] = []
    for summary in summaries:
        track = tracks[summary.storm_id]
        positions: list[dict[str, Any]] = []
        for index, position in enumerate(track.positions):
            pressure = track.pressure_mb[index] if index < len(track.pressure_mb) else None
            rmw = track.rmw_nm[index] if index < len(track.rmw_nm) else None
            r34 = position.radius(34)
            known_r34 = (
                [
                    value
                    for value in (r34.ne, r34.se, r34.sw, r34.nw)
                    if value is not None and value > 0
                ]
                if r34 is not None
                else []
            )
            positions.append(
                {
                    "at": position.valid_at.isoformat().replace("+00:00", "Z"),
                    "lat": position.lat,
                    "lon": position.lon,
                    **(
                        {"max_wind_kt": position.max_wind_kt}
                        if position.max_wind_kt is not None
                        else {}
                    ),
                    **({"pressure_mb": pressure} if pressure is not None else {}),
                    **({"rmw_nm": rmw} if rmw is not None else {}),
                    **({"r34_nm": max(known_r34)} if known_r34 else {}),
                    **({"status": position.status} if position.status else {}),
                }
            )
        entries.append(
            {
                "id": summary.storm_id.lower(),
                "label": summary.label,
                "provenance": summary.provenance,
                "positions": positions,
            }
        )
    return {
        "schema": TRACKS_SCHEMA,
        "basin": "north-atlantic",
        "sources": archive_provenance(),
        "storm_count": len(entries),
        "storms": entries,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.storms.catalogue",
        description="Emit the pinned Jamaica-relevant Atlantic storm catalogue as JSON.",
    )
    parser.add_argument("--near-km", type=float, default=NEAR_KM)
    parser.add_argument("--minimum-peak-kt", type=int, default=MIN_PEAK_KT)
    parser.add_argument(
        "--output",
        type=Path,
        help="write JSON to this path instead of standard output",
    )
    parser.add_argument(
        "--tracks-output",
        type=Path,
        help="also write the compact browser-editable source-track library",
    )
    args = parser.parse_args(argv)
    if args.near_km <= 0:
        parser.error("--near-km must be positive")
    if args.minimum_peak_kt < 0:
        parser.error("--minimum-peak-kt cannot be negative")

    try:
        encoded = serialise_catalogue(
            catalogue_document(
                near_km=args.near_km,
                min_peak_kt=args.minimum_peak_kt,
            )
        )
        tracks_encoded = (
            serialise_catalogue(
                track_library_document(
                    near_km=args.near_km,
                    min_peak_kt=args.minimum_peak_kt,
                )
            )
            if args.tracks_output is not None
            else None
        )
    except CatalogueError as exc:
        parser.error(str(exc))
    if args.output is None:
        import sys

        sys.stdout.buffer.write(encoded)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(encoded)
    if args.tracks_output is not None and tracks_encoded is not None:
        args.tracks_output.parent.mkdir(parents=True, exist_ok=True)
        args.tracks_output.write_bytes(tracks_encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
