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

from dataclasses import dataclass
from functools import lru_cache
from math import asin, cos, radians, sin, sqrt

from app.storms.tracks import StormTrack, load_tracks

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
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
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

    @property
    def label(self) -> str:
        return f"{self.name.title()} {self.year}"

    @property
    def provenance(self) -> str:
        """What the screen must say about this storm's size."""
        if self.radii_measured and self.rmw_measured:
            return "measured"
        if self.radii_measured:
            return "measured radii, modelled core"
        return "modelled size"


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
    )


@lru_cache(maxsize=1)
def _all() -> dict[str, StormTrack]:
    return load_tracks()


def catalogue(
    *, near_km: float = NEAR_KM, min_peak_kt: int = MIN_PEAK_KT
) -> list[StormSummary]:
    """Every storm that reached hurricane strength and passed near Jamaica.

    Sorted by how hard it hit here rather than by date or by peak intensity —
    a category five that stayed 400 km away matters less to this product than a
    category two that crossed the island, and the list is for choosing which
    storm to replay.
    """
    out = [
        summary
        for track in _all().values()
        if track.peak_wind_kt >= min_peak_kt
        for summary in (summarise(track),)
        if summary.closest_km <= near_km
    ]
    return sorted(out, key=lambda s: (-s.wind_at_closest_kt, s.closest_km))


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
