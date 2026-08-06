"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";

import { LighthouseMark } from "../logo";
import { MapPanel } from "./map/MapPanel";
import {
  canonicalStormId,
  districtsAt,
  feedUpto,
  hhmm,
  nf,
  POSTURE_PLAIN,
  snapshotAt,
  strongestWarning,
  useLibrary,
  useReplay,
  worstHit,
  REPLAY_URL,
  type Replay,
  type StormEntry,
} from "./replay";
import type { District } from "./map";
import styles from "./eoc.module.css";

/* EOC console — Act 1.
 *
 * Every value on this screen comes from the selected frame of the replay,
 * and the replay is one generated file (docs/engineering/replay-export-contract.md).
 * Nothing here is a literal: not the posture, not the frame identifier, not the
 * count of frames, not the clock. If it reads like a measurement it was
 * measured, and if there is no measurement it reads as a dash.
 *
 * This is a historical replay, not an operational write surface. The proposed
 * cascade can be inspected, but approval is visibly unavailable until a real
 * ledger and delivery path are connected.
 *
 * Reviewed against docs/design/lighthouse-design-rules.md.
 */

/* Playback rate as storm time against real time, because that is the only thing
 * the number can honestly mean. Most frames are six hours apart, so 21,600×
 * is one frame per second and the whole storm runs in about forty seconds —
 * the pace this screen is actually watched at. The slow setting is for reading
 * the escalation, the fast one for getting to landfall. */
const RATES = [3600, 21600, 86400];
const DEFAULT_RATE = 21600;

/* A frame is never allowed to land faster than the eye resolves it. Without a
 * floor, a replay with two frames minutes apart would flicker through them
 * and the screen would report a state nobody saw. */
const MIN_FRAME_MS = 60;
const NO_FRAMES: Replay["frames"] = [];
const STORM_QUERY = "storm";
const STORM_STORAGE = "lighthouse.console.selected-storm.v1";

const POSTURE_RANK: Record<string, number> = { QUIET: 0, WATCH: 1, READY: 2, ACT: 3 };

function windSourceLabel(source: StormEntry["sizeSource"]): string {
  if (source === "measured") return "measured wind extent";
  if (source === "modelled") return "modelled wind extent";
  if (source === "mixed") return "mixed measured/modelled wind extent";
  return "wind-extent provenance unavailable";
}

function stormEntry(storms: StormEntry[], id: string | null): StormEntry | null {
  if (!id) return null;
  const wanted = canonicalStormId(id);
  return storms.find((storm) => canonicalStormId(storm.id) === wanted) ?? null;
}

function preferredStormId(storms: StormEntry[], defaultId: string | null): string | null {
  const params = new URLSearchParams(window.location.search);
  const fromUrl = stormEntry(storms, params.get(STORM_QUERY));
  if (fromUrl) return fromUrl.id;

  try {
    const fromStorage = stormEntry(storms, window.localStorage.getItem(STORM_STORAGE));
    if (fromStorage) return fromStorage.id;
  } catch {
    // Privacy modes can disable localStorage. The shareable URL and library
    // default still provide deterministic selection.
  }
  return stormEntry(storms, defaultId)?.id ?? storms[0]?.id ?? null;
}

/* Open on the first replay frame at the storm's highest posture — the moment the
 * console first demanded that somebody act. An operator arriving at this screen
 * is not asking where the storm started; they are asking what it did. */
function openingFrame(replay: Replay): number {
  let best = 0;
  let rank = -1;
  replay.frames.forEach((f, i) => {
    const r = POSTURE_RANK[f.posture] ?? 0;
    if (r > rank) {
      rank = r;
      best = i;
    }
  });
  return best;
}

type Connectivity = "checking" | "online" | "offline";

function useConnectivity(): Connectivity {
  const [connectivity, setConnectivity] = useState<Connectivity>("checking");

  useEffect(() => {
    const update = () => setConnectivity(navigator.onLine ? "online" : "offline");
    update();
    window.addEventListener("online", update);
    window.addEventListener("offline", update);
    return () => {
      window.removeEventListener("online", update);
      window.removeEventListener("offline", update);
    };
  }, []);

  return connectivity;
}

type SelectedDistrict = {
  districtKey: string;
  key: string;
  lon: number;
  lat: number;
  parish: string;
};

type LocatedDistrict = District & { lon: number; lat: number };

function isLocatedDistrict(district: District): district is LocatedDistrict {
  return Number.isFinite(district.lon) && Number.isFinite(district.lat);
}

export function EocConsole() {
  /* Which storm, then that storm. The library decides the URL, so switching
   * storms is a fetch rather than a page load — and until it has arrived the
   * reader is held at `loading` rather than pulling the legacy single file and
   * immediately throwing it away.
   *
   * A deployment with no index falls back to that legacy path, which is what
   * keeps an older build working rather than blank. */
  const libraryState = useLibrary();
  const library = libraryState.status === "ready" ? libraryState.library : null;
  const [stormRequest, setStormRequest] = useState<{ id: string; nonce: number } | null>(null);

  /* Resolve URL/local preference before reading a replay. `loading` is not the
   * same thing as "no index": collapsing those states was what started a
   * legacy replay download while index.json was still in flight. */
  useEffect(() => {
    if (!library) return;
    setStormRequest((current) => {
      if (current && stormEntry(library.storms, current.id)) return current;
      const id = preferredStormId(library.storms, library.default);
      return id ? { id, nonce: 0 } : null;
    });
  }, [library]);

  const requestedEntry = library
    ? stormEntry(library.storms, stormRequest?.id ?? null)
    : null;
  const replayUrl =
    libraryState.status === "absent"
      ? REPLAY_URL
      : requestedEntry
        ? `/replay/${requestedEntry.file}`
        : null;
  const state = useReplay(replayUrl, requestedEntry, stormRequest?.nonce ?? 0);
  const replay = state.replay;
  const activeEntry = replay && library
    ? stormEntry(library.storms, replay.event.id)
    : null;
  const evidenceEntry = activeEntry ?? (!replay ? requestedEntry : null);
  const connectivity = useConnectivity();

  const [index, setIndex] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [rate, setRate] = useState(DEFAULT_RATE);
  const [selectedDistrict, setSelectedDistrict] = useState<SelectedDistrict | null>(null);
  const opened = useRef<string | null>(null);
  const focusRequest = useRef(0);
  const mapSection = useRef<HTMLElement>(null);
  const timeline = useRef<HTMLDivElement>(null);

  const onStormChange = useCallback((id: string) => {
    setPlaying(false);
    setStormRequest((current) => ({ id, nonce: (current?.nonce ?? 0) + 1 }));
  }, []);

  /* Once per storm, keyed on which storm. Re-running on every render would
   * drag the scrubber back under the operator's hand; latching on a boolean
   * would strand it — frame 15 of Melissa does not exist in a storm with
   * twelve frames, and the screen would read from an index past the end. */
  useEffect(() => {
    if (!replay || opened.current === replay.event.id) return;
    opened.current = replay.event.id;
    setPlaying(false);
    setIndex(openingFrame(replay));
    setSelectedDistrict(null);
  }, [replay]);

  /* Selection becomes durable only after the requested artifact has passed
   * both the replay contract and its index identity check. That keeps the URL,
   * picker and evidence on one committed storm even when a switch fails. */
  useEffect(() => {
    if (state.status !== "ready" || !requestedEntry || !replay) return;
    if (canonicalStormId(replay.event.id) !== canonicalStormId(requestedEntry.id)) return;
    try {
      window.localStorage.setItem(STORM_STORAGE, requestedEntry.id);
    } catch {
      // The URL remains the durable/shareable selection when storage is denied.
    }
    const url = new URL(window.location.href);
    if (url.searchParams.get(STORM_QUERY) !== requestedEntry.id) {
      url.searchParams.set(STORM_QUERY, requestedEntry.id);
      window.history.replaceState(window.history.state, "", url);
    }
  }, [state.status, requestedEntry, replay]);

  const frames = replay?.frames ?? NO_FRAMES;
  const last = frames.length - 1;
  const displayIndex = replay
    ? opened.current === replay.event.id
      ? Math.min(index, last)
      : openingFrame(replay)
    : 0;
  const frame = frames[displayIndex] ?? null;

  /* The play loop. A timeout chain rather than an interval, because the gap
   * between advisories is a property of the data — NHC issues intermediate
   * advisories — and an interval would claim it is uniform. */
  useEffect(() => {
    if (!playing || !frame) return;
    if (displayIndex >= last) {
      setPlaying(false);
      return;
    }
    const stormMs = Date.parse(frames[displayIndex + 1].at) - Date.parse(frames[displayIndex].at);
    const delay = Math.max(MIN_FRAME_MS, stormMs / rate);
    const id = window.setTimeout(() => setIndex((i) => Math.min(i + 1, last)), delay);
    return () => window.clearTimeout(id);
  }, [playing, displayIndex, last, rate, frames, frame]);

  const onPlay = useCallback(() => {
    if (playing) {
      setPlaying(false);
      return;
    }
    // From the end, Play restarts. A transport control that does nothing when
    // pressed is indistinguishable from a broken one.
    if (displayIndex >= last) setIndex(0);
    setPlaying(true);
  }, [playing, displayIndex, last]);

  const onStep = useCallback(() => {
    setPlaying(false);
    setIndex((i) => Math.min(i + 1, last));
  }, [last]);

  const onRate = useCallback(() => {
    setRate((r) => RATES[(RATES.indexOf(r) + 1) % RATES.length]);
  }, []);

  const onTimelineKeyDown = useCallback((event: KeyboardEvent<HTMLDivElement>) => {
    const focused = event.target instanceof HTMLButtonElement
      ? Number(event.target.dataset.frameIndex)
      : Number.NaN;
    if (!Number.isInteger(focused) || frames.length === 0) return;

    let next = focused;
    if (event.key === "ArrowRight" || event.key === "ArrowDown") {
      next = (focused + 1) % frames.length;
    } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
      next = (focused - 1 + frames.length) % frames.length;
    } else if (event.key === "Home") {
      next = 0;
    } else if (event.key === "End") {
      next = frames.length - 1;
    } else {
      return;
    }

    event.preventDefault();
    setPlaying(false);
    setIndex(next);
    const target = timeline.current
      ?.querySelector<HTMLButtonElement>(`button[data-frame-index="${next}"]`);
    target?.focus({ preventScroll: true });
    target?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [frames.length]);

  const onDistrictFocus = useCallback((district: LocatedDistrict) => {
    focusRequest.current += 1;
    const districtKey = `${district.parish}/${district.district}`;
    setSelectedDistrict({
      districtKey,
      key: `${districtKey}/${focusRequest.current}`,
      lon: district.lon,
      lat: district.lat,
      parish: district.parish,
    });
    mapSection.current?.scrollIntoView({ block: "nearest" });
  }, []);

  /* Joined once per frame, and memoised so the map sees a new object only
   * when the selected evidence actually changed — the map updates its sources by
   * identity and a fresh object every render would push 2,000 features per
   * keystroke. */
  const districts = useMemo(
    () => (replay && frame ? districtsAt(replay, frame) : []),
    [replay, frame],
  );
  const snapshot = useMemo(
    () => (replay && frame ? snapshotAt(replay, frame, districts) : null),
    [replay, frame, districts],
  );
  const feed = useMemo(
    () => (replay ? feedUpto(replay, displayIndex) : []),
    [replay, displayIndex],
  );

  const replaySummary = useMemo(() => {
    if (!replay) return { homes: 0, structures: null as number | null };
    let structures = 0;
    let completeInventory = replay.districts.length > 0;
    for (const district of replay.districts) {
      if (district.structures === undefined) completeInventory = false;
      else structures += district.structures;
    }
    return {
      homes: replay.households.length,
      structures: completeInventory ? structures : null,
    };
  }, [replay]);
  const { homes, structures } = replaySummary;

  /* Mapped from public-source footprints, unlike everything derived from the registry above. `structures`
   * is the island's building footprints; `exposed` is how many of them sit
   * inside the selected wind field for this frame. Null rather than zero
   * when the inventory has not been built, because "not available" and "none"
   * are different statements and only one of them is ours to make. */
  /* The 64 kt band only. Broad 34 kt fields frequently saturate the island and
   * turn the exposure counter into a restatement of total inventory.
   * Hurricane-force exposure is the decision-relevant number here. */
  const exposed = useMemo(
    () =>
      structures === null
        ? null
        : frame?.district_exposed?.reduce((a, band) => a + band[0], 0) ?? null,
    [frame, structures],
  );

  const totals = frame?.totals ?? { destroyed: 0, major: 0, minor: 0, none: 0 };
  const atRisk = totals.destroyed + totals.major;
  const warning = frame ? strongestWarning(frame.watch_codes ?? []) : null;
  /* Contract rule 4: a location the storm has passed is absent, not zero. A
   * zero here would state that the chance is nil, which is a different and
   * false claim. */
  const montego = frame?.probabilities?.["MONTEGO BAY"]?.["48"];
  /* Five, because five is what the panel shows. It ranked every district in the
   * frame so the district review could list them all; that review is gone, and
   * ranking a hundred rows to render five is work done for nobody. */
  const worstDistricts = useMemo(() => worstHit(districts, 5), [districts]);
  const evidenceKind = evidenceEntry?.kind ?? "unknown";
  const isHindcast = evidenceKind === "hindcast";
  const isAdvisory = evidenceKind === "advisory";
  const windSource = evidenceEntry?.sizeSource ?? "unavailable";
  const frameNoun = isHindcast ? "historical fix" : isAdvisory ? "advisory" : "replay frame";
  const windEvidence = isHindcast
    ? windSource === "modelled"
      ? "modelled hindcast wind field"
      : windSource === "measured"
        ? "measured-radii hindcast wind field"
        : windSource === "mixed"
          ? "mixed-source hindcast wind field"
          : "hindcast wind field with unavailable extent provenance"
    : isAdvisory
      ? "forecast wind field"
      : "replay wind field with unavailable evidence provenance";

  const switching = Boolean(
    replay &&
      requestedEntry &&
      activeEntry &&
      canonicalStormId(requestedEntry.id) !== canonicalStormId(activeEntry.id) &&
      state.status === "loading",
  );
  const switchFailed = Boolean(replay && requestedEntry && state.status === "absent");
  const initialFailure =
    !replay &&
    (libraryState.status === "error" ||
      state.status === "absent" ||
      (libraryState.status === "ready" && libraryState.library.storms.length === 0));
  const failureReason =
    libraryState.status === "error"
      ? libraryState.reason
      : state.status === "absent"
        ? state.reason
        : libraryState.status === "ready" && libraryState.library.storms.length === 0
          ? "The storm library contains no published replays."
          : null;

  /* Sync state, rule C4, reporting by exception. That this is a recorded storm
   * is said once, by the provenance line beside the storm's name; repeating it
   * here as a permanent "historical replay" banner was the same sentence a
   * third time. What belongs in a masthead status slot is what is *wrong* — a
   * library still arriving, a storm that would not open, a replay that does not
   * exist. When the replay is simply playing, this is silent. */
  const replayAlert =
    libraryState.status === "loading"
      ? "Reading storm library"
      : switching
        ? `Opening ${requestedEntry?.name} · ${activeEntry?.name} remains visible`
        : switchFailed
          ? `Could not open ${requestedEntry?.name} · fetch or verification failed · ${activeEntry?.name ?? "current replay"} remains visible`
          : initialFailure
            ? "No verified replay available"
            : state.status === "loading"
              ? `Reading ${requestedEntry?.name ?? "legacy"} replay`
              : null;

  /* Where the replay is up to. Transport information, so it sits with the
   * transport rather than in the masthead. */
  const transportNote = !replay
    ? null
    : displayIndex < last
      ? `next ${frameNoun} ${hhmm(frames[displayIndex + 1].at)}Z`
      : `last ${frameNoun}`;

  /* Online is the assumption this screen already runs on, and a line that says
   * so on every render trains an operator to stop reading the line. It speaks
   * when the answer is not the assumed one. */
  const connectionStatus =
    connectivity === "checking"
      ? "Browser network · checking"
      : connectivity === "offline"
        ? replay
          ? "Browser network · offline · loaded replay remains visible"
          : "Browser network · offline · selected replay unavailable"
        : null;

  const missing = initialFailure;

  return (
    // The console is dark because an EOC is read in a dim room during a storm.
    // A product decision, so the surface states it rather than asking.
    <main className={styles.screen} data-theme="dark">
      <header className={styles.chrome}>
        <div className={styles.brand}>
          <LighthouseMark size={24} title="Lighthouse" />
          <span className={styles.brandText}>
            <span className={styles.brandName}>Lighthouse</span>
            <span className={styles.stormLine}>
            {library && library.storms.length > 1 ? (
              <label className={styles.stormPick}>
                <span className={styles.srOnly}>Storm</span>
                <select
                  className={styles.stormSelect}
                  value={activeEntry?.id ?? requestedEntry?.id ?? ""}
                  aria-label="Historical storm replay"
                  onChange={(event) => onStormChange(event.target.value)}
                >
                  {library.storms.map((storm) => (
                    <option key={storm.id} value={storm.id}>
                      {storm.name} — {storm.kind === "hindcast" ? "hindcast" : "advisory replay"}
                    </option>
                  ))}
                </select>
              </label>
            ) : (
              <span className={styles.brandMode}>Emergency operations replay</span>
            )}
            {evidenceEntry ? (
              /* What kind of claim this storm is, next to the storm's name.
                 An advisory replay is what forecasters published at the time;
                 a hindcast projects the track the storm actually took. */
              <span className={styles.provenance}>
                {evidenceEntry.kind === "hindcast" ? "Historical hindcast" : "Historical advisory replay"}
                {` · ${windSourceLabel(evidenceEntry.sizeSource)}`}
              </span>
            ) : libraryState.status === "absent" ? (
              <span className={styles.provenance}>
                Legacy replay · evidence provenance unavailable
              </span>
            ) : null}
            </span>
          </span>
        </div>

        <div className={styles.posture}>
          <span className={styles.postureLabel}>National posture</span>
          <span
            className={styles.postureValue}
            data-level={frame?.posture}
            data-empty={frame ? undefined : "true"}
          >
            {frame ? (POSTURE_PLAIN[frame.posture] ?? frame.posture) : "—"}
          </span>
        </div>

        <div className={styles.readings}>
          <div className={styles.reading}>
            <span
              className={styles.readingValue}
              data-empty={frame?.position.max_wind_kt === undefined ? "true" : undefined}
            >
              {frame?.position.max_wind_kt === undefined
                ? "—"
                : `${frame.position.max_wind_kt} kt`}
            </span>
            <span className={styles.readingLabel}>
              {isHindcast
                ? "Historical best-track sustained wind"
                : isAdvisory ? "Advisory sustained wind" : "Replay sustained wind · provenance unavailable"}
            </span>
          </div>
          <div className={styles.reading}>
            <span
              className={styles.readingValue}
              data-empty={frame?.position.pressure_mb === undefined ? "true" : undefined}
            >
              {frame?.position.pressure_mb === undefined
                ? "—"
                : `${frame.position.pressure_mb} mb`}
            </span>
            <span className={styles.readingLabel}>
              {isHindcast
                ? "Historical best-track central pressure"
                : isAdvisory ? "Advisory central pressure" : "Replay central pressure · provenance unavailable"}
            </span>
          </div>
          <div className={styles.reading}>
            <span className={styles.readingValue} data-empty={montego === undefined ? "true" : undefined}>
              {montego === undefined ? "—" : `${montego}%`}
            </span>
            <span className={styles.readingLabel}>
              {isHindcast
                ? "Contemporaneous 64 kt probability · unavailable"
                : isAdvisory
                  ? "Forecast 64 kt chance · Montego Bay at 48 h"
                  : "64 kt probability · provenance unavailable"}
            </span>
          </div>
          <div className={styles.reading}>
            <span className={styles.readingValue} data-empty={frame ? undefined : "true"}>
              {frame ? nf.format(atRisk) : "—"}
            </span>
            <span className={styles.readingLabel}>Modelled households · major or worse</span>
          </div>
          {/* Observed relief delivery had a reading here, permanently a dash,
              holding a fifth of this band to report that a delivery path does
              not exist yet. A dash is the honest rendering of a measurement
              that failed; it is not the honest rendering of a measurement that
              was never taken. It returns when something is delivering. */}
        </div>

        <div className={styles.stale}>
          {/* Navigation, not status. It sat in the status list reading as one
              more piece of metadata, separated from the sync text by the same
              hairline — so the only way between this console's three screens
              looked like something to skim past. */}
          <nav className={styles.nav} aria-label="Lighthouse screens">
            <Link className={styles.navLink} href="/simulator">
              Storm simulator
            </Link>
            <Link className={styles.navLink} href="/operations">
              Relief operations
            </Link>
          </nav>
          {/* Both stay mounted and both are usually empty. A live region has to
              exist before its content does, or the first thing it ever says is
              the one thing nobody hears. */}
          <span aria-live="polite">{replayAlert}</span>
          <span
            className={styles.connectivity}
            data-state={connectivity}
            aria-live="polite"
          >
            {connectionStatus}
          </span>
        </div>
      </header>

      <div className={styles.body}>
        {/* The screen's heading. It was a full-width caption bar above the map
            restating the storm, the evidence kind and the registry size — all
            three of which are already on screen, and the bar cost the map a row
            of its height to do it. The page still needs one heading, so the
            heading is what is left. */}
        <h1 className={styles.srOnly}>
          {replay
            ? `${replay.event.name} — ${isHindcast ? "historical hindcast" : isAdvisory ? "historical advisory replay" : "legacy replay"}`
            : "Emergency operations replay"}
        </h1>

        <section className={styles.mapPanel} ref={mapSection}>
          <div className={styles.mapCanvas}>
            {/* Mounted once the outcome of the fetch is known, so the map is
                built exactly once. Advisory changes update its data sources;
                rebuilding would refetch the basemap and drop the viewport. */}
            {!replay && state.status === "loading" ? null : (
              <MapPanel
                snapshot={snapshot}
                focus={selectedDistrict}
                evidenceKind={evidenceKind}
                sizeSource={windSource}
              />
            )}
          </div>

          {/* The legend describes the map, so it lists only what the map draws.
              The damage counts moved to the panel with the marks they belonged
              to: they are modelled outcomes for a synthetic registry, and a key
              beside a coastline reads as a key to the coastline.

              One line of counts, because they are one statement about the same
              inventory. Two lines reported the exposed count and the total count
              as if they were unrelated facts; `exposed` is a subset of
              `structures`, so saying so is both shorter and more informative
              than saying each separately. */}
          <div className={styles.legend} aria-label="Map counts and data provenance">
            <span className={styles.legendItem}>
              {structures === null || exposed === null ? (
                <>
                  {exposed === null
                    ? `${isHindcast ? "Hindcast" : isAdvisory ? "Forecast" : "Replay wind-field"} exposure inventory unavailable`
                    : `${nf.format(exposed)} mapped footprints in the selected 64 kt ${windEvidence}`}
                  {structures === null ? " · mapped building inventory unavailable" : ""}
                </>
              ) : (
                <>
                  <b>{nf.format(exposed)}</b> of {nf.format(structures)} mapped building
                  footprints lie in the selected 64 kt {windEvidence}
                </>
              )}
            </span>
            {replay ? (
              <span className={styles.legendItem}>
                <b>{nf.format(homes)}</b> synthetic households modelled across{" "}
                {replay.parishes.length} parishes
              </span>
            ) : (
              <span className={styles.legendItem}>
                {missing
                  ? "Verified replay unavailable · no substitute storm shown"
                  : "Reading and verifying replay"}
              </span>
            )}
            {/* Every word of this was on screen permanently, four sentences
                deep, under a map it was describing. It is the answer to a
                question a reader asks once — "is any of this real?" — so it is
                kept in full and asked for. The sentence that has to survive
                without opening it is the summary. */}
            <details className={styles.provenanceDetails}>
              <summary className={styles.provenanceSummary}>
                {isHindcast
                  ? "Historical hindcast, not a forecast or live product."
                  : isAdvisory
                    ? "Historical advisory replay, not live."
                    : "Legacy historical replay, not live."}
              </summary>
              <p className={styles.provenance}>
                {isHindcast ? (
                  <>
                    Track and intensity are historical best-track observations. Wind extent
                    is {windSource === "modelled"
                      ? "a modelled reconstruction"
                      : windSource === "measured"
                        ? "reconstructed from measured radii"
                        : windSource === "mixed"
                          ? "reconstructed from a mix of measured and modelled radii"
                          : "shown without source provenance and must not be treated as measured"}; no
                    contemporaneous probability forecast is implied. Building footprints are
                    mapped public-source inventory; household locations and damage counts are
                    synthetic model output.
                  </>
                ) : isAdvisory ? (
                  <>
                    Storm position and intensity are advisory observations. Wind areas and
                    probabilities are forecasts published or derived for that advisory. Building
                    footprints are mapped public-source inventory; household locations and damage
                    counts are synthetic model output.
                  </>
                ) : (
                  <>
                    The deployment did not publish a storm index, so Lighthouse cannot verify
                    whether this artifact is an advisory replay or a hindcast, or whether its wind
                    extent is measured or modelled. Building footprints are mapped public-source
                    inventory; household locations and damage counts are synthetic model output.
                  </>
                )}
              </p>
            </details>
          </div>
        </section>

        <aside className={styles.side}>
          {frame ? (
            <div className={styles.gate}>
              <span className={styles.gateRole}>Replay proposal</span>
              <h2 className={styles.gateAsk}>
                Alert cascade for {nf.format(atRisk)} modelled households
              </h2>
              <p className={styles.gateDetail}>
                {warning ? `${warning} in effect. ` : ""}Patois and English, WhatsApp
                with SMS fallback. Proposed from {frameNoun} {frame.n}.
              </p>
              {/* One statement that this cannot act, not three. It was in the
                  eyebrow, in a paragraph and on the button — and the button is
                  the only one of the three a reader is looking at when the
                  question occurs to them, so the button says it. The full
                  sentence stays for anyone reading by ear. */}
              <div className={styles.gateActions}>
                <button
                  type="button"
                  className={styles.gateButton}
                  disabled
                  aria-describedby="replay-action-limit"
                >
                  Approval unavailable in replay
                </button>
              </div>
              <span className={styles.srOnly} id="replay-action-limit">
                Historical replay only. Approval, messaging and ledger writes are not
                connected, so this screen cannot send an alert.
              </span>
              {/* The district review lived here: a disclosure listing the same
                  districts, ranked the same way, centring the map on the same
                  click as the table immediately below it. Two lists of one
                  thing. The table won because it is always open. */}
            </div>
          ) : null}

          <div className={styles.counts}>
            {/* "Select to centre map" is stated once, by the header, instead of
                once per row. Five rows each ending "· centre map" spent the
                same eleven characters five times to describe a property of the
                table, and pushed the parish — which is row-specific — out of
                its own line. */}
            <div className={`${styles.countRow} ${styles.head}`}>
              <span>Highest modelled impact · select to centre map</span>
              <span className={styles.countValue}>Destr.</span>
              <span className={styles.countValue}>Major</span>
            </div>
            {worstDistricts.map((d) => {
              const key = `${d.parish}/${d.district}`;
              const row = (
                <>
                  <span className={styles.countPlace}>
                    {d.district}
                    <span className={styles.countParish}>
                      {d.parish.replace("Saint ", "St ")}
                      {isLocatedDistrict(d) ? "" : " · map location unavailable"}
                    </span>
                  </span>
                  <span className={styles.countValue} data-band="destroyed">
                    {d.destroyed || "—"}
                  </span>
                  <span className={styles.countValue} data-band="major">
                    {d.major || "—"}
                  </span>
                </>
              );
              return isLocatedDistrict(d) ? (
                <button
                  type="button"
                  className={styles.countRow}
                  data-selected={selectedDistrict?.districtKey === key}
                  aria-current={selectedDistrict?.districtKey === key ? "location" : undefined}
                  key={key}
                  onClick={() => onDistrictFocus(d)}
                  aria-label={`Centre map on ${d.district}, ${d.parish}; ${d.destroyed} synthetic households modelled destroyed and ${d.major} modelled major damage`}
                >
                  {row}
                </button>
              ) : (
                <div className={styles.countRow} data-unlocated="true" key={key}>
                  {row}
                </div>
              );
            })}
          </div>

          <div className={styles.feed}>
            {/* "Modelled state changes" came off: every row below already
                reads "… synthetic households modelled as …", so the heading was
                describing the rows to someone who is looking at the rows. */}
            <div className={styles.panelHead}>
              <span>Replay activity</span>
            </div>
            {feed.map((row) => (
              <div className={styles.tline} key={`${row.at}/${row.who}/${row.what}`}>
                <span className={styles.tlineEvent}>
                  {row.what}
                  {/* Only when a person decided. Everything else is automatic,
                      and a column saying so on every row is noise that hides
                      the one line where a human was required. */}
                  {row.disposer ? (
                    <span className={styles.tlineDisposer}>{row.disposer}</span>
                  ) : null}
                </span>
                <span className={styles.tlineMeta}>
                  {row.at} · {row.who}
                </span>
              </div>
            ))}
          </div>
        </aside>
      </div>

      <footer className={styles.controller}>
        <div className={styles.transport}>
          <button
            type="button"
            className={styles.transportButton}
            aria-pressed={playing}
            disabled={!replay}
            onClick={onPlay}
          >
            {playing ? "Pause" : displayIndex >= last ? "Replay" : "Play"}
          </button>
          {/* Not a toggle, so no pressed state to report. It had one. */}
          <button
            type="button"
            className={styles.transportButton}
            disabled={!replay || displayIndex >= last}
            onClick={onStep}
          >
            Step
          </button>
          <button
            type="button"
            className={`${styles.transportButton} ${styles.rate}`}
            disabled={!replay}
            onClick={onRate}
            aria-label={`${rate}× playback rate, storm time against real time`}
          >
            {rate}×
          </button>
        </div>

        {/* The scrub bar is the story: synthetic households by modelled damage
            per advisory, left to right, so the escalation is the control. */}
        {replay ? (
          <div
            ref={timeline}
            className={styles.timeline}
            role="radiogroup"
            aria-label="Historical replay timeline with modelled household damage"
            onKeyDown={onTimelineKeyDown}
          >
            {frames.map((x, i) => {
              const t = x.totals;
              const total = t.destroyed + t.major + t.minor + t.none || 1;
              return (
                <button
                  type="button"
                  role="radio"
                  key={x.n}
                  className={styles.tick}
                  data-current={i === displayIndex}
                  data-frame-index={i}
                  aria-checked={i === displayIndex}
                  aria-label={`Frame ${i + 1} of ${frames.length}, ${frameNoun} ${x.n}: ${t.destroyed} synthetic households modelled destroyed, ${t.major} modelled major`}
                  tabIndex={i === displayIndex ? 0 : -1}
                  title={`${isHindcast ? "Historical fix" : isAdvisory ? "Advisory" : "Replay frame"} ${x.n}`}
                  onClick={() => {
                    setPlaying(false);
                    setIndex(i);
                  }}
                >
                  <span className={styles.tickNone} style={{ height: `${(t.none / total) * 100}%` }} />
                  <span className={styles.tickMinor} style={{ height: `${(t.minor / total) * 100}%` }} />
                  <span className={styles.tickMajor} style={{ height: `${(t.major / total) * 100}%` }} />
                  <span
                    className={styles.tickDestroyed}
                    style={{ height: `${(t.destroyed / total) * 100}%` }}
                  />
                </button>
              );
            })}
          </div>
        ) : (
          /* States what happened and what to check, rather than an empty bar
             that looks like a storm with no history. */
          <p className={styles.absent}>
            {missing
              ? `No verified replay to scrub. ${failureReason ?? "The selected replay is unavailable."} Reload while online or ask the deployment operator to publish the replay; Lighthouse did not substitute a different storm.`
              : "Reading and verifying the selected replay."}
          </p>
        )}

        {/* Every statement about where the replay is now lives here, beside the
            scrubber that moves it. The masthead carried a second copy of the
            frame identifier and its timestamp, and a third line saying which
            frame came next — three readings of one clock, two of them nowhere
            near the control that changes it. */}
        <div className={styles.clock}>
          {frame ? `${hhmm(frame.at)}Z` : "—"}
          <span className={styles.clockLabel}>
            {frame
              ? `Storm time · ${frameNoun} ${frame.n} · frame ${displayIndex + 1} of ${frames.length}`
              : "Storm time · no replay frame"}
          </span>
          {/* Its own line. Appended to the one above it, this was a fourth fact
              on a line that already carried three, and the whole thing is set
              `nowrap` so the time never breaks — which turned the extra words
              into 140px of footer that the scrub bar had to give up, and at a
              phone width pushed the entire controller wider than the screen. */}
          {transportNote ? (
            <span className={styles.clockNext}>{transportNote}</span>
          ) : null}
        </div>
      </footer>
    </main>
  );
}
