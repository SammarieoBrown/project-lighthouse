export type LngLat = [number, number];

export type AuthoredScenario = {
  name: string;
  track: LngLat[];
  maxWindKt: number;
  radius34Nm: number;
  forwardSpeedKt: number;
  startAt: string;
};

export type SimulationFrame = {
  index: number;
  centre: LngLat;
  headingDeg: number;
  elapsedHours: number;
  progress: number;
};

export type DistrictInventory = {
  id: number;
  parish: string;
  district: string;
  lon?: number;
  lat?: number;
  structures?: number;
};

export type HouseholdSample = {
  parish: string;
  community: string;
  roof?: string;
};

export type CommunityImpact = {
  key: string;
  parish: string;
  community: string;
  structures: number;
  windKt: number;
  destroyed: number;
  major: number;
  minor: number;
  none: number;
};

export type ImpactSummary = {
  assessedStructures: number;
  unavailableStructures: number;
  exposed34: number;
  exposed50: number;
  exposed64: number;
  destroyed: number;
  major: number;
  minor: number;
  none: number;
  communities: CommunityImpact[];
};

export type WindControl = Pick<
  AuthoredScenario,
  "maxWindKt" | "radius34Nm" | "forwardSpeedKt"
>;

export type WindVector = { eastKt: number; northKt: number; speedKt: number };

const EARTH_RADIUS_NM = 3440.065;
const DEG = Math.PI / 180;
const INFLOW_DEG = 22.6;
const MAX_RADIUS_NM = 600;
const THRESHOLDS = [34, 50, 64] as const;

const DEFAULT_SCENARIO: AuthoredScenario = {
  name: "Jamaica planning scenario",
  track: [
    [-79.2, 16.8],
    [-78.3, 17.25],
    [-77.45, 17.75],
    [-76.65, 18.12],
    [-75.8, 18.55],
  ],
  maxWindKt: 110,
  radius34Nm: 145,
  forwardSpeedKt: 12,
  startAt: "2026-08-03T12:00:00.000Z",
};

export function defaultScenario(): AuthoredScenario {
  return {
    ...DEFAULT_SCENARIO,
    track: DEFAULT_SCENARIO.track.map(([lon, lat]) => [lon, lat]),
  };
}

export function clampScenario(value: AuthoredScenario): AuthoredScenario {
  return {
    name: value.name.trim().slice(0, 80) || DEFAULT_SCENARIO.name,
    track: value.track
      .filter(([lon, lat]) => Number.isFinite(lon) && Number.isFinite(lat))
      .map(([lon, lat]) => [clamp(lon, -100, -40), clamp(lat, 0, 40)]),
    maxWindKt: Math.round(clamp(value.maxWindKt, 34, 180)),
    radius34Nm: Math.round(clamp(value.radius34Nm, 25, 320)),
    forwardSpeedKt: Math.round(clamp(value.forwardSpeedKt, 2, 40)),
    startAt: Number.isFinite(Date.parse(value.startAt))
      ? new Date(value.startAt).toISOString()
      : DEFAULT_SCENARIO.startAt,
  };
}

export function distanceNm(a: LngLat, b: LngLat): number {
  const lat1 = a[1] * DEG;
  const lat2 = b[1] * DEG;
  const dLat = (b[1] - a[1]) * DEG;
  const dLon = (b[0] - a[0]) * DEG;
  const h = Math.sin(dLat / 2) ** 2
    + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
  return 2 * EARTH_RADIUS_NM * Math.asin(Math.min(1, Math.sqrt(h)));
}

export function bearingDeg(a: LngLat, b: LngLat): number {
  const lat1 = a[1] * DEG;
  const lat2 = b[1] * DEG;
  const dLon = (b[0] - a[0]) * DEG;
  const east = Math.sin(dLon) * Math.cos(lat2);
  const north = Math.cos(lat1) * Math.sin(lat2)
    - Math.sin(lat1) * Math.cos(lat2) * Math.cos(dLon);
  return normalDegrees(Math.atan2(east, north) / DEG);
}

export function destination(origin: LngLat, bearing: number, distance: number): LngLat {
  const angular = distance / EARTH_RADIUS_NM;
  const theta = bearing * DEG;
  const lat1 = origin[1] * DEG;
  const lon1 = origin[0] * DEG;
  const lat2 = Math.asin(
    Math.sin(lat1) * Math.cos(angular)
      + Math.cos(lat1) * Math.sin(angular) * Math.cos(theta),
  );
  const lon2 = lon1 + Math.atan2(
    Math.sin(theta) * Math.sin(angular) * Math.cos(lat1),
    Math.cos(angular) - Math.sin(lat1) * Math.sin(lat2),
  );
  return [normalLongitude(lon2 / DEG), lat2 / DEG];
}

export function simulationFrames(
  scenario: AuthoredScenario,
  intervalHours = 1,
): SimulationFrame[] {
  if (scenario.track.length === 0) return [];
  if (scenario.track.length === 1) {
    return [{ index: 0, centre: scenario.track[0], headingDeg: 0, elapsedHours: 0, progress: 0 }];
  }

  const segments = scenario.track.slice(1).map((point, index) => ({
    from: scenario.track[index],
    to: point,
    distance: distanceNm(scenario.track[index], point),
    heading: bearingDeg(scenario.track[index], point),
  }));
  const total = segments.reduce((sum, segment) => sum + segment.distance, 0);
  if (total <= 0) return [{ index: 0, centre: scenario.track[0], headingDeg: 0, elapsedHours: 0, progress: 0 }];

  const hours = total / Math.max(1, scenario.forwardSpeedKt);
  // Preserve a real hourly cadence for the whole authored corridor. Capping
  // the number of frames while forcing the final frame to the endpoint makes
  // a long storm teleport hundreds of miles on its last step.
  const count = Math.max(2, Math.ceil(hours / intervalHours) + 1);
  const frames: SimulationFrame[] = [];
  for (let index = 0; index < count; index += 1) {
    const travelled = index === count - 1
      ? total
      : Math.min(total, index * intervalHours * scenario.forwardSpeedKt);
    let remaining = travelled;
    let segment = segments[segments.length - 1];
    for (const candidate of segments) {
      segment = candidate;
      if (remaining <= candidate.distance) break;
      remaining -= candidate.distance;
    }
    const share = segment.distance > 0 ? clamp(remaining / segment.distance, 0, 1) : 0;
    frames.push({
      index,
      centre: index === count - 1
        ? scenario.track[scenario.track.length - 1]
        : interpolateGreatCircle(segment.from, segment.to, share),
      headingDeg: segment.heading,
      elapsedHours: Math.min(hours, index * intervalHours),
      progress: travelled / total,
    });
  }
  return frames;
}

/**
 * A normalised Holland radial profile. Its maximum is exactly one at RMW.
 * Size and intensity remain separate controls: B is fitted to the requested
 * 34 kt extent, while the profile is scaled to the authoritative maximum wind.
 */
export function windVectorAt(
  point: LngLat,
  centre: LngLat,
  heading: number,
  control: WindControl,
): WindVector {
  const rNm = distanceNm(centre, point);
  if (rNm < 0.05 || rNm > MAX_RADIUS_NM) return { eastKt: 0, northKt: 0, speedKt: 0 };
  const bearing = bearingDeg(centre, point);
  const rmw = estimateRmwNm(control.maxWindKt);
  const b = fittedB(control, rmw, heading);
  return windVectorWithB(rNm, bearing, heading, control, rmw, b);
}

function windVectorWithB(
  rNm: number,
  bearing: number,
  heading: number,
  control: WindControl,
  rmw: number,
  b: number,
): WindVector {
  // The archive/authored maximum is Earth-relative and therefore already
  // contains the translation contribution. Reserve only half the forward
  // speed for asymmetry and scale the vortex to the remainder: on the aligned
  // flank at RMW the two vectors sum to maxWindKt exactly.
  const effectiveTranslation = Math.min(
    0.5 * control.forwardSpeedKt,
    0.5 * control.maxWindKt,
  );
  const translationAtR = effectiveTranslation * Math.min(1, rmw / rNm);
  const vortexPeak = Math.max(0, control.maxWindKt - effectiveTranslation);
  const vortex = vortexPeak * normalisedHolland(rNm, rmw, b);
  const flow = (bearing - 90 - INFLOW_DEG) * DEG;
  const motion = heading * DEG;
  const eastKt = vortex * Math.sin(flow) + translationAtR * Math.sin(motion);
  const northKt = vortex * Math.cos(flow) + translationAtR * Math.cos(motion);
  return { eastKt, northKt, speedKt: Math.hypot(eastKt, northKt) };
}

export function radiusAtThreshold(
  thresholdKt: number,
  bearing: number,
  heading: number,
  control: WindControl,
): number {
  if (control.maxWindKt < thresholdKt) return 0;
  const rmw = estimateRmwNm(control.maxWindKt);
  let lastInside = 0;
  for (let radius = Math.max(1, rmw); radius <= MAX_RADIUS_NM; radius += 2) {
    const point = destination([0, 0], bearing, radius);
    const speed = windVectorAt(point, [0, 0], heading, control).speedKt;
    if (speed >= thresholdKt) {
      lastInside = radius;
      continue;
    }
    if (!lastInside) continue;
    let lo = lastInside;
    let hi = radius;
    for (let iteration = 0; iteration < 18; iteration += 1) {
      const mid = (lo + hi) / 2;
      const sample = destination([0, 0], bearing, mid);
      if (windVectorAt(sample, [0, 0], heading, control).speedKt >= thresholdKt) lo = mid;
      else hi = mid;
    }
    return lo;
  }
  return lastInside;
}

export function windPolygon(
  centre: LngLat,
  heading: number,
  control: WindControl,
  thresholdKt: (typeof THRESHOLDS)[number],
  samples = 96,
): GeoJSON.Feature<GeoJSON.Polygon> | null {
  if (control.maxWindKt < thresholdKt) return null;
  const ring: LngLat[] = [];
  for (let index = 0; index < samples; index += 1) {
    const bearing = (index / samples) * 360;
    const radius = radiusAtThreshold(thresholdKt, bearing, heading, control);
    if (radius <= 0) return null;
    ring.push(destination(centre, bearing, radius));
  }
  ring.push(ring[0]);
  return {
    type: "Feature",
    properties: { thresholdKt, evidence: "synthesised" },
    geometry: { type: "Polygon", coordinates: [ring] },
  };
}

export function windFields(
  centre: LngLat,
  heading: number,
  control: WindControl,
): GeoJSON.FeatureCollection<GeoJSON.Polygon> {
  return {
    type: "FeatureCollection",
    features: THRESHOLDS
      .map((threshold) => windPolygon(centre, heading, control, threshold))
      .filter((feature): feature is GeoJSON.Feature<GeoJSON.Polygon> => feature !== null),
  };
}

export function calculateImpact(
  districts: DistrictInventory[],
  households: HouseholdSample[],
  centre: LngLat,
  heading: number,
  control: WindControl,
): ImpactSummary {
  const shares = roofShares(households);
  const communities: CommunityImpact[] = [];
  let unavailableStructures = 0;

  for (const district of districts) {
    const structures = district.structures ?? 0;
    if (!Number.isFinite(district.lon) || !Number.isFinite(district.lat) || structures <= 0) {
      unavailableStructures += structures;
      continue;
    }
    const windKt = windVectorAt(
      [district.lon as number, district.lat as number],
      centre,
      heading,
      control,
    ).speedKt;
    const localShares = shares.byCommunity.get(communityKey(district.parish, district.district))
      ?? shares.byParish.get(normalise(district.parish))
      ?? shares.national;
    const counts = impactCounts(structures, windKt, localShares);
    communities.push({
      key: String(district.id),
      parish: district.parish,
      community: district.district,
      structures,
      windKt,
      ...counts,
    });
  }

  const total = communities.reduce(
    (sum, community) => ({
      assessedStructures: sum.assessedStructures + community.structures,
      exposed34: sum.exposed34 + (community.windKt >= 34 ? community.structures : 0),
      exposed50: sum.exposed50 + (community.windKt >= 50 ? community.structures : 0),
      exposed64: sum.exposed64 + (community.windKt >= 64 ? community.structures : 0),
      destroyed: sum.destroyed + community.destroyed,
      major: sum.major + community.major,
      minor: sum.minor + community.minor,
      none: sum.none + community.none,
    }),
    { assessedStructures: 0, exposed34: 0, exposed50: 0, exposed64: 0, destroyed: 0, major: 0, minor: 0, none: 0 },
  );

  return {
    ...total,
    unavailableStructures,
    communities: communities.sort(
      (a, b) => (b.destroyed + b.major) - (a.destroyed + a.major) || b.windKt - a.windKt,
    ),
  };
}

function estimateRmwNm(maxWindKt: number): number {
  return clamp(52 - 0.32 * maxWindKt, 8, 42);
}

function fittedB(control: WindControl, rmwNm: number, heading: number): number {
  const target = 34;
  let low = 0.55;
  let high = 3.5;
  // Northern-hemisphere inflow means the vortex vector aligns with forward
  // motion at heading + 90° + inflow. Fitting the requested size there makes
  // radius34Nm the maximum 34 kt reach, independent of the storm heading.
  const alignedBearing = normalDegrees(heading + 90 + INFLOW_DEG);
  for (let index = 0; index < 32; index += 1) {
    const mid = (low + high) / 2;
    const speed = windVectorWithB(
      control.radius34Nm,
      alignedBearing,
      heading,
      control,
      rmwNm,
      mid,
    ).speedKt;
    // At an outer radius, a larger B makes the field decay faster.
    if (speed > target) low = mid;
    else high = mid;
  }
  return clamp((low + high) / 2, 0.55, 3.5);
}

function normalisedHolland(rNm: number, rmwNm: number, b: number): number {
  if (rNm <= 0) return 0;
  const x = (rmwNm / rNm) ** b;
  return Math.sqrt(Math.max(0, x * Math.exp(1 - x)));
}

type Roof = "zinc" | "shingle" | "tile" | "concrete";
type RoofShare = Record<Roof, number>;

const ROOFS: Roof[] = ["zinc", "shingle", "tile", "concrete"];

function roofShares(households: HouseholdSample[]): {
  byCommunity: Map<string, RoofShare>;
  byParish: Map<string, RoofShare>;
  national: RoofShare;
} {
  const communityCounts = new Map<string, Record<Roof, number>>();
  const parishCounts = new Map<string, Record<Roof, number>>();
  const nationalCounts = emptyRoofs();
  for (const household of households) {
    const roof = knownRoof(household.roof);
    nationalCounts[roof] += 1;
    incrementRoof(communityCounts, communityKey(household.parish, household.community), roof);
    incrementRoof(parishCounts, normalise(household.parish), roof);
  }
  const national = toShares(nationalCounts);
  return {
    byCommunity: new Map([...communityCounts].map(([key, value]) => [key, toShares(value, national)])),
    byParish: new Map([...parishCounts].map(([key, value]) => [key, toShares(value, national)])),
    national,
  };
}

/* Damage against wind speed, per roof class.
 *
 * This replaced a four-branch step function keyed on the same 34/50/64 kt
 * thresholds the wind polygons use, and that function was wrong in a way that
 * was visible from across the room: at 64 kt it assigned *every* weak roof to
 * destroyed and *every* strong roof to major, so a Cat 1 brushing the coast did
 * the same damage as a Cat 5 landfall, and nothing above 64 kt changed the
 * answer at all. A 110 kt scenario reported 1,166,716 destroyed — 63% of every
 * mapped building in Jamaica, which is the zinc share of the inventory and not
 * a damage estimate. It was reporting the roof census.
 *
 * Two things were broken and both are fixed here. Damage is now continuous in
 * wind speed, so intensity changes the outcome across the whole range; and it
 * is a probability rather than a certainty, so at any given speed a roof class
 * spreads across damage states instead of moving to one of them wholesale.
 *
 * The curves are lognormal exceedance functions — P(damage ≥ state) =
 * Φ(ln(v/θ)/β) — which is the standard form in the wind-vulnerability
 * literature and what HAZUS-style fragility uses. θ is the wind at which half
 * the class has reached that state; β is the spread.
 *
 * The medians below are indicative and ordered by how Jamaican roofs actually
 * fail: sheet zinc lifts first because the fastenings go before the sheet does,
 * concrete slab last. They are not calibrated against a Jamaican loss record —
 * there is no such dataset in this repository — and rule C3 means the surface
 * has to say so rather than imply a precision the numbers do not carry. What
 * they are is monotonic, continuous, and defensible in shape.
 */
const FRAGILITY: Record<Roof, { minor: number; major: number; destroyed: number }> = {
  /* Sheet metal on timber purlins, frequently retrofitted and under-fastened.
   * The dominant Jamaican roof and the reason the weak/strong split existed. */
  zinc: { minor: 45, major: 70, destroyed: 100 },
  shingle: { minor: 50, major: 80, destroyed: 115 },
  tile: { minor: 55, major: 90, destroyed: 130 },
  /* Cast slab. Loses openings and finishes long before it loses the roof. */
  concrete: { minor: 65, major: 110, destroyed: 150 },
};

/* One dispersion for every class. A per-class β would be four more numbers with
 * no evidence behind any of them; 0.4 is the middle of the range these curves
 * are usually published with. */
const FRAGILITY_BETA = 0.4;

/* Standard normal CDF via the Abramowitz & Stegun 7.1.26 error function.
 * Max absolute error 1.5e-7, which is far inside anything this model claims. */
function normalCdf(x: number): number {
  const sign = x < 0 ? -1 : 1;
  const z = Math.abs(x) / Math.SQRT2;
  const t = 1 / (1 + 0.3275911 * z);
  const y = 1 - ((((1.061405429 * t - 1.453152027) * t + 1.421413741) * t
    - 0.284496736) * t + 0.254829592) * t * Math.exp(-z * z);
  return 0.5 * (1 + sign * y);
}

/* P(damage ≥ state) for one roof class at one wind speed. */
function exceedance(windKt: number, medianKt: number): number {
  if (windKt <= 0) return 0;
  return normalCdf(Math.log(windKt / medianKt) / FRAGILITY_BETA);
}

function impactCounts(structures: number, windKt: number, shares: RoofShare) {
  /* Below tropical-storm force nothing is claimed. The curves return small but
   * non-zero probabilities down at 20 kt, and a model that reports roof damage
   * in a stiff breeze discredits the numbers that matter. */
  if (windKt < 34 || structures <= 0) {
    return { destroyed: 0, major: 0, minor: 0, none: structures };
  }

  let destroyed = 0;
  let major = 0;
  let minor = 0;

  for (const roof of ROOFS) {
    const share = shares[roof];
    if (share <= 0) continue;
    const count = structures * share;
    const curve = FRAGILITY[roof];
    /* Cumulative, then differenced into exclusive states. The medians are
     * strictly ordered, so each difference is non-negative by construction. */
    const pDestroyed = exceedance(windKt, curve.destroyed);
    const pMajor = exceedance(windKt, curve.major);
    const pMinor = exceedance(windKt, curve.minor);
    destroyed += count * pDestroyed;
    major += count * (pMajor - pDestroyed);
    minor += count * (pMinor - pMajor);
  }

  /* Rounded once at the end and with the remainder absorbed by `none`, so the
   * four states still sum to the structure count exactly — the invariant the
   * model test asserts and the reason the panel's totals can be trusted to
   * add up. */
  const outDestroyed = Math.round(destroyed);
  const outMajor = Math.round(major);
  const outMinor = Math.round(minor);
  return {
    destroyed: outDestroyed,
    major: outMajor,
    minor: outMinor,
    none: Math.max(0, structures - outDestroyed - outMajor - outMinor),
  };
}

function emptyRoofs(): Record<Roof, number> {
  return { zinc: 0, shingle: 0, tile: 0, concrete: 0 };
}

function incrementRoof(map: Map<string, Record<Roof, number>>, key: string, roof: Roof) {
  const counts = map.get(key) ?? emptyRoofs();
  counts[roof] += 1;
  map.set(key, counts);
}

function toShares(counts: Record<Roof, number>, fallback?: RoofShare): RoofShare {
  const total = Object.values(counts).reduce((sum, value) => sum + value, 0);
  if (total <= 0) return fallback ?? { zinc: 0.5, shingle: 0.1, tile: 0.1, concrete: 0.3 };
  return {
    zinc: counts.zinc / total,
    shingle: counts.shingle / total,
    tile: counts.tile / total,
    concrete: counts.concrete / total,
  };
}

function knownRoof(value?: string): Roof {
  return value === "shingle" || value === "tile" || value === "concrete" ? value : "zinc";
}

function communityKey(parish: string, community: string) {
  return `${normalise(parish)}::${normalise(community)}`;
}

function normalise(value: string) {
  return value.trim().toLowerCase().replace(/^st\.?\s+/, "saint ");
}

function interpolateGreatCircle(a: LngLat, b: LngLat, share: number): LngLat {
  const distance = distanceNm(a, b) / EARTH_RADIUS_NM;
  if (distance < 1e-9) return a;
  const sinDistance = Math.sin(distance);
  const left = Math.sin((1 - share) * distance) / sinDistance;
  const right = Math.sin(share * distance) / sinDistance;
  const aLat = a[1] * DEG;
  const aLon = a[0] * DEG;
  const bLat = b[1] * DEG;
  const bLon = b[0] * DEG;
  const x = left * Math.cos(aLat) * Math.cos(aLon) + right * Math.cos(bLat) * Math.cos(bLon);
  const y = left * Math.cos(aLat) * Math.sin(aLon) + right * Math.cos(bLat) * Math.sin(bLon);
  const z = left * Math.sin(aLat) + right * Math.sin(bLat);
  return [Math.atan2(y, x) / DEG, Math.atan2(z, Math.hypot(x, y)) / DEG];
}

function normalDegrees(value: number) {
  return ((value % 360) + 360) % 360;
}

function normalLongitude(value: number) {
  return ((value + 540) % 360) - 180;
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}
