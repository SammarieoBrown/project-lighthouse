import { LighthouseMark } from "../logo";
import { SynopticMap, type Snapshot } from "./map";
import snapshot from "./snapshot.json";
import styles from "./eoc.module.css";

/* EOC console — Act 1, design prototype.
 *
 * Everything on this screen is real: the parish outlines, the wind field, the
 * 500 households and their predicted bands all come from the replay at Melissa
 * advisory 25, exported from the database. Nothing is wired to the API yet, and
 * the controls do not move the replay — this exists to decide what the screen
 * looks like before the plumbing goes in, so that the plumbing only has to be
 * done once.
 *
 * Reviewed against docs/design/lighthouse-design-rules.md. One register per
 * panel; four hues, meaning-named; one moving element, and it is the open human
 * gate. If any of that has drifted, the screen is wrong, not the rules.
 */

export const metadata = {
  title: "Lighthouse — EOC console",
  description: "Act 1 design prototype: posture, wind field, household risk.",
};

const SNAPSHOT = snapshot as unknown as Snapshot & {
  timeline: {
    n: string; at: string;
    destroyed: number; major: number; minor: number; none: number;
    posture: string; watch_codes: string[];
  }[];
  advisory: {
    number: string;
    issued_at: string;
    pressure_mb: number;
    watch_codes: string[];
    positions: { lat: number; lon: number; max_wind_kt: number; gust_kt: number }[];
    probabilities: Record<string, Record<string, { cumulative: Record<string, number> }>>;
  };
};

const BANDS = [
  { key: "DESTROYED", label: "Destroyed", colour: "var(--lh-critical)" },
  { key: "MAJOR", label: "Major", colour: "var(--lh-elevated)" },
  { key: "MINOR", label: "Minor", colour: "var(--lh-watch)" },
  { key: "NONE", label: "No damage expected", colour: "transparent" },
] as const;

function counts() {
  const out: Record<string, number> = { DESTROYED: 0, MAJOR: 0, MINOR: 0, NONE: 0 };
  for (const h of SNAPSHOT.households) out[h.band] = (out[h.band] ?? 0) + 1;
  return out;
}

function byParish(band: string) {
  const out: Record<string, number> = {};
  for (const h of SNAPSHOT.households) {
    if (h.band === band) out[h.parish] = (out[h.parish] ?? 0) + 1;
  }
  return out;
}

/* The feed is built from the same replay the map is drawn from, so what is
 * listed is what actually happened rather than plausible-looking filler.
 *
 * It emits on *change*, not on tick. One line per advisory saying "assessed
 * 500" forty-one times is a feed nobody reads, and a feed nobody reads is where
 * the line that mattered goes unnoticed. An operator wants the four moments the
 * posture moved and the handful of times the number in danger jumped.
 */
type FeedRow = { at: string; who: string; what: string; disposer: string | null };

/* NHC ships watch and warning state as four-letter codes. They are the right
 * thing to store and the wrong thing to show: an operator reading a screen at
 * 3am should not be translating HWR in their head. Strongest first — the whole
 * point of a warning is that it outranks a watch.
 *
 * Rule from the design doc, and it applies to every surface: name things by
 * what people recognise, never by how the system is built. */
const WATCH_WARNING: [string, string][] = [
  ["HWR", "Hurricane warning"],
  ["HWA", "Hurricane watch"],
  ["TWR", "Tropical storm warning"],
  ["TWA", "Tropical storm watch"],
];

function strongestWarning(codes: string[]): string | null {
  const held = new Set(codes);
  for (const [code, plain] of WATCH_WARNING) {
    if (held.has(code)) return plain;
  }
  return null;
}

const POSTURE_PLAIN: Record<string, string> = {
  QUIET: "Quiet",
  WATCH: "Watch",
  READY: "Ready",
  ACT: "Act",
};

function feed(uptoAdvisory: string): FeedRow[] {
  const rows: FeedRow[] = [];
  let posture: string | null = null;
  let codes = "";
  let destroyed = 0;

  for (const t of SNAPSHOT.timeline) {
    const at = new Date(t.at).toISOString().slice(11, 16);

    if (t.posture !== posture) {
      rows.push({
        at,
        who: "Forecast Sentinel",
        what: posture
          ? `Posture raised to ${POSTURE_PLAIN[t.posture]} · advisory ${t.n}`
          : `Posture set to ${POSTURE_PLAIN[t.posture]} · advisory ${t.n}`,
        disposer: t.posture === "ACT" ? "Director" : null,
      });
      posture = t.posture;
    }

    const strongest = strongestWarning(t.watch_codes ?? []) ?? "";
    if (strongest !== codes) {
      if (strongest) {
        rows.push({
          at,
          who: "Forecast Sentinel",
          what: `${strongest} in effect for these parishes · advisory ${t.n}`,
          disposer: null,
        });
      }
      codes = strongest;
    }

    // A jump worth an operator's attention, not every recalculation.
    if (t.destroyed - destroyed >= 25 || (destroyed === 0 && t.destroyed > 0)) {
      rows.push({
        at,
        who: "Risk Mapper",
        what: `${t.destroyed} homes now expected to be destroyed · advisory ${t.n}`,
        disposer: null,
      });
    }
    destroyed = t.destroyed;

    if (t.n === uptoAdvisory) break;
  }

  return rows.reverse();
}

export default function EocPrototype() {
  const band = counts();
  const advisory = SNAPSHOT.advisory;
  const position = advisory.positions[0];
  const issued = new Date(advisory.issued_at);
  const current = SNAPSHOT.timeline.findIndex((t) => t.n === advisory.number);
  const montego = advisory.probabilities["MONTEGO BAY"]?.["64"]?.cumulative?.["48"];

  return (
    // The console is dark because an EOC is read in a dim room during a storm,
    // often with the lights down and a projector running. That is a product
    // decision, so the surface states it rather than asking the viewer.
    <main className={styles.screen} data-theme="dark">
      {/* ------------ chrome: Register II ------------ */}
      <header className={styles.chrome}>
        <div className={styles.brand}>
          <LighthouseMark size={26} title="Lighthouse" />
          <span className={styles.brandName}>Lighthouse</span>
        </div>

        <div className={styles.posture}>
          <span className={styles.postureLabel}>National posture</span>
          <span className={styles.postureValue} data-level="ACT">
            Act
          </span>
        </div>

        <div className={styles.readings}>
          <div className={styles.reading}>
            <span className={styles.readingValue}>{position.max_wind_kt} kt</span>
            <span className={styles.readingLabel}>Sustained wind</span>
          </div>
          <div className={styles.reading}>
            <span className={styles.readingValue}>{advisory.pressure_mb} mb</span>
            <span className={styles.readingLabel}>Central pressure</span>
          </div>
          <div className={styles.reading}>
            <span className={styles.readingValue}>{montego}%</span>
            <span className={styles.readingLabel}>Hurricane wind at Montego Bay</span>
          </div>
          <div className={styles.reading}>
            <span className={styles.readingValue}>{band.DESTROYED + band.MAJOR}</span>
            <span className={styles.readingLabel}>Homes at major risk or worse</span>
          </div>
          <div className={styles.reading}>
            {/* Not zero. Nothing has been delivered, and a zero would be a
                measurement of something that has not happened. */}
            <span className={styles.readingValue} data-empty="true">
              —
            </span>
            <span className={styles.readingLabel}>Time to relief · none yet</span>
          </div>
        </div>

        {/* Staleness is a first-class state, not an afterthought. */}
        <div className={styles.stale}>
          <span>Advisory {advisory.number} · {issued.toISOString().slice(0, 16).replace("T", " ")}Z</span>
          <span>Live · next advisory 21:00Z</span>
        </div>
      </header>

      {/* ------------ body ------------ */}
      <div className={styles.body}>
        <section className={styles.mapPanel}>
          <div className={styles.panelHead}>
            <span>Forecast wind and registered homes</span>
            <span>
              <b>{SNAPSHOT.households.length}</b> homes registered in St Elizabeth and Westmoreland ·
              12 other parishes not yet covered
            </span>
          </div>

          <div className={styles.mapCanvas}>
            <SynopticMap snapshot={SNAPSHOT} />
          </div>

          <div className={styles.legend}>
            {BANDS.map((b) => (
              <span key={b.key} className={styles.legendItem}>
                <span
                  className={styles.legendDot}
                  style={{
                    background: b.colour,
                    border: b.key === "NONE" ? "1px solid var(--lh-quiet)" : "none",
                  }}
                />
                {b.label}
                <span className={styles.legendCount}>{band[b.key]}</span>
              </span>
            ))}
            <span className={styles.legendItem}>
              Rings · wind reaching 34, 50 and 64 knots
            </span>
          </div>
        </section>

        <aside className={styles.side}>
          {/* The one open gate, and the only thing on screen allowed to move. */}
          <div className={styles.gate}>
            <span className={`${styles.gateRole} ${styles.gatePending}`}>
              Director · awaiting approval
            </span>
            <h2 className={styles.gateAsk}>Send alert cascade to 432 households</h2>
            <p className={styles.gateDetail}>
              Hurricane warning in effect. Patois and English, WhatsApp with SMS
              fallback. Proposed by AlertAgent from advisory {advisory.number}.
            </p>
            <div className={styles.gateActions}>
              <button type="button" className={styles.gateButton}>
                Approve cascade
              </button>
              <button type="button" className={`${styles.gateButton} ${styles.secondary}`}>
                Review list
              </button>
            </div>
          </div>

          <div className={styles.counts}>
            <div className={`${styles.countRow} ${styles.head}`}>
              <span>Expected damage</span>
              <span className={styles.countValue}>St Eliz</span>
              <span className={styles.countValue}>West</span>
            </div>
            {BANDS.map((b) => {
              const p = byParish(b.key);
              return (
                <div className={styles.countRow} key={b.key}>
                  <span style={{ color: b.key === "NONE" ? "var(--lh-quiet)" : b.colour }}>
                    {b.label}
                  </span>
                  <span className={styles.countValue}>{p["Saint Elizabeth"] ?? 0}</span>
                  <span className={styles.countValue}>{p["Westmoreland"] ?? 0}</span>
                </div>
              );
            })}
          </div>

          <div className={styles.feed}>
            <div className={styles.panelHead}>
              <span>What happened, and who decided it</span>
            </div>
            {feed(advisory.number).map((row, i) => (
              <div className={styles.tline} key={i}>
                <span className={styles.tlineTime}>{row.at}</span>
                <span className={styles.tlineSubject}>
                  <span className={styles.tlineWho}>{row.who} </span>
                  {row.what}
                </span>
                <span className={row.disposer ? styles.tlineDisposer : styles.tlineAuto}>
                  {row.disposer ?? "— auto"}
                </span>
              </div>
            ))}
          </div>
        </aside>
      </div>

      {/* ------------ controller: Register II ------------ */}
      <footer className={styles.controller}>
        <div className={styles.transport}>
          <button type="button" className={styles.transportButton} aria-pressed="false">
            Play
          </button>
          <button type="button" className={styles.transportButton} aria-pressed="true">
            Step
          </button>
          <button type="button" className={styles.transportButton}>
            60×
          </button>
        </div>

        {/* The scrub bar is the story: households by predicted band, per
            advisory, left to right. The shape of the escalation is the control. */}
        <div className={styles.timeline} role="group" aria-label="Replay timeline">
          {SNAPSHOT.timeline.map((t, i) => {
            const total = t.destroyed + t.major + t.minor + t.none || 1;
            return (
              <button
                type="button"
                key={t.n}
                className={styles.tick}
                data-current={i === current}
                aria-label={`Advisory ${t.n}: ${t.destroyed} destroyed, ${t.major} major`}
                title={`Advisory ${t.n}`}
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

        <div className={styles.clock}>
          {issued.toISOString().slice(11, 16)}Z
          <span className={styles.clockLabel}>Storm time · adv {advisory.number} of 41</span>
        </div>
      </footer>
    </main>
  );
}
