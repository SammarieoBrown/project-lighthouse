"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/* The live half of the EOC console.
 *
 * The replay is a verified recording; this is the present tense, and the two
 * are never blended. The board carries exactly what /v1/hazard/live states:
 * the national posture from the forecast sentinel's live events, and NHC's
 * published storm positions. Wind fields, probabilities and modelled impact
 * are advisory products — they appear when an advisory is ingested, and this
 * module never fabricates them from a position fix.
 */

export type LiveStorm = {
  id: string | null;
  name: string | null;
  classification: string | null;
  intensity_kt: number | null;
  pressure_mb: number | null;
  lat: number;
  lon: number;
  movement_dir_deg: number | null;
  movement_speed_kt: number | null;
  last_update: string | null;
};

export type LiveBoard = {
  as_of: string;
  posture: {
    level: string;
    event: { name: string; since: string } | null;
    source: string;
  };
  basin: {
    status: "ok" | "stale" | "unreachable";
    storms: LiveStorm[] | null;
  };
};

export type LiveState =
  | { status: "loading" }
  | { status: "error"; reason: string }
  | { status: "ready"; board: LiveBoard; fetchedAt: string };

/* NHC advisory cadence is hours and the API caches for five minutes; polling
 * faster would only manufacture the impression of a livelier feed. */
const POLL_MS = 300_000;

export const LIVE_STORM_ID = "__live__";

/* Kingston, the reference point the distances are stated from. One point
 * rather than per-parish precision, because a storm hundreds of kilometres out
 * does not support a finer statement (rule C3). */
const KINGSTON: [number, number] = [-76.79, 18.0];

const EARTH_RADIUS_KM = 6371;

export function distanceKm(lon: number, lat: number): number {
  const rad = (v: number) => (v * Math.PI) / 180;
  const dLat = rad(lat - KINGSTON[1]);
  const dLon = rad(lon - KINGSTON[0]);
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(rad(KINGSTON[1])) * Math.cos(rad(lat)) * Math.sin(dLon / 2) ** 2;
  return Math.round(2 * EARTH_RADIUS_KM * Math.asin(Math.sqrt(a)));
}

const COMPASS = [
  "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
  "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
] as const;

export function bearingFromKingston(lon: number, lat: number): string {
  const rad = (v: number) => (v * Math.PI) / 180;
  const dLon = rad(lon - KINGSTON[0]);
  const y = Math.sin(dLon) * Math.cos(rad(lat));
  const x =
    Math.cos(rad(KINGSTON[1])) * Math.sin(rad(lat)) -
    Math.sin(rad(KINGSTON[1])) * Math.cos(rad(lat)) * Math.cos(dLon);
  const deg = ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360;
  return COMPASS[Math.round(deg / 22.5) % 16];
}

export function compassPoint(deg: number): string {
  return COMPASS[Math.round((((deg % 360) + 360) % 360) / 22.5) % 16];
}

/* NHC's classification codes, spelled out the way the products spell them.
 * An unknown code passes through raw rather than being guessed at. */
const CLASSIFICATIONS: Record<string, string> = {
  TD: "Tropical depression",
  TS: "Tropical storm",
  HU: "Hurricane",
  MH: "Major hurricane",
  STD: "Subtropical depression",
  STS: "Subtropical storm",
  PTC: "Post-tropical cyclone",
  PC: "Potential tropical cyclone",
};

export function classificationLabel(code: string | null): string {
  if (!code) return "Classification unavailable";
  return CLASSIFICATIONS[code.toUpperCase()] ?? code;
}

/* The storm the map centres on: the nearest to Jamaica, because the question
 * this console asks of the basin is always "which one is ours". */
export function nearestStorm(storms: LiveStorm[]): LiveStorm | null {
  if (storms.length === 0) return null;
  return storms.reduce((nearest, storm) =>
    distanceKm(storm.lon, storm.lat) < distanceKm(nearest.lon, nearest.lat)
      ? storm
      : nearest,
  );
}

export function useLive(active: boolean): LiveState {
  const [state, setState] = useState<LiveState>({ status: "loading" });
  const request = useRef(0);

  const load = useCallback(async () => {
    const id = ++request.current;
    try {
      const response = await fetch("/api/lighthouse/v1/hazard/live", {
        cache: "no-store",
      });
      if (!response.ok) {
        throw new Error(`The live board answered ${response.status}.`);
      }
      const board = (await response.json()) as LiveBoard;
      if (id !== request.current) return;
      setState({ status: "ready", board, fetchedAt: new Date().toISOString() });
    } catch (error) {
      if (id !== request.current) return;
      setState({
        status: "error",
        reason:
          error instanceof Error ? error.message : "The live board is unreachable.",
      });
    }
  }, []);

  useEffect(() => {
    if (!active) return;
    void load();
    const timer = window.setInterval(() => void load(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [active, load]);

  return state;
}
