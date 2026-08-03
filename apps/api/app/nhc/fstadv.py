"""Parse the NHC Forecast/Advisory product (``fstadv``).

This is the machine-readable backbone of an advisory: where the storm is, how
strong, which way it is going, how far the damaging wind reaches in each
quadrant, and the same again for every forecast hour out to 120.

The format is a fixed-width teletype product that has barely changed in decades,
which is why regex is the right tool and not a shortcut. What varies is which
lines are *present*: a tropical storm has no 64 KT radii line, long-range
forecasts often carry no radii at all, and the tail of the product switches from
FORECAST to OUTLOOK past 72 hours.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

# "1500 UTC MON OCT 27 2025"
_ISSUED = re.compile(
    r"^(?P<hhmm>\d{4})\s+UTC\s+\w{3}\s+(?P<mon>[A-Z]{3})\s+(?P<day>\d{1,2})\s+(?P<year>\d{4})",
    re.MULTILINE,
)

# "HURRICANE MELISSA FORECAST/ADVISORY NUMBER  25"
_HEADER = re.compile(
    r"^(?P<type>[A-Z -]+?)\s+(?P<name>[A-Z]+)\s+FORECAST/ADVISORY\s+NUMBER\s+(?P<number>\d+[A-Z]?)",
    re.MULTILINE,
)

_STORM_ID = re.compile(r"\b(?P<id>AL\d{6})\b")

# "HURRICANE CENTER LOCATED NEAR 16.4N  78.2W AT 27/1500Z"
_CENTER = re.compile(
    r"CENTER LOCATED NEAR\s+(?P<lat>[\d.]+)(?P<ns>[NS])\s+(?P<lon>[\d.]+)(?P<ew>[EW])"
    r"\s+AT\s+(?P<day>\d{2})/(?P<hhmm>\d{4})Z"
)

# "PRESENT MOVEMENT TOWARD THE WEST OR 270 DEGREES AT   3 KT"
_MOVEMENT = re.compile(r"PRESENT MOVEMENT TOWARD .*?OR\s+(?P<deg>\d+)\s+DEGREES AT\s+(?P<kt>\d+)\s+KT")
_PRESSURE = re.compile(r"ESTIMATED MINIMUM CENTRAL PRESSURE\s+(?P<mb>\d+)\s+MB")
_EYE = re.compile(r"EYE DIAMETER\s+(?P<nm>\d+)\s+NM")
_MAX_WIND = re.compile(r"MAX SUSTAINED WINDS\s+(?P<kt>\d+)\s+KT WITH GUSTS TO\s+(?P<gust>\d+)\s+KT")

# "FORECAST VALID 28/0000Z 16.9N  78.3W" — OUTLOOK is the same shape past 72h,
# and may carry a trailing "...POST-TROP/EXTRATROP" note.
_VALID = re.compile(
    r"^(?P<kind>FORECAST|OUTLOOK) VALID\s+(?P<day>\d{2})/(?P<hhmm>\d{4})Z\s+"
    r"(?P<lat>[\d.]+)(?P<ns>[NS])\s+(?P<lon>[\d.]+)(?P<ew>[EW])",
    re.MULTILINE,
)

_FCST_WIND = re.compile(r"MAX WIND\s+(?P<kt>\d+)\s+KT\.\.\.GUSTS\s+(?P<gust>\d+)\s+KT")

# "64 KT....... 25NE  20SE  20SW  25NW."  and the forecast form "64 KT... 30NE..."
#
# Anchored to the start of the line and to a numeric threshold on purpose: the
# seas line ("4 M SEAS....240NE 210SE...") has the identical quadrant shape and
# would otherwise parse as a 4-knot wind radius, silently inventing a wind field
# the size of the sea state.
_RADII = re.compile(
    r"^(?P<kt>\d{2})\s+KT\.{3,}\s*(?P<ne>\d+)NE\s+(?P<se>\d+)SE\s+(?P<sw>\d+)SW\s+(?P<nw>\d+)NW",
    re.MULTILINE,
)

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

#: NHC uses 9999 as "not available" in the GIS attribute tables. It does not
#: appear in the text product, but downstream readers of both should treat it
#: the same way, so the sentinel is named here rather than in three places.
MISSING = 9999


@dataclass(frozen=True)
class Radii:
    """How far a wind threshold reaches, per quadrant, in nautical miles.

    The four values are almost never equal. At Melissa advisory 25 the 34 kt
    field ran 170 nm northeast and 50 nm southwest — treat it as a circle and
    you warn the wrong parish.
    """

    threshold_kt: int
    ne: int
    se: int
    sw: int
    nw: int

    @property
    def is_empty(self) -> bool:
        return not any((self.ne, self.se, self.sw, self.nw))


@dataclass(frozen=True)
class Position:
    """The storm at one moment — observed, forecast, or long-range outlook."""

    valid_at: datetime
    lat: float
    lon: float
    kind: str  # observed | forecast | outlook
    max_wind_kt: int | None = None
    gust_kt: int | None = None
    radii: tuple[Radii, ...] = ()

    def radius(self, threshold_kt: int) -> Radii | None:
        for r in self.radii:
            if r.threshold_kt == threshold_kt:
                return r
        return None


@dataclass(frozen=True)
class ForecastAdvisory:
    storm_id: str
    storm_name: str
    storm_type: str
    advisory_number: str
    issued_at: datetime
    current: Position
    forecasts: tuple[Position, ...]
    movement_deg: int | None = None
    movement_kt: int | None = None
    pressure_mb: int | None = None
    eye_diameter_nm: int | None = None

    @property
    def positions(self) -> tuple[Position, ...]:
        return (self.current, *self.forecasts)


def _signed(value: str, hemisphere: str) -> float:
    v = float(value)
    return -v if hemisphere in ("S", "W") else v


def _at(day: int, hhmm: str, issued: datetime) -> datetime:
    """Resolve NHC's ``DD/HHMMZ`` against the advisory's own issue date.

    The product gives a day of month and a time, never a month. A forecast
    issued on 27 October and valid at ``01/1200Z`` means 1 November — so a day
    number lower than the issue day has rolled into the next month. Getting this
    wrong moves a forecast point four weeks into the past, and the replay would
    still run; it would just be wrong.
    """
    hour, minute = int(hhmm[:2]), int(hhmm[2:])
    month, year = issued.month, issued.year
    if day < issued.day:
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _radii_in(block: str) -> tuple[Radii, ...]:
    found = [
        Radii(int(m["kt"]), int(m["ne"]), int(m["se"]), int(m["sw"]), int(m["nw"]))
        for m in _RADII.finditer(block)
    ]
    # NHC lists the strongest threshold first; ascending reads better everywhere
    # downstream and makes 34/50/64 comparisons order-independent.
    return tuple(sorted(found, key=lambda r: r.threshold_kt))


def parse_fstadv(text: str) -> ForecastAdvisory:
    """Parse one Forecast/Advisory product into positions and wind fields."""
    issued_match = _ISSUED.search(text)
    header = _HEADER.search(text)
    storm_id = _STORM_ID.search(text)
    center = _CENTER.search(text)
    if not (issued_match and header and storm_id and center):
        missing = [
            name
            for name, m in (
                ("issue time", issued_match),
                ("header", header),
                ("storm id", storm_id),
                ("center position", center),
            )
            if not m
        ]
        raise ValueError(f"not a Forecast/Advisory product — no {', '.join(missing)}")

    issued_at = datetime(
        int(issued_match["year"]),
        _MONTHS[issued_match["mon"]],
        int(issued_match["day"]),
        int(issued_match["hhmm"][:2]),
        int(issued_match["hhmm"][2:]),
        tzinfo=UTC,
    )

    # The current radii are the block between the max-wind line and the REPEAT
    # line. Slicing first keeps the forecast blocks from leaking into it.
    head_end = text.find("REPEAT...")
    head = text[: head_end if head_end != -1 else len(text)]

    max_wind = _MAX_WIND.search(head)
    current = Position(
        valid_at=_at(int(center["day"]), center["hhmm"], issued_at),
        lat=_signed(center["lat"], center["ns"]),
        lon=_signed(center["lon"], center["ew"]),
        kind="observed",
        max_wind_kt=int(max_wind["kt"]) if max_wind else None,
        gust_kt=int(max_wind["gust"]) if max_wind else None,
        radii=_radii_in(head),
    )

    forecasts: list[Position] = []
    matches = list(_VALID.finditer(text))
    for i, m in enumerate(matches):
        # Everything up to the next VALID line belongs to this forecast hour.
        block = text[m.end() : matches[i + 1].start() if i + 1 < len(matches) else len(text)]
        wind = _FCST_WIND.search(block)
        forecasts.append(
            Position(
                valid_at=_at(int(m["day"]), m["hhmm"], issued_at),
                lat=_signed(m["lat"], m["ns"]),
                lon=_signed(m["lon"], m["ew"]),
                kind=m["kind"].lower(),
                max_wind_kt=int(wind["kt"]) if wind else None,
                gust_kt=int(wind["gust"]) if wind else None,
                radii=_radii_in(block),
            )
        )

    movement = _MOVEMENT.search(text)
    pressure = _PRESSURE.search(text)
    eye = _EYE.search(text)

    return ForecastAdvisory(
        storm_id=storm_id["id"].lower(),
        storm_name=header["name"].title(),
        storm_type=header["type"].strip(),
        advisory_number=header["number"],
        issued_at=issued_at,
        current=current,
        forecasts=tuple(forecasts),
        movement_deg=int(movement["deg"]) if movement else None,
        movement_kt=int(movement["kt"]) if movement else None,
        pressure_mb=int(pressure["mb"]) if pressure else None,
        eye_diameter_nm=int(eye["nm"]) if eye else None,
    )
