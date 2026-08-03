/* The replay reader.
 *
 * One generated file drives the whole console. Its shape is fixed by
 * docs/engineering/replay-export-contract.md — the agreement between the thing
 * that writes it (apps/api) and this. Nothing here invents a field, and nothing
 * here assumes a field the contract calls optional.
 *
 * A file, not an endpoint, because the console has to work on a venue network
 * that may not exist. Live operation returns this same shape, so the reader does
 * not change when it arrives.
 */

import { useEffect, useState } from "react";

import type { District, Household, Snapshot } from "./map";

export type Posture = "QUIET" | "WATCH" | "READY" | "ACT";

type Geometry = { type: "Polygon" | "MultiPolygon"; coordinates: unknown };
type LineGeometry = { type: "LineString"; coordinates: number[][] };

export type ReplayFrame = {
  /** Advisory number as printed by NHC — a string because NHC prints "11A". */
  n: string;
  at: string;
  posture: Posture;
  watch_codes: string[];
  position: {
    lon?: number;
    lat?: number;
    max_wind_kt?: number;
    gust_kt?: number;
    pressure_mb?: number;
  };
  /* Geometry is omitted, never null, when the storm is not yet that strong —
   * contract rule 4. A 50 kt field on a 45 kt storm is a claim, not a blank. */
  wind34?: Geometry;
  wind50?: Geometry;
  wind64?: Geometry;
  cone?: Geometry;
  track?: LineGeometry;
  /** Cumulative probability of 64 kt: location → forecast hour → percent.
   *  Locations drop out once the storm has passed them. Absent is not zero. */
  probabilities?: Record<string, Record<string, number>>;
  totals: { destroyed: number; major: number; minor: number; none: number };
  /** Parallel to `districts`: [destroyed, major, minor, none] each. */
  district_counts: number[][];
  /** Parallel to `households`, one character each. See BANDS. */
  household_bands: string;
  /** Parallel to `districts`: mapped structures at [64, 50, 34] kt, mutually
   *  exclusive. Absent when the building inventory has not been built — which
   *  is why it is optional rather than zeroed, since zero would assert that
   *  nothing is exposed rather than that the inventory was unavailable. */
  district_exposed?: number[][];
};

export type Replay = {
  generated_at: string;
  event: { id: string; name: string; advisory_count: number };
  parishes: { name: string; registry: boolean; geometry: Geometry }[];
  districts: {
    id: number;
    parish: string;
    district: string;
    n: number;
    /** Omitted when the registry has no located household from which to derive
     *  a district centre. */
    lon?: number;
    lat?: number;
    /** Mapped building footprints in this district. Absent without inventory. */
    structures?: number;
  }[];
  households: {
    id: number;
    /** Some registry records are deliberately unlocated. They still count in
     *  the model, but are omitted from map geometry rather than turned into an
     *  invalid point at `[undefined, undefined]`. */
    lon?: number;
    lat?: number;
    parish: string;
    community: string;
    roof?: string;
  }[];
  frames: ReplayFrame[];
};

/* One character per household keeps damage bands compact without duplicating
 * the static registry fields in every advisory. */
const BANDS: Record<string, string> = {
  d: "DESTROYED",
  m: "MAJOR",
  n: "MINOR",
  o: "NONE",
};

export const POSTURE_PLAIN: Record<string, string> = {
  QUIET: "Quiet",
  WATCH: "Watch",
  READY: "Ready",
  ACT: "Act",
};

/* NHC ships watch and warning state as four-letter codes. Right to store, wrong
 * to show: nobody should be translating HWR in their head at 3am. Strongest
 * first, because the point of a warning is that it outranks a watch. */
const WATCH_WARNING: [string, string][] = [
  ["HWR", "Hurricane warning"],
  ["HWA", "Hurricane watch"],
  ["TWR", "Tropical storm warning"],
  ["TWA", "Tropical storm watch"],
];

export function strongestWarning(codes: string[]): string | null {
  const held = new Set(codes);
  for (const [code, plain] of WATCH_WARNING) if (held.has(code)) return plain;
  return null;
}

/* Fixed locale, because the server renders in whatever locale Node was started
 * with and the browser hydrates in the user's — and the two disagreeing about a
 * thousands separator is a hydration mismatch that regenerates the whole tree. */
export const nf = new Intl.NumberFormat("en-JM");

/** `2025-10-27T15:00:00Z` → `15:00`. ISO slicing, never toLocaleTimeString:
 *  the same hydration trap as the number format, one field over. */
export function hhmm(at: string): string {
  return new Date(at).toISOString().slice(11, 16);
}

/** `2025-10-27T15:00:00Z` → `2025-10-27 15:00`. */
export function stamp(at: string): string {
  return new Date(at).toISOString().slice(0, 16).replace("T", " ");
}

/* ---------------------------------------------------------------------------
 * Reading the file
 * ------------------------------------------------------------------------ */

export const REPLAY_URL = "/replay/replay.json";
export const LIBRARY_URL = "/replay/index.json";

/* One entry per storm the console can open.
 *
 * `kind` and `sizeSource` are not decoration. An advisory replay is what the
 * National Hurricane Center published while the storm was happening. A
 * hindcast projects the track the storm actually took, which is perfect
 * foresight and a claim nobody made at the time. `sizeSource` says whether the
 * wind field's extent was measured or produced by our model — for Gilbert 1988
 * the radii were digitised decades later, and for most storms before 2004 they
 * were never recorded at all.
 *
 * A picker that listed these as interchangeable would be the exact failure the
 * evidence contract exists to prevent, so both travel with the entry and both
 * reach the screen. */
export type StormEntry = {
  id: string;
  name: string;
  advisories: number;
  from: string;
  to: string;
  file: string;
  kind: "advisory" | "hindcast";
  sizeSource: "measured" | "modelled";
  bytes?: number;
};

export type Library = { default: string | null; storms: StormEntry[] };

export type ReplayState =
  | { status: "loading" }
  | { status: "ready"; replay: Replay }
  | { status: "absent"; reason: string };

const POSTURES = new Set<Posture>(["QUIET", "WATCH", "READY", "ACT"]);
const BAND_CODES = new Set(Object.keys(BANDS));
const UTC_ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$/;
const ADVISORY = /^(\d+)([A-Z]?)$/;

function rejectNulls(value: unknown, path: string): void {
  if (value === null) throw new Error(`${path} must omit unavailable data rather than use null`);
  if (Array.isArray(value)) {
    value.forEach((item, index) => rejectNulls(item, `${path}[${index}]`));
    return;
  }
  if (typeof value === "object") {
    Object.entries(value as Record<string, unknown>).forEach(([key, item]) =>
      rejectNulls(item, `${path}.${key}`),
    );
  }
}

function recordAt(value: unknown, path: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${path} must be an object`);
  }
  return value as Record<string, unknown>;
}

function arrayAt(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value)) throw new Error(`${path} must be an array`);
  return value;
}

function stringAt(value: unknown, path: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`${path} must be a non-empty string`);
  }
  return value;
}

function booleanAt(value: unknown, path: string): boolean {
  if (typeof value !== "boolean") throw new Error(`${path} must be a boolean`);
  return value;
}

function numberAt(value: unknown, path: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`${path} must be a finite number`);
  }
  return value;
}

function countAt(value: unknown, path: string): number {
  const count = numberAt(value, path);
  if (!Number.isInteger(count) || count < 0) {
    throw new Error(`${path} must be a non-negative integer`);
  }
  return count;
}

function timestampAt(value: unknown, path: string): string {
  const stamp = stringAt(value, path);
  if (!UTC_ISO.test(stamp) || !Number.isFinite(Date.parse(stamp))) {
    throw new Error(`${path} must be a UTC ISO timestamp ending in Z`);
  }
  return stamp;
}

function positionAt(value: unknown, path: string): number[] {
  const position = arrayAt(value, path);
  if (position.length < 2) throw new Error(`${path} must contain longitude and latitude`);
  const coordinates = position.map((item, index) => numberAt(item, `${path}[${index}]`));
  if (coordinates[0] < -180 || coordinates[0] > 180 || coordinates[1] < -90 || coordinates[1] > 90) {
    throw new Error(`${path} is outside WGS84 bounds`);
  }
  return coordinates;
}

function ringAt(value: unknown, path: string): void {
  const ring = arrayAt(value, path);
  if (ring.length < 4) throw new Error(`${path} must contain at least four positions`);
  const positions = ring.map((point, index) => positionAt(point, `${path}[${index}]`));
  const first = positions[0];
  const last = positions[positions.length - 1];
  if (first[0] !== last[0] || first[1] !== last[1]) {
    throw new Error(`${path} must be closed`);
  }
}

function polygonAt(value: unknown, path: string): void {
  const rings = arrayAt(value, path);
  if (rings.length === 0) throw new Error(`${path} must contain at least one ring`);
  rings.forEach((ring, index) => ringAt(ring, `${path}[${index}]`));
}

function geometryAt(value: unknown, path: string): void {
  const geometry = recordAt(value, path);
  const type = stringAt(geometry.type, `${path}.type`);
  if (type === "Polygon") {
    polygonAt(geometry.coordinates, `${path}.coordinates`);
    return;
  }
  if (type === "MultiPolygon") {
    const polygons = arrayAt(geometry.coordinates, `${path}.coordinates`);
    if (polygons.length === 0) throw new Error(`${path}.coordinates must contain a polygon`);
    polygons.forEach((polygon, index) => polygonAt(polygon, `${path}.coordinates[${index}]`));
    return;
  }
  throw new Error(`${path}.type must be Polygon or MultiPolygon`);
}

function lineGeometryAt(value: unknown, path: string): void {
  const geometry = recordAt(value, path);
  if (geometry.type !== "LineString") throw new Error(`${path}.type must be LineString`);
  const points = arrayAt(geometry.coordinates, `${path}.coordinates`);
  if (points.length < 2) throw new Error(`${path}.coordinates must contain at least two positions`);
  points.forEach((point, index) => positionAt(point, `${path}.coordinates[${index}]`));
}

function advisoryKey(value: string, path: string): [number, number] {
  const match = ADVISORY.exec(value);
  if (!match) throw new Error(`${path} must be a number with an optional letter suffix`);
  return [Number(match[1]), match[2] ? match[2].charCodeAt(0) - 64 : 0];
}

/** The map joins model output to parish geometry by name. Jamaica data sources
 *  alternate between `St`, `St.` and `Saint`; treating those as distinct would
 *  silently drop impact from the administrative boundary. Keep this key in one
 *  exported helper so every replay-side identity check uses the same rule. */
export function canonicalParishKey(name: string): string {
  return name.trim().toLowerCase().replace(/^st\.?\s+/, "saint ");
}

function canonicalDistrictKey(name: string): string {
  return name.trim().toLowerCase();
}

function validateCounts(value: unknown, path: string, expectedRows: number): number[][] {
  const rows = arrayAt(value, path);
  if (rows.length !== expectedRows) {
    throw new Error(`${path} has ${rows.length} rows for ${expectedRows} districts`);
  }
  return rows.map((row, rowIndex) => {
    const counts = arrayAt(row, `${path}[${rowIndex}]`);
    if (counts.length !== 4) throw new Error(`${path}[${rowIndex}] must contain four damage bands`);
    return counts.map((count, index) => countAt(count, `${path}[${rowIndex}][${index}]`));
  });
}

/** Validate everything the UI or map consumes before any frame can render.
 *  The exporter also checks the contract, but the browser can receive a stale,
 *  truncated, or hand-edited file and must fail closed instead of drawing
 *  plausible-looking bad geography. */
export function validateReplay(raw: unknown): Replay {
  // Contract rule 4 applies to the whole document, including fields a newer
  // exporter may add before this reader consumes them. Silently ignoring an
  // unknown null would let unavailable data acquire the visual meaning zero.
  rejectNulls(raw, "replay.json");
  const replay = recordAt(raw, "replay.json");
  timestampAt(replay.generated_at, "generated_at");

  const event = recordAt(replay.event, "event");
  stringAt(event.id, "event.id");
  stringAt(event.name, "event.name");
  const advisoryCount = countAt(event.advisory_count, "event.advisory_count");

  const parishes = arrayAt(replay.parishes, "parishes");
  if (parishes.length === 0) throw new Error("replay.json has no parishes");
  const parishNames = new Map<string, string>();
  parishes.forEach((value, index) => {
    const parish = recordAt(value, `parishes[${index}]`);
    const name = stringAt(parish.name, `parishes[${index}].name`);
    const key = canonicalParishKey(name);
    const duplicate = parishNames.get(key);
    if (duplicate) {
      throw new Error(
        `parishes[${index}].name duplicates ${duplicate} after St/Saint canonicalization`,
      );
    }
    parishNames.set(key, name);
    booleanAt(parish.registry, `parishes[${index}].registry`);
    geometryAt(parish.geometry, `parishes[${index}].geometry`);
  });

  const districts = arrayAt(replay.districts, "districts");
  const districtSizes: number[] = [];
  const districtStructures: (number | null)[] = [];
  const districtIds = new Set<number>();
  const districtKeys = new Set<string>();
  districts.forEach((value, index) => {
    const district = recordAt(value, `districts[${index}]`);
    const id = countAt(district.id, `districts[${index}].id`);
    if (districtIds.has(id)) throw new Error(`districts[${index}].id duplicates ${id}`);
    districtIds.add(id);
    const parish = stringAt(district.parish, `districts[${index}].parish`);
    const parishKey = canonicalParishKey(parish);
    if (!parishNames.has(parishKey)) {
      throw new Error(`districts[${index}].parish does not reference a declared parish: ${parish}`);
    }
    const name = stringAt(district.district, `districts[${index}].district`);
    const districtKey = `${parishKey}\u0000${canonicalDistrictKey(name)}`;
    if (districtKeys.has(districtKey)) {
      throw new Error(`districts[${index}] duplicates (${parish}, ${name})`);
    }
    districtKeys.add(districtKey);
    districtSizes.push(countAt(district.n, `districts[${index}].n`));
    const hasLon = "lon" in district;
    const hasLat = "lat" in district;
    if (hasLon !== hasLat) {
      throw new Error(`districts[${index}] must provide both lon and lat, or neither`);
    }
    if (hasLon && hasLat) positionAt([district.lon, district.lat], `districts[${index}].location`);
    if ("structures" in district) {
      districtStructures.push(countAt(district.structures, `districts[${index}].structures`));
    } else districtStructures.push(null);
  });

  const households = arrayAt(replay.households, "households");
  const householdIds = new Set<number>();
  households.forEach((value, index) => {
    const household = recordAt(value, `households[${index}]`);
    const id = countAt(household.id, `households[${index}].id`);
    if (householdIds.has(id)) throw new Error(`households[${index}].id duplicates ${id}`);
    householdIds.add(id);
    const parish = stringAt(household.parish, `households[${index}].parish`);
    if (!parishNames.has(canonicalParishKey(parish))) {
      throw new Error(`households[${index}].parish does not reference a declared parish: ${parish}`);
    }
    stringAt(household.community, `households[${index}].community`);
    if ("roof" in household) {
      stringAt(household.roof, `households[${index}].roof`);
    }

    const hasLon = "lon" in household;
    const hasLat = "lat" in household;
    if (hasLon !== hasLat) {
      throw new Error(`households[${index}] must provide both lon and lat, or neither`);
    }
    if (hasLon && hasLat) positionAt([household.lon, household.lat], `households[${index}].location`);
  });
  const districtsWithStructures = districtStructures.filter(
    (structures) => structures !== null,
  ).length;
  if (districtsWithStructures > 0 && districtsWithStructures !== districts.length) {
    throw new Error(
      `district structure inventory is partial (${districtsWithStructures} of ${districts.length})`,
    );
  }
  const completeStructureInventory =
    districts.length > 0 && districtsWithStructures === districts.length;

  const frames = arrayAt(replay.frames, "frames");
  if (frames.length === 0) throw new Error("replay.json has no frames");
  if (advisoryCount !== frames.length) {
    throw new Error(`event.advisory_count is ${advisoryCount}, but replay has ${frames.length} frames`);
  }

  let priorAdvisory: [number, number] | null = null;
  let priorTimestamp = -Infinity;
  let framesWithExposure = 0;
  frames.forEach((value, frameIndex) => {
    const frame = recordAt(value, `frames[${frameIndex}]`);
    const advisory = stringAt(frame.n, `frames[${frameIndex}].n`);
    const key = advisoryKey(advisory, `frames[${frameIndex}].n`);
    if (priorAdvisory && (key[0] < priorAdvisory[0] || (key[0] === priorAdvisory[0] && key[1] <= priorAdvisory[1]))) {
      throw new Error(`advisory ${advisory} is not in ascending order`);
    }
    priorAdvisory = key;
    const timestamp = timestampAt(frame.at, `advisory ${advisory}.at`);
    const timestampMs = Date.parse(timestamp);
    if (timestampMs <= priorTimestamp) {
      throw new Error(`advisory ${advisory}.at is not later than the prior frame`);
    }
    priorTimestamp = timestampMs;

    const posture = stringAt(frame.posture, `advisory ${advisory}.posture`) as Posture;
    if (!POSTURES.has(posture)) throw new Error(`advisory ${advisory}.posture is unknown: ${posture}`);
    arrayAt(frame.watch_codes, `advisory ${advisory}.watch_codes`).forEach((code, index) => {
      stringAt(code, `advisory ${advisory}.watch_codes[${index}]`);
    });

    const position = recordAt(frame.position, `advisory ${advisory}.position`);
    const hasLon = "lon" in position;
    const hasLat = "lat" in position;
    if (hasLon !== hasLat) {
      throw new Error(`advisory ${advisory}.position must provide both lon and lat, or neither`);
    }
    if (hasLon && hasLat) positionAt([position.lon, position.lat], `advisory ${advisory}.position`);
    ["max_wind_kt", "gust_kt", "pressure_mb"].forEach((field) => {
      if (!(field in position)) return;
      const reading = numberAt(position[field], `advisory ${advisory}.position.${field}`);
      if (reading < 0) throw new Error(`advisory ${advisory}.position.${field} cannot be negative`);
    });

    ["wind34", "wind50", "wind64", "cone"].forEach((field) => {
      if (field in frame) geometryAt(frame[field], `advisory ${advisory}.${field}`);
    });
    if ("track" in frame) lineGeometryAt(frame.track, `advisory ${advisory}.track`);

    if ("probabilities" in frame) {
      const probabilities = recordAt(frame.probabilities, `advisory ${advisory}.probabilities`);
      Object.entries(probabilities).forEach(([location, rawHorizons]) => {
        const horizons = recordAt(rawHorizons, `advisory ${advisory}.probabilities.${location}`);
        Object.entries(horizons).forEach(([hour, rawProbability]) => {
          if (!/^\d+$/.test(hour)) throw new Error(`advisory ${advisory}: invalid forecast hour ${hour}`);
          const probability = numberAt(rawProbability, `advisory ${advisory}.probabilities.${location}.${hour}`);
          if (probability < 0 || probability > 100) {
            throw new Error(`advisory ${advisory}: probability at ${location} ${hour}h must be 0–100`);
          }
        });
      });
    }

    const totals = recordAt(frame.totals, `advisory ${advisory}.totals`);
    const expectedTotals = ["destroyed", "major", "minor", "none"].map((band) =>
      countAt(totals[band], `advisory ${advisory}.totals.${band}`),
    );
    const districtCounts = validateCounts(
      frame.district_counts,
      `advisory ${advisory}.district_counts`,
      districts.length,
    );
    const summed = [0, 0, 0, 0];
    districtCounts.forEach((counts, districtIndex) => {
      counts.forEach((count, bandIndex) => {
        summed[bandIndex] += count;
      });
      const districtTotal = counts.reduce((total, count) => total + count, 0);
      if (districtTotal !== districtSizes[districtIndex]) {
        throw new Error(
          `advisory ${advisory}.district_counts[${districtIndex}] totals ${districtTotal}, expected ${districtSizes[districtIndex]}`,
        );
      }
    });
    if (summed.some((total, index) => total !== expectedTotals[index])) {
      throw new Error(`advisory ${advisory}: district counts do not match national totals`);
    }

    const householdBands = stringAt(frame.household_bands, `advisory ${advisory}.household_bands`);
    if (householdBands.length !== households.length) {
      throw new Error(
        `advisory ${advisory}: ${householdBands.length} household bands for ${households.length} households`,
      );
    }
    const bandTotals = [0, 0, 0, 0];
    const bandIndexes: Record<string, number> = { d: 0, m: 1, n: 2, o: 3 };
    for (let index = 0; index < householdBands.length; index += 1) {
      const code = householdBands[index];
      if (!BAND_CODES.has(code)) {
        throw new Error(`advisory ${advisory}: unknown household band '${code}' at index ${index}`);
      }
      bandTotals[bandIndexes[code]] += 1;
    }
    if (bandTotals.some((total, index) => total !== expectedTotals[index])) {
      throw new Error(`advisory ${advisory}: household bands do not match national totals`);
    }

    if ("district_exposed" in frame) {
      framesWithExposure += 1;
      if (!completeStructureInventory) {
        throw new Error(
          `advisory ${advisory}.district_exposed requires structures for every district`,
        );
      }
      const exposure = arrayAt(frame.district_exposed, `advisory ${advisory}.district_exposed`);
      if (exposure.length !== districts.length) {
        throw new Error(`advisory ${advisory}: ${exposure.length} exposure rows for ${districts.length} districts`);
      }
      exposure.forEach((rawBands, districtIndex) => {
        const bands = arrayAt(rawBands, `advisory ${advisory}.district_exposed[${districtIndex}]`);
        if (bands.length !== 3) {
          throw new Error(`advisory ${advisory}.district_exposed[${districtIndex}] must contain 64, 50 and 34 kt counts`);
        }
        let exposed = 0;
        bands.forEach((count, bandIndex) => {
          exposed += countAt(
            count,
            `advisory ${advisory}.district_exposed[${districtIndex}][${bandIndex}]`,
          );
        });
        const denominator = districtStructures[districtIndex];
        if (denominator !== null) {
          if (exposed > denominator) {
            throw new Error(
              `advisory ${advisory}.district_exposed[${districtIndex}] totals ${exposed}, above its ${denominator}-structure inventory`,
            );
          }
        }
      });
    }
  });

  if (framesWithExposure > 0 && framesWithExposure !== frames.length) {
    throw new Error(
      `district exposure is partial (${framesWithExposure} of ${frames.length} advisories)`,
    );
  }

  return raw as Replay;
}

/* Fetched in an effect rather than imported so the browser and service worker
 * own replay availability, and this reader can later target the live endpoint
 * without changing the component boundary. The server and first client render
 * both show loading; data lands after hydration, so there is no mismatch. */
/* The storm library, read once.
 *
 * Optional by construction. An older deployment — or a clone that has exported
 * a single storm and nothing else — has no index, and that must degrade to the
 * one replay it does have rather than to an empty screen. An absent library is
 * a console with one storm, not a broken console.
 */
export function useLibrary(): Library | null {
  const [library, setLibrary] = useState<Library | null>(null);

  useEffect(() => {
    let live = true;
    fetch(LIBRARY_URL, { cache: "no-store" })
      .then((res) => (res.ok ? res.json() : null))
      .then((raw: unknown) => {
        if (!live || !raw || typeof raw !== "object") return;
        const source = raw as { default?: unknown; storms?: unknown };
        if (!Array.isArray(source.storms)) return;
        const storms = source.storms
          .filter((s): s is Record<string, unknown> => !!s && typeof s === "object")
          .map((s) => ({
            id: String(s.id ?? ""),
            name: String(s.name ?? ""),
            advisories: Number(s.advisories ?? 0),
            from: String(s.from ?? ""),
            to: String(s.to ?? ""),
            file: String(s.file ?? ""),
            kind: s.kind === "hindcast" ? ("hindcast" as const) : ("advisory" as const),
            sizeSource:
              s.size_source === "modelled" ? ("modelled" as const) : ("measured" as const),
            bytes: typeof s.bytes === "number" ? s.bytes : undefined,
          }))
          .filter((s) => s.id && s.file);
        if (storms.length) {
          setLibrary({
            default: typeof source.default === "string" ? source.default : storms[0].id,
            storms,
          });
        }
      })
      .catch(() => {
        // Silent, and deliberately. A missing index is the expected state on a
        // deployment that predates the library; it is not an error to report.
      });
    return () => {
      live = false;
    };
  }, []);

  return library;
}

/**
 * Read one storm.
 *
 * `url` is a parameter rather than a constant so the console can change storms
 * without a page load. Passing null holds the reader at `loading`, which is
 * what the first render does while the library is still arriving — fetching
 * the legacy path first and then immediately replacing it would download a
 * megabyte nobody asked for.
 */
export function useReplay(url: string | null = REPLAY_URL): ReplayState {
  const [state, setState] = useState<ReplayState>({ status: "loading" });

  useEffect(() => {
    if (!url) return;
    let live = true;
    setState({ status: "loading" });
    fetch(url, { cache: "no-store" })
      .then(async (res) => {
        if (!res.ok) throw new Error(`${url} returned ${res.status}`);
        return validateReplay(await res.json());
      })
      .then((replay) => {
        if (live) setState({ status: "ready", replay });
      })
      .catch((error: unknown) => {
        const reason = error instanceof Error ? error.message : String(error);
        console.warn("[replay]", reason);
        if (live) setState({ status: "absent", reason });
      });
    return () => {
      live = false;
    };
  }, [url]);

  return state;
}

/* ---------------------------------------------------------------------------
 * Per-frame derivations
 *
 * Parish and registry geography is static; forecasts and model results change
 * by frame. Only the district summary needed by the current map is joined on a
 * frame change. Household points remain available for future detail views but
 * are not materialised into every map snapshot.
 * ------------------------------------------------------------------------ */

export function districtsAt(replay: Replay, frame: ReplayFrame): District[] {
  return replay.districts.map((d, i) => {
    const [destroyed, major, minor, none] = frame.district_counts[i];
    return {
      parish: d.parish,
      district: d.district,
      n: d.n,
      lon: d.lon ?? undefined,
      lat: d.lat ?? undefined,
      destroyed,
      major,
      minor,
      none,
    };
  });
}

export function householdsAt(replay: Replay, frame: ReplayFrame): Household[] {
  const located: Household[] = [];
  replay.households.forEach((household, index) => {
    if (typeof household.lon !== "number" || typeof household.lat !== "number") return;
    located.push({
      lon: household.lon,
      lat: household.lat,
      band: BANDS[frame.household_bands[index]],
      parish: household.parish,
      community: household.community,
      roof: household.roof ?? undefined,
    });
  });
  return located;
}

/** The shape the map already speaks. One frame of it. */
export function snapshotAt(
  replay: Replay,
  frame: ReplayFrame,
  districts = districtsAt(replay, frame),
): Snapshot {
  return {
    parishes: replay.parishes,
    wind34: frame.wind34 ?? null,
    wind50: frame.wind50 ?? null,
    wind64: frame.wind64 ?? null,
    cone: frame.cone ?? null,
    track: frame.track ?? null,
    centre:
      typeof frame.position.lon === "number" && typeof frame.position.lat === "number"
        ? [frame.position.lon, frame.position.lat]
        : null,
    districts,
  };
}

export function nationalTotals(frame: ReplayFrame, homes: number) {
  return { ...frame.totals, homes };
}

/* Where to send people first. A ranked list beats a table of every parish: the
 * question in an operations room is not "what is the distribution", it is
 * "where do we go", and that is the top of a list. */
export function worstHit(districts: District[], limit = 5): District[] {
  return [...districts]
    .filter((d) => d.destroyed + d.major > 0)
    .sort((a, b) => b.destroyed * 2 + b.major - (a.destroyed * 2 + a.major))
    .slice(0, limit);
}

/* The feed emits on change, not on tick. Forty-one lines saying "assessed
 * 2,000" is a feed nobody reads, and a feed nobody reads is where the line that
 * mattered goes unnoticed. */
export type FeedRow = { at: string; who: string; what: string; disposer: string | null };

export function feedUpto(replay: Replay, index: number): FeedRow[] {
  const rows: FeedRow[] = [];
  let posture: string | null = null;
  let warning = "";
  let destroyed = 0;

  const end = Math.min(index, replay.frames.length - 1);
  for (let frameIndex = 0; frameIndex <= end; frameIndex += 1) {
    const f = replay.frames[frameIndex];
    const at = hhmm(f.at);

    if (f.posture !== posture) {
      /* Raised or lowered — both are real and a storm that passes does both.
       * Saying "raised" on the way down would be the interface asserting
       * something the data contradicts. */
      const direction = posture === null ? "set to" : RANK[f.posture] > RANK[posture] ? "raised to" : "lowered to";
      rows.push({
        at,
        who: "Forecast Sentinel",
        what: `Posture ${direction} ${POSTURE_PLAIN[f.posture] ?? f.posture}`,
        disposer: f.posture === "ACT" ? "Director" : null,
      });
      posture = f.posture;
    }

    const strongest = strongestWarning(f.watch_codes ?? []) ?? "";
    if (strongest !== warning) {
      if (strongest) {
        rows.push({ at, who: "Forecast Sentinel", what: `${strongest} in effect`, disposer: null });
      }
      warning = strongest;
    }

    if (f.totals.destroyed - destroyed >= 60 || (destroyed === 0 && f.totals.destroyed > 0)) {
      rows.push({
        at,
        who: "Risk Mapper",
        what: `${nf.format(f.totals.destroyed)} synthetic households modelled as destroyed`,
        disposer: null,
      });
    }
    destroyed = f.totals.destroyed;
  }
  return rows.reverse();
}

const RANK: Record<string, number> = { QUIET: 0, WATCH: 1, READY: 2, ACT: 3 };
