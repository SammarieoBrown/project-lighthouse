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
import type { ErrorEvent, GeoJSONSource, ImageSource, MapMouseEvent, MapSourceDataEvent } from "maplibre-gl";
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
  bearingDeg,
  destination,
  radiusAtThreshold,
  windVectorAt,
  windFields,
} from "./model";
import {
  createSyntheticWeatherImage,
  type SyntheticWeatherPalette,
} from "./synthetic-weather-layer";

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
  reducedMotion: boolean;
  showWindThresholds: boolean;
  showImpactOverlay: boolean;
  imageryTemplate?: string;
  onTrackChange: (track: LngLat[]) => void;
  onFailure: (reason: string) => void;
  onParticleStatus: (status: "ready" | "unavailable", reason?: string) => void;
  onWeatherStatus: (status: "ready" | "unavailable", reason?: string) => void;
  onImageryStatus: (status: "idle" | "loading" | "ready" | "unavailable") => void;
};

const TILES_BASE = (process.env.NEXT_PUBLIC_TILES_URL ?? "").trim().replace(/\/+$/, "");
const REGION_ARCHIVE = "caribbean-z11.pmtiles";
const ISLAND_ARCHIVE = "jamaica-z15.pmtiles";
const BASEMAP_SWITCH = 10.5;
const WORKER = "/maplibre/maplibre-gl-worker.mjs";
const COVERED: [[number, number], [number, number]] = [[-92, 7], [-57, 28]];
const WEATHER_SOURCE = "sim-synthetic-weather-image";
const WEATHER_LAYER = "sim-synthetic-weather";
const FLOW_SOURCE = "sim-modelled-wind-flow";
const FLOW_FRAME_MS = 120;
const WEATHER_FRAME_MS = 600;
const WEATHER_PHASE_RADIANS_PER_SECOND = 0.065;
const FLOW_BOUND_SECTORS = 72;
const FLOW_MARKS = 280;
let cachedFlowBoundsKey = "";
let cachedFlowBounds: number[] = [];

export default function SimulatorMap({
  scenario,
  frame,
  parishes,
  communities,
  drawing,
  reducedMotion,
  showWindThresholds,
  showImpactOverlay,
  imageryTemplate,
  onTrackChange,
  onFailure,
  onParticleStatus,
  onWeatherStatus,
  onImageryStatus,
}: SimulatorMapProps) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<MapLibreMap | null>(null);
  const weatherPalette = useRef<SyntheticWeatherPalette | null>(null);
  const weatherPhase = useRef(0);
  const ready = useRef(false);
  const observedImageryReady = useRef(false);
  const dragged = useRef<number | null>(null);
  const moved = useRef(false);
  const latest = useRef({ scenario, frame, parishes, communities, drawing });
  latest.current = { scenario, frame, parishes, communities, drawing };
  const trackChange = useRef(onTrackChange);
  trackChange.current = onTrackChange;
  const callbacks = useRef({ onFailure, onParticleStatus, onWeatherStatus, onImageryStatus });
  callbacks.current = { onFailure, onParticleStatus, onWeatherStatus, onImageryStatus };

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
    weatherPalette.current = colours.weather;

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
            [FLOW_SOURCE]: {
              type: "geojson",
              data: windFlowData(latest.current.scenario, latest.current.frame, weatherPhase.current),
            },
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
              layout: { visibility: showImpactOverlay ? "visible" : "none" },
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
              layout: { visibility: showWindThresholds ? "visible" : "none" },
              paint: {
                "fill-color": [
                  "match", ["get", "thresholdKt"],
                  34, colours.hazard34,
                  50, colours.hazard50,
                  colours.hazard64,
                ],
                "fill-opacity": ["match", ["get", "thresholdKt"], 34, 0.025, 50, 0.04, 0.055],
              },
            },
            {
              id: "sim-wind-lines",
              type: "line",
              source: "sim-wind",
              layout: { visibility: showWindThresholds ? "visible" : "none" },
              paint: {
                "line-color": [
                  "match", ["get", "thresholdKt"],
                  34, colours.hazard34,
                  50, colours.hazard50,
                  colours.hazard64,
                ],
                "line-width": ["match", ["get", "thresholdKt"], 34, 0.8, 50, 1, 1.3],
                "line-opacity": 0.58,
              },
            },
            {
              id: "sim-flow-lines",
              type: "line",
              source: FLOW_SOURCE,
              layout: { "line-cap": "round", "line-join": "round" },
              paint: {
                "line-color": colours.hazard64,
                "line-width": [
                  "interpolate", ["linear"], ["get", "speedKt"],
                  8, 0.8,
                  64, 1.25,
                  130, 1.75,
                ],
                "line-opacity": [
                  "interpolate", ["linear"], ["get", "speedKt"],
                  8, 0.42,
                  64, 0.7,
                  130, 0.86,
                ],
                "line-blur": 0.15,
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
      try {
        upsertSyntheticWeather(
          instance,
          latest.current.scenario,
          initial,
          colours.weather,
          weatherPhase.current,
        );
        callbacks.current.onWeatherStatus("ready");
      } catch (error) {
        callbacks.current.onWeatherStatus(
          "unavailable",
          error instanceof Error ? error.message : "modelled precipitation layer unavailable",
        );
      }
      callbacks.current.onParticleStatus(initial ? "ready" : "unavailable", initial ? undefined : "wind frame unavailable");
    });
    map.current = instance;
    return () => {
      ready.current = false;
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
    setGeoJson(instance, FLOW_SOURCE, windFlowData(scenario, frame, weatherPhase.current));
    setGeoJson(instance, "sim-centre", centreData(frame));
    for (const layer of ["sim-wind-fill", "sim-wind-lines"]) {
      if (instance.getLayer(layer)) {
        instance.setLayoutProperty(layer, "visibility", showWindThresholds ? "visible" : "none");
      }
    }
    if (instance.getLayer("sim-impact")) {
      instance.setLayoutProperty("sim-impact", "visibility", showImpactOverlay ? "visible" : "none");
    }
    instance.getCanvas().style.cursor = drawing ? "crosshair" : "";
    try {
      upsertSyntheticWeather(
        instance,
        scenario,
        frame,
        weatherPalette.current ?? readColours(instance.getContainer()).weather,
        weatherPhase.current,
      );
      setSyntheticWeatherVisible(instance, !observedImageryReady.current);
      callbacks.current.onWeatherStatus("ready");
    } catch (error) {
      callbacks.current.onWeatherStatus(
        "unavailable",
        error instanceof Error ? error.message : "modelled precipitation layer unavailable",
      );
    }
    callbacks.current.onParticleStatus(frame ? "ready" : "unavailable", frame ? undefined : "wind frame unavailable");
  }, [scenario, frame, parishes, communities, drawing, reducedMotion, showWindThresholds, showImpactOverlay]);

  useEffect(() => {
    if (reducedMotion) return;
    let handle = 0;
    let previous = performance.now();
    let lastFlowFrame = previous - FLOW_FRAME_MS;
    let lastWeatherFrame = previous - WEATHER_FRAME_MS;
    const animate = (now: number) => {
      const elapsedSeconds = Math.min(0.25, Math.max(0, now - previous) / 1000);
      previous = now;
      weatherPhase.current += elapsedSeconds * WEATHER_PHASE_RADIANS_PER_SECOND;
      const instance = map.current;
      const current = latest.current;
      if (
        instance
        && ready.current
        && current.frame
        && document.visibilityState === "visible"
        && now - lastFlowFrame >= FLOW_FRAME_MS
      ) {
        setGeoJson(
          instance,
          FLOW_SOURCE,
          windFlowData(current.scenario, current.frame, weatherPhase.current),
        );
        lastFlowFrame = now;
      }
      if (
        instance
        && ready.current
        && current.frame
        && !observedImageryReady.current
        && document.visibilityState === "visible"
        && now - lastWeatherFrame >= WEATHER_FRAME_MS
      ) {
        try {
          upsertSyntheticWeather(
            instance,
            current.scenario,
            current.frame,
            weatherPalette.current ?? readColours(instance.getContainer()).weather,
            weatherPhase.current,
          );
        } catch (error) {
          callbacks.current.onWeatherStatus(
            "unavailable",
            error instanceof Error ? error.message : "modelled precipitation animation unavailable",
          );
        }
        lastWeatherFrame = now;
      }
      handle = requestAnimationFrame(animate);
    };
    handle = requestAnimationFrame(animate);
    return () => cancelAnimationFrame(handle);
  }, [reducedMotion]);

  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready.current) return;
    const layer = "sim-goes-imagery";
    const source = "sim-goes-imagery";
    if (!imageryTemplate) {
      observedImageryReady.current = false;
      if (instance.getLayer(layer)) instance.removeLayer(layer);
      if (instance.getSource(source)) instance.removeSource(source);
      onImageryStatus("idle");
      setSyntheticWeatherVisible(instance, true);
      return;
    }
    onImageryStatus("loading");
    observedImageryReady.current = false;
    setSyntheticWeatherVisible(instance, true);
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
      if (event.sourceId === source && event.isSourceLoaded) {
        observedImageryReady.current = true;
        onImageryStatus("ready");
        setSyntheticWeatherVisible(instance, false);
      }
    };
    const onError = (event: ErrorEvent) => {
      const sourceId = "sourceId" in event ? String(event.sourceId) : "";
      const message = String(event.error?.message ?? event.error ?? "");
      if (sourceId === source || message.includes(imageryTemplate)) {
        observedImageryReady.current = false;
        onImageryStatus("unavailable");
        setSyntheticWeatherVisible(instance, true);
      }
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
      aria-label="Editable storm simulation map with modelled precipitation and wind, not observed. Add or drag track points when drawing is enabled."
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

function windFlowData(
  scenario: AuthoredScenario,
  frame: SimulationFrame | null,
  phaseRad: number,
): GeoJSON.FeatureCollection<GeoJSON.LineString> {
  if (!frame) return { type: "FeatureCollection", features: [] };
  const bounds = flowBoundsFor(scenario, frame);
  const features: Array<GeoJSON.Feature<GeoJSON.LineString>> = [];
  for (let index = 0; index < FLOW_MARKS; index += 1) {
    const baseBearing = hashUnit(index, 17, 0x51ed270b) * Math.PI * 2;
    const radialShare = 0.06 + Math.pow(hashUnit(index, 31, 0x9e3779b9), 0.66) * 0.91;
    const bearingRad = normalRadians(
      baseBearing - phaseRad * (0.72 + (1 - radialShare) * 1.95),
    );
    const bearing = bearingRad * 180 / Math.PI;
    const irregular = 1
      + 0.09 * Math.sin(bearingRad * 3 + frame.headingDeg * Math.PI / 360)
      + 0.05 * Math.sin(bearingRad * 7 - 0.8)
      + 0.025 * Math.sin(bearingRad * 13 + 1.4);
    const outerGap = hashUnit(index, 53, 0x85ebca6b);
    const sectorDensity = clamp(
      0.72 + 0.14 * Math.sin(bearingRad * 3.4 + 0.6) + 0.08 * Math.sin(bearingRad * 8.1),
      0.44,
      0.92,
    );
    if (radialShare > 0.58 && outerGap > sectorDensity) continue;
    const radiusNm = radialShare * flowBoundAt(bounds, bearing) * irregular;
    const point = destination(frame.centre, bearing, radiusNm);
    const vector = windVectorAt(point, frame.centre, frame.headingDeg, scenario);
    if (vector.speedKt < 8) continue;
    const direction = vector.speedKt > 0
      ? normalDegrees(Math.atan2(vector.eastKt, vector.northKt) * 180 / Math.PI)
      : bearingDeg(frame.centre, point);
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

function flowBoundsFor(scenario: AuthoredScenario, frame: SimulationFrame) {
  const key = JSON.stringify([
    scenario.maxWindKt,
    scenario.radius34Nm,
    scenario.forwardSpeedKt,
    Number(frame.headingDeg.toFixed(3)),
  ]);
  if (key === cachedFlowBoundsKey && cachedFlowBounds.length === FLOW_BOUND_SECTORS) {
    return cachedFlowBounds;
  }
  const lower = Math.max(65, scenario.radius34Nm * 0.68);
  const upper = Math.max(110, scenario.radius34Nm * 1.8);
  cachedFlowBounds = Array.from({ length: FLOW_BOUND_SECTORS }, (_, index) => {
    const bearing = index / FLOW_BOUND_SECTORS * 360;
    return clamp(
      radiusAtThreshold(18, bearing, frame.headingDeg, scenario),
      lower,
      upper,
    );
  });
  cachedFlowBoundsKey = key;
  return cachedFlowBounds;
}

function flowBoundAt(bounds: number[], bearing: number) {
  const position = normalDegrees(bearing) / 360 * FLOW_BOUND_SECTORS;
  const low = Math.floor(position) % FLOW_BOUND_SECTORS;
  const high = (low + 1) % FLOW_BOUND_SECTORS;
  const amount = position - Math.floor(position);
  return bounds[low] + (bounds[high] - bounds[low]) * amount;
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

function upsertSyntheticWeather(
  map: MapLibreMap,
  scenario: AuthoredScenario,
  frame: SimulationFrame | null,
  palette: SyntheticWeatherPalette,
  animationPhaseRad = 0,
) {
  const generated = createSyntheticWeatherImage(
    { scenario, frame, animationPhaseRad },
    palette,
  );
  if (!generated) {
    setSyntheticWeatherVisible(map, false);
    return;
  }
  const source = map.getSource(WEATHER_SOURCE) as ImageSource | undefined;
  if (source) {
    source.updateImage({ image: generated.image, coordinates: generated.coordinates });
    return;
  }
  map.addSource(WEATHER_SOURCE, {
    type: "image",
    url: generated.image.toDataURL("image/png"),
    coordinates: generated.coordinates,
  });
  map.addLayer({
    id: WEATHER_LAYER,
    type: "raster",
    source: WEATHER_SOURCE,
    paint: {
      "raster-opacity": 1,
      "raster-fade-duration": 0,
      "raster-resampling": "linear",
    },
  }, firstMapLabelLayer(map) ?? "sim-impact");
}

function setSyntheticWeatherVisible(map: MapLibreMap, visible: boolean) {
  if (!map.getLayer(WEATHER_LAYER)) return;
  map.setLayoutProperty(WEATHER_LAYER, "visibility", visible ? "visible" : "none");
}

function firstMapLabelLayer(map: MapLibreMap) {
  return map.getStyle().layers.find((layer) => layer.type === "symbol")?.id;
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
    weather: {
      rain: read("--lh-weather-rain", "#36b85a"),
      cold: read("--lh-weather-cold", "#f4dc35"),
      deep: read("--lh-weather-deep", "#f39a2e"),
      severe: read("--lh-weather-severe", "#ef3d32"),
      core: read("--lh-weather-core", "#d41486"),
      extreme: read("--lh-weather-extreme", "#ffe7f2"),
    },
  };
}

function normalise(value: string) {
  return value.trim().toLowerCase().replace(/^st\.?\s+/, "saint ");
}

function normalRadians(value: number) {
  const tau = Math.PI * 2;
  return ((value % tau) + tau) % tau;
}

function normalDegrees(value: number) {
  return ((value % 360) + 360) % 360;
}

function hashUnit(index: number, salt: number, seed: number) {
  let hash = Math.imul(index + 1, 0x27d4eb2d) ^ Math.imul(salt, 0x165667b1) ^ seed;
  hash = Math.imul(hash ^ (hash >>> 15), 0x85ebca6b);
  hash ^= hash >>> 13;
  return (hash >>> 0) / 0x100000000;
}

function clamp(value: number, low: number, high: number) {
  return Math.max(low, Math.min(high, value));
}

function roundCoordinate(value: number) {
  return Number(value.toFixed(4));
}
