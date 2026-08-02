import { LighthouseLockup, LighthouseMark } from "./logo";
import { ThemeToggle } from "./theme-toggle";
import styles from "./page.module.css";

/* Specimen sheet for the design substrate.
 *
 * This is deliberately not a mock console. It exists so the tokens can be
 * looked at directly and caught drifting, and so the first thing deployed from
 * `main` is an honest artefact rather than a placeholder screen that pretends
 * to be product.
 *
 * Every value on this page comes from a token. Sample data is synthetic and
 * follows the same rule the whole platform follows: no household is named.
 */

const MEANINGS = [
  {
    token: "--lh-critical",
    swatch: "var(--lh-critical)",
    text: "var(--lh-critical-text)",
    lead: "Act now.",
    body: "ACT posture, URGENT triage, and a human gate that is blocking money. Nothing else in the product is this colour, which is what makes it worth looking at when it appears.",
  },
  {
    token: "--lh-elevated",
    swatch: "var(--lh-elevated)",
    text: "var(--lh-elevated-text)",
    lead: "Attend soon.",
    body: "READY posture, HIGH triage, an approval that is open and unactioned.",
  },
  {
    token: "--lh-watch",
    swatch: "var(--lh-watch)",
    text: "var(--lh-watch-text)",
    lead: "Monitor.",
    body: "WATCH posture, MED triage, agent work in flight. Something is happening and nobody needs to move yet.",
  },
  {
    token: "--lh-confirmed",
    swatch: "var(--lh-confirmed)",
    text: "var(--lh-confirmed-text)",
    lead: "Terminal good.",
    body: "Verified, delivered, chain valid. Reserved for states that are finished and true. Never a button — a button is a request, not an outcome.",
  },
  {
    token: "(none)",
    swatch: null,
    text: "var(--lh-figure)",
    lead: "Quiet.",
    body: "QUIET posture and LOW triage have no hue at all. The absence is the state, which keeps three hues meaning three things instead of seven hues meaning nothing.",
  },
];

const TYPE_LADDER = [
  ["--lh-text-micro", "11px", "labels, eyebrows"],
  ["--lh-text-fine", "12px", "dense data rows"],
  ["--lh-text-base", "13px", "console body"],
  ["--lh-text-read", "15px", "portal prose"],
  ["--lh-text-lead", "18px", "section heads"],
  ["--lh-text-title", "24px", "screen titles"],
  ["--lh-text-display", "34px", "wordmark"],
  ["--lh-text-hero", "48px", "portal opening"],
];

const SPACE_LADDER = [2, 4, 8, 12, 16, 24, 32, 48];

/* One Storm File, walked through the states it actually has. Synthetic. */
const TRANSITIONS = [
  {
    seq: "1281",
    at: "04:12:07",
    file: "SF-0417",
    from: "TRIAGED",
    to: "VERIFIED",
    proposer: "VerificationAgent",
    disposer: null,
    note: "confidence 0.94, five signals",
    hash: "7c3a1f8e",
  },
  {
    seq: "1284",
    at: "09:41:55",
    file: "SF-0417",
    from: "VERIFIED",
    to: "ALLOCATED",
    proposer: "LogisticsAgent",
    disposer: "Director",
    note: "allocation plan approved",
    hash: "b0d94a22",
  },
  {
    seq: "1290",
    at: "14:18:02",
    file: "SF-0417",
    from: "ALLOCATED",
    to: "SETTLED",
    proposer: "LedgerAgent",
    disposer: "Finance Officer",
    note: "disbursement signed and confirmed",
    hash: "e51c7d06",
  },
];

export default function Specimen() {
  return (
    <main className={styles.sheet}>
      <header className={styles.masthead}>
        <h1 className={styles.wordmark}>
          <LighthouseLockup size={34} />
        </h1>
        <span className={`${styles.mastheadMeta} lh-data`}>
          Design substrate · v0.1 · 2026-08-02
        </span>
        <ThemeToggle />
      </header>

      <p className={styles.standfirst}>
        The committed token system for the EOC console and the public
        transparency portal. Three registers over one substrate. Every value
        here has a reason written beside it in{" "}
        <code className="lh-data">app/tokens.css</code>; a value without a
        reason does not belong in the file.
      </p>

      {/* ---------------- the mark ---------------- */}
      <section className={styles.section}>
        <div className={styles.sectionHead}>
          <h2 className={styles.sectionTitle}>The mark</h2>
          <p className={styles.sectionNote}>
            Drawn in the same language as Register III — ruled lines, nothing
            else. One asset for both grounds: it is all currentColor.
          </p>
        </div>

        <div className={styles.marks}>
          <div className={styles.markCell}>
            <LighthouseMark size={72} title="Lighthouse" />
            <span className={styles.markCaption}>72 · full</span>
          </div>
          <div className={styles.markCell}>
            <LighthouseMark size={32} />
            <span className={styles.markCaption}>32 · full</span>
          </div>
          <div className={styles.markCell}>
            <LighthouseMark size={20} beam={false} />
            <span className={styles.markCaption}>20 · no beam</span>
          </div>
          <div className={styles.markCell}>
            <LighthouseMark size={16} beam={false} />
            <span className={styles.markCaption}>16 · no beam</span>
          </div>
          <div className={styles.markCell}>
            <LighthouseLockup size={28} />
            <span className={styles.markCaption}>Lockup</span>
          </div>
        </div>

        <div className={styles.marks}>
          <div className={styles.markCell}>
            <div className={styles.groundSwatch} data-theme="dark">
              <LighthouseLockup size={24} />
            </div>
            <span className={styles.markCaption}>Dark · the console</span>
          </div>
          <div className={styles.markCell}>
            <div className={styles.groundSwatch} data-theme="light">
              <LighthouseLockup size={24} />
            </div>
            <span className={styles.markCaption}>Light · the portal</span>
          </div>
        </div>

        <p className={styles.meaningText} style={{ marginTop: "var(--lh-space-5)" }}>
          A lighthouse tower is a stack of horizontal bands — the painted
          daymark that makes it identifiable from sea in daylight, before the
          light is any use to anyone. A ledger is also a stack of horizontal
          bands. The mark is built on that coincidence: the tower is a stack of
          recorded entries, tapering as it rises, standing on a base wider than
          itself. The beam is thrown but never sweeps. A mark that flashed at
          you from a toolbar would break rule M1 on its first frame.
        </p>
      </section>

      {/* ---------------- registers ---------------- */}
      <section className={styles.section}>
        <div className={styles.sectionHead}>
          <h2 className={styles.sectionTitle}>Three registers</h2>
          <p className={styles.sectionNote}>
            One register dominates per surface, never two on a screen. If they
            need to co-exist, the screen is wrong and gets split.
          </p>
        </div>

        <div className={styles.registers}>
          <div className={styles.register}>
            <div className={styles.registerLabel}>
              I · Synoptic
              <span className={styles.registerOwns}>
                The map and every hazard layer
              </span>
            </div>
            <div className={styles.registerBody}>
              <div className={`${styles.synoptic} lh-data`}>
                <div className={styles.reading}>
                  <span className={styles.readingValue}>64</span>
                  <span className={styles.readingLabel}>kt probability</span>
                </div>
                <div className={styles.reading}>
                  <span className={styles.readingValue}>38%</span>
                  <span className={styles.readingLabel}>St Elizabeth</span>
                </div>
                <div className={styles.reading}>
                  <span className={styles.readingValue}>117°</span>
                  <span className={styles.readingLabel}>bearing</span>
                </div>
                <div className={styles.reading}>
                  <span className={styles.readingValue}>T−54h</span>
                  <span className={styles.readingLabel}>to landfall</span>
                </div>
              </div>
              <p>
                Borrowed wholesale from the products we consume rather than
                reinvented, because an ODPEM officer already reads this language
                fluently. Every contour drawn on the map carries a real value
                from a real advisory, or it is not drawn.
              </p>
            </div>
          </div>

          <div className={styles.register}>
            <div className={styles.registerLabel}>
              II · Signage
              <span className={styles.registerOwns}>
                Console chrome, posture, queues, review, gates
              </span>
            </div>
            <div className={styles.registerBody}>
              <div className={styles.signage}>
                Posture: Ready
                <span className={styles.signageSub}>
                  Director approval required before the alert cascade sends
                </span>
              </div>
              <p>
                Read under stress, at distance, sometimes on a projector. There
                is almost no colour here on purpose, so weight and width carry
                the hierarchy instead — which is the whole reason the display
                face has a width axis.
              </p>
            </div>
          </div>

          <div className={styles.register}>
            <div className={styles.registerLabel}>
              III · Register
              <span className={styles.registerOwns}>
                Ledger, audit trail, public portal, donor journey
              </span>
            </div>
            <div className={styles.registerBody}>
              <div className={`${styles.ledgerSample} lh-data`}>
                {TRANSITIONS.map((t) => (
                  <div className={styles.ledgerRow} key={t.seq}>
                    <span className={styles.seq}>{t.seq}</span>
                    <span className={styles.seq}>{t.at}</span>
                    <span>
                      {t.file} · {t.to.toLowerCase()}
                    </span>
                  </div>
                ))}
              </div>
              <p>
                Read slowly, by someone who wants convincing. Sequence numbering
                is legitimate here and nowhere else in the product, because the
                chain genuinely is an ordered sequence and its order carries
                information the reader needs.
              </p>
            </div>
          </div>
        </div>
      </section>

      {/* ---------------- meaning ---------------- */}
      <section className={styles.section}>
        <div className={styles.sectionHead}>
          <h2 className={styles.sectionTitle}>Meaning</h2>
          <p className={styles.sectionNote}>
            Four hues for the entire product, named for what they mean rather
            than what they look like. A hue may never be borrowed for
            decoration, a button, a link, or a chart series.
          </p>
        </div>

        <div className={styles.meaning}>
          {MEANINGS.map((m) => (
            <div className={styles.meaningRow} key={m.token}>
              <span
                className={`${styles.swatch} ${m.swatch ? "" : styles.swatchNone}`}
                style={m.swatch ? { background: m.swatch } : undefined}
                aria-hidden="true"
              />
              <span className={`${styles.token} lh-data`}>{m.token}</span>
              <p className={styles.meaningText}>
                {/* The swatch shows the mark tier, the lead word shows the
                    text tier. Both are on screen so the pair can be judged
                    together rather than one at a time. */}
                <b style={{ color: m.text }}>{m.lead}</b> {m.body}
              </p>
            </div>
          ))}
        </div>
      </section>

      {/* ---------------- scales ---------------- */}
      <section className={styles.section}>
        <div className={styles.sectionHead}>
          <h2 className={styles.sectionTitle}>Scale</h2>
          <p className={styles.sectionNote}>
            Small at the bottom, because a triage queue is a table and not an
            article. Two radii, both nearly square.
          </p>
        </div>

        <div className={styles.scales}>
          <div className={styles.ladder}>
            {TYPE_LADDER.map(([token, px, use]) => (
              <div className={styles.ladderRow} key={token}>
                <span className={`${styles.ladderKey} lh-data`}>{px}</span>
                <span style={{ fontSize: `var(${token})` }}>{use}</span>
              </div>
            ))}
          </div>

          <div className={styles.ladder}>
            {SPACE_LADDER.map((px, i) => (
              <div className={styles.ladderRow} key={px}>
                <span className={`${styles.ladderKey} lh-data`}>
                  --lh-space-{i + 1}
                </span>
                <span className={styles.barRow}>
                  <span className={styles.bar} style={{ width: `${px}px` }} />
                  <span className={`${styles.barValue} lh-data`}>{px}px</span>
                </span>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ---------------- signature ---------------- */}
      <section className={styles.section}>
        <div className={styles.sectionHead}>
          <h2 className={styles.sectionTitle}>The transition line</h2>
          <p className={styles.sectionNote}>
            One object at three levels of detail. It is the only element that
            crosses all three registers unchanged, and it renders the sentence
            the platform is arguing.
          </p>
        </div>

        <div className={styles.levels}>
          <div className={styles.level}>
            <p className={styles.levelLabel}>Compressed — operator feed</p>
            <div className={`${styles.tline} lh-data`}>
              {TRANSITIONS.map((t) => (
                <div className={styles.tlineFeed} key={t.seq}>
                  <span className={styles.seq}>{t.at}</span>
                  <span className={styles.proposer}>{t.proposer}</span>
                  <span className={styles.state}>{t.to.toLowerCase()}</span>
                  <span className={styles.seq}>{t.file}</span>
                </div>
              ))}
            </div>
          </div>

          <div className={styles.level}>
            <p className={styles.levelLabel}>Full — audit trail</p>
            <div className={`${styles.tline} lh-data`}>
              <div className={`${styles.tlineAudit} ${styles.tlineHead}`}>
                <span>Seq</span>
                <span>Time</span>
                <span>File</span>
                <span>Proposed</span>
                <span>Disposed</span>
                <span>Chain</span>
              </div>
              {TRANSITIONS.map((t) => (
                <div className={styles.tlineAudit} key={t.seq}>
                  <span className={styles.seq}>{t.seq}</span>
                  <span className={styles.seq}>{t.at}</span>
                  <span>{t.file}</span>
                  <span>
                    <span className={styles.state}>
                      {t.from} → {t.to}
                    </span>{" "}
                    <span className={styles.proposer}>by {t.proposer}</span>
                  </span>
                  <span
                    className={
                      t.disposer ? styles.disposer : styles.disposerNone
                    }
                  >
                    {t.disposer ?? "— auto"}
                  </span>
                  <span className={styles.hash}>{t.hash}</span>
                </div>
              ))}
            </div>
            <p className={styles.meaningText} style={{ marginTop: "var(--lh-space-3)" }}>
              A row with no disposer is a decision no human made. That is legal
              for a verification and illegal for anything that moves money, and
              the column exists so the difference is visible rather than
              asserted.
            </p>
          </div>

          <div className={styles.level}>
            <p className={styles.levelLabel}>Narrated — donor journey</p>
            <p className={styles.narrated}>
              Your J$5,000 was received at <time>06:02</time>, pooled with
              nineteen other donations, allocated to St Elizabeth at{" "}
              <time>09:41</time> under a plan a Director approved, and confirmed
              delivered to a verified household at <time>14:18</time>. Time from
              that household filing its claim to relief in hand:{" "}
              <span className="lh-data">39 hours</span>.
            </p>
          </div>
        </div>
      </section>

      <footer className={styles.footer}>
        Rules, including the thirty banned defaults and the pre-merge checklist:{" "}
        <a href="https://github.com/SammarieoBrown/project-lighthouse/blob/main/docs/design/lighthouse-design-rules.md">
          docs/design/lighthouse-design-rules.md
        </a>
        . Nothing on this page moves, because nothing on this page is a summons.
      </footer>
    </main>
  );
}
