"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

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
  useReplay,
  worstHit,
  type Replay,
} from "./replay";
import styles from "./eoc.module.css";

/* EOC console — Act 1.
 *
 * Every value on this screen comes from the selected advisory of the replay,
 * and the replay is one generated file (docs/engineering/replay-export-contract.md).
 * Nothing here is a literal: not the posture, not the advisory number, not the
 * count of advisories, not the clock. If it reads like a measurement it was
 * measured, and if there is no measurement it reads as a dash.
 *
 * The approval gate is still inert — approving a cascade writes to a ledger
 * that does not exist yet. Its text follows the advisory; its buttons do
 * nothing, and that is stated here rather than discovered by a judge.
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

export function EocConsole() {
  const state = useReplay();
  const replay = state.status === "ready" ? state.replay : null;

  const [index, setIndex] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [rate, setRate] = useState(DEFAULT_RATE);
  const opened = useRef(false);

  // Once, when the file lands. Re-running this on every render would drag the
  // scrubber back under the operator's hand.
  useEffect(() => {
    if (!replay || opened.current) return;
    opened.current = true;
    setIndex(openingFrame(replay));
  }, [replay]);

  const frames = replay?.frames ?? [];
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

  /* Joined once per advisory, and memoised so the map sees a new object only
   * when the advisory actually changed — the map updates its sources by
   * identity and a fresh object every render would push 2,000 features per
   * keystroke. */
  const districts = useMemo(
    () => (replay && frame ? districtsAt(replay, frame) : []),
    [replay, frame],
  );
  const snapshot = useMemo(
    () => (replay && frame ? snapshotAt(replay, frame) : null),
    [replay, frame],
  );
  const feed = useMemo(() => (replay ? feedUpto(replay, index) : []), [replay, index]);

  const homes = useMemo(
    () => (replay ? replay.districts.reduce((a, d) => a + d.n, 0) : 0),
    [replay],
  );
  const maxDistrict = useMemo(
    () => (replay ? Math.max(...replay.districts.map((d) => d.n), 1) : 1),
    [replay],
  );

  /* Measured, unlike everything derived from the registry above. `structures`
   * is the island's building footprints; `exposed` is how many of them sit
   * inside the forecast wind field for this advisory. Null rather than zero
   * when the inventory has not been built, because "not measured" and "none"
   * are different statements and only one of them is ours to make. */
  const structures = useMemo(
    () => (replay ? replay.districts.reduce((a, d) => a + (d.structures ?? 0), 0) : 0),
    [replay],
  );
  /* The 64 kt band only. The 34 kt field is the union across every forecast
   * hour out to five days, and for a storm this size it swallows the whole
   * island — at advisory 15 it reported 1,842,165 of 1,842,165, which is true,
   * useless, and reads as a broken counter. Hurricane-force wind is the number
   * somebody acts on. */
  const exposed = useMemo(
    () => frame?.district_exposed?.reduce((a, band) => a + band[0], 0) ?? null,
    [frame],
  );

  const totals = frame?.totals ?? { destroyed: 0, major: 0, minor: 0, none: 0 };
  const atRisk = totals.destroyed + totals.major;
  const warning = frame ? strongestWarning(frame.watch_codes ?? []) : null;
  /* Contract rule 4: a location the storm has passed is absent, not zero. A
   * zero here would state that the chance is nil, which is a different and
   * false claim. */
  const montego = frame?.probabilities?.["MONTEGO BAY"]?.["48"];

  /* Sync state, rule C4. This is a recorded storm being replayed, so it says
   * so — a console that reads "live" over October 2025 advisories is asserting
   * something the data flatly contradicts. */
  const sync =
    state.status === "loading"
      ? "Reading replay"
      : state.status === "absent"
        ? "No replay data"
        : index < last
          ? `Replay · next advisory ${hhmm(frames[index + 1].at)}Z`
          : "Replay · last advisory";

  const missing = state.status === "absent";

  return (
    // The console is dark because an EOC is read in a dim room during a storm.
    // A product decision, so the surface states it rather than asking.
    <main className={styles.screen} data-theme="dark">
      <header className={styles.chrome}>
        <div className={styles.brand}>
          <LighthouseMark size={24} title="Lighthouse" />
          <span className={styles.brandName}>Lighthouse</span>
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
            <span className={styles.readingValue} data-empty={frame ? undefined : "true"}>
              {frame ? `${frame.position.max_wind_kt} kt` : "—"}
            </span>
            <span className={styles.readingLabel}>Sustained wind</span>
          </div>
          <div className={styles.reading}>
            <span className={styles.readingValue} data-empty={frame ? undefined : "true"}>
              {frame ? `${frame.position.pressure_mb} mb` : "—"}
            </span>
            <span className={styles.readingLabel}>Central pressure</span>
          </div>
          <div className={styles.reading}>
            <span className={styles.readingValue} data-empty={montego === undefined ? "true" : undefined}>
              {montego === undefined ? "—" : `${montego}%`}
            </span>
            <span className={styles.readingLabel}>Hurricane wind at Montego Bay</span>
          </div>
          <div className={styles.reading}>
            <span className={styles.readingValue} data-empty={frame ? undefined : "true"}>
              {frame ? nf.format(atRisk) : "—"}
            </span>
            <span className={styles.readingLabel}>Homes at major risk or worse</span>
          </div>
          <div className={styles.reading}>
            {/* Not zero. Nothing has been delivered, and a zero would measure
                something that has not happened. */}
            <span className={styles.readingValue} data-empty="true">
              —
            </span>
            <span className={styles.readingLabel}>Time to relief · none yet</span>
          </div>
        </div>

        <div className={styles.stale}>
          <span>
            {frame ? `Advisory ${frame.n} · ${stamp(frame.at)}Z` : "No advisory"}
          </span>
          <span>{sync}</span>
        </div>
      </header>

      <div className={styles.body}>
        <section className={styles.mapPanel}>
          <div className={styles.panelHead}>
            <span>Forecast wind and expected damage</span>
            <span>
              {replay ? (
                <>
                  <b>{nf.format(homes)}</b> homes registered across all{" "}
                  {replay.parishes.length} parishes
                </>
              ) : missing ? (
                "No replay data · /replay/replay.json"
              ) : (
                "Reading replay"
              )}
            </span>
          </div>

          <div className={styles.mapCanvas}>
            {/* Mounted once the outcome of the fetch is known, so the map is
                built exactly once. Its district scale is fixed at construction
                and a second build would refetch the basemap. */}
            {state.status === "loading" ? null : (
              <MapPanel snapshot={snapshot} maxDistrict={maxDistrict} advisoryIndex={index} />
            )}
          </div>

          {/* The legend describes the map, so it lists only what the map draws.
              The damage counts moved to the panel with the marks they belonged
              to: they are modelled outcomes for a synthetic registry, and a key
              beside a coastline reads as a key to the coastline. */}
          <div className={styles.legend}>
            <span className={styles.legendItem} style={{ color: "var(--lh-hazard-50)" }}>
              Blue bands · wind reaching 34, 50 and 64 knots
            </span>
            {exposed === null ? null : (
              <span className={styles.legendItem}>
                <b>{nf.format(exposed)}</b> structures in hurricane-force wind
              </span>
            )}
            <span className={styles.legendItem}>
              {nf.format(structures)} on the island · measured footprints
            </span>
          </div>
        </section>

        <aside className={styles.side}>
          {/* The one open gate, and the only thing on screen allowed to move. */}
          {frame ? (
            <div className={styles.gate}>
              {/* Trigger 2 of the motion budget is a gate that is open and
                  unactioned. With nothing to cascade there is no gate open, so
                  the label holds still — a pulse over an empty proposal is
                  exactly the decoration that makes a real alert ignorable. */}
              <span
                className={`${styles.gateRole} ${atRisk > 0 ? styles.gatePending : ""}`}
              >
                Director · awaiting approval
              </span>
              <h2 className={styles.gateAsk}>
                Send alert cascade to {nf.format(atRisk)} homes
              </h2>
              <p className={styles.gateDetail}>
                {warning ? `${warning} in effect. ` : ""}Patois and English, WhatsApp
                with SMS fallback. Proposed from advisory {frame.n}.
              </p>
              <div className={styles.gateActions}>
                {/* STILL INERT. Approving a cascade writes an approved_by row to
                    a ledger that does not exist yet, and a button that fakes
                    the write would fake the one guarantee this product makes.
                    Both are wired when the API lands. */}
                <button type="button" className={styles.gateButton}>
                  Approve cascade
                </button>
                <button type="button" className={`${styles.gateButton} ${styles.secondary}`}>
                  Review list
                </button>
              </div>
            </div>
          ) : null}

          <div className={styles.counts}>
            <div className={`${styles.countRow} ${styles.head}`}>
              <span>Worst hit right now</span>
              <span className={styles.countValue}>Destr.</span>
              <span className={styles.countValue}>Major</span>
            </div>
            {worstHit(districts).map((d) => (
              <div className={styles.countRow} key={`${d.parish}-${d.district}`}>
                <span className={styles.countPlace}>
                  {d.district}
                  <span className={styles.countParish}>{d.parish.replace("Saint ", "St ")}</span>
                </span>
                <span className={styles.countValue} style={{ color: "var(--lh-critical)" }}>
                  {d.destroyed || "—"}
                </span>
                <span className={styles.countValue} style={{ color: "var(--lh-elevated)" }}>
                  {d.major || "—"}
                </span>
              </div>
            ))}
          </div>

          <div className={styles.feed}>
            <div className={styles.panelHead}>
              <span>What happened, and who decided it</span>
            </div>
            {feed.map((row, i) => (
              <div className={styles.tline} key={i}>
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
            Play
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

        {/* The scrub bar is the story: homes by expected damage per advisory,
            left to right, so the shape of the escalation is the control. */}
        {replay ? (
          <div className={styles.timeline} role="group" aria-label="Replay timeline">
            {frames.map((x, i) => {
              const t = x.totals;
              const total = t.destroyed + t.major + t.minor + t.none || 1;
              return (
                <button
                  type="button"
                  key={x.n}
                  className={styles.tick}
                  data-current={i === index}
                  aria-current={i === index ? "true" : undefined}
                  aria-label={`Advisory ${x.n}: ${t.destroyed} destroyed, ${t.major} major`}
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
          /* States what happened and what to do, rather than an empty bar that
             looks like a storm with no history. A generated file missing on a
             fresh clone is a normal state, like the basemap archives. */
          <p className={styles.absent}>
            {missing
              ? "No replay to scrub. Generate public/replay/replay.json — see docs/engineering/replay-export-contract.md"
              : "Reading the replay."}
          </p>
        )}

        <div className={styles.clock}>
          {frame ? `${hhmm(frame.at)}Z` : "—"}
          <span className={styles.clockLabel}>
            {frame
              ? `Storm time · advisory ${frame.n} of ${frames.length}`
              : "Storm time · no advisory"}
          </span>
        </div>
      </footer>
    </main>
  );
}
