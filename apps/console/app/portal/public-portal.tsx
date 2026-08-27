"use client";

/* Register III — the register. Ruled lines, real sequence numbers from the
 * hash chain, the feel of a log that has been kept properly. Reader: a donor
 * or an auditor, reading slowly and wanting to be convinced.
 *
 * Light ground, set on the section rather than inherited, because this is read
 * in daylight on a phone by someone who is not in a crisis — the opposite room
 * from the console (design rules, Part 1 rule 30).
 *
 * Rule C5 governs everything here: aggregate only. No household dots, no
 * names, no photographs, no testimonial cards. A design that turns a person
 * who lost their roof into marketing has lost the argument the product makes.
 */

import { useCallback, useEffect, useState } from "react";

import { LighthouseMark } from "../logo";
import styles from "./portal.module.css";

type Pool = {
  pool_id: string;
  name: string;
  scope_kind: string;
  scope_value: string | null;
  balance: string;
  total_received: string;
  donation_count: number;
};

type LedgerEntry = {
  seq: number;
  action: string;
  ts: string;
};

type Aggregate = {
  count: number;
  amount: string;
  currency: string;
  median_time_to_relief_hours: number | null;
  time_to_relief_sample: number;
};

type Journey = {
  received: { amount: string; currency: string; at: string };
  pooled: { pool_name: string; balance_now: string };
  allocated: {
    household_count: number;
    line_count: number;
    items: string[];
    parishes: string[];
    parishes_withheld_until_bucket: boolean;
  };
  disbursed_and_confirmed: {
    confirmed_count: number;
    first_confirmed_at: string | null;
  };
};

const REFRESH_MS = 30_000;

export function PublicPortal() {
  const [pools, setPools] = useState<Pool[]>([]);
  const [entries, setEntries] = useState<LedgerEntry[]>([]);
  const [aggregate, setAggregate] = useState<Aggregate | null>(null);
  const [chainValid, setChainValid] = useState<boolean | null>(null);
  const [asOf, setAsOf] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [givePool, setGivePool] = useState("");
  const [giveAmount, setGiveAmount] = useState("");
  const [giveHandle, setGiveHandle] = useState("");
  const [giving, setGiving] = useState(false);
  const [giveError, setGiveError] = useState<string | null>(null);
  const [receipt, setReceipt] = useState<{ id: string; amount: string; pool: string } | null>(null);
  const [journeyId, setJourneyId] = useState("");
  const [journey, setJourney] = useState<Journey | null>(null);
  const [journeyError, setJourneyError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [ledgerResponse, poolsResponse] = await Promise.all([
        fetch("/api/lighthouse/v1/public/ledger?latest=true&limit=25", {
          cache: "no-store",
        }),
        fetch("/api/lighthouse/v1/public/pools", { cache: "no-store" }),
      ]);
      if (!ledgerResponse.ok || !poolsResponse.ok) {
        throw new Error("The ledger is not reachable right now.");
      }
      const ledger = await ledgerResponse.json();
      const poolBody = await poolsResponse.json();
      setEntries(ledger.entries ?? []);
      setAggregate(ledger.aggregate ?? null);
      setChainValid(ledger.chain?.valid ?? null);
      setPools(poolBody.pools ?? []);
      setAsOf(new Date().toISOString());
      setError(null);
    } catch (loadError) {
      setError(
        loadError instanceof Error
          ? loadError.message
          : "The ledger is not reachable right now.",
      );
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), REFRESH_MS);
    return () => clearInterval(timer);
  }, [load]);

  const give = useCallback(async () => {
    if (giving) return;
    const pool = pools.find((candidate) => candidate.pool_id === givePool);
    const amount = giveAmount.trim();
    const handle = giveHandle.trim();
    /* A button that does nothing is worse than one that refuses. The browser's
     * own `required` accepts a single space, so the handle is checked here
     * against what the API will actually be sent. */
    if (!pool) {
      setGiveError("Choose a pool before giving.");
      return;
    }
    if (!amount) {
      setGiveError("Enter an amount in JMD.");
      return;
    }
    if (!handle) {
      setGiveError("Enter the handle this donation is shown under. A space is not a handle.");
      return;
    }
    setGiving(true);
    setGiveError(null);
    try {
      const response = await fetch("/api/lighthouse/v1/public/donations", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          pool_id: pool.pool_id,
          donor_handle: handle,
          amount,
        }),
      });
      const body = await response.json().catch(() => null);
      if (!response.ok) {
        const detail = body && typeof body === "object" && "detail" in body
          ? body.detail
          : null;
        throw new Error(
          typeof detail === "string"
            ? detail
            : "The donation could not be recorded. Nothing was taken.",
        );
      }
      /* The reference is the whole receipt: without it the donation cannot be
       * followed and a donor who assumes it failed will give twice, because
       * this POST carries no idempotency key. So an unreadable 2xx body is
       * reported as what it is — recorded, reference lost — and never as a
       * failure. */
      const recorded = body && typeof body === "object" ? body as Record<string, unknown> : null;
      const reference = recorded && recorded.donation_id != null
        ? String(recorded.donation_id)
        : "";
      if (!reference) {
        throw new Error(
          "Your donation was recorded, but this page did not get the reference back. "
          + "Do not give again — the amount is already in the pool. Note the time and "
          + "ask an operator to find it.",
        );
      }
      setReceipt({
        id: reference,
        amount: recorded?.amount != null ? String(recorded.amount) : amount,
        pool: pool.name,
      });
      setGiveAmount("");
      // The reference goes straight into the journey lookup rather than asking
      // the donor to copy it — and the panel below is cleared, so it cannot
      // keep showing an earlier donation's journey under this new reference.
      setJourney(null);
      setJourneyError(null);
      setJourneyId(reference);
      await load();
    } catch (failure) {
      setGiveError(
        failure instanceof Error
          ? failure.message
          : "The donation could not be recorded. Nothing was taken.",
      );
    } finally {
      setGiving(false);
    }
  }, [pools, givePool, giveAmount, giveHandle, giving, load]);

  const lookUpJourney = useCallback(async () => {
    const id = journeyId.trim();
    if (!id) return;
    setJourneyError(null);
    try {
      const response = await fetch(
        `/api/lighthouse/v1/public/donations/${encodeURIComponent(id)}/journey`,
        { cache: "no-store" },
      );
      if (!response.ok) {
        throw new Error("No donation with that reference.");
      }
      setJourney(await response.json());
    } catch (lookupError) {
      setJourney(null);
      setJourneyError(
        lookupError instanceof Error
          ? lookupError.message
          : "No donation with that reference.",
      );
    }
  }, [journeyId]);

  return (
    <div className={styles.ground} data-theme="light">
    <main className={styles.page}>
      <header className={styles.head}>
        <LighthouseMark size={32} title="Lighthouse" />
        <div>
          <h1>Public ledger</h1>
          <p>
            Where relief went, what it cost, and how long it took. No household
            is named here and none ever will be.
          </p>
        </div>
      </header>

      {/* C4: every data surface carries an "as of" and a sync state, and stale
          is unmistakably distinct from live rather than merely dimmed. */}
      <p className={styles.asOf} data-stale={error ? "true" : undefined}>
        {error
          ? `Not updating · ${error}`
          : asOf
            ? `As of ${new Date(asOf).toLocaleTimeString()} · refreshes every 30 seconds`
            : "Reading the ledger…"}
      </p>

      <section className={styles.figures} aria-label="Confirmed relief">
        <div>
          <span className={styles.figureLabel}>Confirmed relief</span>
          <strong className="lh-data">
            {aggregate ? `${aggregate.currency} ${aggregate.amount}` : "—"}
          </strong>
          <small>
            {aggregate ? `${aggregate.count} confirmed payment(s)` : "no data yet"}
          </small>
        </div>
        <div>
          <span className={styles.figureLabel}>Median time to relief</span>
          {/* C3: hours, because that is the precision the replay measures. No
              decimal place the pipeline cannot defend. */}
          <strong className="lh-data">
            {aggregate?.median_time_to_relief_hours === null
            || aggregate?.median_time_to_relief_hours === undefined
              ? "—"
              : `${Math.round(aggregate.median_time_to_relief_hours)} h`}
          </strong>
          <small>
            {aggregate?.time_to_relief_sample
              ? `across ${aggregate.time_to_relief_sample} household(s), filed to confirmed`
              : "nothing confirmed yet"}
          </small>
        </div>
        <div>
          <span className={styles.figureLabel}>Chain</span>
          <strong className="lh-data">
            {chainValid === null ? "—" : chainValid ? "Intact" : "BROKEN"}
          </strong>
          <small>every entry re-hashed on read</small>
        </div>
      </section>

      <section aria-label="Donation pools">
        <h2>Pools</h2>
        {pools.length === 0 ? (
          <p className={styles.empty}>
            No pool has been opened yet. Balances appear here as soon as one is.
          </p>
        ) : (
          <table className={styles.table}>
            <thead>
              <tr>
                <th scope="col">Pool</th>
                <th scope="col">Scope</th>
                <th scope="col">Received</th>
                <th scope="col">Available</th>
              </tr>
            </thead>
            <tbody>
              {pools.map((pool) => (
                <tr key={pool.pool_id}>
                  <td>{pool.name}</td>
                  <td>
                    {pool.scope_kind === "PARISH" ? pool.scope_value : "Event-wide"}
                  </td>
                  <td className="lh-data">{pool.total_received}</td>
                  <td className="lh-data">{pool.balance}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p className={styles.note}>
          Donations are simulated for this release. The platform records and
          directs; a registered charity partner holds the funds.
        </p>
      </section>

      <section aria-label="Give to a pool">
        <h2>Give</h2>
        <p className={styles.note}>
          Simulated for this release — no card is charged and no money moves.
          A registered charity partner would hold real funds; the platform
          records and directs.
        </p>
        {pools.length === 0 ? (
          <p className={styles.empty}>
            Giving opens when the first pool does.
          </p>
        ) : receipt ? (
          <div className={styles.receipt} role="status">
            <p>
              <strong>
                <span className="lh-data">JMD {receipt.amount}</span>
                {" "}given to {receipt.pool} — simulated.
              </strong>
            </p>
            <p>
              Your reference is <code className="lh-data">{receipt.id}</code> —
              save it. It is the one way to follow this donation, and it is
              already in the lookup below.
            </p>
            <button type="button" onClick={() => setReceipt(null)}>
              Give again
            </button>
          </div>
        ) : (
          <form
            className={styles.give}
            onSubmit={(event) => {
              event.preventDefault();
              void give();
            }}
          >
            <label>
              <span>Pool</span>
              <select
                value={givePool}
                onChange={(event) => setGivePool(event.target.value)}
                required
              >
                <option value="" disabled>
                  Choose where it goes
                </option>
                {pools.map((pool) => (
                  <option key={pool.pool_id} value={pool.pool_id}>
                    {pool.name}
                    {pool.scope_kind === "PARISH" ? ` — ${pool.scope_value}` : " — event-wide"}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>Amount · JMD</span>
              <input
                inputMode="decimal"
                pattern="[0-9]+([.][0-9]{1,2})?"
                placeholder="10000"
                value={giveAmount}
                onChange={(event) => setGiveAmount(event.target.value)}
                required
              />
            </label>
            <label>
              <span>Shown publicly as</span>
              <input
                maxLength={60}
                placeholder="A handle, not your name"
                value={giveHandle}
                onChange={(event) => setGiveHandle(event.target.value)}
                required
              />
            </label>
            <button type="submit" disabled={giving}>
              {giving ? "Giving…" : "Give (simulated)"}
            </button>
            {giveError ? (
              <p className={styles.error} role="alert">{giveError}</p>
            ) : null}
          </form>
        )}
      </section>

      <section aria-label="Follow a donation">
        <h2>Follow a donation</h2>
        <p className={styles.note}>
          Enter a donation reference to see where it went. Households are
          counted, never named.
        </p>
        <div className={styles.lookup}>
          <label htmlFor="donation-ref">Donation reference</label>
          <input
            id="donation-ref"
            value={journeyId}
            onChange={(event) => setJourneyId(event.target.value)}
            placeholder="00000000-0000-0000-0000-000000000000"
            spellCheck={false}
          />
          <button type="button" onClick={() => void lookUpJourney()}>
            Follow it
          </button>
        </div>
        {journeyError ? (
          <p className={styles.error} role="alert">{journeyError}</p>
        ) : null}
        {journey ? (
          <ol className={styles.journey}>
            <li>
              <span>Received</span>
              <p className="lh-data">
                {journey.received.currency} {journey.received.amount}
              </p>
              <small>{new Date(journey.received.at).toLocaleString()}</small>
            </li>
            <li>
              <span>Pooled</span>
              <p>{journey.pooled.pool_name}</p>
              <small className="lh-data">
                {journey.pooled.balance_now} still available
              </small>
            </li>
            <li>
              <span>Allocated</span>
              <p className="lh-data">
                {journey.allocated.household_count} household(s)
              </p>
              <small>
                {journey.allocated.items.length > 0
                  ? journey.allocated.items.join(", ")
                  : "no goods drawn from this pool yet"}
                {journey.allocated.parishes_withheld_until_bucket
                  ? " · parishes withheld until ten households are served"
                  : journey.allocated.parishes.length > 0
                    ? ` · ${journey.allocated.parishes.join(", ")}`
                    : ""}
              </small>
            </li>
            <li>
              <span>Delivered</span>
              <p className="lh-data">
                {journey.disbursed_and_confirmed.confirmed_count} confirmed
              </p>
              <small>
                {journey.disbursed_and_confirmed.first_confirmed_at
                  ? `first confirmed ${new Date(
                      journey.disbursed_and_confirmed.first_confirmed_at,
                    ).toLocaleString()}`
                  : "nothing confirmed from this pool yet"}
              </small>
            </li>
          </ol>
        ) : null}
      </section>

      <section aria-label="Recent entries">
        <h2>Recent entries</h2>
        {entries.length === 0 ? (
          <p className={styles.empty}>
            Nothing has been recorded yet. Entries appear here as they are
            written.
          </p>
        ) : (
          <ol className={styles.entries}>
            {entries.map((entry) => (
              <li key={entry.seq}>
                {/* Sequence numbering is legitimate here and nowhere else: the
                    chain genuinely is an ordered sequence and its order carries
                    information the reader needs. */}
                <span className="lh-data">{entry.seq}</span>
                <span>{entry.action.replaceAll(".", " · ").replaceAll("_", " ")}</span>
                <time className="lh-data" dateTime={entry.ts}>
                  {new Date(entry.ts).toLocaleString()}
                </time>
              </li>
            ))}
          </ol>
        )}
      </section>
    </main>
    </div>
  );
}
