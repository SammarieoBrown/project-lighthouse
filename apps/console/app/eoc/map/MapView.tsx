"use client";

import { layers as basemapLayers } from "@protomaps/basemaps";
/* Named imports: MapLibre 6's ESM build has no default export, and the
 * bundler catches it rather than the browser, which is the good outcome. */
import {
  AttributionControl,
  Map as MapLibreMap,
  NavigationControl,
  ScaleControl,
  addProtocol,
  setWorkerUrl,
} from "maplibre-gl";
import type { MapMouseEvent } from "maplibre-gl";
import { Protocol } from "pmtiles";
import { useEffect, useRef, useState } from "react";

import "maplibre-gl/dist/maplibre-gl.css";

import type { Snapshot } from "../map";
import { lighthouseFlavor, pruneLayers, readTokens } from "./flavor";
import { dataLayers, dataSources, readMapColours, ZOOM_SWITCH } from "./layers";

/* MapLibre, wired by hand.
 *
 * No react-map-gl. There is one map on this product, it reads a static
 * snapshot, and the design system wants total control of every layer — a
 * hundred lines of init beats a dependency whose main offer is JSX sugar for
 * markers we would restyle anyway.
 *
 * Everything it needs is local: tiles, glyphs, sprites. The console has to draw
 * a map in a building that has lost power and its internet with it, and a
 * basemap that silently fetches font ranges at draw time is not offline — it
 * just looks offline until the day it matters.
 */

const TILES = "/tiles/caribbean-z13.pmtiles";
const GLYPHS = "/tiles/assets/fonts/{fontstack}/{range}.pbf";

// MapLibre 6 rejects a relative sprite URL outright. Resolved against the
// current origin rather than hard-coded, so this still works offline from a
// file server, a laptop, or Vercel.
const SPRITE_PATH = "/tiles/assets/sprites/black";

// Esri is the online-only satellite fallback: their licence permits live tile
// use with attribution but prohibits caching outside ArcGIS, so it can never be
// part of the offline bundle. NOAA's post-storm imagery is the primary and is
// cached; this is what you get when that has not been fetched.
const ESRI_IMAGERY =
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";

/* MapLibre's worker, served by us.
 *
 * Turbopack drops MapLibre's inline worker, and the failure is completely
 * silent: the map builds, the canvas sizes, the controls draw, the scale bar
 * even reports a sensible distance — and not one tile ever loads, because
 * every source is parsed in a worker that never started. It renders as a map
 * of the open sea.
 *
 * The worker imports maplibre-gl-shared.mjs from its own directory, so both
 * files are copied into public/maplibre by the `assets` script. Shipping the
 * worker without its sibling fails on the worker's first import — the same
 * silent blank map, one step further along.
 */
const WORKER = "/maplibre/maplibre-gl-worker.mjs";

// Both are global and throw if repeated, so they happen once per document
// rather than once per mount.
let configured = false;

function configureMapLibre() {
  if (configured) return;
  setWorkerUrl(`${window.location.origin}${WORKER}`);
  addProtocol("pmtiles", new Protocol().tile);
  configured = true;
}

export type MapViewProps = {
  snapshot: Snapshot;
  satellite: boolean;
  onZoomChange?: (zoom: number) => void;
};

export default function MapView({ snapshot, satellite, onZoomChange }: MapViewProps) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<MapLibreMap | null>(null);
  const [failed, setFailed] = useState<string | null>(null);

  useEffect(() => {
    if (!container.current || map.current) return;
    configureMapLibre();

    // Read from the container, not the document. `data-theme` is set on the
    // console's <main>, so resolving tokens at documentElement returns whatever
    // ground the *browser* prefers — which painted the whole basemap near-white
    // on a machine set to light mode. Same failure as the text colours earlier:
    // ask the element that is actually inside the themed subtree.
    const tokens = readTokens(container.current);
    const colours = readMapColours(container.current);
    const maxDistrict = Math.max(...snapshot.districts.map((d) => d.n), 1);

    let instance: MapLibreMap;
    try {
      instance = new MapLibreMap({
        container: container.current,
        // Jamaica and the water the storm is crossing. The storm centre sits
        // south of the island, so a frame tight on the coast loses the thing
        // bearing down on it.
        center: [-77.3, 17.9],
        zoom: 7.4,
        maxZoom: 17,
        minZoom: 5,
        attributionControl: false,
        style: {
          version: 8,
          glyphs: GLYPHS,
          sprite: `${window.location.origin}${SPRITE_PATH}`,
          sources: {
            basemap: {
              type: "vector",
              // Explicit tile template rather than `url:`, so MapLibre never
              // waits on a TileJSON round trip through the protocol. One less
              // asynchronous step between a cold start and a drawn map, and the
              // bounds and zoom range are things we already know.
              tiles: [`pmtiles://${window.location.origin}${TILES}/{z}/{x}/{y}`],
              minzoom: 0,
              // Must match the archive. MapLibre overzooms past this rather
              // than showing nothing, so the map keeps working when somebody
              // zooms to a street — it just stops adding vector detail, which
              // is where the satellite layer takes over.
              maxzoom: 13,
              bounds: [-85.5, 15.5, -67.5, 24.0],
              attribution: "© OpenStreetMap",
            },
            ...dataSources(snapshot),
          },
          layers: [
            ...pruneLayers(basemapLayers("basemap", lighthouseFlavor(tokens), { lang: "en" })),
            ...dataLayers(colours, maxDistrict),
          ],
        },
      });
    } catch (error) {
      // WebGL2 is required by MapLibre 6. A machine without it is rare and
      // exactly the machine a demo will be run on, so say so rather than
      // rendering an empty box — the caller falls back to the SVG map.
      setFailed(error instanceof Error ? error.message : "map failed to start");
      return;
    }

    instance.addControl(new NavigationControl({ showCompass: false }), "top-right");
    instance.addControl(
      new AttributionControl({
        compact: false,
        customAttribution: "© OpenStreetMap · Imagery © NOAA / Esri",
      }),
      "bottom-right",
    );
    instance.addControl(new ScaleControl({ unit: "metric" }), "bottom-left");

    instance.on("zoom", () => onZoomChange?.(instance.getZoom()));
    instance.on("error", (e) => {
      // Every map error gets logged. MapLibre swallows style failures into this
      // event and renders an empty canvas, which looks exactly like a map of
      // the sea — the failure mode most likely to survive all the way to a
      // demo. A blank map must never be silent.
      const message = String(e?.error?.message ?? e?.error ?? "unknown");
      console.error("[map]", message, e?.error);
      if (message.includes(TILES) || message.includes("pmtiles")) {
        setFailed("basemap tiles missing — run data/tiles/fetch_basemap.py");
      }
    });

    map.current = instance;
    // Development handle. A map that renders nothing is very hard to debug from
    // the outside, and this is the difference between reading its actual source
    // state and guessing at it.
    if (process.env.NODE_ENV !== "production") {
      (window as unknown as { lhMap?: MapLibreMap }).lhMap = instance;
    }
    return () => {
      instance.remove();
      map.current = null;
    };
  }, [snapshot, onZoomChange]);

  // Satellite toggles as a layer, not a restyle: rebuilding the style would
  // drop the data layers and the current viewport with them.
  useEffect(() => {
    const instance = map.current;
    if (!instance) return;

    const apply = () => {
      const has = instance.getLayer("lh-satellite");
      if (satellite && !has) {
        if (!instance.getSource("lh-satellite")) {
          instance.addSource("lh-satellite", {
            type: "raster",
            tiles: [ESRI_IMAGERY],
            tileSize: 256,
            maxzoom: 19,
            attribution: "Imagery © Esri",
          });
        }
        // Under the hazard and impact layers, over the basemap — imagery is
        // context for the data, not a replacement for it.
        instance.addLayer(
          { id: "lh-satellite", type: "raster", source: "lh-satellite", paint: { "raster-opacity": 0.9 } },
          "lh-cone-fill",
        );
      } else if (!satellite && has) {
        instance.removeLayer("lh-satellite");
      }
    };

    if (instance.isStyleLoaded()) apply();
    else instance.once("load", apply);
  }, [satellite]);

  if (failed) {
    return (
      <div role="status" style={{ padding: "var(--lh-space-5)", color: "var(--lh-quiet)" }}>
        {failed}
      </div>
    );
  }

  return <div ref={container} style={{ width: "100%", height: "100%" }} />;
}

export { ZOOM_SWITCH };
