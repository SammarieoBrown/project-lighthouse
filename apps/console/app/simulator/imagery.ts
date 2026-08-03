export type ImageryFrame = { at: string; tiles: string };

export type ImageryStorm = {
  id: string;
  source: "NOAA GOES-19";
  frames: ImageryFrame[];
};

export type ImageryManifest = { storms: ImageryStorm[] };

export type ImageryMatch = ImageryFrame & { source: ImageryStorm["source"] };

const STORM_ID = /^al\d{6}$/;
const PMTILES_TEMPLATE = /^pmtiles:\/\/https:\/\/[^\s{}]+\/\{z\}\/\{x\}\/\{y\}$/;
const MAX_OBSERVATION_OFFSET_MS = 90 * 60 * 1000;

export function validateImageryManifest(raw: unknown): ImageryManifest {
  const source = objectAt(raw, "storm-imagery/index.json");
  const ids = new Set<string>();
  const storms = arrayAt(source.storms, "imagery.storms").map((value, stormIndex): ImageryStorm => {
    const path = `imagery.storms[${stormIndex}]`;
    const storm = objectAt(value, path);
    const id = stringAt(storm.id, `${path}.id`).toLowerCase();
    if (!STORM_ID.test(id)) throw new Error(`${path}.id is not an Atlantic storm identifier`);
    if (ids.has(id)) throw new Error(`${path}.id duplicates ${id}`);
    ids.add(id);
    if (storm.source !== "NOAA GOES-19") throw new Error(`${path}.source must be NOAA GOES-19`);
    let prior = -Infinity;
    const frames = arrayAt(storm.frames, `${path}.frames`).map((value, frameIndex): ImageryFrame => {
      const framePath = `${path}.frames[${frameIndex}]`;
      const frame = objectAt(value, framePath);
      const at = timestampAt(frame.at, `${framePath}.at`);
      const timestamp = Date.parse(at);
      if (timestamp <= prior) throw new Error(`${framePath}.at is not later than the prior frame`);
      prior = timestamp;
      const tiles = stringAt(frame.tiles, `${framePath}.tiles`);
      if (!PMTILES_TEMPLATE.test(tiles)) {
        throw new Error(`${framePath}.tiles must be an HTTPS PMTiles template`);
      }
      return { at, tiles };
    });
    return { id, source: "NOAA GOES-19", frames };
  });
  return { storms };
}

export function nearestImagery(
  manifest: ImageryManifest,
  stormId: string,
  eventAt: string,
): ImageryMatch | null {
  const target = Date.parse(eventAt);
  if (!Number.isFinite(target)) return null;
  const storm = manifest.storms.find((entry) => entry.id === stormId.toLowerCase());
  if (!storm || storm.frames.length === 0) return null;
  const frame = storm.frames.reduce((nearest, candidate) =>
    Math.abs(Date.parse(candidate.at) - target) < Math.abs(Date.parse(nearest.at) - target)
      ? candidate
      : nearest,
  );
  if (Math.abs(Date.parse(frame.at) - target) > MAX_OBSERVATION_OFFSET_MS) return null;
  return { ...frame, source: storm.source };
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

function timestampAt(value: unknown, path: string): string {
  const timestamp = stringAt(value, path);
  if (!timestamp.endsWith("Z") || !Number.isFinite(Date.parse(timestamp))) {
    throw new Error(`${path} must be a valid UTC timestamp`);
  }
  return timestamp;
}
