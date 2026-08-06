import {
  bearingDeg,
  destination,
  distanceNm,
  windVectorAt,
  type LngLat,
  type WindControl,
} from "../../simulator/model";

/* Modelled circulation for a published advisory.
 *
 * The replay map draws the advisory's wind field as three threshold polygons,
 * and those polygons are the evidence: NHC publishes the largest extent of 34,
 * 50 and 64 kt anywhere in each quadrant, and the blunt stepped shape is the
 * faithful rendering of exactly that claim. It is not smoothed here and must
 * not be — a curve through four quadrant maxima draws a *smaller* field than
 * the advisory states across most of every quadrant, which on a hazard map is
 * the wrong direction to be wrong in. See apps/api/app/nhc/geometry.py, where
 * that decision is argued at length.
 *
 * What the polygons cannot show is that a hurricane turns. Three nested
 * outlines say how far the wind reaches; they say nothing about which way it
 * blows, and an operator reading a wind field wants both. This module supplies
 * the second half — a modelled surface circulation for the selected advisory,
 * drawn as short flow marks over the published outlines.
 *
 * Every parameter it runs on is derived from that advisory and nothing else:
 * the centre and intensity the advisory states, the heading of its own forecast
 * track, the speed measured between its own frames, and a 34 kt radius read off
 * its own published polygon. It invents no rainfall — the simulator's modelled
 * precipitation field stays in the simulator, because an advisory publishes no
 * precipitation and drawing one here would be the exact failure the stepped
 * polygons exist to avoid.
 *
 * The marks are modelled and the surface says so. They are a reading of the
 * advisory, not a second measurement of it.
 */

export type AdvisoryWind = {
  centre: LngLat;
  headingDeg: number;
  control: WindControl;
};

/* The simulator's control ranges, so a field derived here cannot ask the shared
 * wind model for something the authored path could never produce. */
const MIN_R34_NM = 25;
const MAX_R34_NM = 320;
const MIN_FORWARD_KT = 2;
const MAX_FORWARD_KT = 40;

const FLOW_MARKS = 240;
const FLOW_BOUND_SECTORS = 72;

/* Below tropical-storm force there is no organised circulation worth drawing,
 * and the parametric profile is not meaningful there either. */
const MIN_INTENSITY_KT = 34;

type Ring = number[][];

export function advisoryWind(
  centre: [number, number] | null | undefined,
  maxWindKt: number | undefined,
  headingDeg: number | null,
  forwardSpeedKt: number | null,
  wind34: { type: string; coordinates: unknown } | null | undefined,
): AdvisoryWind | null {
  if (!centre || maxWindKt === undefined || maxWindKt < MIN_INTENSITY_KT) return null;
  if (headingDeg === null || !Number.isFinite(headingDeg)) return null;

  const radius34Nm = radius34From(wind34, centre, headingDeg);
  if (radius34Nm === null) return null;

  return {
    centre,
    headingDeg,
    control: {
      maxWindKt,
      radius34Nm,
      forwardSpeedKt: clamp(forwardSpeedKt ?? 10, MIN_FORWARD_KT, MAX_FORWARD_KT),
    },
  };
}

/* How far 34 kt reaches, read off the advisory's own polygon.
 *
 * The polygon is not one ring. Per the replay contract it is the union of the
 * wind field over the next 48 hours of forecast track, so ahead of the storm it
 * is a swath hundreds of miles long and tells you nothing about the field right
 * now. Behind the storm it is a different matter: the union only ever adds
 * *future* positions, so nothing has been swept into the rear semicircle and
 * the boundary there is the current ring and only the current ring.
 *
 * So the radius is measured behind the storm, and taken as the maximum over
 * that arc — because a quadrant radius is itself a maximum, and reading it back
 * as an average would quietly shrink the field.
 */
function radius34From(
  geometry: { type: string; coordinates: unknown } | null | undefined,
  centre: LngLat,
  headingDeg: number,
): number | null {
  const rings = ringsOf(geometry);
  if (rings.length === 0) return null;

  /* The rear arc, generously bounded away from the flanks so a storm turning
   * inside its own swath cannot pick up the forward sweep. */
  const rearBearing = normalDegrees(headingDeg + 180);
  let best = 0;
  for (const ring of rings) {
    for (const point of ring) {
      if (point.length < 2) continue;
      const target: LngLat = [point[0], point[1]];
      const offset = Math.abs(angleDelta(bearingDeg(centre, target), rearBearing));
      if (offset > 70) continue;
      best = Math.max(best, distanceNm(centre, target));
    }
  }

  if (best <= 0) return null;
  return clamp(best, MIN_R34_NM, MAX_R34_NM);
}

/* Short trails, one per sampled parcel, oriented along the modelled flow.
 *
 * Identical in construction to the simulator's, because it is the same claim
 * about the same physics and two implementations of one idea drift. The
 * rotation comes from `phaseRad`, which the caller advances with real time.
 */
export function advisoryFlow(
  wind: AdvisoryWind | null,
  phaseRad: number,
): GeoJSON.FeatureCollection<GeoJSON.LineString> {
  if (!wind) return { type: "FeatureCollection", features: [] };

  const bounds = flowBounds(wind.control);
  const features: Array<GeoJSON.Feature<GeoJSON.LineString>> = [];

  for (let index = 0; index < FLOW_MARKS; index += 1) {
    const baseBearing = hashUnit(index, 17, 0x51ed270b) * Math.PI * 2;
    const radialShare = 0.06 + Math.pow(hashUnit(index, 31, 0x9e3779b9), 0.66) * 0.91;
    const bearingRad = normalRadians(
      baseBearing - phaseRad * (0.72 + (1 - radialShare) * 1.95),
    );
    const bearing = (bearingRad * 180) / Math.PI;

    /* A hurricane is not a set of concentric circles and a field of evenly
     * spaced marks reads as one. The perturbation is deterministic in bearing,
     * so a parcel does not jitter between frames. */
    const irregular = 1
      + 0.09 * Math.sin(bearingRad * 3 + (wind.headingDeg * Math.PI) / 360)
      + 0.05 * Math.sin(bearingRad * 7 - 0.8)
      + 0.025 * Math.sin(bearingRad * 13 + 1.4);

    const outerGap = hashUnit(index, 53, 0x85ebca6b);
    const sectorDensity = clamp(
      0.72 + 0.14 * Math.sin(bearingRad * 3.4 + 0.6) + 0.08 * Math.sin(bearingRad * 8.1),
      0.44,
      0.92,
    );
    if (radialShare > 0.58 && outerGap > sectorDensity) continue;

    const radiusNm = radialShare * boundAt(bounds, bearing) * irregular;
    const point = destination(wind.centre, bearing, radiusNm);
    const vector = windVectorAt(point, wind.centre, wind.headingDeg, wind.control);
    if (vector.speedKt < 8) continue;

    const direction = normalDegrees(
      (Math.atan2(vector.eastKt, vector.northKt) * 180) / Math.PI,
    );
    const trailNm = clamp(1.8 + vector.speedKt / 15, 2.2, 8.6);
    features.push({
      type: "Feature",
      properties: { speedKt: Number(vector.speedKt.toFixed(1)) },
      geometry: {
        type: "LineString",
        coordinates: [destination(point, direction + 180, trailNm), point],
      },
    });
  }

  return { type: "FeatureCollection", features };
}

function flowBounds(control: WindControl): number[] {
  const lower = Math.max(65, control.radius34Nm * 0.68);
  const upper = Math.max(110, control.radius34Nm * 1.8);
  return Array.from({ length: FLOW_BOUND_SECTORS }, (_, index) => {
    const share = index / FLOW_BOUND_SECTORS;
    return lower + (upper - lower) * (0.5 - 0.5 * Math.cos(share * Math.PI * 2));
  });
}

function boundAt(bounds: number[], bearing: number): number {
  const index = Math.round((normalDegrees(bearing) / 360) * bounds.length) % bounds.length;
  return bounds[index] ?? bounds[0];
}

function ringsOf(geometry: { type: string; coordinates: unknown } | null | undefined): Ring[] {
  if (!geometry) return [];
  if (geometry.type === "Polygon") return (geometry.coordinates as Ring[]) ?? [];
  if (geometry.type === "MultiPolygon") {
    return ((geometry.coordinates as Ring[][]) ?? []).flat();
  }
  return [];
}

/* Signed difference between two bearings, in [-180, 180]. */
function angleDelta(a: number, b: number): number {
  return ((a - b + 540) % 360) - 180;
}

function normalDegrees(value: number): number {
  return ((value % 360) + 360) % 360;
}

function normalRadians(value: number): number {
  const tau = Math.PI * 2;
  return ((value % tau) + tau) % tau;
}

function hashUnit(index: number, salt: number, seed: number): number {
  let hash = Math.imul(index + salt, seed) ^ seed;
  hash = Math.imul(hash ^ (hash >>> 15), 0x2c1b3c6d);
  hash = Math.imul(hash ^ (hash >>> 12), 0x297a2d39);
  return ((hash ^ (hash >>> 15)) >>> 0) / 4294967296;
}

function clamp(value: number, low: number, high: number): number {
  return Math.max(low, Math.min(high, value));
}
