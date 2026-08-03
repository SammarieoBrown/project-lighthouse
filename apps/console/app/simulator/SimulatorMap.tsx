"use client";

import { layers as basemapLayers } from "@protomaps/basemaps";
import {
  AttributionControl,
  Map as MapLibreMap,
  NavigationControl,
  ScaleControl,
  addProtocol,
  setWorkerUrl,
} from "maplibre-gl";
import type { ErrorEvent, GeoJSONSource, MapMouseEvent, MapSourceDataEvent } from "maplibre-gl";
import { Protocol } from "pmtiles";
import { useEffect, useRef } from "react";

import "maplibre-gl/dist/maplibre-gl.css";

import {
  lighthouseFlavor,
  pruneLayers,
  readTokens,
  retarget,
} from "../eoc/map/flavor";
import {
  type AuthoredScenario,
  type CommunityImpact,
  type SimulationFrame,
  type LngLat,
  windFields,
} from "./model";
import { ParticleWindLayer } from "./particle-wind-layer";

type Parish = {
  name: string;
  geometry: { type: "Polygon" | "MultiPolygon"; coordinates: unknown };
};

export type SimulatorMapProps = {
  scenario: AuthoredScenario;
  frame: SimulationFrame | null;
  parishes: Parish[];
  communities: CommunityImpact[];
  drawing: boolean;
  playing: boolean;
  reducedMotion: boolean;
  imageryTemplate?: string;
  onTrackChange: (track: LngLat[]) => void;
  onFailure: (reason: string) => void;
  onParticleStatus: (status: "ready" | "unavailable", reason?: string) => void;
  onImageryStatus: (status: "idle" | "loading" | "ready" | "unavailable") => void;
};

const TILES_BASE = (process.env.NEXT_PUBLIC_TILES_URL ?? "").trim().replace(/\/+$/, "");
const REGION_ARCHIVE = "caribbean-z11.pmtiles";
const ISLAND_ARCHIVE = "jamaica-z15.pmtiles";
const BASEMAP_SWITCH = 10.5;
const WORKER = "/maplibre/maplibre-gl-worker.mjs";
const COVERED: [[number, number], [number, number]] = [[-92, 7], [-57, 28]];

export default function SimulatorMap({
  scenario,
  frame,
  parishes,
  communities,
  drawing,
  playing,
  reducedMotion,
  imageryTemplate,
  onTrackChange,
  onFailure,
  onParticleStatus,
  onImageryStatus,
}: SimulatorMapProps) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<MapLibreMap | null>(null);
  const particle = useRef<ParticleWindLayer | null>(null);
  const ready = useRef(false);
  const dragged = useRef<number | null>(null);
  const moved = useRef(false);
  const latest = useRef({ scenario, frame, parishes, communities, drawing });
  latest.current = { scenario, frame, parishes, communities, drawing };
  const trackChange = useRef(onTrackChange);
  trackChange.current = onTrackChange;
  const callbacks = useRef({ onFailure, onParticleStatus, onImageryStatus });
  callbacks.current = { onFailure, onParticleStatus, onImageryStatus };

  useEffect(() => {
    if (!container.current || map.current) return;
    setWorkerUrl(`${window.location.origin}${WORKER}`);
    addProtocol("pmtiles", new Protocol().tile);
    const local = !TILES_BASE;
    const region = local
      ? `${window.location.origin}/map/${REGION_ARCHIVE}`
      : `${TILES_BASE}/${REGION_ARCHIVE}`;
    const island = local
      ? `${window.location.origin}/map/${ISLAND_ARCHIVE}`
      : `${TILES_BASE}/${ISLAND_ARCHIVE}`;
    const glyphs = local
      ? `${window.location.origin}/tiles/assets/fonts/{fontstack}/{range}.pbf`
      : `${TILES_BASE}/assets/fonts/{fontstack}/{range}.pbf`;
    const sprite = local
      ? `${window.location.origin}/tiles/assets/sprites/black`
      : `${TILES_BASE}/assets/sprites/black`;
    const tokens = readTokens(container.current);
    const colours = readColours(container.current);

    let instance: MapLibreMap;
    try {
      instance = new MapLibreMap({
        container: container.current,
        center: [-77.3, 17.8],
        zoom: 6.45,
        minZoom: 5.2,
        maxZoom: 15,
        maxBounds: COVERED,
        pitch: 0,
        bearing: 0,
        maxPitch: 0,
        dragRotate: false,
        pitchWithRotate: false,
        touchPitch: false,
        attributionControl: false,
        style: {
          version: 8,
          glyphs,
          sprite,
          sources: {
            region: {
              type: "vector",
              tiles: [`pmtiles://${region}/{z}/{x}/{y}`],
              minzoom: 0,
              maxzoom: 11,
              bounds: [-92, 7, -57, 28],
              attribution: '<a href="https://protomaps.com">Protomaps</a> © OpenStreetMap',
            },
            island: {
              type: "vector",
              tiles: [`pmtiles://${island}/{z}/{x}/{y}`],
              minzoom: 0,
              maxzoom: 15,
              bounds: [-78.6, 17.6, -75.9, 18.7],
              attribution: '<a href="https://protomaps.com">Protomaps</a> © OpenStreetMap',
            },
            "sim-parishes": { type: "geojson", data: parishData(latest.current.parishes, latest.current.communities) },
            "sim-track": { type: "geojson", data: trackData(latest.current.scenario.track) },
            "sim-handles": { type: "geojson", data: handleData(latest.current.scenario.track) },
            "sim-wind": { type: "geojson", data: windData(latest.current.scenario, latest.current.frame) },
            "sim-centre": { type: "geojson", data: centreData(latest.current.frame) },
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
            {
              id: "sim-impact",
              type: "fill",
              source: "sim-parishes",
              paint: {
                "fill-color": [
                  "match", ["get", "band"],
                  "destroyed", colours.critical,
                  "major", colours.elevated,
                  "rgba(0,0,0,0)",
                ],
                "fill-opacity": ["match", ["get", "band"], "destroyed", 0.34, "major", 0.27, 0],
              },
            },
            {
              id: "sim-parish-lines",
              type: "line",
              source: "sim-parishes",
              paint: { "line-color": colours.quiet, "line-width": 0.8, "line-opacity": 0.55 },
            },
            {
              id: "sim-wind-fill",
              type: "fill",
              source: "sim-wind",
              paint: {
                "fill-color": [
                  "match", ["get", "thresholdKt"],
                  34, colours.hazard34,
                  50, colours.hazard50,
                  colours.hazard64,
                ],
                "fill-opacity": ["match", ["get", "thresholdKt"], 34, 0.05, 50, 0.07, 0.1],
              },
            },
            {
              id: "sim-wind-lines",
              type: "line",
              source: "sim-wind",
              paint: {
                "line-color": [
                  "match", ["get", "thresholdKt"],
                  34, colours.hazard34,
                  50, colours.hazard50,
                  colours.hazard64,
                ],
                "line-width": ["match", ["get", "thresholdKt"], 34, 1, 50, 1.25, 1.75],
                "line-opacity": 0.82,
              },
            },
            {
              id: "sim-track-line",
              type: "line",
              source: "sim-track",
              paint: {
                "line-color": colours.figure,
                "line-width": 1.5,
                "line-dasharray": [5, 3],
                "line-opacity": 0.82,
              },
            },
            {
              id: "sim-handles",
              type: "circle",
              source: "sim-handles",
              paint: {
                "circle-radius": 5,
                "circle-color": colours.ground,
                "circle-stroke-color": colours.figure,
                "circle-stroke-width": 1.5,
              },
            },
            {
              id: "sim-centre-ring",
              type: "circle",
              source: "sim-centre",
              paint: {
                "circle-radius": 11,
                "circle-color": "rgba(0,0,0,0)",
                "circle-stroke-color": colours.figure,
                "circle-stroke-width": 1.5,
              },
            },
            {
              id: "sim-centre-eye",
              type: "circle",
              source: "sim-centre",
              paint: { "circle-radius": 2.5, "circle-color": colours.figure },
            },
          ],
        },
      });
    } catch (error) {
      callbacks.current.onFailure(error instanceof Error ? error.message : "simulation map failed to start");
      return;
    }

    instance.keyboard.disableRotation();
    instance.touchZoomRotate.disableRotation();
    instance.addControl(new NavigationControl({ showCompass: false }), "top-right");
    instance.addControl(new ScaleControl({ unit: "metric" }), "bottom-left");
    instance.addControl(new AttributionControl({ compact: true }), "bottom-right");

    const onClick = (event: MapMouseEvent) => {
      if (!latest.current.drawing || moved.current) {
        moved.current = false;
        return;
      }
      const features = instance.queryRenderedFeatures(event.point, { layers: ["sim-handles"] });
      if (features.length > 0) return;
      trackChange.current([
        ...latest.current.scenario.track,
        [roundCoordinate(event.lngLat.lng), roundCoordinate(event.lngLat.lat)],
      ]);
    };

    const onMouseDown = (event: MapMouseEvent) => {
      if (!latest.current.drawing) return;
      const feature = instance.queryRenderedFeatures(event.point, { layers: ["sim-handles"] })[0];
      const index = Number(feature?.properties?.index);
      if (!Number.isInteger(index)) return;
      event.preventDefault();
      dragged.current = index;
      moved.current = false;
      instance.dragPan.disable();
    };
    const onMouseMove = (event: MapMouseEvent) => {
      if (dragged.current === null) return;
      moved.current = true;
      const track = latest.current.scenario.track.map((point, index) =>
        index === dragged.current
          ? [roundCoordinate(event.lngLat.lng), roundCoordinate(event.lngLat.lat)] as LngLat
          : point,
      );
      trackChange.current(track);
    };
    const onMouseUp = () => {
      dragged.current = null;
      instance.dragPan.enable();
    };

    instance.on("click", onClick);
    instance.on("mousedown", "sim-handles", onMouseDown);
    instance.on("mousemove", onMouseMove);
    instance.on("mouseup", onMouseUp);
    instance.on("mouseenter", "sim-handles", () => { instance.getCanvas().style.cursor = "grab"; });
    instance.on("mouseleave", "sim-handles", () => { instance.getCanvas().style.cursor = latest.current.drawing ? "crosshair" : ""; });
    instance.on("error", (event) => {
      const message = String(event?.error?.message ?? event?.error ?? "unknown map error");
      if (message.includes(REGION_ARCHIVE) || message.includes(ISLAND_ARCHIVE)) {
        callbacks.current.onFailure("basemap tiles are unavailable");
      }
    });
    instance.on("load", () => {
      ready.current = true;
      const initial = latest.current.frame;
      if (initial) {
        const layer = new ParticleWindLayer(
          {
            centre: initial.centre,
            headingDeg: initial.headingDeg,
            control: latest.current.scenario,
            running: false,
            reducedMotion,
          },
          toRgba(colours.hazard50, 0.42),
          (reason) => callbacks.current.onParticleStatus("unavailable", reason),
        );
        try {
          instance.addLayer(layer, "sim-track-line");
          particle.current = layer;
          callbacks.current.onParticleStatus("ready");
        } catch (error) {
          callbacks.current.onParticleStatus(
            "unavailable",
            error instanceof Error ? error.message : "particle wind layer unavailable",
          );
        }
      }
    });
    map.current = instance;
    return () => {
      ready.current = false;
      particle.current = null;
      instance.remove();
      map.current = null;
    };
  }, []);

  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready.current) return;
    setGeoJson(instance, "sim-parishes", parishData(parishes, communities));
    setGeoJson(instance, "sim-track", trackData(scenario.track));
    setGeoJson(instance, "sim-handles", handleData(scenario.track));
    setGeoJson(instance, "sim-wind", windData(scenario, frame));
    setGeoJson(instance, "sim-centre", centreData(frame));
    instance.getCanvas().style.cursor = drawing ? "crosshair" : "";
    if (frame) {
      particle.current?.setState({
        centre: frame.centre,
        headingDeg: frame.headingDeg,
        control: scenario,
        running: playing,
        reducedMotion,
      });
    }
  }, [scenario, frame, parishes, communities, drawing, playing, reducedMotion]);

  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready.current) return;
    const layer = "sim-goes-imagery";
    const source = "sim-goes-imagery";
    if (!imageryTemplate) {
      if (instance.getLayer(layer)) instance.removeLayer(layer);
      if (instance.getSource(source)) instance.removeSource(source);
      onImageryStatus("idle");
      return;
    }
    onImageryStatus("loading");
    try {
      // The nearest observed frame changes as simulated time advances. Raster
      // tile URLs are immutable on a source, so replace the tiny source/layer
      // pair instead of accidentally leaving the first GOES frame on screen.
      if (instance.getLayer(layer)) instance.removeLayer(layer);
      if (instance.getSource(source)) instance.removeSource(source);
      instance.addSource(source, {
        type: "raster",
        tiles: [imageryTemplate],
        tileSize: 256,
        attribution: "NOAA GOES imagery",
      });
      instance.addLayer({
        id: layer,
        type: "raster",
        source,
        paint: { "raster-opacity": 0.82 },
      }, "sim-impact");
    } catch (error) {
      onImageryStatus("unavailable");
      return;
    }
    const onData = (event: MapSourceDataEvent) => {
      if (event.sourceId === source && event.isSourceLoaded) onImageryStatus("ready");
    };
    const onError = (event: ErrorEvent) => {
      const sourceId = "sourceId" in event ? String(event.sourceId) : "";
      const message = String(event.error?.message ?? event.error ?? "");
      if (sourceId === source || message.includes(imageryTemplate)) onImageryStatus("unavailable");
    };
    instance.on("sourcedata", onData);
    instance.on("error", onError);
    return () => {
      instance.off("sourcedata", onData);
      instance.off("error", onError);
    };
  }, [imageryTemplate, onImageryStatus]);

  return (
    <div
      ref={container}
      role="application"
      aria-label="Editable storm simulation map. Add or drag track points when drawing is enabled."
      style={{ width: "100%", height: "100%" }}
    />
  );
}

function parishData(parishes: Parish[], communities: CommunityImpact[]): GeoJSON.FeatureCollection {
  const byParish = new Map<string, { structures: number; majorPlus: number; destroyed: number }>();
  for (const community of communities) {
    const key = normalise(community.parish);
    const current = byParish.get(key) ?? { structures: 0, majorPlus: 0, destroyed: 0 };
    current.structures += community.structures;
    current.majorPlus += community.major + community.destroyed;
    current.destroyed += community.destroyed;
    byParish.set(key, current);
  }
  return {
    type: "FeatureCollection",
    features: parishes.map((parish) => {
      const totals = byParish.get(normalise(parish.name)) ?? { structures: 0, majorPlus: 0, destroyed: 0 };
      const destroyedShare = totals.structures > 0 ? totals.destroyed / totals.structures : 0;
      const majorShare = totals.structures > 0 ? totals.majorPlus / totals.structures : 0;
      const band = destroyedShare >= 0.25 ? "destroyed" : majorShare >= 0.25 ? "major" : "none";
      return {
        type: "Feature",
        properties: { name: parish.name, band, ...totals },
        geometry: parish.geometry as GeoJSON.Geometry,
      };
    }),
  };
}

function trackData(track: LngLat[]): GeoJSON.FeatureCollection {
  return {
    type: "FeatureCollection",
    features: track.length >= 2
      ? [{ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: track } }]
      : [],
  };
}

function handleData(track: LngLat[]): GeoJSON.FeatureCollection {
  return {
    type: "FeatureCollection",
    features: track.map((point, index) => ({
      type: "Feature",
      properties: { index },
      geometry: { type: "Point", coordinates: point },
    })),
  };
}

function windData(
  scenario: AuthoredScenario,
  frame: SimulationFrame | null,
): GeoJSON.FeatureCollection<GeoJSON.Polygon> {
  return frame
    ? windFields(frame.centre, frame.headingDeg, scenario)
    : { type: "FeatureCollection", features: [] };
}

function centreData(frame: SimulationFrame | null): GeoJSON.FeatureCollection {
  return {
    type: "FeatureCollection",
    features: frame
      ? [{ type: "Feature", properties: {}, geometry: { type: "Point", coordinates: frame.centre } }]
      : [],
  };
}

function setGeoJson(map: MapLibreMap, id: string, data: GeoJSON.GeoJSON) {
  (map.getSource(id) as GeoJSONSource | undefined)?.setData(data);
}

function readColours(element: HTMLElement) {
  const style = getComputedStyle(element);
  const read = (name: string, fallback: string) => style.getPropertyValue(name).trim() || fallback;
  return {
    ground: read("--lh-ground", "#101413"),
    figure: read("--lh-figure", "#e9eae4"),
    quiet: read("--lh-quiet", "#7f8b85"),
    critical: read("--lh-critical", "#e4574a"),
    elevated: read("--lh-elevated", "#e8a33d"),
    hazard34: read("--lh-hazard-34", "#3f6c96"),
    hazard50: read("--lh-hazard-50", "#5590c9"),
    hazard64: read("--lh-hazard-64", "#7fb8f0"),
  };
}

function toRgba(colour: string, alpha: number): [number, number, number, number] {
  const value = colour.startsWith("#") ? colour.slice(1) : "5590c9";
  const full = value.length === 3 ? value.split("").map((part) => part + part).join("") : value;
  return [
    parseInt(full.slice(0, 2), 16) / 255,
    parseInt(full.slice(2, 4), 16) / 255,
    parseInt(full.slice(4, 6), 16) / 255,
    alpha,
  ];
}

function normalise(value: string) {
  return value.trim().toLowerCase().replace(/^st\.?\s+/, "saint ");
}

function roundCoordinate(value: number) {
  return Number(value.toFixed(4));
}
