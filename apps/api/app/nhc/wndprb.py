"""Parse the NHC Wind Speed Probabilities product (``wndprb``).

This is the product that answers the question the cone cannot: not where the
storm centre might go, but how likely a given place is to actually feel a given
wind. At Melissa advisory 25 it put Montego Bay at a cumulative 63% for
hurricane-force wind within 36 hours and Kingston at 27%, and that difference is
the reason relief stages west.

Its limitation is the reason ``advisory.wind_field_*`` holds geometry and
``risk_assessment.p34/p50/p64`` holds scalars: the product reports 26 named
locations, only two of them in Jamaica. It is a point sample, not a surface.
Anything finer than Kingston and Montego Bay is interpolation, and whatever does
that interpolation has to say so in ``risk_assessment.method``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

_ISSUED = re.compile(
    r"^(?P<hhmm>\d{4})\s+UTC\s+\w{3}\s+(?P<mon>[A-Z]{3})\s+(?P<day>\d{1,2})\s+(?P<year>\d{4})",
    re.MULTILINE,
)
_HEADER = re.compile(
    r"^(?:[A-Z -]+?)\s+(?P<name>[A-Z]+)\s+WIND SPEED PROBABILITIES\s+NUMBER\s+(?P<number>\d+[A-Z]?)",
    re.MULTILINE,
)
_STORM_ID = re.compile(r"\b(?P<id>AL\d{6})\b")

# "MONTEGO BAY    34 80  19(99)   1(99)   X(99)   X(99)   X(99)   X(99)"
#
# The first window carries no cumulative figure because at 12 hours the
# incremental and cumulative values are the same number; NHC omits the
# redundancy rather than repeating it.
_ROW = re.compile(
    r"^(?P<location>[A-Z][A-Z0-9 .'/-]{2,13})\s+(?P<kt>34|50|64)\s+"
    r"(?P<first>\d+|X)\s+(?P<rest>(?:\s*(?:\d+|X)\(\s*(?:\d+|X)\)){2,})\s*$",
    re.MULTILINE,
)
_CELL = re.compile(r"(?P<inc>\d+|X)\(\s*(?P<cum>\d+|X)\)")

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

#: The seven windows the product reports, in hours from issue.
FORECAST_HOURS: tuple[int, ...] = (12, 24, 36, 48, 72, 96, 120)


def _pct(token: str) -> int:
    """``X`` means below 0.5%, which rounds to zero and is reported as zero.

    Worth knowing that it is not the same claim as an explicit 0: NHC prints X
    for "less than half a percent", not "impossible". At the resolution this
    product is published in, the distinction has nowhere to go — but a reader
    who assumes 0 means ruled out has assumed too much.
    """
    return 0 if token == "X" else int(token)


@dataclass(frozen=True)
class LocationProbability:
    """One location, one wind threshold, seven forecast windows."""

    location: str
    threshold_kt: int
    #: Chance of onset within each window alone, keyed by forecast hour.
    incremental: dict[int, int]
    #: Chance of onset at any point up to and including that hour.
    cumulative: dict[int, int]

    def cumulative_at(self, hours: int) -> int:
        return self.cumulative[hours]


@dataclass(frozen=True)
class WindProbabilities:
    storm_id: str
    storm_name: str
    advisory_number: str
    issued_at: datetime
    rows: tuple[LocationProbability, ...]

    def for_location(self, name: str) -> tuple[LocationProbability, ...]:
        target = name.upper()
        return tuple(r for r in self.rows if r.location == target)

    @property
    def locations(self) -> tuple[str, ...]:
        seen: list[str] = []
        for row in self.rows:
            if row.location not in seen:
                seen.append(row.location)
        return tuple(seen)


def parse_wndprb(text: str) -> WindProbabilities:
    """Parse one Wind Speed Probabilities product."""
    issued_match = _ISSUED.search(text)
    header = _HEADER.search(text)
    storm_id = _STORM_ID.search(text)
    if not (issued_match and header and storm_id):
        raise ValueError("not a Wind Speed Probabilities product")

    issued_at = datetime(
        int(issued_match["year"]),
        _MONTHS[issued_match["mon"]],
        int(issued_match["day"]),
        int(issued_match["hhmm"][:2]),
        int(issued_match["hhmm"][2:]),
        tzinfo=UTC,
    )

    rows: list[LocationProbability] = []
    for match in _ROW.finditer(text):
        cells = _CELL.findall(match["rest"])
        # The bare first column plus the parenthesised remainder must add up to
        # the seven published windows. A short row means the layout moved, and
        # silently keeping a partial row would put a 120-hour probability under
        # a 96-hour key.
        if len(cells) + 1 != len(FORECAST_HOURS):
            continue

        first = _pct(match["first"])
        incremental = {FORECAST_HOURS[0]: first}
        cumulative = {FORECAST_HOURS[0]: first}
        for hour, (inc, cum) in zip(FORECAST_HOURS[1:], cells, strict=True):
            incremental[hour] = _pct(inc)
            cumulative[hour] = _pct(cum)

        rows.append(
            LocationProbability(
                location=match["location"].strip(),
                threshold_kt=int(match["kt"]),
                incremental=incremental,
                cumulative=cumulative,
            )
        )

    return WindProbabilities(
        storm_id=storm_id["id"].lower(),
        storm_name=header["name"].title(),
        advisory_number=header["number"],
        issued_at=issued_at,
        rows=tuple(rows),
    )
