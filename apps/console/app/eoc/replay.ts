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

type Geometry = { type: string; coordinates: unknown };
type LineGeometry = { type: string; coordinates: number[][] };

export type ReplayFrame = {
  /** Advisory number as printed by NHC — a string because NHC prints "11A". */
  n: string;
  at: string;
  posture: Posture;
  watch_codes: string[];
  position: {
    lon: number;
    lat: number;
    max_wind_kt: number;
    gust_kt: number;
    pressure_mb: number;
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
  /** Parallel to `districts`: measured structures at [64, 50, 34] kt, mutually
   *  exclusive. Absent when the building inventory has not been built — which
   *  is why it is optional rather than zeroed, since zero would assert that
   *  nothing is exposed rather than that nothing was measured. */
  district_exposed?: number[][];
};

export type Replay = {
  generated_at: string;
  event: { id: string; name: string; advisory_count: number };
  parishes: { name: string; registry: boolean; geometry: Geometry }[];
  districts: {
    id: number; parish: string; district: string; n: number; lon: number; lat: number;
    /** Measured building footprints in this district. Absent without inventory. */
    structures?: number;
  }[];
  households: { id: number; lon: number; lat: number; parish: string; community: string; roof: string }[];
  frames: ReplayFrame[];
};

/* One character per household rather than 2,000 objects — the difference
 * between 82 KB and 3 MB across 41 frames. */
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

export type ReplayState =
  | { status: "loading" }
  | { status: "ready"; replay: Replay }
  | { status: "absent"; reason: string };

/* Contract rule 1: ordering is the join. The exporter asserts these lengths
 * before writing and the reader asserts them after, because a silent off-by-one
 * here mislabels real homes — it has to fail loudly on both sides or it is not
 * checked at all. */
function validate(raw: unknown): Replay {
  const r = raw as Replay;
  if (!r || typeof r !== "object") throw new Error("replay.json is not an object");
  if (!Array.isArray(r.frames) || r.frames.length === 0) throw new Error("replay.json has no frames");
  if (!Array.isArray(r.districts) || !Array.isArray(r.households)) {
    throw new Error("replay.json is missing districts or households");
  }
  for (const f of r.frames) {
    if (f.district_counts?.length !== r.districts.length) {
      throw new Error(`advisory ${f.n}: ${f.district_counts?.length} district counts for ${r.districts.length} districts`);
    }
    if (f.household_bands?.length !== r.households.length) {
      throw new Error(`advisory ${f.n}: ${f.household_bands?.length} household bands for ${r.households.length} households`);
    }
  }
  return r;
}

/* Fetched in an effect rather than imported, so the file can be absent at build
 * time — which it is on a fresh clone and in CI, exactly like the basemap
 * archives. The server renders the loading state and the first client render
 * matches it; the data lands after hydration, so there is nothing to mismatch. */
export function useReplay(): ReplayState {
  const [state, setState] = useState<ReplayState>({ status: "loading" });

  useEffect(() => {
    let live = true;
    fetch(REPLAY_URL, { cache: "no-store" })
      .then(async (res) => {
        if (!res.ok) throw new Error(`${REPLAY_URL} returned ${res.status}`);
        return validate(await res.json());
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
  }, []);

  return state;
}

/* ---------------------------------------------------------------------------
 * Per-frame derivations
 *
 * The static half of the file — parishes, district positions, household
 * positions — is 288 KB that never moves. The frame carries the 15.8 KB that
 * does. Joining them is this file's job and it happens once per frame.
 * ------------------------------------------------------------------------ */

export function districtsAt(replay: Replay, frame: ReplayFrame): District[] {
  return replay.districts.map((d, i) => {
    const [destroyed, major, minor, none] = frame.district_counts[i];
    return { parish: d.parish, district: d.district, n: d.n, lon: d.lon, lat: d.lat, destroyed, major, minor, none };
  });
}

export function householdsAt(replay: Replay, frame: ReplayFrame): Household[] {
  return replay.households.map((h, i) => ({
    lon: h.lon,
    lat: h.lat,
    band: BANDS[frame.household_bands[i]] ?? "NONE",
    parish: h.parish,
    community: h.community,
    roof: h.roof,
  }));
}

/** The shape the map already speaks. One frame of it. */
export function snapshotAt(replay: Replay, frame: ReplayFrame): Snapshot {
  return {
    parishes: replay.parishes,
    wind34: frame.wind34 ?? null,
    wind50: frame.wind50 ?? null,
    wind64: frame.wind64 ?? null,
    cone: frame.cone ?? null,
    track: frame.track ?? null,
    centre: [frame.position.lon, frame.position.lat],
    districts: districtsAt(replay, frame),
    households: householdsAt(replay, frame),
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

  for (const f of replay.frames.slice(0, index + 1)) {
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
        what: `${nf.format(f.totals.destroyed)} homes now expected to be destroyed`,
        disposer: null,
      });
    }
    destroyed = f.totals.destroyed;
  }
  return rows.reverse();
}

const RANK: Record<string, number> = { QUIET: 0, WATCH: 1, READY: 2, ACT: 3 };
