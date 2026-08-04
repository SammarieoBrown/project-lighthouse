"use client";

import { useCallback, useRef, useState } from "react";

import styles from "./operations.module.css";

type SettlementState =
  | "AWAITING_FINANCE_SIGNATURE"
  | "SIGNED_PENDING_SIMULATED_EXECUTION"
  | "SIMULATED_EXECUTING"
  | "SIMULATED_CONFIRMED"
  | "SIMULATED_FAILED";

type Settlement = {
  allocation_id: string;
  claim_ref: string;
  amount: string | number;
  currency: "JMD";
  payer_route: "GOV_RELIEF";
  state: SettlementState;
  batch_id: string | null;
  disbursement_id: string | null;
  channel: "BANK" | "MOBILE_MONEY" | "VOUCHER" | null;
  executor_provenance: "SIMULATED_DEMO" | null;
  provider_confirmation_ref: string | null;
  confirmed_at: string | null;
};

type SettlementQueue = {
  settlements: Settlement[];
  execution: {
    enabled: boolean;
    executor_provenance: "SIMULATED_DEMO" | null;
    no_real_payment_provider: true;
  };
};

type ActionResult = {
  idempotent_replay: boolean;
  money_movement: string;
  no_real_money_moved: true;
  disbursement?: { id: string };
};

type Props = {
  onLedgerChanged: () => Promise<void>;
};

const money = new Intl.NumberFormat("en-JM", {
  style: "currency",
  currency: "JMD",
  maximumFractionDigits: 0,
});

async function jsonOrDetail(response: Response): Promise<unknown> {
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = body && typeof body === "object" && "detail" in body
      ? String((body as { detail: unknown }).detail)
      : `Request failed (${response.status})`;
    throw new Error(detail);
  }
  return body;
}

function stateLabel(state: SettlementState): string {
  return {
    AWAITING_FINANCE_SIGNATURE: "Awaiting finance signature",
    SIGNED_PENDING_SIMULATED_EXECUTION: "Signed · simulation not started",
    SIMULATED_EXECUTING: "Simulation executing",
    SIMULATED_CONFIRMED: "Simulated confirmation recorded",
    SIMULATED_FAILED: "Simulation failed",
  }[state];
}

export function SettlementWorkbench({ onLedgerChanged }: Props) {
  const [tokenDraft, setTokenDraft] = useState("");
  const [token, setToken] = useState("");
  const [queue, setQueue] = useState<SettlementQueue | null>(null);
  const [loading, setLoading] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const intents = useRef(new Map<string, string>());

  const load = useCallback(async (activeToken = token) => {
    if (!activeToken) return;
    setLoading(true);
    setError(null);
    try {
      const response = await fetch("/api/lighthouse/v1/settlements?limit=100", {
        cache: "no-store",
        headers: { authorization: `Bearer ${activeToken}` },
      });
      const body = (await jsonOrDetail(response)) as SettlementQueue;
      setQueue({
        ...body,
        settlements: Array.isArray(body.settlements) ? body.settlements : [],
      });
    } catch (reason) {
      setQueue(null);
      setError(reason instanceof Error ? reason.message : "Settlement queue is unavailable.");
    } finally {
      setLoading(false);
    }
  }, [token]);

  const intentKey = useCallback((signature: string) => {
    const existing = intents.current.get(signature);
    if (existing) return existing;
    const key = crypto.randomUUID();
    intents.current.set(signature, key);
    return key;
  }, []);

  const mutate = useCallback(async (
    settlement: Settlement,
    action: "sign" | "execute",
  ) => {
    if (!token) return;
    const identity = action === "sign" ? settlement.allocation_id : settlement.disbursement_id;
    if (!identity) return;
    const requestBody = action === "sign"
      ? JSON.stringify({
          channel: "BANK",
          executor_provenance: "SIMULATED_DEMO",
          note: note.trim() || undefined,
        })
      : JSON.stringify({
          executor_provenance: "SIMULATED_DEMO",
          acknowledge_no_real_money: true,
        });
    const signature = `${action}\n${identity}\n${requestBody}`;
    const key = intentKey(signature);
    const path = action === "sign"
      ? `/api/lighthouse/v1/allocations/${encodeURIComponent(identity)}/disbursements/sign`
      : `/api/lighthouse/v1/disbursements/${encodeURIComponent(identity)}/execute`;

    setBusyId(identity);
    setError(null);
    setNotice(null);
    try {
      const response = await fetch(path, {
        method: "POST",
        headers: {
          authorization: `Bearer ${token}`,
          "content-type": "application/json",
          "idempotency-key": key,
        },
        body: requestBody,
      });
      const result = (await jsonOrDetail(response)) as ActionResult;
      intents.current.delete(signature);
      setNotice(action === "sign"
        ? `Batch signed. No money moved${result.idempotent_replay ? " (safe replay)" : ""}.`
        : `Simulated confirmation recorded. No real money moved${result.idempotent_replay ? " (safe replay)" : ""}.`);
      if (action === "sign") setNote("");
      await Promise.all([load(token), onLedgerChanged()]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Settlement action failed.");
    } finally {
      setBusyId(null);
    }
  }, [intentKey, load, note, onLedgerChanged, token]);

  return (
    <section className={styles.settlement} aria-labelledby="settlement-heading">
      <div className={styles.sectionHead}>
        <div>
          <span className={styles.eyebrow}>Act 3 · finance gate</span>
          <h2 id="settlement-heading">Sign and simulate relief settlement</h2>
        </div>
        <strong className={styles.simulationBadge}>Simulation only · no payment rail</strong>
      </div>

      <div className={styles.settlementAccess}>
        <label className={styles.field}>
          <span>Finance Officer signing token</span>
          <input
            type="password"
            value={tokenDraft}
            autoComplete="off"
            spellCheck={false}
            disabled={loading || busyId !== null}
            onChange={(event) => setTokenDraft(event.target.value)}
            placeholder="Paste five-minute Finance Officer token"
          />
        </label>
        <label className={styles.field}>
          <span>Signature note · optional</span>
          <input
            value={note}
            maxLength={500}
            disabled={busyId !== null}
            onChange={(event) => setNote(event.target.value)}
            placeholder="Finance review completed."
          />
        </label>
        <button
          type="button"
          className={`${styles.approveButton} ${styles.openButton}`}
          disabled={!tokenDraft.trim() || loading || busyId !== null}
          onClick={() => {
            const next = tokenDraft.trim();
            setToken(next);
            void load(next);
          }}
        >
          {loading ? "Opening…" : token ? "Refresh finance queue" : "Open finance queue"}
        </button>
      </div>

      <p className={styles.noMovement}>
        Signing creates a pending simulated disbursement. Execution requires a second explicit
        acknowledgement. Neither step contacts a bank, mobile-money service, voucher issuer, or
        any other payment provider.
      </p>

      {error ? <p className={styles.error} role="alert">{error}</p> : null}
      {notice ? <p className={styles.successLine} role="status">{notice}</p> : null}
      {!token ? (
        <p className={styles.empty}>The redacted finance queue stays locked until a Finance Officer authenticates.</p>
      ) : loading ? (
        <p className={styles.empty}>Reading signed settlement state…</p>
      ) : queue?.settlements.length === 0 ? (
        <p className={styles.empty}>No Director-approved cash allocations are awaiting settlement.</p>
      ) : queue ? (
        <div className={styles.settlementTable}>
          <div className={styles.settlementHead} aria-hidden="true">
            <span>Claim</span><span>Relief</span><span>State</span><span>Finance action</span>
          </div>
          {queue.settlements.map((item) => {
            const canSign = item.state === "AWAITING_FINANCE_SIGNATURE";
            const canExecute = item.state === "SIGNED_PENDING_SIMULATED_EXECUTION"
              && queue.execution.enabled;
            const identity = canSign ? item.allocation_id : item.disbursement_id;
            return (
              <div className={styles.settlementRow} data-state={item.state} key={item.allocation_id}>
                <span>
                  <b>{item.claim_ref}</b>
                  <small>Household identity withheld</small>
                </span>
                <span className={styles.data}>
                  {money.format(Number(item.amount))}
                  <small>{item.payer_route.replaceAll("_", " ")}</small>
                </span>
                <span>
                  {stateLabel(item.state)}
                  <small>{item.provider_confirmation_ref ?? item.executor_provenance ?? "No execution record"}</small>
                </span>
                <span>
                  <button
                    type="button"
                    className={styles.rowAction}
                    disabled={busyId !== null || (!canSign && !canExecute)}
                    onClick={() => void mutate(item, canSign ? "sign" : "execute")}
                  >
                    {busyId === identity
                      ? "Recording…"
                      : canSign
                        ? "Sign batch"
                        : canExecute
                          ? "Run simulation"
                          : item.state === "SIMULATED_CONFIRMED"
                            ? "Confirmed in simulation"
                            : queue.execution.enabled
                              ? "No action"
                              : "Simulation disabled"}
                  </button>
                </span>
              </div>
            );
          })}
        </div>
      ) : null}
    </section>
  );
}
