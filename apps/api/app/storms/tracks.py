"""Read Atlantic storm tracks from HURDAT2 and the Extended Best Track.

One storm becomes many here. Everything upstream of this module speaks NHC
teletype and knows only Melissa; everything downstream speaks `Position` and
`Radii` and does not care where they came from. This is the join.

**Two files, because neither is sufficient.**

HURDAT2 is NHC's reanalysed best track — authoritative on where a storm was and
how strong. It carries quadrant wind radii only from 2004 and radius of maximum
wind only from 2021; before that the columns are `-999`. So on its own it can
say Gilbert was a 110 kt hurricane over Jamaica and cannot say how wide it was,
which is the one thing a wind field needs.

EBTRK exists to fill exactly that gap, back to 1988. Where both have a value we
keep HURDAT2's, because it is the reanalysis; where HURDAT2 is silent we take
EBTRK's. The merge is on `(storm_id, timestamp)` and nothing is interpolated —
a track point with no radii from either source reports none, and the caller
decides whether to model them or refuse the storm.

**What is deliberately not trusted.** EBTRK's eye diameter, POCI and ROCI
columns are corrupt in the older records — Gilbert reports an outermost closed
isobar of 12 hPa, which is not a pressure. They are not read. Ambient pressure
is taken as a constant instead, which is standard practice anyway.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.nhc.fstadv import Position, Radii

REPO = Path(__file__).parents[4]
CACHE = REPO / "data" / "storms" / "cache"
HURDAT2 = CACHE / "hurdat2-atlantic.txt"
EBTRK = CACHE / "ebtrk-atlantic.txt"

#: The three thresholds the whole product is built on. Matches nhc.ingest.
THRESHOLDS = (34, 50, 64)

#: Ambient pressure at the edge of a tropical cyclone. Used in place of EBTRK's
#: POCI column, which is unreliable before roughly 2000 — see the module note.
AMBIENT_MB = 1010

#: HURDAT2 writes -999 for a field it never analysed; EBTRK writes -99.
_MISSING = {-999, -99, 9999}


class TrackError(RuntimeError):
    pass


@dataclass(frozen=True)
class StormTrack:
    """One storm's whole life, as observed."""

    storm_id: str  # ATCF form, e.g. AL081988
    name: str
    year: int
    positions: tuple[Position, ...]
    #: Radius of maximum wind per position index, nautical miles. Sparse: a
    #: value only where a source published one. Kept alongside rather than on
    #: Position because Position is the NHC advisory model and RMW is not in it.
    rmw_nm: tuple[float | None, ...] = ()
    pressure_mb: tuple[int | None, ...] = ()

    @property
    def peak_wind_kt(self) -> int:
        return max((p.max_wind_kt or 0) for p in self.positions)

    @property
    def min_pressure_mb(self) -> int | None:
        known = [p for p in self.pressure_mb if p]
        return min(known) if known else None

    @property
    def started_at(self) -> datetime:
        return self.positions[0].valid_at

    @property
    def ended_at(self) -> datetime:
        return self.positions[-1].valid_at

    def has_radii(self) -> bool:
        return any(not r.is_empty for p in self.positions for r in p.radii)


def _int(raw: str) -> int | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = int(float(raw))
    except ValueError:
        return None
    return None if value in _MISSING else value


def _radii_from(values: list[int | None]) -> tuple[Radii, ...]:
    """Twelve numbers — NE/SE/SW/NW at 34, 50, 64 — into the shared model.

    A threshold with all four missing is omitted rather than emitted as zeros:
    "not analysed" and "the wind does not reach" are different claims, and
    `quadrant_polygon_wkt` already returns None for the second one.
    """
    out = []
    for i, threshold in enumerate(THRESHOLDS):
        quad = values[i * 4 : i * 4 + 4]
        if all(v is None for v in quad):
            continue
        ne, se, sw, nw = (v or 0 for v in quad)
        out.append(Radii(threshold_kt=threshold, ne=ne, se=se, sw=sw, nw=nw))
    return tuple(out)


# ---------------------------------------------------------------------------
# HURDAT2
# ---------------------------------------------------------------------------
# Header:  AL081988,            GILBERT,     49,
# Data:    19880912, 1800,  , HU, 17.7N,  76.5W, 110,  960, -999, ... , -999
#          date      time  rec  status lat lon  vmax pmin  <12 radii>  rmw


def _hurdat_latlon(raw: str) -> float:
    raw = raw.strip()
    value = float(raw[:-1])
    return -value if raw[-1] in "SW" else value


def read_hurdat2(path: Path = HURDAT2) -> dict[str, StormTrack]:
    if not path.exists():
        raise TrackError(f"{path} is missing — run data/storms/fetch_tracks.py")

    storms: dict[str, StormTrack] = {}
    storm_id = name = ""
    positions: list[Position] = []
    rmws: list[float | None] = []
    pressures: list[int | None] = []

    def flush() -> None:
        if storm_id and positions:
            storms[storm_id] = StormTrack(
                storm_id=storm_id,
                name=name,
                year=int(storm_id[-4:]),
                positions=tuple(positions),
                rmw_nm=tuple(rmws),
                pressure_mb=tuple(pressures),
            )

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(",")]

        # A header has the storm id in column 0 and no date; a data line starts
        # with an eight-digit date. Distinguishing on shape rather than on the
        # declared row count means a miscounted header cannot desynchronise the
        # whole file.
        if parts[0][:2].isalpha():
            flush()
            storm_id, name = parts[0], parts[1]
            positions, rmws, pressures = [], [], []
            continue

        if len(parts) < 8:
            continue
        stamp = datetime.strptime(f"{parts[0]}{parts[1]}", "%Y%m%d%H%M").replace(tzinfo=UTC)
        radii = _radii_from([_int(p) for p in parts[8:20]]) if len(parts) >= 20 else ()
        positions.append(
            Position(
                valid_at=stamp,
                lat=_hurdat_latlon(parts[4]),
                lon=_hurdat_latlon(parts[5]),
                kind="observed",
                max_wind_kt=_int(parts[6]),
                radii=radii,
            )
        )
        pressures.append(_int(parts[7]))
        rmws.append(float(_int(parts[20])) if len(parts) >= 21 and _int(parts[20]) else None)

    flush()
    return storms


# ---------------------------------------------------------------------------
# Extended Best Track
# ---------------------------------------------------------------------------
# Fixed width, and it has to be. Whitespace splitting looks fine until a
# two-digit radius pads to " 50" and the four-value block splits into separate
# tokens — token counts across this file run from 18 to 27.
#
# AL081988 GILBERT     091606 1988  22.9   94.8 110  946  29  12   12  12 250200250250 200150150200 125100100125
# 0        9           21     28    33     39   46   50   55  59   63  68 72           85           98

_EB = {
    "id": (0, 8),
    "name": (9, 21),
    "mmddhh": (21, 27),
    "year": (28, 32),
    "lat": (33, 38),
    "lon": (39, 45),
    "vmax": (46, 49),
    "pmin": (50, 54),
    "rmw": (55, 58),
    "r34": (72, 84),
    "r50": (85, 97),
    "r64": (98, 110),
}


def _eb_block(raw: str) -> list[int | None]:
    """A 12-character block is four 3-character radii, not four tokens."""
    return [_int(raw[i : i + 3]) for i in range(0, 12, 3)]


def read_ebtrk(path: Path = EBTRK) -> dict[str, dict[datetime, tuple[tuple[Radii, ...], float | None]]]:
    """`storm_id → timestamp → (radii, rmw)`. Only what HURDAT2 lacks."""
    if not path.exists():
        raise TrackError(f"{path} is missing — run data/storms/fetch_tracks.py")

    out: dict[str, dict[datetime, tuple[tuple[Radii, ...], float | None]]] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if len(line) < 110:
            continue
        cut = {k: line[a:b] for k, (a, b) in _EB.items()}
        try:
            year = int(cut["year"])
            stamp = datetime.strptime(f"{year}{cut['mmddhh'].strip()}", "%Y%m%d%H").replace(
                tzinfo=UTC
            )
        except ValueError:
            continue

        radii = _radii_from(
            _eb_block(cut["r34"]) + _eb_block(cut["r50"]) + _eb_block(cut["r64"])
        )
        rmw = _int(cut["rmw"])
        if not radii and rmw is None:
            continue
        out.setdefault(cut["id"].strip(), {})[stamp] = (radii, float(rmw) if rmw else None)
    return out


# ---------------------------------------------------------------------------
# The merge
# ---------------------------------------------------------------------------


def load_tracks(*, hurdat2: Path = HURDAT2, ebtrk: Path = EBTRK) -> dict[str, StormTrack]:
    """Every Atlantic storm, with EBTRK filling HURDAT2's gaps.

    HURDAT2 wins wherever it has a value — it is the reanalysis and EBTRK is
    the operational record. EBTRK is consulted only where HURDAT2 is silent,
    which in practice means wind radii before 2004 and RMW before 2021.
    """
    storms = read_hurdat2(hurdat2)
    extended = read_ebtrk(ebtrk) if ebtrk.exists() else {}

    filled: dict[str, StormTrack] = {}
    for storm_id, track in storms.items():
        extra = extended.get(storm_id)
        if not extra:
            filled[storm_id] = track
            continue

        positions, rmws = [], []
        for index, position in enumerate(track.positions):
            eb_radii, eb_rmw = extra.get(position.valid_at, ((), None))
            positions.append(
                position if position.radii else
                Position(
                    valid_at=position.valid_at,
                    lat=position.lat,
                    lon=position.lon,
                    kind=position.kind,
                    max_wind_kt=position.max_wind_kt,
                    gust_kt=position.gust_kt,
                    radii=eb_radii,
                )
            )
            existing = track.rmw_nm[index] if index < len(track.rmw_nm) else None
            rmws.append(existing if existing else eb_rmw)

        filled[storm_id] = StormTrack(
            storm_id=track.storm_id,
            name=track.name,
            year=track.year,
            positions=tuple(positions),
            rmw_nm=tuple(rmws),
            pressure_mb=track.pressure_mb,
        )
    return filled
