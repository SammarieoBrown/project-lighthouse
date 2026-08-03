"""Turn quadrant wind radii into polygons.

NHC publishes the extent of each wind threshold as four numbers — how far it
reaches northeast, southeast, southwest and northwest — because that is how
forecasters reason about an asymmetric storm. To ask whether a household is
inside the 64 kt field, those four numbers have to become a shape.

The shape is deliberately blunt: constant radius within each quadrant, with a
step at the boundary. Smoothing between quadrants would look better and claim
more than the source supports — NHC's own wording is "the largest radii expected
anywhere in that quadrant", so a quadrant is a maximum, not a curve through four
control points. Drawing the interpolation would be inventing precision, which is
the same failure as rendering a confidence score without its signals.
"""

from __future__ import annotations

import math

#: Earth's mean radius in nautical miles. The wind radii are published in nm, so
#: working in nm avoids a conversion nobody would notice was wrong.
EARTH_RADIUS_NM = 3440.065

#: Degrees between sampled points along the arc. Twelve points per quadrant is
#: enough that the polygon edge sits within a few hundred metres of the true arc
#: at these radii, and coarse enough to keep 41 advisories cheap to store.
_STEP_DEG = 7.5


def destination(lat: float, lon: float, bearing_deg: float, distance_nm: float) -> tuple[float, float]:
    """Point at a bearing and distance from a start point, on a sphere.

    Spherical rather than ellipsoidal on purpose: at these distances the
    difference is a few hundred metres, well inside the uncertainty of the wind
    radii themselves, and PostGIS re-measures on the geography type anyway.
    """
    if distance_nm <= 0:
        return lat, lon

    angular = distance_nm / EARTH_RADIUS_NM
    bearing = math.radians(bearing_deg)
    lat1, lon1 = math.radians(lat), math.radians(lon)

    lat2 = math.asin(
        math.sin(lat1) * math.cos(angular) + math.cos(lat1) * math.sin(angular) * math.cos(bearing)
    )
    lon2 = lon1 + math.atan2(
        math.sin(bearing) * math.sin(angular) * math.cos(lat1),
        math.cos(angular) - math.sin(lat1) * math.sin(lat2),
    )
    # Normalise into [-180, 180] so a storm crossing the antimeridian does not
    # produce a polygon that wraps the planet the long way.
    return math.degrees(lat2), (math.degrees(lon2) + 540) % 360 - 180


def _radius_at(bearing_deg: float, ne: float, se: float, sw: float, nw: float) -> float:
    bearing = bearing_deg % 360
    if bearing < 90:
        return ne
    if bearing < 180:
        return se
    if bearing < 270:
        return sw
    return nw


def quadrant_polygon_wkt(
    lat: float,
    lon: float,
    *,
    ne: float,
    se: float,
    sw: float,
    nw: float,
) -> str | None:
    """WKT polygon for one wind threshold at one position.

    Returns ``None`` when every quadrant is zero — a threshold the storm does
    not currently reach. That is a real state and not an error: a tropical storm
    has no hurricane-force field, and the honest representation of an area that
    does not exist is no geometry rather than a degenerate one.
    """
    if not any((ne, se, sw, nw)):
        return None

    points: list[tuple[float, float]] = []
    bearing = 0.0
    while bearing < 360:
        points.append(destination(lat, lon, bearing, _radius_at(bearing, ne, se, sw, nw)))
        # Land exactly on each quadrant boundary so the step between quadrants
        # is a clean edge rather than a diagonal across the seam.
        next_bearing = bearing + _STEP_DEG
        boundary = (bearing // 90 + 1) * 90
        if bearing < boundary < next_bearing:
            points.append(destination(lat, lon, boundary - 1e-9, _radius_at(bearing, ne, se, sw, nw)))
            points.append(destination(lat, lon, boundary, _radius_at(boundary, ne, se, sw, nw)))
        bearing = next_bearing

    points.append(points[0])  # close the ring
    ring = ", ".join(f"{lng:.6f} {lt:.6f}" for lt, lng in points)
    return f"POLYGON(({ring}))"
