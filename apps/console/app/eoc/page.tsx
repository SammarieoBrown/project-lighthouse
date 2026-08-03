import { LighthouseMark } from "../logo";
import { SynopticMap, type District, type Snapshot } from "./map";
import snapshot from "./snapshot.json";
import styles from "./eoc.module.css";

/* EOC console — Act 1, design prototype.
 *
 * Everything is real: parish outlines, the wind field, 2,000 registered homes
 * across all fourteen parishes and their expected damage, exported from the
 * replay at Melissa advisory 25. Nothing is wired to the API yet and the
 * transport controls do not move anything — this exists to settle the screen
 * before the plumbing goes in, so the plumbing is only done once.
 *
 * Reviewed against docs/design/lighthouse-design-rules.md.
 */

export const metadata = {
  title: "Lighthouse — EOC console",
  description: "Act 1 design prototype: posture, wind field, expected damage.",
};

type Timeline = {
  n: string;
  at: string;
  destroyed: number;
  major: number;
  minor: number;
  none: number;
  posture: string;
  watch_codes: string[];
};

const SNAPSHOT = snapshot as unknown as Snapshot & {
  timeline: Timeline[];
  advisory: {
    number: string;
    issued_at: string;
    pressure_mb: number;
    positions: { lat: number; lon: number; max_wind_kt: number; gust_kt: number }[];
    probabilities: Record<string, Record<string, { cumulative: Record<string, number> }>>;
  };
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

const POSTURE_PLAIN: Record<string, string> = {
  QUIET: "Quiet",
  WATCH: "Watch",
  READY: "Ready",
  ACT: "Act",
};

function strongestWarning(codes: string[]): string | null {
  const held = new Set(codes);
  for (const [code, plain] of WATCH_WARNING) if (held.has(code)) return plain;
  return null;
}

/* Fixed locale, because the server renders in whatever locale Node was started
 * with and the browser hydrates in the user's — and the two disagreeing about a
 * thousands separator is a hydration mismatch that regenerates the whole tree. */
const nf = new Intl.NumberFormat("en-JM");

function totals() {
  return SNAPSHOT.districts.reduce(
    (a, d) => ({
      homes: a.homes + d.n,
      destroyed: a.destroyed + d.destroyed,
      major: a.major + d.major,
      minor: a.minor + d.minor,
      none: a.none + d.none,
    }),
    { homes: 0, destroyed: 0, major: 0, minor: 0, none: 0 },
  );
}

/* Where to send people first. A ranked list beats a table of every parish: the
 * question in an operations room is not "what is the distribution", it is
 * "where do we go", and that is the top of a list. */
function worstHit(limit = 7): District[] {
  return [...SNAPSHOT.districts]
    .filter((d) => d.destroyed + d.major > 0)
    .sort((a, b) => b.destroyed * 2 + b.major - (a.destroyed * 2 + a.major))
    .slice(0, limit);
}

/* The feed emits on change, not on tick. Forty-one lines saying "assessed
 * 2,000" is a feed nobody reads, and a feed nobody reads is where the line that
 * mattered goes unnoticed. */
type FeedRow = { at: string; who: string; what: string; disposer: string | null };

function feed(upto: string): FeedRow[] {
  const rows: FeedRow[] = [];
  let posture: string | null = null;
  let warning = "";
  let destroyed = 0;

  for (const t of SNAPSHOT.timeline) {
    const at = new Date(t.at).toISOString().slice(11, 16);

    if (t.posture !== posture) {
      rows.push({
        at,
        who: "Forecast Sentinel",
        what: `${posture ? "Posture raised to" : "Posture set to"} ${POSTURE_PLAIN[t.posture]}`,
        disposer: t.posture === "ACT" ? "Director" : null,
      });
      posture = t.posture;
    }

    const strongest = strongestWarning(t.watch_codes ?? []) ?? "";
    if (strongest !== warning) {
      if (strongest) {
        rows.push({ at, who: "Forecast Sentinel", what: `${strongest} in effect`, disposer: null });
      }
      warning = strongest;
    }

    if (t.destroyed - destroyed >= 60 || (destroyed === 0 && t.destroyed > 0)) {
      rows.push({
        at,
        who: "Risk Mapper",
        what: `${nf.format(t.destroyed)} homes now expected to be destroyed`,
        disposer: null,
      });
    }
    destroyed = t.destroyed;

    if (t.n === upto) break;
  }
  return rows.reverse();
}

export default function EocPrototype() {
  const t = totals();
  const advisory = SNAPSHOT.advisory;
  const position = advisory.positions[0];
  const issued = new Date(advisory.issued_at);
  const current = SNAPSHOT.timeline.findIndex((x) => x.n === advisory.number);
  const montego = advisory.probabilities["MONTEGO BAY"]?.["64"]?.cumulative?.["48"];

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
            <span className={styles.readingValue}>
              {nf.format(t.destroyed + t.major)}
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
            Advisory {advisory.number} · {issued.toISOString().slice(0, 16).replace("T", " ")}Z
          </span>
          <span>Live · next advisory 21:00Z</span>
        </div>
      </header>

      <div className={styles.body}>
        <section className={styles.mapPanel}>
          <div className={styles.panelHead}>
            <span>Forecast wind and expected damage</span>
            <span>
              <b>{nf.format(t.homes)}</b> homes registered across all 14 parishes
            </span>
          </div>

          <div className={styles.mapCanvas}>
            <SynopticMap snapshot={SNAPSHOT} />
          </div>

          <div className={styles.legend}>
            <span className={styles.legendItem}>
              <span className={styles.legendDot} style={{ background: "var(--lh-critical)" }} />
              Mostly destroyed
              <span className={styles.legendCount}>{nf.format(t.destroyed)} homes</span>
            </span>
            <span className={styles.legendItem}>
              <span className={styles.legendDot} style={{ background: "var(--lh-elevated)" }} />
              Major damage
              <span className={styles.legendCount}>{nf.format(t.major)} homes</span>
            </span>
            <span className={styles.legendItem}>
              <span
                className={styles.legendDot}
                style={{ border: "1px solid var(--lh-quiet)" }}
              />
              Minor or none
              <span className={styles.legendCount}>
                {nf.format(t.minor + t.none)} homes
              </span>
            </span>
            <span className={styles.legendItem}>Circle size · homes registered there</span>
            <span className={styles.legendItem} style={{ color: "var(--lh-hazard-50)" }}>
              Blue bands · wind reaching 34, 50 and 64 knots
            </span>
          </div>
        </section>

        <aside className={styles.side}>
          {/* The one open gate, and the only thing on screen allowed to move. */}
          <div className={styles.gate}>
            <span className={`${styles.gateRole} ${styles.gatePending}`}>
              Director · awaiting approval
            </span>
            <h2 className={styles.gateAsk}>
              Send alert cascade to {nf.format(t.destroyed + t.major)} homes
            </h2>
            <p className={styles.gateDetail}>
              Hurricane warning in effect. Patois and English, WhatsApp with SMS
              fallback. Proposed from advisory {advisory.number}.
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
              <span>Worst hit right now</span>
              <span className={styles.countValue}>Destr.</span>
              <span className={styles.countValue}>Major</span>
            </div>
            {worstHit().map((d) => (
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
            {feed(advisory.number).map((row, i) => (
              <div className={styles.tline} key={i}>
                <span className={styles.tlineTime}>{row.at}</span>
                <span className={styles.tlineSubject}>
                  <span className={styles.tlineWho}>{row.who} </span>
                  {row.what}
                </span>
                <span className={row.disposer ? styles.tlineDisposer : styles.tlineAuto}>
                  {row.disposer ?? "auto"}
                </span>
              </div>
            ))}
          </div>
        </aside>
      </div>

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

        {/* The scrub bar is the story: homes by expected damage per advisory,
            left to right, so the shape of the escalation is the control. */}
        <div className={styles.timeline} role="group" aria-label="Replay timeline">
          {SNAPSHOT.timeline.map((x, i) => {
            const total = x.destroyed + x.major + x.minor + x.none || 1;
            return (
              <button
                type="button"
                key={x.n}
                className={styles.tick}
                data-current={i === current}
                aria-label={`Advisory ${x.n}: ${x.destroyed} destroyed, ${x.major} major`}
                title={`Advisory ${x.n}`}
              >
                <span className={styles.tickNone} style={{ height: `${(x.none / total) * 100}%` }} />
                <span className={styles.tickMinor} style={{ height: `${(x.minor / total) * 100}%` }} />
                <span className={styles.tickMajor} style={{ height: `${(x.major / total) * 100}%` }} />
                <span
                  className={styles.tickDestroyed}
                  style={{ height: `${(x.destroyed / total) * 100}%` }}
                />
              </button>
            );
          })}
        </div>

        <div className={styles.clock}>
          {issued.toISOString().slice(11, 16)}Z
          <span className={styles.clockLabel}>Storm time · advisory {advisory.number} of 41</span>
        </div>
      </footer>
    </main>
  );
}
