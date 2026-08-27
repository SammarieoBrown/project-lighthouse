import type { Metadata } from "next";
import Link from "next/link";

import { LighthouseLockup } from "./logo";
import styles from "./landing.module.css";

/* The front door. Full-bleed, light ground, Register III.
 *
 * Every band runs the full viewport width with a wide working container
 * inside it; columns are divided by hairline rules, newspaper-fashion, not by
 * cards. The hero is left-set because rule 2 bans the centred oversized
 * full-sentence headline, and the headline is a name for the thing rather
 * than a sentence about it.
 *
 * Every figure in the numbers band is a fact about the shipped system —
 * counted, not aspirational — because the first claim this page makes must
 * be one a judge can check.
 */

export const metadata: Metadata = {
  title: "Lighthouse — agentic disaster relief for Jamaica",
  description:
    "Agents propose, humans dispose, the ledger remembers. Relief coordination "
    + "from a household's voice note to a signed, auditable payment.",
};

const FIGURES = [
  { value: "9", label: "agents, from forecast to audit" },
  { value: "3", label: "human signature gates the database enforces" },
  { value: "5", label: "independent verification signals per claim" },
  { value: "41", label: "real NHC advisories replayed" },
  { value: "1.84M", label: "mapped building footprints" },
  { value: "14", label: "parishes in the synthetic registry" },
];

const ACTS = [
  {
    ordinal: "01",
    title: "The storm approaches",
    body:
      "Forecast Sentinel reads NHC advisories and sets national posture. Risk "
      + "Mapper multiplies the hazard by a household registry to say who is "
      + "exposed, not just where the wind is. The Alert Agent drafts a warning "
      + "cascade — and a Director signs before it can reach a single phone.",
    href: "/eoc",
    label: "Open the replay map",
  },
  {
    ordinal: "02",
    title: "A household speaks",
    body:
      "A voice note in Patois becomes a structured claim. Five independent "
      + "signals score it — hazard, satellite, neighbours, registry, media "
      + "integrity — and confidence is never shown without them. A photo of "
      + "the damage becomes a cost estimate a Director rules on. Anything the "
      + "agents will not stand behind goes to a human, with the evidence.",
    href: "/operations",
    label: "Open relief operations",
  },
  {
    ordinal: "03",
    title: "Relief moves",
    body:
      "Triage orders the queue. Logistics matches claims to cash and stock. A "
      + "Director releases, a Finance Officer signs, and the Ledger Agent "
      + "audits what happened. Every step is an entry in a hash-chained "
      + "ledger, and money cannot move without the signature — that rule "
      + "lives in the database, not in a code review.",
    href: "/portal",
    label: "Read the public ledger",
  },
];

const AGENTS = [
  ["Forecast Sentinel", "Watches NHC feeds, sets national posture", "autonomous"],
  ["Risk Mapper", "Hazard × registry → per-household risk", "autonomous"],
  ["Alert Agent", "Drafts warning cascades in English and Patois", "proposes · Director signs"],
  ["Intake Agent", "Voice note → structured claim, safety-of-life first", "autonomous"],
  ["Verification Agent", "Five signals, one confidence, never alone", "confidence-gated"],
  ["Damage Assessment", "Reads claim photos, proposes a cost range", "proposes · Director signs"],
  ["Triage Agent", "Medical, then habitability, then property", "autonomous · annotates only"],
  ["Logistics Agent", "Matches verified claims to cash and stock", "proposes · Director signs"],
  ["Ledger Agent", "Reconciles payments, flags what does not add up", "autonomous · after signature"],
] as const;

const SURFACES = [
  ["/eoc", "Replay map", "Hurricane Melissa's real advisories over the registry"],
  ["/operations", "Relief operations", "The working console — sign-in required"],
  ["/portal", "Public ledger", "Where relief went; nobody is named"],
  ["/simulator", "Storm simulator", "Author a track, see modelled impact"],
  ["/design", "Design substrate", "The token system and why each value"],
] as const;

export default function Home() {
  return (
    <div className={styles.ground} data-theme="light">
      <header className={styles.topbar}>
        <div className={styles.container}>
          <LighthouseLockup />
          <nav aria-label="Surfaces">
            <Link href="/eoc">Replay</Link>
            <Link href="/operations">Operations</Link>
            <Link href="/simulator">Simulator</Link>
            <Link href="/portal">Ledger</Link>
          </nav>
        </div>
      </header>

      <section className={styles.hero} aria-label="What Lighthouse is">
        <div className={styles.container}>
          <h1>
            Disaster relief,
            <br />
            from voice note to signed payment.
          </h1>
          <div className={styles.heroSide}>
            <p>
              After a hurricane, the months between a household losing its roof
              and receiving help are not spent deciding. They are spent finding
              out who was hit, and proving it. Lighthouse gives the finding out
              to agents and keeps the deciding human — enforced in the
              database, where it cannot be argued with.
            </p>
            <p className={styles.motto}>
              Agents propose, humans dispose, the ledger remembers.
            </p>
            <div className={styles.actions}>
              <Link className={styles.primary} href="/eoc">
                Open the replay map
              </Link>
              <Link className={styles.secondary} href="/portal">
                Read the public ledger
              </Link>
            </div>
          </div>
        </div>
      </section>

      <section className={styles.figures} aria-label="The system in numbers">
        <div className={styles.container}>
          {FIGURES.map((figure) => (
            <div key={figure.label}>
              <strong className="lh-data">{figure.value}</strong>
              <span>{figure.label}</span>
            </div>
          ))}
        </div>
      </section>

      <section className={styles.band} aria-label="How it works">
        <div className={styles.container}>
          <h2 className={styles.kicker}>How it works</h2>
          <ol className={styles.acts}>
            {ACTS.map((act) => (
              <li key={act.ordinal}>
                <span className={`${styles.ordinal} lh-data`}>{act.ordinal}</span>
                <h3>{act.title}</h3>
                <p>{act.body}</p>
                <Link className={styles.rowLink} href={act.href}>
                  {act.label}
                </Link>
              </li>
            ))}
          </ol>
        </div>
      </section>

      <section className={styles.band} aria-label="The nine agents">
        <div className={styles.container}>
          <h2 className={styles.kicker}>Nine agents, three signatures</h2>
          <p className={styles.bandLede}>
            Each agent does one job and holds exactly the authority the
            transition table grants it. The ones that touch people or money can
            only propose — an alert, an estimate, an allocation — and a named
            human signs before anything leaves the system.
          </p>
          <ul className={styles.agents}>
            {AGENTS.map(([name, role, authority]) => (
              <li key={name}>
                <h3>{name}</h3>
                <p>{role}</p>
                <span className={`${styles.authority} lh-data`}>{authority}</span>
              </li>
            ))}
          </ul>
        </div>
      </section>

      <section className={styles.band} aria-label="The surfaces">
        <div className={styles.container}>
          <h2 className={styles.kicker}>Open it</h2>
          <ul className={styles.surfaces}>
            {SURFACES.map(([href, name, blurb]) => (
              <li key={href}>
                <Link href={href}>{name}</Link>
                <p>{blurb}</p>
              </li>
            ))}
          </ul>
        </div>
      </section>

      <footer className={styles.foot}>
        <div className={styles.container}>
          <p>
            Every household here is synthetic and every payment is a simulation
            — no real money moves and no real household&rsquo;s data is held.
            The storms are real: NHC&rsquo;s own advisories, replayed as issued.
            The ledger is hash-chained and re-verified on every read, so the
            numbers can be checked rather than believed.
          </p>
          <p className={styles.footNote}>Built in Jamaica.</p>
        </div>
      </footer>
    </div>
  );
}
