"""A parametric wind field, and the quadrant radii it implies.

Everything downstream of `nhc.geometry.quadrant_polygon_wkt` needs four numbers
per threshold: how far 34, 50 and 64 kt reach to the north-east, south-east,
south-west and north-west. For a modern storm NHC publishes them. For Gilbert
they were digitised decades later. For a storm somebody draws on the map this
afternoon, nobody has ever published them, and this module is where they come
from.

**The model.** Holland (1980) for the radial profile, Holland (2008) for its
shape parameter, plus the two asymmetries that make a hurricane look and behave
like one rather than like a bullseye:

    V(r) = sqrt( (B/rho)(Rmax/r)^B * dP * exp(-(Rmax/r)^B) + (rf/2)^2 ) - rf/2

Forward motion is added as a vector, damped by `min(1, Rmax/r)` so the storm
carries its own speed near the core and less of it far out. Inflow angle turns
the wind across the isobars — Zhang & Uhlhorn (2012) measured a mean of 22.6
degrees at the surface from 1,600 dropsondes. Without it the field is a set of
concentric circles, which is neither what a hurricane does nor what one looks
like.

**On sources.** The equations are from the papers, cited below. CLIMADA and
TCRM implement the same mathematics and are both GPL-3.0; neither was read
while writing this, and neither needs to be — the formulas are in the
literature and mathematics is not copyrightable.

- Holland (1980), *An Analytic Model of the Wind and Pressure Profiles in
  Hurricanes*, MWR 108(8).
- Holland (2008), *A Revised Hurricane Pressure-Wind Model*, MWR 136(9), eq. 11.
- Vickery & Wadhera (2008), JAMC 47(10) — the radius-of-maximum-wind estimate.
- Zhang & Uhlhorn (2012), MWR 140(11) — surface inflow angle.

**What this is not.** It is not a boundary layer model. FEMA's Hazus and the
insurance industry solve a translating slab over the pressure field with surface
friction; this is the profile alone. It also has no terrain: Jamaica's Blue
Mountains rise to 2,256 m and will do things to a wind field that no parametric
profile can express. The output is a smooth idealisation of a storm, and the
screen has to say so.
"""

from __future__ import annotations

from math import atan2, cos, exp, pi, radians, sin, sqrt

from app.nhc.fstadv import Radii

#: Air density at the sea surface in a hurricane, kg/m^3. Holland's 1980 value
#: and the one every implementation since has used.
RHO_AIR = 1.15

#: Ambient pressure at the outer edge, hPa. EBTRK publishes a POCI column but
#: it is corrupt in the older records, so a constant is both simpler and more
#: honest than a number we would have to distrust.
AMBIENT_MB = 1010.0

#: Gradient winds are above the boundary layer; the surface feels less. 0.9 is
#: the standard reduction and what CLIMADA, TCRM and the reanalyses all use.
SURFACE_FACTOR = 0.9

#: Zhang & Uhlhorn (2012), mean surface inflow angle. One rotation, and the
#: single cheapest thing that makes the field read as a hurricane.
INFLOW_DEG = 22.6

#: Earth's angular velocity, rad/s — for the Coriolis term.
OMEGA = 7.2921e-5

KT_TO_MS = 0.514444
MS_TO_KT = 1.0 / KT_TO_MS
NM_TO_KM = 1.852
KM_TO_NM = 1.0 / NM_TO_KM

#: Quadrant centre bearings, in the order the Radii model stores them.
QUADRANTS = ((0.0, "ne"), (90.0, "se"), (180.0, "sw"), (270.0, "nw"))

#: How far out to look for a threshold crossing. Beyond this the profile is
#: below tropical storm force for any plausible storm, and a root found further
#: out would be numerical noise rather than wind.
MAX_SEARCH_NM = 600.0


def coriolis(lat: float) -> float:
    return 2.0 * OMEGA * sin(radians(abs(lat)))


def holland_b(
    *,
    vmax_ms: float,
    delta_p_hpa: float,
    lat: float,
    translation_ms: float = 0.0,
    dpdt_hpa_h: float = 0.0,
) -> float:
    """The profile's shape parameter.

    Two routes, and which one applies depends on what the archive gave us.

    With a peak wind, invert Holland (1980) at the radius of maximum wind:
    ``B = vmax^2 * rho * e / dP``. This is exact by construction — the profile
    reproduces the observed peak.

    Without one, Holland (2008) eq. 11 estimates B from pressure alone, and it
    is the better model in one respect worth knowing: it depends on how fast
    the storm is deepening and how fast it is moving, so B varies along a track
    rather than being one number for a whole storm. A single B cannot represent
    a storm through an eyewall replacement, and systematically under-predicts
    wind far from the centre.

    Clamped to [1.0, 2.5], the range the literature reports. Outside it the
    exponential either flattens into no storm at all or spikes into a profile
    no instrument has measured.
    """
    if delta_p_hpa <= 0:
        return 1.0

    if vmax_ms > 0:
        b = (vmax_ms**2) * RHO_AIR * exp(1.0) / (delta_p_hpa * 100.0)
    else:
        x = 0.6 * (1.0 - delta_p_hpa / 215.0)
        b = (
            -4.4e-5 * delta_p_hpa**2
            + 0.01 * delta_p_hpa
            + 0.03 * dpdt_hpa_h
            - 0.014 * abs(lat)
            + 0.15 * (translation_ms**x if translation_ms > 0 else 0.0)
            + 1.0
        )
    return max(1.0, min(2.5, b))


def estimate_rmw_nm(*, delta_p_hpa: float, lat: float) -> float:
    """Radius of maximum wind, when no source published one.

    Vickery & Wadhera (2008): ``ln(Rmax) = 3.015 - 6.291e-5 dP^2 + 0.0337 |lat|``
    with Rmax in kilometres. Intense storms have tight cores and high-latitude
    storms have broad ones, which is what the two terms say.

    Before 2021 this is the usual case, not the exception — HURDAT2 carries no
    RMW at all until then.
    """
    km = exp(3.015 - 6.291e-5 * (delta_p_hpa**2) + 0.0337 * abs(lat))
    return max(5.0, min(60.0, km * KM_TO_NM))


def gradient_wind_ms(
    r_km: float, *, rmw_km: float, b: float, delta_p_hpa: float, lat: float
) -> float:
    """Holland's cyclostrophic-plus-Coriolis profile at one radius."""
    if r_km <= 0:
        return 0.0
    f = coriolis(lat)
    r_m = r_km * 1000.0
    ratio = (rmw_km / r_km) ** b
    core = (b / RHO_AIR) * ratio * (delta_p_hpa * 100.0) * exp(-ratio)
    fr = r_m * f / 2.0
    return max(0.0, sqrt(max(0.0, core + fr * fr)) - fr)


def surface_wind_kt(
    r_km: float,
    bearing_deg: float,
    *,
    rmw_km: float,
    b: float,
    delta_p_hpa: float,
    lat: float,
    translation_ms: float = 0.0,
    heading_deg: float = 0.0,
    northern: bool = True,
) -> float:
    """Wind speed at a point, given as a bearing and distance from the eye.

    Three things happen here that the radial profile alone does not do.

    The gradient wind is reduced to the surface. The storm's own motion is
    added as a vector — which is why the right-hand side of a northward-moving
    Atlantic hurricane is the dangerous one, and why a symmetric model warns
    the wrong parish. And the flow is turned inward across the isobars, so the
    field spirals rather than circles.
    """
    v = gradient_wind_ms(r_km, rmw_km=rmw_km, b=b, delta_p_hpa=delta_p_hpa, lat=lat)
    v *= SURFACE_FACTOR

    # Tangential direction: counter-clockwise in the northern hemisphere, and
    # rotated inward by the inflow angle.
    #
    # The sign is subtracted, and getting it wrong is silent. Cyclonic flow in
    # the northern hemisphere means the wind at a point due north of the eye
    # blows west — bearing 0 minus 90. Adding instead returns east-southeast,
    # which spins the storm backwards, puts the strong flank on the wrong side,
    # and therefore writes the largest quadrant radius into the wrong quadrant.
    # Nothing about that fails loudly: the field is still a plausible hurricane,
    # mirrored, and it warns the wrong parish.
    spin = 1.0 if northern else -1.0
    flow = bearing_deg - spin * (90.0 + INFLOW_DEG)
    ux = v * sin(radians(flow))
    uy = v * cos(radians(flow))

    if translation_ms > 0:
        # Damped outward: the core is carried along at close to the full
        # forward speed, the outer field much less. Mouton & Nordbeck's
        # min(1, Rmax/r) is the standard form.
        share = min(1.0, rmw_km / r_km) if r_km > 0 else 1.0
        ux += translation_ms * share * sin(radians(heading_deg))
        uy += translation_ms * share * cos(radians(heading_deg))

    return sqrt(ux * ux + uy * uy) * MS_TO_KT


def radius_of_kt(
    threshold_kt: int,
    bearing_deg: float,
    *,
    rmw_nm: float,
    b: float,
    delta_p_hpa: float,
    lat: float,
    translation_ms: float = 0.0,
    heading_deg: float = 0.0,
    northern: bool = True,
    step_nm: float = 2.0,
) -> float:
    """How far `threshold_kt` reaches along one bearing, in nautical miles.

    **The outermost crossing, not the first.** The profile rises from the eye
    to a peak at the radius of maximum wind and falls away outside it, so a
    threshold below the peak is crossed twice. Only the outer crossing bounds
    the area that experiences that wind — taking the inner one would report a
    hurricane a few miles wide.

    Walks outward on a fixed step and refines the bracket by bisection. A
    closed-form inverse of Holland's profile exists only without the Coriolis
    term; with it, and with translation added on top, scanning is both simpler
    and impossible to get subtly wrong.
    """
    rmw_km = rmw_nm * NM_TO_KM
    kw = dict(
        rmw_km=rmw_km,
        b=b,
        delta_p_hpa=delta_p_hpa,
        lat=lat,
        translation_ms=translation_ms,
        heading_deg=heading_deg,
        northern=northern,
    )

    last_inside = 0.0
    r_nm = max(step_nm, rmw_nm)
    while r_nm <= MAX_SEARCH_NM:
        if surface_wind_kt(r_nm * NM_TO_KM, bearing_deg, **kw) >= threshold_kt:
            last_inside = r_nm
        elif last_inside:
            lo, hi = last_inside, r_nm
            for _ in range(24):
                mid = (lo + hi) / 2.0
                if surface_wind_kt(mid * NM_TO_KM, bearing_deg, **kw) >= threshold_kt:
                    lo = mid
                else:
                    hi = mid
            return round(lo)
        r_nm += step_nm

    return round(last_inside)


def fit_b_to_r34(
    r34_nm: float,
    *,
    rmw_nm: float,
    delta_p_hpa: float,
    lat: float,
    translation_ms: float = 0.0,
) -> float:
    """Choose the profile shape that reaches 34 kt where the storm actually did.

    **This is the correction that makes the model usable, and it is worth being
    explicit about why it is needed.** Holland's B derived from peak wind alone
    fixes the profile at its peak and lets the outer field fall where it may.
    Measured against storms we have radii for, that under-predicts badly — the
    34 kt field of Gilbert 1988 came out at 64 nm against a measured 250, and
    Matthew 2016 at 20 nm against 170. The literature says the same thing:
    a single B systematically under-predicts wind far from the centre.

    So B is fitted rather than derived. Size is a second, independent property
    of a storm — two hurricanes of identical intensity can differ threefold in
    extent — and a model with one free parameter cannot express both. Given an
    outer radius, this finds the B that reproduces it.

    Lower B means a flatter, broader profile, so the search is monotone and
    bisection is safe.

    Floored at 1.0, the literature's lower bound. Without the floor the fit
    will happily flatten a major hurricane until its 64 kt field disappears
    entirely in order to reach a wide 34 kt radius — a profile no instrument
    has measured, and a storm that would report no hurricane-force wind at all.
    Where the two cannot both be satisfied, intensity wins and the outer radius
    comes up short.
    """
    lo, hi = 1.0, 2.5
    for _ in range(28):
        mid = (lo + hi) / 2.0
        reach = radius_of_kt(
            34,
            0.0,
            rmw_nm=rmw_nm,
            b=mid,
            delta_p_hpa=delta_p_hpa,
            lat=lat,
            translation_ms=translation_ms,
            northern=lat >= 0,
            step_nm=5.0,
        )
        if reach > r34_nm:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def climatological_r34_nm(*, vmax_kt: float, lat: float) -> float:
    """How far 34 kt reaches, for a storm whose size nobody recorded.

    Storm size correlates only weakly with intensity — the relationship is real
    but loose, and latitude matters because systems broaden as they move
    poleward. This is a stand-in for a measurement, used when the archive has
    none, and the screen must not present what follows from it as observed.

    Anchored on the median extent of the storms in this archive that do carry
    measured radii, which keeps it in the right range for the Atlantic rather
    than for the global mean.
    """
    base = 60.0 + 1.15 * max(0.0, vmax_kt - 34.0)
    return max(40.0, min(280.0, base * (1.0 + 0.02 * max(0.0, abs(lat) - 15.0))))


def radii_for(
    *,
    vmax_kt: float,
    pressure_mb: float | None,
    lat: float,
    rmw_nm: float | None = None,
    r34_nm: float | None = None,
    translation_kt: float = 0.0,
    heading_deg: float = 0.0,
    thresholds: tuple[int, ...] = (34, 50, 64),
    ambient_mb: float = AMBIENT_MB,
) -> tuple[Radii, ...]:
    """The four quadrant radii per threshold — what the polygon builder wants.

    This is the whole point of the module: it produces exactly the shape NHC
    publishes, so a modelled storm and a measured one travel the same path
    through the rest of the system and nothing downstream has to know which is
    which. What must not be lost is that the caller knows, and says so.

    A threshold the storm never reaches is omitted rather than returned as
    zeros, matching `quadrant_polygon_wkt`, which returns None for an empty
    ring rather than drawing a point.
    """
    vmax_ms = max(0.0, vmax_kt) * KT_TO_MS

    if pressure_mb and pressure_mb < ambient_mb:
        delta_p = ambient_mb - pressure_mb
    else:
        # No pressure in the archive — invert Holland at the peak instead. The
        # relation is the same equation solved the other way, so a storm with a
        # wind and no pressure and a storm with a pressure and no wind end up
        # on the same profile.
        delta_p = (vmax_ms**2) * RHO_AIR * exp(1.0) / 100.0 if vmax_ms > 0 else 0.0
    if delta_p <= 0:
        return ()

    translation_ms = max(0.0, translation_kt) * KT_TO_MS
    northern = lat >= 0
    climatological = climatological_r34_nm(vmax_kt=vmax_kt, lat=lat)
    target_r34 = r34_nm if r34_nm else climatological

    if rmw_nm:
        rmw = rmw_nm
    else:
        # A storm's size is coherent: a system with a 250 nm gale radius does
        # not have the eyewall of one with 80 nm. Scaling the core with the
        # outer field keeps the profile physical, and without it the shape
        # parameter alone has to stretch the whole storm — which it cannot do
        # past its floor, so the size control silently stops responding.
        rmw = estimate_rmw_nm(delta_p_hpa=delta_p, lat=lat) * (
            target_r34 / climatological if climatological > 0 else 1.0
        )
        rmw = max(4.0, min(90.0, rmw))

    # Size is fitted, intensity is derived. B from peak wind alone reproduces
    # the peak and badly under-predicts the outer field; fitting it to a 34 kt
    # radius reproduces both.
    if target_r34 > rmw:
        b = fit_b_to_r34(
            target_r34,
            rmw_nm=rmw,
            delta_p_hpa=delta_p,
            lat=lat,
            translation_ms=translation_ms,
        )
    else:
        b = holland_b(vmax_ms=vmax_ms, delta_p_hpa=delta_p, lat=lat)

    out: list[Radii] = []
    for threshold in thresholds:
        quad: dict[str, int] = {}
        for bearing, name in QUADRANTS:
            quad[name] = int(
                radius_of_kt(
                    threshold,
                    bearing,
                    rmw_nm=rmw,
                    b=b,
                    delta_p_hpa=delta_p,
                    lat=lat,
                    translation_ms=translation_ms,
                    heading_deg=heading_deg,
                    northern=northern,
                )
            )
        if any(quad.values()):
            out.append(Radii(threshold_kt=threshold, **quad))
    return tuple(out)


def bearing_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Initial great-circle bearing from a to b, degrees clockwise from north."""
    lat1, lon1, lat2, lon2 = map(radians, (a[0], a[1], b[0], b[1]))
    dlon = lon2 - lon1
    y = sin(dlon) * cos(lat2)
    x = cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(dlon)
    return (atan2(y, x) * 180.0 / pi) % 360.0
