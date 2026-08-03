import type {
  ExpressionSpecification,
  LayerSpecification,
  Map as MapLibreMap,
  SourceSpecification,
} from "maplibre-gl";
import type { GeoJSONSource } from "maplibre-gl";

import type { Snapshot } from "../map";

/* The two data layers that sit on the basemap: hazard and impact.
 *
 * Hazard is where the wind reaches. Impact is which homes it hits. They use
 * separate colour scales — cool for hazard, warm for impact — so the eye can
 * hold both at once; see the hazard ramp note in tokens.css for why that is a
 * stated exception to the colour rule rather than drift.
 *
 * Zoom does the aggregation. Below z12.5 the country is 131 district circles,
 * because two thousand individual dots over an island is a texture rather than
 * a map. Above it, the individual homes — which is the zoom at which a person
 * is asking about one street, not one country.
 */

export const ZOOM_SWITCH = 12.5;

/* What the map is drawn on. Three states rather than a satellite on/off,
 * because "what is underneath the data" is one question with three answers and
 * a screen should not ask it twice.
 *
 * `structures` is not a cosmetic mode. It answers a different question from the
 * other two — not "where is this" but "what is standing here" — and it is the
 * only view where the buildings are the subject rather than the backdrop. */
export type BaseView = "map" | "satellite" | "structures";

/* Buildings live only in the island archive and Protomaps carries them from
 * about z14. The region archive stops at z11 and has none, so below this the
 * structures view has nothing to show and must say so rather than presenting an
 * empty screen as an answer. */
export const STRUCTURES_MIN_ZOOM = 13.5;

export type MapColours = {
  hazard34: string;
  hazard50: string;
  hazard64: string;
  critical: string;
  elevated: string;
  quiet: string;
  figure: string;
  ground: string;
};

export function readMapColours(el: HTMLElement = document.documentElement): MapColours {
  const s = getComputedStyle(el);
  const get = (n: string, fallback: string) => s.getPropertyValue(n).trim() || fallback;
  return {
    hazard34: get("--lh-hazard-34", "#3f6c96"),
    hazard50: get("--lh-hazard-50", "#5590c9"),
    hazard64: get("--lh-hazard-64", "#7fb8f0"),
    critical: get("--lh-critical", "#e4574a"),
    elevated: get("--lh-elevated", "#e8a33d"),
    quiet: get("--lh-quiet", "#7f8b85"),
    figure: get("--lh-figure", "#e9eae4"),
    ground: get("--lh-ground", "#101413"),
  };
}

type Feature = GeoJSON.Feature;
type Collection = GeoJSON.FeatureCollection;

const EMPTY: Collection = { type: "FeatureCollection", features: [] };

/* Every data source on this map is per-advisory, so they are built as one set
 * and handed to the map twice over: once as source definitions at init, and
 * thereafter through setData.
 *
 * That split is the whole point. Rebuilding the map for a new advisory would
 * tear down the basemap, refetch its tiles and lose the viewport — a flicker
 * on every step and an unusable Play. The map is built once; only its data
 * moves.
 *
 * Districts and homes carry their counts as feature properties, so every paint
 * decision below is an expression over real values rather than a colour baked
 * in at export time. Change the severity rule and the map follows. */
export function frameData(snapshot: Snapshot | null): Record<string, Collection> {
  if (!snapshot) {
    return {
      "lh-hazard": EMPTY, "lh-cone": EMPTY, "lh-track": EMPTY,
      "lh-storm": EMPTY, "lh-districts": EMPTY, "lh-homes": EMPTY,
    };
  }

  const hazard = (geometry: unknown, kt: number): Feature[] =>
    geometry ? [{ type: "Feature", properties: { kt }, geometry: geometry as GeoJSON.Geometry }] : [];

  const one = (geometry: unknown): Collection =>
    geometry
      ? {
          type: "FeatureCollection",
          features: [{ type: "Feature", properties: {}, geometry: geometry as GeoJSON.Geometry }],
        }
      : EMPTY;

  const centre = snapshot.centre ?? (snapshot.track?.coordinates?.[0] as [number, number] | undefined);

  return {
    "lh-hazard": {
      type: "FeatureCollection",
      features: [
        ...hazard(snapshot.wind34, 34),
        ...hazard(snapshot.wind50, 50),
        ...hazard(snapshot.wind64, 64),
      ],
    },
    "lh-cone": one(snapshot.cone),
    "lh-track": one(snapshot.track),
    "lh-storm": centre
      ? {
          type: "FeatureCollection",
          features: [{ type: "Feature", properties: {}, geometry: { type: "Point", coordinates: centre } }],
        }
      : EMPTY,
    "lh-districts": {
      type: "FeatureCollection",
      features: snapshot.districts.map((d) => ({
        type: "Feature",
        properties: {
          district: d.district,
          parish: d.parish,
          n: d.n,
          destroyed: d.destroyed,
          major: d.major,
          // Precomputed because MapLibre expressions cannot divide by a
          // per-feature maximum, and the severity rule is a share.
          severe: d.destroyed >= d.n * 0.25 ? 2 : d.destroyed + d.major >= d.n * 0.25 ? 1 : 0,
          r: Math.sqrt(d.n),
        },
        geometry: { type: "Point", coordinates: [d.lon, d.lat] },
      })),
    },
    "lh-homes": {
      type: "FeatureCollection",
      features: (snapshot.households ?? []).map((h) => ({
        type: "Feature",
        properties: { band: h.band, parish: h.parish, community: h.community, roof: h.roof },
        geometry: { type: "Point", coordinates: [h.lon, h.lat] },
      })),
    },
  };
}

/* Point sources only. A polygon source with no tile buffer shows seams where a
 * wind band crosses a tile edge, and the wind bands are the largest polygons on
 * the map. */
const UNBUFFERED = new Set(["lh-storm", "lh-districts", "lh-homes"]);

export function dataSources(snapshot: Snapshot | null): Record<string, SourceSpecification> {
  const spec: Record<string, SourceSpecification> = {};
  for (const [id, features] of Object.entries(frameData(snapshot))) {
    spec[id] = { type: "geojson", data: features, ...(UNBUFFERED.has(id) ? { buffer: 0 } : {}) };
  }
  return spec;
}

/** Move the map to another advisory. setData on the sources that already
 *  exist — never a new Map, never a new style. */
export function applyFrame(map: MapLibreMap, snapshot: Snapshot | null): void {
  for (const [id, features] of Object.entries(frameData(snapshot))) {
    const source = map.getSource(id) as GeoJSONSource | undefined;
    source?.setData(features);
  }
}

export function dataLayers(c: MapColours, maxDistrict: number): LayerSpecification[] {
  const bandColour = [
    "match",
    ["get", "band"],
    "DESTROYED", c.critical,
    "MAJOR", c.elevated,
    "MINOR", c.quiet,
    "rgba(0,0,0,0)",
  ] as unknown as ExpressionSpecification;

  return [
    // Cone first and faintest: where the centre might go is a different and
    // much less useful question than who gets hit.
    {
      id: "lh-cone-fill",
      type: "fill",
      source: "lh-cone",
      paint: { "fill-color": c.figure, "fill-opacity": 0.04 },
    },

    // Wind field, weakest outermost.
    {
      id: "lh-hazard-fill",
      type: "fill",
      source: "lh-hazard",
      paint: {
        "fill-color": ["match", ["get", "kt"], 34, c.hazard34, 50, c.hazard50, c.hazard64],
        "fill-opacity": ["match", ["get", "kt"], 34, 0.06, 50, 0.08, 0.11],
      },
    },
    {
      id: "lh-hazard-line",
      type: "line",
      source: "lh-hazard",
      paint: {
        "line-color": ["match", ["get", "kt"], 34, c.hazard34, 50, c.hazard50, c.hazard64],
        "line-width": ["match", ["get", "kt"], 34, 1, 50, 1.25, 1.75],
        "line-opacity": 0.7,
      },
    },
    {
      id: "lh-track",
      type: "line",
      source: "lh-track",
      paint: {
        "line-color": c.figure,
        "line-width": 1.25,
        "line-dasharray": [6, 4],
        "line-opacity": 0.5,
      },
    },

    /* No district circles and no household dots.
     *
     * Both were marks for the synthetic registry: 2,000 households dropped
     * inside community polygons by a seeded generator, aggregated into bubbles
     * sized by how many landed where. Every one sat at coordinates where
     * nothing necessarily stands, so the map was drawing our random seed on top
     * of Jamaica — and drawing it in the most confident register the screen has,
     * a filled circle in a severity colour.
     *
     * What is left is measured: real coastlines, the real forecast wind field,
     * and real building footprints out of the basemap archive. The counts moved
     * to the panel, where a number can carry the word "structures" and say
     * where it came from. A map should not assert what a table can qualify.
     *
     * The sources stay defined and fed — see dataSources — because the SVG
     * fallback still draws households and the panel still reads their bands. */

    /* The storm centre, drawn exactly as the SVG fallback draws it — a ring and
     * a dot in the figure colour. Present here because the two maps must not
     * disagree about where the storm is, and because a wind field with no
     * centre leaves the one point every reading in the header refers to
     * unmarked. No hue: the position is not a severity. */
    {
      id: "lh-storm-ring",
      type: "circle",
      source: "lh-storm",
      paint: {
        "circle-radius": 10,
        "circle-color": "rgba(0,0,0,0)",
        "circle-stroke-width": 1.25,
        "circle-stroke-color": c.figure,
        "circle-stroke-opacity": 0.8,
      },
    },
    {
      id: "lh-storm-eye",
      type: "circle",
      source: "lh-storm",
      paint: { "circle-radius": 2.5, "circle-color": c.figure },
    },
  ] as LayerSpecification[];
}
