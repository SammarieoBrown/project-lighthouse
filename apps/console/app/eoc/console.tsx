"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";

import { LighthouseMark } from "../logo";
import { MapPanel } from "./map/MapPanel";
import {
  districtsAt,
  feedUpto,
  hhmm,
  nf,
  POSTURE_PLAIN,
  snapshotAt,
  stamp,
  strongestWarning,
  useLibrary,
  useReplay,
  worstHit,
  REPLAY_URL,
  type Replay,
} from "./replay";
import type { District } from "./map";
import styles from "./eoc.module.css";

/* EOC console — Act 1.
 *
 * Every value on this screen comes from the selected advisory of the replay,
 * and the replay is one generated file (docs/engineering/replay-export-contract.md).
 * Nothing here is a literal: not the posture, not the advisory number, not the
 * count of advisories, not the clock. If it reads like a measurement it was
 * measured, and if there is no measurement it reads as a dash.
 *
 * This is a historical replay, not an operational write surface. The proposed
 * cascade can be inspected, but approval is visibly unavailable until a real
 * ledger and delivery path are connected.
 *
 * Reviewed against docs/design/lighthouse-design-rules.md.
 */

/* Playback rate as storm time against real time, because that is the only thing
 * the number can honestly mean. Advisories are six hours apart, so 21,600×
 * is one advisory per second and the whole storm runs in about forty seconds —
 * the pace this screen is actually watched at. The slow setting is for reading
 * the escalation, the fast one for getting to landfall. */
const RATES = [3600, 21600, 86400];
const DEFAULT_RATE = 21600;

/* A frame is never allowed to land faster than the eye resolves it. Without a
 * floor, a replay with two advisories minutes apart would flicker through them
 * and the screen would report a state nobody saw. */
const MIN_FRAME_MS = 60;
const NO_FRAMES: Replay["frames"] = [];

const POSTURE_RANK: Record<string, number> = { QUIET: 0, WATCH: 1, READY: 2, ACT: 3 };

/* Open on the first advisory at the storm's highest posture — the moment the
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
  const library = useLibrary();
  const [stormId, setStormId] = useState<string | null>(null);
  const selected = library
    ? (library.storms.find((s) => s.id === (stormId ?? library.default)) ?? library.storms[0])
    : null;
  const replayUrl = library ? (selected ? `/replay/${selected.file}` : null) : REPLAY_URL;
  const state = useReplay(replayUrl);
  const replay = state.status === "ready" ? state.replay : null;
  const connectivity = useConnectivity();

  const [index, setIndex] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [rate, setRate] = useState(DEFAULT_RATE);
  const [reviewOpen, setReviewOpen] = useState(false);
  const [selectedDistrict, setSelectedDistrict] = useState<SelectedDistrict | null>(null);
  const opened = useRef<string | null>(null);
  const focusRequest = useRef(0);
  const mapSection = useRef<HTMLElement>(null);
  const timeline = useRef<HTMLDivElement>(null);

  /* Once per storm, keyed on which storm. Re-running on every render would
   * drag the scrubber back under the operator's hand; latching on a boolean
   * would strand it — advisory 15 of Melissa does not exist in a storm with
   * twelve frames, and the screen would read from an index past the end. */
  useEffect(() => {
    if (!replay || opened.current === replay.event.id) return;
    opened.current = replay.event.id;
    setPlaying(false);
    setIndex(openingFrame(replay));
  }, [replay]);

  const frames = replay?.frames ?? NO_FRAMES;
  const last = frames.length - 1;
  const frame = frames[index] ?? null;

  /* The play loop. A timeout chain rather than an interval, because the gap
   * between advisories is a property of the data — NHC issues intermediate
   * advisories — and an interval would claim it is uniform. */
  useEffect(() => {
    if (!playing || !frame) return;
    if (index >= last) {
      setPlaying(false);
      return;
    }
    const stormMs = Date.parse(frames[index + 1].at) - Date.parse(frames[index].at);
    const delay = Math.max(MIN_FRAME_MS, stormMs / rate);
    const id = window.setTimeout(() => setIndex((i) => Math.min(i + 1, last)), delay);
    return () => window.clearTimeout(id);
  }, [playing, index, last, rate, frames, frame]);

  const onPlay = useCallback(() => {
    if (playing) {
      setPlaying(false);
      return;
    }
    // From the end, Play restarts. A transport control that does nothing when
    // pressed is indistinguishable from a broken one.
    if (index >= last) setIndex(0);
    setPlaying(true);
  }, [playing, index, last]);

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

  /* Joined once per advisory, and memoised so the map sees a new object only
   * when the advisory actually changed — the map updates its sources by
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
  const feed = useMemo(() => (replay ? feedUpto(replay, index) : []), [replay, index]);

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
   * inside the forecast wind field for this advisory. Null rather than zero
   * when the inventory has not been built, because "not available" and "none"
   * are different statements and only one of them is ours to make. */
  /* The 64 kt band only. The 34 kt field is the union across every forecast
   * hour out to five days, and for a storm this size it swallows the whole
   * island — at advisory 15 it reported 1,842,165 of 1,842,165, which is true,
   * useless, and reads as a broken counter. Hurricane-force wind is the number
   * somebody acts on. */
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
  const rankedDistricts = useMemo(
    () => worstHit(districts, districts.length),
    [districts],
  );
  const worstDistricts = rankedDistricts.slice(0, 5);

  /* Sync state, rule C4. This is a recorded storm being replayed, so it says
   * so — a console that reads "live" over October 2025 advisories is asserting
   * something the data flatly contradicts. */
  const replayStatus =
    state.status === "loading"
      ? "Reading replay"
      : state.status === "absent"
        ? "No replay data"
        : index < last
          ? `Replay · next advisory ${hhmm(frames[index + 1].at)}Z`
          : "Replay · last advisory";
  const connectionStatus =
    connectivity === "checking"
      ? "Browser network · checking"
      : connectivity === "offline"
        ? replay
          ? "Browser network · offline · cached replay available"
          : "Browser network · offline · replay unavailable"
        : "Browser network · online · historical replay";

  const missing = state.status === "absent";

  return (
    // The console is dark because an EOC is read in a dim room during a storm.
    // A product decision, so the surface states it rather than asking.
    <main className={styles.screen} data-theme="dark">
      <header className={styles.chrome}>
        <div className={styles.brand}>
          <LighthouseMark size={24} title="Lighthouse" />
          <span className={styles.brandText}>
            <span className={styles.brandName}>Lighthouse</span>
            <span className={styles.brandMode}>Emergency operations replay</span>
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
            <span className={styles.readingLabel}>Observed sustained wind</span>
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
            <span className={styles.readingLabel}>Observed central pressure</span>
          </div>
          <div className={styles.reading}>
            <span className={styles.readingValue} data-empty={montego === undefined ? "true" : undefined}>
              {montego === undefined ? "—" : `${montego}%`}
            </span>
            <span className={styles.readingLabel}>Forecast 64 kt chance · Montego Bay at 48 h</span>
          </div>
          <div className={styles.reading}>
            <span className={styles.readingValue} data-empty={frame ? undefined : "true"}>
              {frame ? nf.format(atRisk) : "—"}
            </span>
            <span className={styles.readingLabel}>Modelled households · major or worse</span>
          </div>
          <div className={styles.reading}>
            {/* Not zero. Nothing has been delivered, and a zero would measure
                something that has not happened. */}
            <span className={styles.readingValue} data-empty="true">
              —
            </span>
            <span className={styles.readingLabel}>Observed relief delivery · unavailable</span>
          </div>
        </div>

        <div className={styles.stale} aria-live="polite">
          {library && library.storms.length > 1 ? (
            <label className={styles.stormPick}>
              <span className={styles.srOnly}>Storm</span>
              <select
                className={styles.stormSelect}
                value={selected?.id ?? ""}
                onChange={(event) => setStormId(event.target.value)}
              >
                {library.storms.map((storm) => (
                  <option key={storm.id} value={storm.id}>
                    {storm.name}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          {selected ? (
            /* What kind of claim this storm is, stated where the storm is
               named. An advisory replay is what forecasters published at the
               time; a hindcast projects the track the storm actually took. */
            <span className={styles.provenance}>
              {selected.kind === "hindcast" ? "Hindcast" : "Advisory replay"}
              {selected.sizeSource === "modelled" ? " · modelled wind extent" : ""}
            </span>
          ) : null}
          <span>
            {frame ? `Advisory ${frame.n} · ${stamp(frame.at)}Z` : "No advisory"}
          </span>
          <span>{replayStatus}</span>
          <span className={styles.connectivity} data-state={connectivity}>
            {connectionStatus}
          </span>
        </div>
      </header>

      <div className={styles.body}>
        <section className={styles.mapPanel} ref={mapSection}>
          <div className={styles.panelHead}>
            <h1 className={styles.panelTitle}>Selected forecast and mapped structure exposure</h1>
            <span>
              {replay ? (
                <>
                  <b>{nf.format(homes)}</b> synthetic households modelled across{" "}
                  {replay.parishes.length} parishes
                </>
              ) : missing ? (
                "Replay unavailable · check /replay/replay.json"
              ) : (
                "Reading replay"
              )}
            </span>
          </div>

          <div className={styles.mapCanvas}>
            {/* Mounted once the outcome of the fetch is known, so the map is
                built exactly once. Advisory changes update its data sources;
                rebuilding would refetch the basemap and drop the viewport. */}
            {state.status === "loading" ? null : (
              <MapPanel
                snapshot={snapshot}
                focus={selectedDistrict}
              />
            )}
          </div>

          {/* The legend describes the map, so it lists only what the map draws.
              The damage counts moved to the panel with the marks they belonged
              to: they are modelled outcomes for a synthetic registry, and a key
              beside a coastline reads as a key to the coastline. */}
          <div className={styles.legend} aria-label="Map counts and data provenance">
            <span className={styles.legendItem}>
              {exposed === null ? (
                "Forecast exposure inventory unavailable"
              ) : (
                <><b>{nf.format(exposed)}</b> mapped footprints in the selected 64 kt forecast</>
              )}
            </span>
            <span className={styles.legendItem}>
              {structures === null
                ? "Mapped building inventory unavailable"
                : `${nf.format(structures)} mapped building footprints from public datasets`}
            </span>
            <p className={styles.provenance}>
              <b>Historical replay, not live.</b> Storm position and intensity are advisory
              observations. Wind areas and probabilities are forecasts. Building footprints
              are mapped public-source inventory; household locations and damage counts are synthetic model output.
            </p>
          </div>
        </section>

        <aside className={styles.side}>
          {frame ? (
            <div className={styles.gate}>
              <span className={styles.gateRole}>Replay proposal · action unavailable</span>
              <h2 className={styles.gateAsk}>
                Alert cascade for {nf.format(atRisk)} modelled households
              </h2>
              <p className={styles.gateDetail}>
                {warning ? `${warning} in effect. ` : ""}Patois and English, WhatsApp
                with SMS fallback. Proposed from advisory {frame.n}.
              </p>
              <p className={styles.replayLimit} id="replay-action-limit">
                Historical replay only. Approval, messaging and ledger writes are not
                connected, so this screen cannot send an alert.
              </p>
              <div className={styles.gateActions}>
                <button
                  type="button"
                  className={styles.gateButton}
                  disabled
                  aria-describedby="replay-action-limit"
                >
                  Approval unavailable
                </button>
                <button
                  type="button"
                  className={`${styles.gateButton} ${styles.secondary}`}
                  aria-expanded={reviewOpen}
                  aria-controls="affected-districts"
                  onClick={() => setReviewOpen((open) => !open)}
                >
                  {reviewOpen ? "Close review" : "Review districts"}
                </button>
              </div>
              {reviewOpen ? (
                <div className={styles.reviewList} id="affected-districts">
                  <div className={styles.reviewHead}>
                    <span>Affected districts</span>
                    <span>Modelled major+</span>
                  </div>
                  {rankedDistricts.length > 0 ? (
                    rankedDistricts.map((district) => {
                      const key = `${district.parish}/${district.district}`;
                      const row = (
                        <>
                          <span>{district.district}</span>
                          <span className={styles.reviewParish}>
                            {district.parish.replace("Saint ", "St ")}
                            {isLocatedDistrict(district) ? " · centre map" : " · map location unavailable"}
                          </span>
                          <span className={styles.countValue}>
                            {nf.format(district.destroyed + district.major)}
                          </span>
                        </>
                      );
                      return isLocatedDistrict(district) ? (
                        <button
                          type="button"
                          className={styles.reviewRow}
                          data-selected={selectedDistrict?.districtKey === key}
                          aria-current={selectedDistrict?.districtKey === key ? "location" : undefined}
                          key={key}
                          onClick={() => onDistrictFocus(district)}
                          aria-label={`Centre map on ${district.district}, ${district.parish}; ${district.destroyed + district.major} synthetic households modelled major or worse`}
                        >
                          {row}
                        </button>
                      ) : (
                        <div className={styles.reviewRow} data-unlocated="true" key={key}>
                          {row}
                        </div>
                      );
                    })
                  ) : (
                    <p className={styles.reviewEmpty}>No modelled major damage in this frame.</p>
                  )}
                </div>
              ) : null}
            </div>
          ) : null}

          <div className={styles.counts}>
            <div className={`${styles.countRow} ${styles.head}`}>
              <span>Highest modelled impact · synthetic</span>
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
                      {isLocatedDistrict(d) ? " · centre map" : " · map location unavailable"}
                    </span>
                  </span>
                  <span className={styles.countValue} style={{ color: "var(--lh-critical)" }}>
                    {d.destroyed || "—"}
                  </span>
                  <span className={styles.countValue} style={{ color: "var(--lh-elevated)" }}>
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
            <div className={styles.panelHead}>
              <span>Replay activity · modelled state changes</span>
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
            {playing ? "Pause" : index >= last ? "Replay" : "Play"}
          </button>
          {/* Not a toggle, so no pressed state to report. It had one. */}
          <button
            type="button"
            className={styles.transportButton}
            disabled={!replay || index >= last}
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
                  data-current={i === index}
                  data-frame-index={i}
                  aria-checked={i === index}
                  aria-label={`Frame ${i + 1} of ${frames.length}, advisory ${x.n}: ${t.destroyed} synthetic households modelled destroyed, ${t.major} modelled major`}
                  tabIndex={i === index ? 0 : -1}
                  title={`Advisory ${x.n}`}
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
              ? `No replay to scrub. ${state.reason} Check public/replay/replay.json.`
              : "Reading the replay."}
          </p>
        )}

        <div className={styles.clock}>
          {frame ? `${hhmm(frame.at)}Z` : "—"}
          <span className={styles.clockLabel}>
            {frame
              ? `Storm time · advisory ${frame.n} · frame ${index + 1} of ${frames.length}`
              : "Storm time · no advisory"}
          </span>
        </div>
      </footer>
    </main>
  );
}
