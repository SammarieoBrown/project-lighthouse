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
import { buildingWeights, lighthouseFlavor, pruneLayers, readTokens, retarget } from "./flavor";
import {
  applyFrame, dataLayers, dataSources, readMapColours, ZOOM_SWITCH, type BaseView,
} from "./layers";

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

/* Where the basemap comes from, and it is two places.
 *
 * The archives are 138 MB of fetched-not-committed build output, so a deploy
 * has no way to produce them and production served 404s for every tile and
 * every sprite — silently, because the panel falls back to the SVG map and a
 * static but correct map does not look like a failure. They are published to a
 * public bucket instead, which serves HTTP Range natively.
 *
 * NEXT_PUBLIC_TILES_URL points at that bucket. Read as a literal member
 * expression because Next inlines these at build time and a computed key is not
 * inlined — the variable would be undefined in the browser and the failure
 * would be, again, a map of the open sea.
 *
 * Unset means local: the archives come from app/map/[file]/route.ts rather than
 * public/, because Next's static handler answers the first Range request with
 * the whole 98 MB body. That route stays, and offline development with it.
 *
 * Trimmed before use, and that is not defensive padding. Setting this with
 * `echo | vercel env add` stores the trailing newline, and the pmtiles protocol
 * matches its tile URLs with a regex whose `.` does not cross a newline — so the
 * URL silently fails to parse, every source dies with "Invalid PMTiles protocol
 * URL", and the map renders as open sea. One invisible byte, whole map gone. */
const TILES_BASE = (process.env.NEXT_PUBLIC_TILES_URL ?? "").trim().replace(/\/+$/, "");

const REGION_ARCHIVE = "caribbean-z11.pmtiles";
const ISLAND_ARCHIVE = "jamaica-z15.pmtiles";

/* Where the region hands over to the island. Jamaica fills the frame by here,
 * so nobody sees the seam. */
const BASEMAP_SWITCH = 10.5;

/* The region we hold tiles for. The map is fenced to it rather than allowed to
 * wander past the data: a basemap that ends mid-pan reads as a broken map, and
 * an operator who can scroll into blank ocean has been given a control that
 * only does something wrong. Constrain the viewport, do not chase the bbox. */
const COVERED: [[number, number], [number, number]] = [
  [-92.0, 7.0],
  [-57.0, 28.0],
];
/* Glyphs and sprites live beside the archives. The fontstack directories have
 * spaces in their names ("Noto Sans Regular") — the templates are handed to
 * MapLibre intact and it encodes them, so nothing here builds those URLs by
 * hand. Committed locally: about a megabyte, and without them the map renders
 * and silently loses every place name. */
const ASSETS = "assets/fonts/{fontstack}/{range}.pbf";

// MapLibre 6 rejects a relative sprite URL outright, so both arms are absolute.
const SPRITE = "assets/sprites/black";

/** The four asset URLs, resolved against the bucket or the local origin. */
function assetUrls(origin: string) {
  const local = !TILES_BASE;
  return {
    region: local ? `${origin}/map/${REGION_ARCHIVE}` : `${TILES_BASE}/${REGION_ARCHIVE}`,
    island: local ? `${origin}/map/${ISLAND_ARCHIVE}` : `${TILES_BASE}/${ISLAND_ARCHIVE}`,
    glyphs: local ? `${origin}/tiles/${ASSETS}` : `${TILES_BASE}/${ASSETS}`,
    sprite: local ? `${origin}/tiles/${SPRITE}` : `${TILES_BASE}/${SPRITE}`,
    local,
  };
}

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

/* What survives in the structures view besides the buildings.
 *
 * Land and water, and they are not optional. Jamaica's shape on this map comes
 * entirely from the basemap — there is no parish outline layer of our own — so
 * hiding every basemap layer takes the island with it. Buildings only exist
 * from about z14, which left the wide view as wind bands floating on an empty
 * sea: nothing to orient by, and no way to tell a missing layer from a country
 * with no buildings in it.
 *
 * So the coast stays and the roads, labels, landuse and POIs go. The buildings
 * become the only thing drawn *on* the island rather than the only thing on
 * the screen.
 *
 * The label exclusion is load-bearing, not tidiness: Protomaps names its text
 * layers `water_label_ocean`, `earth_label_islands` and so on, so matching on
 * "water" or "earth" alone keeps every place name on the map and the view is
 * no longer isolated at all. */
const keepInStructures = (id: string) =>
  /-(background|earth|water)/.test(id) && !id.includes("label");

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
  /** The current advisory. Changes on every step of the replay. */
  snapshot: Snapshot | null;
  /** Homes in the largest district. The circle scale is a share of it, and it
   *  is static across the storm — so it belongs to the map's construction and
   *  not to the frame. Passed as a number so the init effect has a primitive
   *  dependency that does not change when the advisory does. */
  maxDistrict: number;
  base: BaseView;
  onZoomChange?: (zoom: number) => void;
  /** Reported upward so the panel can fall back to the SVG map. */
  onFail?: (reason: string) => void;
};

export default function MapView({ snapshot, maxDistrict, base, onZoomChange, onFail }: MapViewProps) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<MapLibreMap | null>(null);
  const [failed, setFailed] = useState<string | null>(null);

  /* The frame the map was built with, read once at init and never a dependency
   * of it. Scrubbing must not rebuild the map: a new MapLibreMap per advisory
   * refetches the basemap, drops the viewport and flickers the whole panel on
   * every step. Frames arrive through setData in the effect below. */
  const latest = useRef(snapshot);
  latest.current = snapshot;

  /* Our own load flag rather than isStyleLoaded(). That method also returns
   * false while tiles are still arriving, long after the `load` event has
   * fired — so a frame applied on `once("load")` after the event would wait for
   * a callback that is never coming, and the map would sit on an old advisory
   * with nothing in the console to say why. */
  const ready = useRef(false);

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
    const url = assetUrls(window.location.origin);

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
        // Far enough out to see the whole basin and no further.
        minZoom: 5.2,
        maxBounds: COVERED,
        attributionControl: false,
        style: {
          version: 8,
          glyphs: url.glyphs,
          sprite: url.sprite,
          sources: {
            // Explicit tile templates rather than `url:`, so MapLibre never
            // waits on a TileJSON round trip through the protocol. The bounds
            // and zoom ranges are things we already know.
            region: {
              type: "vector",
              tiles: [`pmtiles://${url.region}/{z}/{x}/{y}`],
              minzoom: 0,
              maxzoom: 11,
              bounds: [-92.0, 7.0, -57.0, 28.0],
              attribution: "© OpenStreetMap",
            },
            island: {
              type: "vector",
              tiles: [`pmtiles://${url.island}/{z}/{x}/{y}`],
              minzoom: 0,
              maxzoom: 15,
              bounds: [-78.6, 17.6, -75.9, 18.7],
              attribution: "© OpenStreetMap",
            },
            ...dataSources(latest.current),
          },
          layers: [
            ...retarget(
              pruneLayers(basemapLayers("region", lighthouseFlavor(tokens), { lang: "en" })),
              "region",
              { maxzoom: BASEMAP_SWITCH },
            ),
            ...retarget(
              pruneLayers(basemapLayers("island", lighthouseFlavor(tokens), { lang: "en" })),
              "island",
              { minzoom: BASEMAP_SWITCH },
            ),
            ...dataLayers(colours, maxDistrict),
          ],
        },
      });
    } catch (error) {
      // WebGL2 is required by MapLibre 6. A machine without it is rare and
      // exactly the machine a demo will be run on, so say so rather than
      // rendering an empty box — the caller falls back to the SVG map.
      const reason = error instanceof Error ? error.message : "map failed to start";
      setFailed(reason);
      onFail?.(reason);
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
    instance.on("load", () => {
      ready.current = true;
      // Whatever advisory is selected by now, not the one this map was built
      // with. The replay can be scrubbed while the basemap is still coming up.
      applyFrame(instance, latest.current);
    });
    instance.on("error", (e) => {
      // Every map error gets logged. MapLibre swallows style failures into this
      // event and renders an empty canvas, which looks exactly like a map of
      // the sea — the failure mode most likely to survive all the way to a
      // demo. A blank map must never be silent.
      const message = String(e?.error?.message ?? e?.error ?? "unknown");
      console.error("[map]", message, e?.error);
      if (message.includes("pmtiles")) {
        // Names the source that is actually configured. Telling somebody on a
        // deploy to run a local fetch script is a wrong instruction, which is
        // worse than none.
        const reason = url.local
          ? "basemap tiles not staged — run data/tiles/fetch_basemap.py"
          : `basemap tiles unreachable at ${TILES_BASE}`;
        setFailed(reason);
        onFail?.(reason);
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
      ready.current = false;
      instance.remove();
      map.current = null;
    };
  }, [maxDistrict, onZoomChange, onFail]);

  /* Every step of the replay lands here: setData on sources that already exist.
   * No transition and no easing — a playing timeline is a state change, not a
   * decoration, and rule M1 reserves movement for a summons. */
  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready.current) return;
    applyFrame(instance, snapshot);
  }, [snapshot]);

  /* Structures: the basemap steps back so the buildings are the only thing on
   * it. Every basemap layer is hidden except the buildings, which are turned up
   * from the near-invisible weight they carry as context.
   *
   * Visibility and paint, never a restyle — the same reason the satellite layer
   * is added rather than swapped in. Our own layers all carry the `lh-` prefix
   * and are left alone: the hazard bands stay, the parish outlines stay, and
   * those outlines are what keeps the view navigable once the coastline the
   * basemap was drawing is gone. */
  useEffect(() => {
    const instance = map.current;
    if (!instance) return;

    const apply = () => {
      const style = instance.getStyle();
      if (!style?.layers) return;
      const structures = base === "structures";

      for (const layer of style.layers) {
        if (layer.id.startsWith("lh-")) continue;
        const isBuilding = layer.id.endsWith("-buildings");
        instance.setLayoutProperty(
          layer.id,
          "visibility",
          !structures || isBuilding || keepInStructures(layer.id) ? "visible" : "none",
        );
        if (isBuilding && layer.type === "fill") {
          const w = buildingWeights(readTokens(container.current ?? undefined));
          instance.setPaintProperty(
            layer.id,
            "fill-color",
            structures ? w.subject : w.quiet,
          );
        }
      }
    };

    if (ready.current) apply();
    else instance.once("load", apply);
  }, [base]);

  // Satellite toggles as a layer, not a restyle: rebuilding the style would
  // drop the data layers and the current viewport with them.
  useEffect(() => {
    const instance = map.current;
    if (!instance) return;
    const satellite = base === "satellite";

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
  }, [base]);

  // The panel renders the SVG fallback once it hears about this; keeping the
  // container mounted meanwhile avoids tearing down a map that may recover.
  return (
    <div
      ref={container}
      aria-hidden={failed ? true : undefined}
      style={{ width: "100%", height: "100%", display: failed ? "none" : undefined }}
    />
  );
}

export { ZOOM_SWITCH };
