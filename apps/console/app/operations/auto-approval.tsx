"use client";

import { useCallback, useEffect, useState } from "react";

import { credentialIsDead, jsonOrDetail } from "./credential";
import styles from "./operations.module.css";

export type Policy = {
  id: string;
  hazard_event_id: string;
  max_amount: string;
  min_confidence: string;
  min_signals: number;
  requires_assessment: boolean;
  payer_route: string;
  pool_id: string | null;
  pool_name: string | null;
  created_at: string;
  revoked_at: string | null;
};

type Pool = { pool_id: string; name: string; balance: string };

const money = new Intl.NumberFormat("en-JM", { maximumFractionDigits: 0 });

/* The Director's standing authorization. Everything here is a bound on what
 * an agent may do without asking again, so the panel states each bound in the
 * sentence the Director would use, not as a form field with a label. */
export function AutoApproval({
  hazardEventId,
  pools,
  activeToken,
  onCredentialDead,
  onChanged,
}: {
  hazardEventId: string | null;
  pools: Pool[];
  activeToken: string;
  onCredentialDead: (notice: string) => void;
  onChanged: () => void;
}) {
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [ceiling, setCeiling] = useState("60000");
  const [confidence, setConfidence] = useState("0.85");
  const [signals, setSignals] = useState("4");
  const [payer, setPayer] = useState("GOV_RELIEF");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!activeToken) return;
    try {
      const response = await fetch("/api/lighthouse/v1/auto-approval/policies", {
        cache: "no-store",
        headers: { authorization: `Bearer ${activeToken}` },
      });
      const body = (await jsonOrDetail(response)) as { policies?: Policy[] };
      setPolicies(Array.isArray(body.policies) ? body.policies : []);
    } catch {
      setPolicies([]);
    }
  }, [activeToken]);

  useEffect(() => {
    void load();
  }, [load]);

  const active = policies.find((policy) => policy.revoked_at === null) ?? null;

  const authorize = useCallback(async () => {
    if (!hazardEventId || !activeToken) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const response = await fetch("/api/lighthouse/v1/auto-approval/policies", {
        method: "POST",
        cache: "no-store",
        headers: {
          authorization: `Bearer ${activeToken}`,
          "content-type": "application/json",
        },
        body: JSON.stringify({
          hazard_event_id: hazardEventId,
          max_amount: Number.parseFloat(ceiling).toFixed(2),
          min_confidence: Number.parseFloat(confidence).toFixed(3),
          min_signals: Number.parseInt(signals, 10),
          requires_assessment: true,
          payer_route: payer === "GOV_RELIEF" ? "GOV_RELIEF" : "DONOR_POOL",
          pool_id: payer === "GOV_RELIEF" ? undefined : payer,
        }),
      });
      await jsonOrDetail(response);
      setNotice("Authorization recorded. Agents may settle within it from now on.");
      await load();
      onChanged();
    } catch (failure) {
      if (credentialIsDead(failure)) {
        onCredentialDead("Your credential expired. Confirm your password to continue.");
      }
      setError(failure instanceof Error ? failure.message : "Could not authorize.");
    } finally {
      setBusy(false);
    }
  }, [
    hazardEventId, activeToken, ceiling, confidence, signals, payer, load, onChanged,
    onCredentialDead,
  ]);

  const revoke = useCallback(async () => {
    if (!active || !activeToken) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const response = await fetch(
        `/api/lighthouse/v1/auto-approval/policies/${encodeURIComponent(active.id)}/revoke`,
        {
          method: "POST",
          cache: "no-store",
          headers: {
            authorization: `Bearer ${activeToken}`,
            "content-type": "application/json",
          },
          body: "{}",
        },
      );
      await jsonOrDetail(response);
      setNotice("Authorization withdrawn. Every claim returns to a human.");
      await load();
      onChanged();
    } catch (failure) {
      if (credentialIsDead(failure)) {
        onCredentialDead("Your credential expired. Confirm your password to continue.");
      }
      setError(failure instanceof Error ? failure.message : "Could not revoke.");
    } finally {
      setBusy(false);
    }
  }, [active, activeToken, load, onChanged, onCredentialDead]);

  return (
    <section className={styles.authorization}>
      <span className={styles.eyebrow}>Act 3 · standing authorization</span>
      <h2>Delegate the small claims</h2>
      {active ? (
        <>
          <p className={styles.authorizationActive}>
            Agents may settle a verified claim up to{" "}
            <b>J${money.format(Number(active.max_amount))}</b> when its confidence is at
            least <b>{Number(active.min_confidence).toFixed(2)}</b> with{" "}
            <b>{active.min_signals} of 5</b> signals scored and a damage estimate on
            file, funded from{" "}
            <b>{active.pool_name ?? "government relief"}</b>. Anything larger, thinner,
            or unfunded waits for a human.
          </p>
          <button
            type="button"
            className={styles.revokeButton}
            disabled={busy || !activeToken}
            onClick={() => void revoke()}
          >
            {busy ? "Withdrawing…" : "Withdraw authorization"}
          </button>
        </>
      ) : (
        <>
          <p className={styles.noMovement}>
            No authorization is in force: every claim waits for a human. Setting one
            delegates bounded authority to the agents and is recorded in the ledger
            under your name.
          </p>
          <div className={styles.authorizationGrid}>
            <label className={styles.field}>
              <span>Settle up to · JMD</span>
              <input
                type="number"
                min={1}
                max={1000000}
                step={1000}
                value={ceiling}
                disabled={busy}
                onChange={(event) => setCeiling(event.target.value)}
              />
            </label>
            <label className={styles.field}>
              <span>Minimum confidence</span>
              <input
                type="number"
                min={0}
                max={1}
                step={0.05}
                value={confidence}
                disabled={busy}
                onChange={(event) => setConfidence(event.target.value)}
              />
            </label>
            <label className={styles.field}>
              <span>Signals required</span>
              <input
                type="number"
                min={1}
                max={5}
                step={1}
                value={signals}
                disabled={busy}
                onChange={(event) => setSignals(event.target.value)}
              />
            </label>
            <label className={styles.field}>
              <span>Funded from</span>
              <select
                className={styles.payerSelect}
                value={payer}
                disabled={busy}
                onChange={(event) => setPayer(event.target.value)}
              >
                <option value="GOV_RELIEF">Government relief</option>
                {pools.map((pool) => (
                  <option key={pool.pool_id} value={pool.pool_id}>
                    {pool.name} · J${money.format(Number(pool.balance))}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <button
            type="button"
            className={styles.approveButton}
            disabled={busy || !activeToken || !hazardEventId}
            onClick={() => void authorize()}
          >
            {busy ? "Recording…" : "Authorize agents within these bounds"}
          </button>
          {!activeToken ? (
            <p className={styles.noMovement}>
              Confirm your password above to delegate.
            </p>
          ) : null}
        </>
      )}
      {notice ? <p className={styles.successLine} role="status">{notice}</p> : null}
      {error ? <p className={styles.error} role="alert">{error}</p> : null}
    </section>
  );
}
