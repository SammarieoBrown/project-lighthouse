import { clampScenario, distanceNm, type AuthoredScenario, type LngLat } from "./model.ts";

export type SizeProvenance = "measured" | "mixed" | "modelled" | "unavailable";

export type CatalogueEntry = {
  id: string;
  label: string;
  name: string;
  year: number;
  closestKm: number;
  peakWindKt: number;
  points: number;
  provenance: SizeProvenance;
};

export type StormCatalogue = {
  stormCount: number;
  storms: CatalogueEntry[];
};

export type ArchiveTrackPoint = {
  at: string;
  lat: number;
  lon: number;
  maxWindKt?: number;
  pressureMb?: number;
  rmwNm?: number;
  r34Nm?: number;
  status?: string;
};

export type ArchiveTrack = {
  id: string;
  label: string;
  provenance: SizeProvenance;
  positions: ArchiveTrackPoint[];
};

export type TrackLibrary = {
  stormCount: number;
  storms: ArchiveTrack[];
};

const STORM_ID = /^al\d{6}$/;
const PROVENANCE = new Set<SizeProvenance>(["measured", "mixed", "modelled", "unavailable"]);
const JAMAICA: LngLat = [-77.3, 18.11];
const RELEVANT_WINDOW_NM = 500;

export function validateStormCatalogue(raw: unknown): StormCatalogue {
  const source = objectAt(raw, "catalogue.json");
  if (source.schema !== "lighthouse.storm-catalogue.v1") {
    throw new Error("catalogue.json has an unsupported schema");
  }
  const rawStorms = arrayAt(source.storms, "catalogue.storms");
  const stormCount = integerAt(source.storm_count, "catalogue.storm_count");
  if (stormCount !== rawStorms.length) {
    throw new Error(`catalogue declares ${stormCount} storms but contains ${rawStorms.length}`);
  }
  const ids = new Set<string>();
  const storms = rawStorms.map((value, index): CatalogueEntry => {
    const path = `catalogue.storms[${index}]`;
    const row = objectAt(value, path);
    const id = stormIdAt(row.id, `${path}.id`);
    if (ids.has(id)) throw new Error(`${path}.id duplicates ${id}`);
    ids.add(id);
    const provenance = provenanceAt(row.provenance, `${path}.provenance`);
    return {
      id,
      label: stringAt(row.label, `${path}.label`),
      name: stringAt(row.name, `${path}.name`),
      year: integerAt(row.year, `${path}.year`),
      closestKm: nonNegativeAt(row.closest_km, `${path}.closest_km`),
      peakWindKt: nonNegativeAt(row.peak_wind_kt, `${path}.peak_wind_kt`),
      points: integerAt(row.points, `${path}.points`),
      provenance,
    };
  });
  return { stormCount, storms };
}

export function validateTrackLibrary(raw: unknown): TrackLibrary {
  const source = objectAt(raw, "catalogue-tracks.json");
  if (source.schema !== "lighthouse.storm-track-library.v1") {
    throw new Error("catalogue-tracks.json has an unsupported schema");
  }
  const rawStorms = arrayAt(source.storms, "trackLibrary.storms");
  const stormCount = integerAt(source.storm_count, "trackLibrary.storm_count");
  if (stormCount !== rawStorms.length) {
    throw new Error(`track library declares ${stormCount} storms but contains ${rawStorms.length}`);
  }
  const ids = new Set<string>();
  const storms = rawStorms.map((value, index): ArchiveTrack => {
    const path = `trackLibrary.storms[${index}]`;
    const row = objectAt(value, path);
    const id = stormIdAt(row.id, `${path}.id`);
    if (ids.has(id)) throw new Error(`${path}.id duplicates ${id}`);
    ids.add(id);
    const rawPositions = arrayAt(row.positions, `${path}.positions`);
    if (rawPositions.length < 1) throw new Error(`${path}.positions must contain at least one fix`);
    let prior = -Infinity;
    const positions = rawPositions.map((position, positionIndex): ArchiveTrackPoint => {
      const pointPath = `${path}.positions[${positionIndex}]`;
      const item = objectAt(position, pointPath);
      const at = timestampAt(item.at, `${pointPath}.at`);
      const timestamp = Date.parse(at);
      if (timestamp <= prior) throw new Error(`${pointPath}.at is not later than the prior fix`);
      prior = timestamp;
      const lat = finiteAt(item.lat, `${pointPath}.lat`);
      const lon = finiteAt(item.lon, `${pointPath}.lon`);
      if (lat < -90 || lat > 90 || lon < -180 || lon > 180) {
        throw new Error(`${pointPath} is outside WGS84 bounds`);
      }
      return {
        at,
        lat,
        lon,
        ...(item.max_wind_kt === undefined
          ? {}
          : { maxWindKt: nonNegativeAt(item.max_wind_kt, `${pointPath}.max_wind_kt`) }),
        ...(item.pressure_mb === undefined
          ? {}
          : { pressureMb: nonNegativeAt(item.pressure_mb, `${pointPath}.pressure_mb`) }),
        ...(item.rmw_nm === undefined
          ? {}
          : { rmwNm: nonNegativeAt(item.rmw_nm, `${pointPath}.rmw_nm`) }),
        ...(item.r34_nm === undefined
          ? {}
          : { r34Nm: nonNegativeAt(item.r34_nm, `${pointPath}.r34_nm`) }),
        ...(item.status === undefined ? {} : { status: stringAt(item.status, `${pointPath}.status`) }),
      };
    });
    return {
      id,
      label: stringAt(row.label, `${path}.label`),
      provenance: provenanceAt(row.provenance, `${path}.provenance`),
      positions,
    };
  });
  return { stormCount, storms };
}

export function scenarioFromArchiveTrack(track: ArchiveTrack): AuthoredScenario {
  const positions = relevantPositions(track.positions);
  const coordinates = downsampleTrack(positions.map(({ lon, lat }) => [lon, lat]));
  const winds = positions.flatMap(({ maxWindKt }) => maxWindKt === undefined ? [] : [maxWindKt]);
  const r34 = positions.flatMap((position) => position.r34Nm && position.r34Nm > 0 ? [position.r34Nm] : []);
  return clampScenario({
    name: `${track.label} edited scenario`,
    track: coordinates,
    maxWindKt: Math.max(34, ...winds),
    radius34Nm: roundToFive(percentile(r34, 0.75) || 145),
    forwardSpeedKt: estimateForwardSpeed(positions),
    startAt: positions[0].at,
  });
}

function relevantPositions(positions: ArchiveTrackPoint[]): ArchiveTrackPoint[] {
  if (positions.length <= 1) return positions;
  const inside = positions
    .map((position, index) => ({ index, distance: distanceNm([position.lon, position.lat], JAMAICA) }))
    .filter(({ distance }) => distance <= RELEVANT_WINDOW_NM)
    .map(({ index }) => index);
  const closest = positions.reduce(
    (best, position, index) => {
      const distance = distanceNm([position.lon, position.lat], JAMAICA);
      return distance < best.distance ? { index, distance } : best;
    },
    { index: 0, distance: Number.POSITIVE_INFINITY },
  ).index;
  const first = inside[0] ?? closest;
  const last = inside.at(-1) ?? closest;
  const start = Math.max(0, first - 1);
  const end = Math.min(positions.length, Math.max(last + 2, start + 2));
  return positions.slice(start, end);
}

function downsampleTrack(points: LngLat[], maximum = 18): LngLat[] {
  if (points.length <= maximum) return points;
  const step = (points.length - 1) / (maximum - 1);
  const selected = Array.from({ length: maximum }, (_, index) => points[Math.round(index * step)]);
  return selected.filter((point, index) => index === 0 || point[0] !== selected[index - 1][0] || point[1] !== selected[index - 1][1]);
}

function estimateForwardSpeed(positions: ArchiveTrackPoint[]): number {
  let distance = 0;
  let hours = 0;
  for (let index = 1; index < positions.length; index += 1) {
    const previous = positions[index - 1];
    const current = positions[index];
    const elapsed = (Date.parse(current.at) - Date.parse(previous.at)) / 3_600_000;
    if (elapsed <= 0) continue;
    distance += distanceNm([previous.lon, previous.lat], [current.lon, current.lat]);
    hours += elapsed;
  }
  return Math.round(Math.min(40, Math.max(2, hours > 0 ? distance / hours : 12)));
}

function percentile(values: number[], share: number): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.floor((sorted.length - 1) * share))];
}

function roundToFive(value: number): number {
  return Math.round(Math.min(320, Math.max(25, value)) / 5) * 5;
}

function objectAt(value: unknown, path: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${path} must be an object`);
  return value as Record<string, unknown>;
}

function arrayAt(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value)) throw new Error(`${path} must be an array`);
  return value;
}

function stringAt(value: unknown, path: string): string {
  if (typeof value !== "string" || value.length === 0) throw new Error(`${path} must be a non-empty string`);
  return value;
}

function finiteAt(value: unknown, path: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) throw new Error(`${path} must be a finite number`);
  return value;
}

function nonNegativeAt(value: unknown, path: string): number {
  const number = finiteAt(value, path);
  if (number < 0) throw new Error(`${path} cannot be negative`);
  return number;
}

function integerAt(value: unknown, path: string): number {
  const number = nonNegativeAt(value, path);
  if (!Number.isInteger(number)) throw new Error(`${path} must be an integer`);
  return number;
}

function stormIdAt(value: unknown, path: string): string {
  const id = stringAt(value, path).toLowerCase();
  if (!STORM_ID.test(id)) throw new Error(`${path} is not an Atlantic storm identifier`);
  return id;
}

function provenanceAt(value: unknown, path: string): SizeProvenance {
  const provenance = stringAt(value, path) as SizeProvenance;
  if (!PROVENANCE.has(provenance)) throw new Error(`${path} has unknown provenance ${provenance}`);
  return provenance;
}

function timestampAt(value: unknown, path: string): string {
  const timestamp = stringAt(value, path);
  if (!timestamp.endsWith("Z") || !Number.isFinite(Date.parse(timestamp))) {
    throw new Error(`${path} must be a valid UTC timestamp`);
  }
  return timestamp;
}
