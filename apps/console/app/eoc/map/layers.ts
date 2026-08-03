import type { LayerSpecification, Map as MapLibreMap, SourceSpecification } from "maplibre-gl";
import type { GeoJSONSource } from "maplibre-gl";

import { parishImpacts, type Snapshot } from "../map";

/* The two data layers that sit on the basemap: hazard and impact.
 *
 * Hazard is where the selected forecast says wind may reach. Impact is the
 * selected advisory's model output, summed to real parish boundaries. They use
 * separate colour scales — cool for hazard, warm for impact — so the eye can
 * hold both at once. Synthetic household and district coordinates never become
 * map marks; they are model scaffolding, not observations on the ground.
 */

/* What the map is drawn on. Three states rather than a satellite on/off,
 * because "what is underneath the data" is one question with three answers and
 * a screen should not ask it twice.
 *
 * `structures` is not a cosmetic mode. It answers a different question from the
 * other two — not "where is this" but "what is standing here" — and it is the
 * only view where the buildings are the subject rather than the backdrop. */
export type BaseView = "map" | "satellite" | "structures";

/* Where our own structures tileset begins. It is built z9–15, so the wide
 * shot — a whole parish of settlement at once — works, which is the entire
 * reason we build it rather than styling the basemap's buildings. Those start
 * at about z14 and carry no attribute to colour by.
 *
 * Below z9 there is genuinely nothing to draw, and the panel says so rather
 * than offering an empty screen as an answer. */
export const STRUCTURES_MIN_ZOOM = 9;
export const STRUCTURE_FOOTPRINT_ZOOM = 14;

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
 * Parish features carry selected-advisory aggregate counts, so every paint
 * decision below is an expression over model values joined to real geometry. */
export function frameData(snapshot: Snapshot | null): Record<string, Collection> {
  if (!snapshot) {
    return {
      "lh-hazard": EMPTY, "lh-cone": EMPTY, "lh-track": EMPTY,
      "lh-storm": EMPTY, "lh-parish-impact": EMPTY,
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
  const impacts = parishImpacts(snapshot);
  const labelled = new Set(
    [...impacts]
      .filter((impact) => impact.majorPlus > 0)
      .sort((a, b) => b.majorPlus - a.majorPlus)
      .slice(0, 5)
      .map((impact) => impact.name),
  );

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
    "lh-parish-impact": {
      type: "FeatureCollection",
      features: impacts.map((impact) => ({
        type: "Feature",
        properties: {
          name: impact.name,
          label: impact.label,
          homes: impact.homes,
          destroyed: impact.destroyed,
          major: impact.major,
          minor: impact.minor,
          none: impact.none,
          majorPlus: impact.majorPlus,
          band: impact.band,
          showLabel: labelled.has(impact.name),
        },
        geometry: impact.geometry as GeoJSON.Geometry,
      })),
    },
  };
}

/* Point sources only. A polygon source with no tile buffer shows seams where a
 * wind band crosses a tile edge, and the wind bands are the largest polygons on
 * the map. */
const UNBUFFERED = new Set(["lh-storm"]);

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

export function dataLayers(c: MapColours): LayerSpecification[] {
  return [
    /* Expected impact first. The administrative geometry is measured; the
     * counts and fill are explicitly modelled for the selected advisory. */
    {
      id: "lh-parish-impact-fill",
      type: "fill",
      source: "lh-parish-impact",
      paint: {
        "fill-color": [
          "match",
          ["get", "band"],
          "destroyed", c.critical,
          "major", c.elevated,
          "rgba(0,0,0,0)",
        ],
        "fill-opacity": [
          "match",
          ["get", "band"],
          "destroyed", 0.38,
          "major", 0.3,
          0,
        ],
      },
    },

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

    /* Real parish boundaries keep the aggregate impact legible beneath the
     * forecast. A selected worst-hit row can strengthen this same geometry;
     * it never adds a synthetic district point. */
    {
      id: "lh-parish-impact-outline",
      type: "line",
      source: "lh-parish-impact",
      paint: {
        "line-color": [
          "match",
          ["get", "band"],
          "destroyed", c.critical,
          "major", c.elevated,
          c.quiet,
        ],
        "line-width": [
          "match",
          ["get", "band"],
          "destroyed", 1.5,
          "major", 1.25,
          0.75,
        ],
        "line-opacity": [
          "match",
          ["get", "band"],
          "destroyed", 0.85,
          "major", 0.75,
          0.4,
        ],
      },
    },
    {
      id: "lh-focused-parish",
      type: "line",
      source: "lh-parish-impact",
      filter: ["==", ["get", "name"], ""],
      paint: {
        "line-color": c.figure,
        "line-width": 2.5,
        "line-opacity": 0.95,
      },
    },
    {
      id: "lh-impact-labels",
      type: "symbol",
      source: "lh-parish-impact",
      maxzoom: 12,
      filter: ["==", ["get", "showLabel"], true],
      layout: {
        "text-field": [
          "concat",
          ["get", "label"],
          "\n",
          ["to-string", ["get", "majorPlus"]],
          " major+",
        ],
        "text-font": ["Noto Sans Medium"],
        "text-size": 11,
        "text-line-height": 1.05,
        "text-letter-spacing": 0.02,
        "text-allow-overlap": false,
      },
      paint: {
        "text-color": c.figure,
        "text-halo-color": c.ground,
        "text-halo-width": 1.25,
      },
    },

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
