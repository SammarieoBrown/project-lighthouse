"""Parsers for National Hurricane Center products.

We consume forecasts, we do not make them. These modules turn NHC's published
text and shapefiles into rows we can query, and nothing here decides anything —
the risk model reads what this produces, it does not reach into the archive.

The cached Melissa advisories in ``data/replay/cache`` are the fixtures. Parsing
is tested against the real files rather than invented samples, because the whole
category of bug worth catching here is "the real format has a case we did not
imagine", and a handcrafted sample cannot contain a case we did not imagine.
"""

from app.nhc.fstadv import ForecastAdvisory, Position, Radii, parse_fstadv
from app.nhc.geometry import quadrant_polygon_wkt
from app.nhc.wndprb import LocationProbability, WindProbabilities, parse_wndprb

__all__ = [
    "ForecastAdvisory",
    "LocationProbability",
    "Position",
    "Radii",
    "WindProbabilities",
    "parse_fstadv",
    "parse_wndprb",
    "quadrant_polygon_wkt",
]
