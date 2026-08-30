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
import type { IControl, MapSourceDataEvent } from "maplibre-gl";
import { Protocol } from "pmtiles";
import { useCallback, useEffect, useRef, useState } from "react";

import "maplibre-gl/dist/maplibre-gl.css";

import type { GeoJSONSource } from "maplibre-gl";

import type { MapFocus, Snapshot } from "../map";
import { advisoryFlow, advisoryWind } from "./advisory-wind";
import { buildingWeights, lighthouseFlavor, pruneLayers, readTokens, retarget } from "./flavor";
import {
  applyFrame,
  dataLayers,
  dataSources,
  readMapColours,
  STRUCTURE_FOOTPRINT_ZOOM,
  STRUCTURES_MIN_ZOOM,
  type BaseView,
} from "./layers";

/* How fast the flow marks orbit. Slow on purpose: this is a cyclone turning,
 * not a spinner. Fast enough to read as rotation within a second or two of
 * looking, slow enough that it never competes with the posture chip for the
 * corner of an eye. */
const FLOW_RADIANS_PER_SECOND = 0.09;

/* MapLibre, wired by hand.
 *
 * No react-map-gl. There is one map on this product, it reads a static
 * snapshot, and the design system wants total control of every layer — a
 * hundred lines of init beats a dependency whose main offer is JSX sugar for
 * markers we would restyle anyway.
 *
 * The core basemap, glyphs and sprites are locally stageable. Reference imagery
 * is explicitly online-only and optional. The console has to preserve forecast
 * and impact evidence when a venue loses its internet, and a basemap that
 * silently fetches font ranges at draw time is not offline — it just looks
 * offline until the day it matters.
 */

/* Where the basemap comes from, and it is two places.
 *
 * The basemap archives are fetched-not-committed build output, so a deploy
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
 * public/, because Next's static handler can answer the first Range request
 * with the whole archive body. That route stays, and offline development with it.
 *
 * Trimmed before use, and that is not defensive padding. Setting this with
 * `echo | vercel env add` stores the trailing newline, and the pmtiles protocol
 * matches its tile URLs with a regex whose `.` does not cross a newline — so the
 * URL silently fails to parse, every source dies with "Invalid PMTiles protocol
 * URL", and the map renders as open sea. One invisible byte, whole map gone. */
const TILES_BASE = (process.env.NEXT_PUBLIC_TILES_URL ?? "").trim().replace(/\/+$/, "");

const REGION_ARCHIVE = "caribbean-z11.pmtiles";
const ISLAND_ARCHIVE = "jamaica-z15.pmtiles";
/* Ours, not Protomaps'. It is a mapped inventory of building geometry. Any
 * old forecast-entry attributes in the archive are intentionally ignored: the
 * selected advisory's current polygons already carry hazard, while a building
 * footprint carries only the neutral meaning "a mapped structure is here". */
const STRUCTURES_ARCHIVE = "structures-z15.pmtiles";

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
/* Glyphs and sprites live beside the archives locally, and a configured public
 * tile host must publish the same asset tree. The fontstack directories have
 * spaces in their names ("Noto Sans Regular") — the templates are handed to
 * MapLibre intact and it encodes them, so nothing here builds those URLs by
 * hand. Without these assets the map silently loses every place name. */
const ASSETS = "assets/fonts/{fontstack}/{range}.pbf";

// MapLibre 6 rejects a relative sprite URL outright, so both arms are absolute.
const SPRITE = "assets/sprites/black";

/** The four asset URLs, resolved against the bucket or the local origin. */
function assetUrls(origin: string) {
  const local = !TILES_BASE;
  return {
    region: local ? `${origin}/map/${REGION_ARCHIVE}` : `${TILES_BASE}/${REGION_ARCHIVE}`,
    island: local ? `${origin}/map/${ISLAND_ARCHIVE}` : `${TILES_BASE}/${ISLAND_ARCHIVE}`,
    structures: local
      ? `${origin}/map/${STRUCTURES_ARCHIVE}`
      : `${TILES_BASE}/${STRUCTURES_ARCHIVE}`,
    glyphs: local ? `${origin}/tiles/${ASSETS}` : `${TILES_BASE}/${ASSETS}`,
    sprite: local ? `${origin}/tiles/${SPRITE}` : `${TILES_BASE}/${SPRITE}`,
    local,
  };
}

// Esri reference imagery is online-only: its licence permits live tile use with
// attribution but prohibits caching outside ArcGIS. It is context imagery and
// is never represented as post-event damage evidence.
const ESRI_IMAGERY =
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";
const PROTOMAPS_ATTRIBUTION =
  '<a href="https://protomaps.com">Protomaps</a> © <a href="https://openstreetmap.org">OpenStreetMap</a>';
const MAPLIBRE_ATTRIBUTION =
  '<a href="https://maplibre.org/" target="_blank">MapLibre</a>';
const VIDA_ATTRIBUTION =
  '<a href="https://source.coop/vida/google-microsoft-osm-open-buildings">VIDA combined buildings</a> · Google Open Buildings, Microsoft GlobalML, OpenStreetMap · ODbL';

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

const JAMAICA_CENTER: [number, number] = [-77.3, 17.9];
const JAMAICA_ZOOM = 7.4;

class ResetJamaicaControl implements IControl {
  private map?: MapLibreMap;
  private container?: HTMLDivElement;

  onAdd(map: MapLibreMap): HTMLElement {
    this.map = map;
    const container = document.createElement("div");
    container.className = "maplibregl-ctrl maplibregl-ctrl-group lh-map-reset";
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Jamaica";
    button.title = "Reset map to Jamaica";
    button.setAttribute("aria-label", "Reset map to Jamaica");
    button.addEventListener("click", this.reset);
    container.append(button);
    this.container = container;
    return container;
  }

  onRemove(): void {
    this.container?.querySelector("button")?.removeEventListener("click", this.reset);
    this.container?.remove();
    this.container = undefined;
    this.map = undefined;
  }

  private reset = () => {
    // A reset is a state correction, not an animation. jumpTo also guarantees
    // that pitch and bearing cannot survive a browser gesture.
    this.map?.jumpTo({ center: JAMAICA_CENTER, zoom: JAMAICA_ZOOM, bearing: 0, pitch: 0 });
  };
}

// Both are global and throw if repeated, so they happen once per document
// rather than once per mount.
let configured = false;

function configureMapLibre() {
  if (configured) return;
  setWorkerUrl(`${window.location.origin}${WORKER}`);
  addProtocol("pmtiles", new Protocol().tile);
  configured = true;
}

function applyFocus(
  map: MapLibreMap,
  focus: MapFocus | null,
  move = true,
) {
  if (map.getLayer("lh-focused-parish")) {
    const parish = focus?.parish?.replace(/^St\.?\s+/, "Saint ") ?? "";
    map.setFilter("lh-focused-parish", ["==", ["get", "name"], parish]);
  }
  if (focus && move) {
    map.jumpTo({
      center: [focus.lon, focus.lat],
      zoom: Math.max(map.getZoom(), 11),
      bearing: 0,
      pitch: 0,
    });
  }
}

export type MapViewProps = {
  /** The current advisory. Changes on every step of the replay. */
  snapshot: Snapshot | null;
  base: BaseView;
  /** What the camera frames. The replay is about Jamaica; the live board is
   *  about the basin, and opening it on the island would hide every
   *  disturbance it exists to show. A jump, not an easing — changing view is
   *  a state change, and rule M1 reserves motion for a summons. */
  view?: "island" | "basin";
  /** Evidence-aware accessible name; a hindcast must never be announced as a forecast. */
  ariaLabel?: string;
  /** A list selection may centre the map on its district reference point. The
   *  point itself is never drawn; the real parish geometry is highlighted. */
  focus?: MapFocus | null;
  onZoomChange?: (zoom: number) => void;
  /** Reported upward so the panel can fall back to the SVG map. */
  onFail?: (reason: string) => void;
  /** A structures-only failure leaves the basemap healthy and is reported as a
   *  local availability state rather than a failure of the whole map. */
  onStructuresStatus?: (
    status: "idle" | "loading" | "ready" | "unavailable",
    reason?: string,
  ) => void;
  /** Reference imagery is optional. Its status lets the panel distinguish a
   *  loaded raster from the standard basemap that remains underneath it. */
  onImageryStatus?: (
    status: "idle" | "loading" | "ready" | "unavailable",
    reason?: string,
  ) => void;
};

export default function MapView({
  snapshot,
  base,
  view = "island",
  ariaLabel = "Interactive map of the selected replay with unavailable evidence provenance and synthetic modelled impact across Jamaica",
  focus = null,
  onZoomChange,
  onFail,
  onStructuresStatus,
  onImageryStatus,
}: MapViewProps) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<MapLibreMap | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [structuresStatus, setStructuresStatus] = useState<
    "idle" | "loading" | "ready" | "unavailable"
  >("idle");
  const structuresStatusRef = useRef(structuresStatus);
  structuresStatusRef.current = structuresStatus;
  const imageryStatusRef = useRef<"idle" | "loading" | "ready" | "unavailable">("idle");

  const reportStructures = useCallback((
    status: "idle" | "loading" | "ready" | "unavailable",
    reason?: string,
  ) => {
    setStructuresStatus(status);
    onStructuresStatus?.(status, reason);
  }, [onStructuresStatus]);

  const reportImagery = useCallback((
    status: "idle" | "loading" | "ready" | "unavailable",
    reason?: string,
  ) => {
    if (imageryStatusRef.current === status) return;
    imageryStatusRef.current = status;
    onImageryStatus?.(status, reason);
  }, [onImageryStatus]);

  /* The frame the map was built with, read once at init and never a dependency
   * of it. Scrubbing must not rebuild the map: a new MapLibreMap per advisory
   * refetches the basemap, drops the viewport and flickers the whole panel on
   * every step. Frames arrive through setData in the effect below. */
  const latest = useRef(snapshot);
  latest.current = snapshot;
  const latestFocus = useRef(focus);
  latestFocus.current = focus;

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
        center: JAMAICA_CENTER,
        zoom: JAMAICA_ZOOM,
        maxZoom: 17,
        // Far enough out that the basin view can fit the whole covered
        // region — the live board frames COVERED with fitBounds, and a floor
        // above the fitting zoom silently crops the Gulf and the Leewards off
        // its edges (it did, at 5.2 and again at 4.5 on a half-width pane).
        // No further out than the fit needs: past the fence there is only
        // blank ocean to scroll into.
        minZoom: 4,
        maxBounds: COVERED,
        bearing: 0,
        pitch: 0,
        maxPitch: 0,
        dragRotate: false,
        pitchWithRotate: false,
        touchPitch: false,
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
              attribution: PROTOMAPS_ATTRIBUTION,
            },
            island: {
              type: "vector",
              tiles: [`pmtiles://${url.island}/{z}/{x}/{y}`],
              minzoom: 0,
              maxzoom: 15,
              bounds: [-78.6, 17.6, -75.9, 18.7],
              attribution: PROTOMAPS_ATTRIBUTION,
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
            ...dataLayers(colours),
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

    instance.keyboard.disableRotation();
    instance.touchZoomRotate.disableRotation();
    instance.addControl(new NavigationControl({ showCompass: false }), "top-right");
    instance.addControl(new ResetJamaicaControl(), "top-right");
    instance.addControl(
      // Each source supplies its own attribution. Leave `compact` unset so
      // MapLibre can collapse this control when the map is narrower than the
      // legal copy, while keeping it expanded whenever it comfortably fits.
      new AttributionControl({ customAttribution: MAPLIBRE_ATTRIBUTION }),
      "bottom-right",
    );
    instance.addControl(new ScaleControl({ unit: "metric" }), "bottom-left");

    instance.on("zoom", () => onZoomChange?.(instance.getZoom()));
    instance.on("load", () => {
      ready.current = true;
      // Whatever advisory is selected by now, not the one this map was built
      // with. The replay can be scrubbed while the basemap is still coming up.
      applyFrame(instance, latest.current);
      applyFocus(instance, latestFocus.current, true);
    });
    instance.on("error", (e) => {
      // Every map error gets logged. MapLibre swallows style failures into this
      // event and renders an empty canvas, which looks exactly like a map of
      // the sea — the failure mode most likely to survive all the way to a
      // demo. A blank map must never be silent.
      const message = String(e?.error?.message ?? e?.error ?? "unknown");
      console.error("[map]", message, e?.error);
      const sourceId = (e as unknown as { sourceId?: string }).sourceId;
      const structuresError = sourceId === "lh-structures" || message.includes(STRUCTURES_ARCHIVE);
      if (structuresError) {
        for (const id of ["lh-structure-points", "lh-structures"]) {
          if (instance.getLayer(id)) instance.setLayoutProperty(id, "visibility", "none");
        }
        reportStructures("unavailable", "structure inventory could not be read");
        return;
      }

      const imageryError = sourceId === "lh-satellite"
        || message.includes("server.arcgisonline.com")
        || message.includes("World_Imagery");
      if (imageryError) {
        // A partially loaded raster can look authoritative even after the
        // source has failed. Remove it as a visual claim and reveal the local,
        // standard basemap that has remained underneath throughout.
        if (instance.getLayer("lh-satellite")) {
          instance.setLayoutProperty("lh-satellite", "visibility", "none");
        }
        reportImagery("unavailable", "reference imagery could not be read");
        return;
      }

      const basemapError = sourceId === "region"
        || sourceId === "island"
        || message.includes(REGION_ARCHIVE)
        || message.includes(ISLAND_ARCHIVE);
      if (basemapError) {
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
  }, [onZoomChange, onFail, reportImagery, reportStructures]);

  /* Every step of the replay lands here: setData on sources that already exist.
   * No transition and no easing — a playing timeline is a state change, not a
   * decoration, and rule M1 reserves movement for a summons. */
  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready.current) return;
    applyFrame(instance, snapshot);
  }, [snapshot]);

  /* The camera follows the selected view, instantly. Runs once at mount too,
   * which is a no-op for the island view the map was built with. */
  useEffect(() => {
    const instance = map.current;
    if (!instance) return;
    if (view === "basin") {
      instance.fitBounds(COVERED, { animate: false, padding: 24 });
    } else {
      instance.jumpTo({ center: JAMAICA_CENTER, zoom: JAMAICA_ZOOM });
    }
  }, [view]);

  /* The flow marks turn.
   *
   * This is the console's first moving pixel and it is a written exception to
   * rule M1, not an oversight — see the design rules, Part 3 M1. The argument
   * that makes it honest is M1's own: motion means state changed, and a
   * cyclone's defining state is that it is rotating. Three static outlines can
   * say how far the wind reaches and cannot say that it turns at all.
   *
   * What keeps it inside the rule: it is bounded to this one source, it carries
   * no colour, chrome or affordance of its own, it never signals status, and it
   * stops dead for a hidden tab or a reduced-motion preference. Nothing else on
   * this screen moves.
   *
   * `prefers-reduced-motion` is read here in JS rather than left to the CSS
   * token block, because that block only shrinks durations and cannot reach a
   * source update. When it is set the marks are still drawn — the circulation
   * is evidence, and the reader who cannot take motion still needs it — they
   * simply hold still. */
  useEffect(() => {
    const instance = map.current;
    if (!instance) return;

    const wind = advisoryWind(
      snapshot?.centre,
      snapshot?.motion?.maxWindKt,
      snapshot?.motion?.headingDeg ?? null,
      snapshot?.motion?.forwardSpeedKt ?? null,
      snapshot?.wind34,
    );
    if (!wind) return;

    const still = window.matchMedia("(prefers-reduced-motion: reduce)");
    if (still.matches) return;

    let frameId = 0;
    let last = 0;
    let phase = 0;

    const tick = (now: number) => {
      frameId = window.requestAnimationFrame(tick);
      if (document.hidden) {
        last = now;
        return;
      }
      const elapsed = last === 0 ? 0 : Math.min(0.25, (now - last) / 1000);
      last = now;
      phase += elapsed * FLOW_RADIANS_PER_SECOND;
      const source = instance.getSource("lh-flow") as GeoJSONSource | undefined;
      source?.setData(advisoryFlow(wind, phase));
    };

    frameId = window.requestAnimationFrame(tick);
    return () => window.cancelAnimationFrame(frameId);
  }, [snapshot]);

  /* A ranked-list selection changes the viewport and strengthens the matching
   * real parish outline. The supplied district coordinate is used only as a
   * camera target; it is never rendered as if it were an observed location. */
  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready.current) return;
    applyFocus(instance, focus);
  }, [focus?.key, focus?.lat, focus?.lon, focus?.parish]);

  /* Structures adds a layer; it does not take the map away.
   *
   * The first version hid every basemap layer so the buildings stood alone,
   * copying the standalone damage viewer. That viewer is looked at. This is
   * navigated — and stripped of roads, rivers and town names there was no way
   * to tell Black River from Old Harbour, which is the first question anyone
   * asks of a map before acting on it. Same context in all three views.
   *
   * Visibility only, never a restyle: rebuilding the style drops the data
   * layers and the viewport with them. */
  useEffect(() => {
    const instance = map.current;
    if (!instance) return;

    const apply = () => {
      const style = instance.getStyle();
      if (!style?.layers) return;
      const structures = base === "structures" && structuresStatus === "ready";

      for (const layer of style.layers) {
        if (layer.id.startsWith("lh-")) continue;
        // Only the basemap's own buildings change: ours replace them, and two
        // sets of footprints drawn over each other is just a heavier smudge.
        // Everything else — coastline, roads, rivers, town names — stays
        // exactly as it is in the Map view.
        const isBuilding = layer.id.endsWith("-buildings");
        instance.setLayoutProperty(
          layer.id,
          "visibility",
          structures && isBuilding ? "none" : "visible",
        );
      }
    };

    if (ready.current) apply();
    else instance.once("load", apply);
  }, [base, structuresStatus]);

  /* Our own building tileset, added only when asked for.
   *
   * Lazy on purpose: it is the largest archive on the map and most sessions
   * never open the structures view, so the source is created on first entry
   * rather than at init. Left in place afterwards — a second visit should not
   * refetch what is already in the browser.
   *
   * It is deliberately neutral. Forecast extent comes from the current
   * advisory's polygons above it; old first-entry attributes in a cached archive
   * do not colour a structure and cannot turn a forecast into cumulative fact. */
  useEffect(() => {
    const instance = map.current;
    if (!instance || base !== "structures") return;

    // The aggregate archive begins at z9. Selecting the mode should show its
    // subject immediately, not open an empty view with a repair instruction.
    if (instance.getZoom() < STRUCTURES_MIN_ZOOM) {
      instance.jumpTo({ zoom: STRUCTURES_MIN_ZOOM, bearing: 0, pitch: 0 });
    }

    let cancelled = false;
    const controller = new AbortController();
    const archive = assetUrls(window.location.origin).structures;

    const apply = async () => {
      const retrying = structuresStatusRef.current === "unavailable";
      if (retrying) {
        // A tile-level failure happens after the source and layers exist. Clear
        // that failed cache on the next explicit selection so a transient
        // archive/network error can recover without reloading the whole page.
        for (const id of ["lh-structure-points", "lh-structures"]) {
          if (instance.getLayer(id)) instance.removeLayer(id);
        }
        if (instance.getSource("lh-structures")) instance.removeSource("lh-structures");
      }
      if (instance.getSource("lh-structures")) {
        reportStructures("ready");
        return;
      }

      reportStructures("loading");
      try {
        /* Verify this optional archive independently. Requiring a one-byte 206
         * prevents an ignored Range header from starting a download of the
         * whole archive, and means its absence can be handled before MapLibre
         * turns it into a generic map error. */
        const response = await fetch(archive, {
          headers: { Range: "bytes=0-0" },
          signal: controller.signal,
        });
        await response.body?.cancel();
        if (response.status !== 206) {
          throw new Error(response.ok
            ? "structure host does not support byte ranges"
            : `structure archive returned ${response.status}`);
        }
        if (cancelled) return;

        const subject = buildingWeights(readTokens(container.current ?? undefined)).subject;
        instance.addSource("lh-structures", {
          type: "vector",
          tiles: [`pmtiles://${archive}/{z}/{x}/{y}`],
          minzoom: STRUCTURES_MIN_ZOOM,
          maxzoom: 15,
          bounds: [-78.6, 17.6, -75.9, 18.7],
          attribution: VIDA_ATTRIBUTION,
        });

        /* Below z14 each point is a deterministic grid aggregate whose `w`
         * property is the exact number of mapped structures represented.
         * Radius uses that count, while bounded stops keep dense cells from
         * covering neighbouring places. At z14 mapped source footprints replace it. */
        instance.addLayer(
          {
            id: "lh-structure-points",
            type: "circle",
            source: "lh-structures",
            "source-layer": "structure_points",
            minzoom: STRUCTURES_MIN_ZOOM,
            maxzoom: STRUCTURE_FOOTPRINT_ZOOM,
            paint: {
              "circle-radius": [
                "interpolate",
                ["linear"],
                ["sqrt", ["coalesce", ["get", "w"], 1]],
                1, 1,
                4, 1.6,
                10, 2.4,
                32, 4,
              ],
              "circle-color": subject,
              "circle-opacity": 0.8,
            },
          },
          "lh-cone-fill",
        );
        instance.addLayer(
          {
            id: "lh-structures",
            type: "fill",
            source: "lh-structures",
            "source-layer": "structures",
            minzoom: STRUCTURE_FOOTPRINT_ZOOM,
            paint: { "fill-color": subject, "fill-opacity": 0.92 },
          },
          "lh-cone-fill",
        );
        reportStructures("ready");
      } catch (error) {
        if (cancelled || controller.signal.aborted) return;
        const reason = error instanceof Error ? error.message : "structure inventory unavailable";
        console.warn("[map]", reason);
        reportStructures("unavailable", reason);
      }
    };

    const run = () => void apply();
    if (ready.current) run();
    else instance.once("load", run);
    return () => {
      cancelled = true;
      controller.abort();
      instance.off("load", run);
    };
  }, [base, reportStructures]);

  // The structures layer is hidden rather than removed when the view changes,
  // so its tiles survive a round trip through the other two bases.
  useEffect(() => {
    const instance = map.current;
    if (!instance?.getLayer("lh-structures")) return;
    const visibility = base === "structures" && structuresStatus === "ready" ? "visible" : "none";
    for (const id of ["lh-structure-points", "lh-structures"]) {
      instance.setLayoutProperty(id, "visibility", visibility);
    }
  }, [base, structuresStatus]);

  // Reference imagery toggles as a layer, not a restyle: rebuilding the style
  // would drop the data layers and the current viewport with them. The local
  // basemap stays visible below it, including throughout loading and failure.
  useEffect(() => {
    const instance = map.current;
    if (!instance) return;
    const satellite = base === "satellite";

    const onSourceData = (event: MapSourceDataEvent) => {
      if (
        event.sourceId === "lh-satellite"
        && event.tile
        && event.isSourceLoaded
        && imageryStatusRef.current === "loading"
      ) {
        reportImagery("ready");
      }
    };

    const onMapIdle = () => {
      if (
        instance.getSource("lh-satellite")
        && instance.isSourceLoaded("lh-satellite")
        && imageryStatusRef.current === "loading"
      ) {
        reportImagery("ready");
      }
    };

    if (satellite) {
      instance.on("sourcedata", onSourceData);
      // `idle` also covers a successful return to already cached tiles, where
      // no new tile-completion event is required.
      instance.on("idle", onMapIdle);
    }

    const apply = () => {
      const hasLayer = instance.getLayer("lh-satellite");
      if (satellite) {
        const retrying = imageryStatusRef.current === "unavailable";
        if (retrying) {
          if (hasLayer) instance.removeLayer("lh-satellite");
          if (instance.getSource("lh-satellite")) instance.removeSource("lh-satellite");
        }
        reportImagery("loading");
        if (!instance.getSource("lh-satellite")) {
          instance.addSource("lh-satellite", {
            type: "raster",
            tiles: [ESRI_IMAGERY],
            tileSize: 256,
            maxzoom: 19,
            attribution: "Sources: Esri, Maxar, Earthstar Geographics, and the GIS User Community",
          });
        }
        if (!hasLayer || retrying) {
          // Under the hazard and impact layers, over the basemap — imagery is
          // context for the data, not a replacement for it.
          instance.addLayer(
            {
              id: "lh-satellite",
              type: "raster",
              source: "lh-satellite",
              paint: { "raster-opacity": 0.9 },
            },
            "lh-parish-impact-fill",
          );
        } else {
          instance.setLayoutProperty("lh-satellite", "visibility", "visible");
        }

      } else if (hasLayer) {
        instance.setLayoutProperty("lh-satellite", "visibility", "none");
      }
    };

    /* ready.current, never isStyleLoaded(). That method also returns false
     * while tiles are still arriving, long after `load` has fired — so a
     * satellite toggle during tile load registered a `once("load")` for an
     * event already past, and the raster was never removed. Switching from
     * Satellite to Structures left the imagery sitting on top of everything. */
    if (ready.current) apply();
    else instance.once("load", apply);
    return () => {
      instance.off("load", apply);
      instance.off("sourcedata", onSourceData);
      instance.off("idle", onMapIdle);
    };
  }, [base, reportImagery]);

  // The panel renders the SVG fallback once it hears about this; keeping the
  // container mounted meanwhile avoids tearing down a map that may recover.
  return (
    <div
      ref={container}
      role="region"
      aria-hidden={failed ? true : undefined}
      aria-label={ariaLabel}
      style={{ width: "100%", height: "100%", display: failed ? "none" : undefined }}
    />
  );
}
