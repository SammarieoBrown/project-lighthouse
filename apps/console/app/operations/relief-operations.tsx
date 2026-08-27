"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { LighthouseMark } from "../logo";
import styles from "./operations.module.css";
import { stepUp, useOperatorSession } from "./operator-session";
import { SettlementWorkbench } from "./settlement-workbench";
import { SignIn } from "./sign-in";

type Claim = {
  id: string;
  claim_ref: string;
  status: string;
  verification_state: string;
  damage_type: string | null;
  reported_needs: string[];
  parish: string | null;
  community: string | null;
  sol: boolean;
  severity: string | null;
  triage_rank: number | null;
  partial: boolean;
  channel: string;
  filed_at: string;
  evidence_count: number;
};

type ClaimDetail = Claim & {
  evidence: Array<{
    id: string;
    kind: string;
    created_at: string;
    has_uri: boolean;
    sha256: string | null;
  }>;
  verification: null | {
    id: string;
    confidence: number;
    verdict: string;
    capped: boolean;
    signals: Partial<Record<VerificationSignalName, VerificationSignal>>;
    created_at: string;
  };
  damage_assessment: null | {
    id: string;
    verdict: string;
    band: string;
    estimate_low: number;
    estimate_high: number;
    currency: string;
    confidence: number;
    rationale: string | null;
    evidence_count: number;
    decided: boolean;
    created_at: string;
  };
  routing: null | {
    route: string;
    insurer_name: string | null;
    fnol_available: boolean;
    decided_at: string;
  };
};

type VerificationSignal = {
  present: boolean;
  score?: number;
  note?: string;
  evidence?: Record<string, unknown>;
};

const VERIFICATION_SIGNAL_NAMES = [
  "hazard_sufficiency",
  "satellite_change",
  "neighbour_corroboration",
  "registry_match",
  "media_integrity",
] as const;

type VerificationSignalName = (typeof VERIFICATION_SIGNAL_NAMES)[number];

type LedgerEntry = {
  seq: number;
  id?: string;
  hash: string;
  prev_hash?: string | null;
  action: string;
  subject_type?: string;
  recorded_at?: string;
  recorded_on?: string;
  ts?: string;
  parish?: string | null;
  resource?: string;
  amount?: string | number | null;
  currency?: string | null;
  payer_route?: string | null;
  allocation?: {
    resource?: string;
    amount?: string | number | null;
    currency?: string | null;
    payer_route?: string | null;
  };
  settlement?: {
    resource?: string;
    amount?: string | number | null;
    currency?: string | null;
    payer_route?: string | null;
    channel?: string | null;
    executor_provenance?: string | null;
    simulated?: boolean;
  };
  money_movement?: { status?: string };
};

type ApprovalResult = {
  approval: {
    id: string;
    gate: string;
    approved_by: { id: string; display_name: string; role: string };
    approved_at: string;
    reauthenticated_at: string;
  };
  allocation: {
    id: string;
    plan_id: string;
    claim_id: string;
    resource: string;
    amount: string;
    currency: string;
    payer_route: string;
    state: string;
  };
  ledger: {
    seq: number;
    id: string;
    hash: string;
    action: string;
    recorded_at: string;
  };
  money_movement: {
    status: "NOT_INITIATED";
    disbursement_id: null;
    external_ref: null;
  };
  idempotent_replay: boolean;
};

type LedgerChain = {
  valid: boolean;
  algorithm: string;
  scope: string;
  head_seq: number | null;
  head_hash: string | null;
};

type LedgerAggregate = {
  scope: "CONFIRMED_SIMULATED_RELIEF_ONLY";
  count: number;
  amount: string | number;
  currency: "JMD";
  no_real_money_moved: true;
};

type LoadState = "locked" | "loading" | "ready" | "error";

const money = new Intl.NumberFormat("en-JM", {
  style: "currency",
  currency: "JMD",
  maximumFractionDigits: 0,
});
const when = new Intl.DateTimeFormat("en-JM", {
  dateStyle: "medium",
  timeStyle: "short",
  timeZone: "America/Jamaica",
});
const onDate = new Intl.DateTimeFormat("en-JM", {
  dateStyle: "medium",
  timeZone: "UTC",
});

function shortHash(value: string | null | undefined): string {
  return value ? `${value.slice(0, 10)}…${value.slice(-6)}` : "—";
}

function evidenceSummary(value: Record<string, unknown> | undefined): string | null {
  if (!value) return null;
  const parts: string[] = [];
  for (const [key, raw] of Object.entries(value)) {
    if (parts.length >= 8) break;
    if (raw && typeof raw === "object" && !Array.isArray(raw)) {
      const nested = evidenceSummary(raw as Record<string, unknown>);
      if (nested) parts.push(nested);
    } else if (Array.isArray(raw)) {
      if (raw.length) parts.push(`${key.replaceAll("_", " ")}: ${raw.join(", ")}`);
    } else if (["string", "number", "boolean"].includes(typeof raw)) {
      parts.push(`${key.replaceAll("_", " ")}: ${String(raw)}`);
    }
  }
  return parts.length ? parts.join(" · ") : null;
}

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

export function ReliefOperations() {
  const [claims, setClaims] = useState<Claim[]>([]);
  const [ledger, setLedger] = useState<LedgerEntry[]>([]);
  const [claimsState, setClaimsState] = useState<LoadState>("locked");
  const [ledgerState, setLedgerState] = useState<LoadState>("loading");
  const [ledgerChain, setLedgerChain] = useState<LedgerChain | null>(null);
  const [ledgerAggregate, setLedgerAggregate] = useState<LedgerAggregate | null>(null);
  const [claimsError, setClaimsError] = useState<string | null>(null);
  const [ledgerError, setLedgerError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [claimDetail, setClaimDetail] = useState<ClaimDetail | null>(null);
  const [detailState, setDetailState] = useState<LoadState>("loading");
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailRefresh, setDetailRefresh] = useState(0);
  /* The password, held only long enough to exchange it for a credential. The
   * five-minute token it mints is what everything downstream uses, exactly as
   * before — this field used to be where a token got pasted after a trip to a
   * terminal. */
  const [operatorPassword, setOperatorPassword] = useState("");
  const [stepUpError, setStepUpError] = useState<string | null>(null);
  const [activeToken, setActiveToken] = useState("");
  const [note, setNote] = useState("");
  const [approving, setApproving] = useState(false);
  const [approval, setApproval] = useState<ApprovalResult | null>(null);
  const [approvalError, setApprovalError] = useState<string | null>(null);
  const [reviewNote, setReviewNote] = useState("");
  const [reviewing, setReviewing] = useState(false);
  const [damageNote, setDamageNote] = useState("");
  const [deciding, setDeciding] = useState(false);
  const [damageNotice, setDamageNotice] = useState<string | null>(null);
  const [damageError, setDamageError] = useState<string | null>(null);
  const [reviewError, setReviewError] = useState<string | null>(null);
  const [reviewNotice, setReviewNotice] = useState<string | null>(null);
  const claimsRequest = useRef(0);
  const approvalIntent = useRef<{ signature: string; key: string } | null>(null);

  const loadClaims = useCallback(async (token = activeToken) => {
    const requestId = ++claimsRequest.current;
    if (!token) {
      setClaims([]);
      setClaimsState("locked");
      setSelectedId(null);
      return;
    }
    setClaimsState("loading");
    setClaimsError(null);
    try {
      const response = await fetch("/api/lighthouse/api/claims?limit=100", {
        cache: "no-store",
        headers: { authorization: `Bearer ${token}` },
      });
      const body = (await jsonOrDetail(response)) as { claims?: Claim[] };
      if (requestId !== claimsRequest.current) return;
      const next = Array.isArray(body.claims) ? body.claims : [];
      setClaims(next);
      setClaimsState("ready");
      setClaimsError(null);
      setSelectedId((current) => {
        if (current && next.some((claim) => claim.id === current)) return current;
        return next.find((claim) => claim.status === "VERIFIED")?.id ?? next[0]?.id ?? null;
      });
    } catch (error) {
      if (requestId !== claimsRequest.current) return;
      setClaims([]);
      setSelectedId(null);
      setClaimsState("error");
      setClaimsError(error instanceof Error ? error.message : "Claims are unavailable.");
    }
  }, [activeToken]);

  const loadLedger = useCallback(async () => {
    try {
      const response = await fetch("/api/lighthouse/v1/public/ledger?latest=true&limit=50", {
        cache: "no-store",
      });
      const body = (await jsonOrDetail(response)) as {
        entries?: LedgerEntry[];
        chain?: LedgerChain;
        aggregate?: LedgerAggregate;
      };
      if (!body.chain?.valid) {
        throw new Error("Full ledger integrity check failed; public records are withheld.");
      }
      setLedger(Array.isArray(body.entries) ? body.entries : []);
      setLedgerChain(body.chain ?? null);
      setLedgerAggregate(body.aggregate ?? null);
      setLedgerState("ready");
      setLedgerError(null);
    } catch (error) {
      setLedger([]);
      setLedgerChain(null);
      setLedgerAggregate(null);
      setLedgerState("error");
      setLedgerError(error instanceof Error ? error.message : "Ledger is unavailable.");
    }
  }, []);

  const refresh = useCallback(async () => {
    await Promise.all([activeToken ? loadClaims(activeToken) : Promise.resolve(), loadLedger()]);
  }, [activeToken, loadClaims, loadLedger]);

  useEffect(() => {
    void loadLedger();
    const timer = window.setInterval(() => void refresh(), 15_000);
    return () => window.clearInterval(timer);
  }, [loadLedger, refresh]);

  useEffect(() => {
    if (!selectedId || !activeToken) {
      setClaimDetail(null);
      setDetailState("locked");
      setDetailError(null);
      return;
    }
    const controller = new AbortController();
    setDetailState("loading");
    setDetailError(null);
    fetch(`/api/lighthouse/api/claims/${encodeURIComponent(selectedId)}`, {
      cache: "no-store",
      headers: { authorization: `Bearer ${activeToken}` },
      signal: controller.signal,
    })
      .then(jsonOrDetail)
      .then((body) => {
        if (!controller.signal.aborted) {
          setClaimDetail(body as ClaimDetail);
          setDetailState("ready");
        }
      })
      .catch((error) => {
        if (!controller.signal.aborted) {
          setClaimDetail(null);
          setDetailState("error");
          setDetailError(error instanceof Error ? error.message : "Claim evidence is unavailable.");
        }
      });
    return () => controller.abort();
  }, [selectedId, activeToken, detailRefresh]);

  const selected = useMemo(
    () => claims.find((claim) => claim.id === selectedId) ?? null,
    [claims, selectedId],
  );
  const verificationSignals = claimDetail?.verification
    ? VERIFICATION_SIGNAL_NAMES.map((name) => [name, claimDetail.verification?.signals[name]] as const)
    : [];
  const hasCompleteSignalBundle = Boolean(
    selected
    && claimDetail?.id === selected.id
    && detailState === "ready"
    && claimDetail.verification
    && VERIFICATION_SIGNAL_NAMES.every((name) => {
      const signal = claimDetail.verification?.signals[name];
      return Boolean(
        signal
        && typeof signal.present === "boolean"
        && (!signal.present
          || (typeof signal.score === "number" && signal.score >= 0 && signal.score <= 1)),
      );
    }),
  );
  const completeVerification = Boolean(
    hasCompleteSignalBundle
    && claimDetail?.status === "VERIFIED"
    && claimDetail.verification
    && ["AUTO_VERIFIED", "APPROVED"].includes(claimDetail.verification.verdict),
  );
  const reviewReady = Boolean(
    selected?.status === "FILED"
    && claimDetail?.verification
    && ["REVIEW", "FLAGGED"].includes(claimDetail.verification.verdict)
    && hasCompleteSignalBundle
    && activeToken
    && reviewNote.trim().length >= 10,
  );
  const approvalReady = Boolean(
    selected?.status === "VERIFIED" && activeToken && completeVerification,
  );
  const verified = claims.filter((claim) => claim.status === "VERIFIED").length;
  const safetyOfLife = claims.filter((claim) => claim.sol).length;

  const approve = useCallback(async () => {
    if (!selected || !approvalReady || !activeToken) return;
    setApproving(true);
    setApproval(null);
    setApprovalError(null);

    try {
      const requestBody = JSON.stringify({
        resource: "CASH",
        amount: "45000.00",
        currency: "JMD",
        payer_route: "GOV_RELIEF",
        note: note.trim() || undefined,
      });
      const intentSignature = `${selected.id}\n${requestBody}`;
      if (approvalIntent.current?.signature !== intentSignature) {
        approvalIntent.current = {
          signature: intentSignature,
          key: crypto.randomUUID(),
        };
      }
      const intentKey = approvalIntent.current.key;
      const response = await fetch(
        `/api/lighthouse/v1/claims/${encodeURIComponent(selected.id)}/allocations/approve`,
        {
          method: "POST",
          headers: {
            authorization: `Bearer ${activeToken}`,
            "content-type": "application/json",
            "idempotency-key": intentKey,
          },
          body: requestBody,
        },
      );
      const result = (await jsonOrDetail(response)) as ApprovalResult;
      if (approvalIntent.current?.key === intentKey) approvalIntent.current = null;
      setApproval(result);
      setNote("");
      await loadLedger();
    } catch (error) {
      setApprovalError(error instanceof Error ? error.message : "Approval failed.");
    } finally {
      setApproving(false);
    }
  }, [selected, approvalReady, activeToken, note, loadLedger]);

  const approvalClaim = approval
    ? claims.find((claim) => claim.id === approval.allocation.claim_id) ?? null
    : null;

  const decideDamage = useCallback(async (verdict: "APPROVED" | "REJECTED") => {
    const assessment = claimDetail?.damage_assessment;
    if (!selected || !activeToken || !assessment || damageNote.trim().length < 10) return;
    setDeciding(true);
    setDamageError(null);
    setDamageNotice(null);
    try {
      const response = await fetch(
        `/api/lighthouse/v1/claims/${encodeURIComponent(selected.id)}/damage-assessment/review`,
        {
          method: "POST",
          headers: {
            authorization: `Bearer ${activeToken}`,
            "content-type": "application/json",
          },
          body: JSON.stringify({
            assessment_id: assessment.id,
            verdict,
            rationale: damageNote.trim(),
          }),
        },
      );
      const result = (await jsonOrDetail(response)) as { idempotent_replay?: boolean };
      setDamageNote("");
      setDamageNotice(
        `${verdict === "APPROVED" ? "Estimate approved" : "Estimate rejected"} by Director`
        + (result.idempotent_replay
          ? " · existing decision replayed safely"
          : " · immutable decision recorded"),
      );
      setDetailRefresh((value) => value + 1);
      await loadLedger();
    } catch (error) {
      setDamageError(error instanceof Error ? error.message : "Decision failed.");
    } finally {
      setDeciding(false);
    }
  }, [selected, activeToken, claimDetail, damageNote, loadLedger]);

  const reviewClaim = useCallback(async (verdict: "APPROVED" | "REJECTED") => {
    if (!selected || !reviewReady || !activeToken) return;
    setReviewing(true);
    setReviewError(null);
    setReviewNotice(null);
    try {
      const response = await fetch(
        `/api/lighthouse/v1/claims/${encodeURIComponent(selected.id)}/verification/review`,
        {
          method: "POST",
          headers: {
            authorization: `Bearer ${activeToken}`,
            "content-type": "application/json",
          },
          body: JSON.stringify({
            verification_id: claimDetail?.verification?.id,
            verdict,
            rationale: reviewNote.trim(),
          }),
        },
      );
      const result = (await jsonOrDetail(response)) as { idempotent_replay?: boolean };
      setReviewNote("");
      setReviewNotice(
        `${verdict === "APPROVED" ? "Claim verified" : "Claim rejected"} by Review Clerk`
        + (result.idempotent_replay ? " · existing decision replayed safely" : " · immutable decision recorded"),
      );
      await loadClaims(activeToken);
      setDetailRefresh((value) => value + 1);
    } catch (error) {
      setReviewError(error instanceof Error ? error.message : "Review decision failed.");
    } finally {
      setReviewing(false);
    }
  }, [activeToken, claimDetail, loadClaims, reviewNote, reviewReady, selected]);

  /* The shift, not the approval. Signing in opens the queues this role may
   * read; it never approves anything on its own — that still costs a password
   * and produces a five-minute credential. */
  const { state: sessionState, signIn, signOut } = useOperatorSession();

  const chrome = (
    <header className={styles.header}>
      <div className={styles.identity}>
        <LighthouseMark size={28} title="Lighthouse" />
        <div>
          <span className={styles.brand}>Lighthouse</span>
          <span className={styles.mode}>Relief operations · Acts 2 and 3</span>
        </div>
      </div>
      <nav className={styles.nav} aria-label="Lighthouse products">
        <Link href="/eoc">EOC map</Link>
        <Link href="/simulator">Storm simulator</Link>
        {sessionState.status === "in" ? (
          <>
            <span className={styles.operator}>
              {sessionState.operator.display_name} · {sessionState.operator.role}
            </span>
            <button
              type="button"
              onClick={() => void refresh()}
              disabled={claimsState === "loading" || approving}
            >
              Refresh
            </button>
            <button
              type="button"
              onClick={() => {
                setActiveToken("");
                void signOut();
              }}
            >
              Sign out
            </button>
          </>
        ) : null}
      </nav>
    </header>
  );

  if (sessionState.status !== "in") {
    return (
      <main className={styles.screen} data-theme="dark">
        {chrome}
        {sessionState.status === "loading" ? (
          <p className={styles.empty}>Checking your session…</p>
        ) : (
          <SignIn onSignIn={signIn} />
        )}
      </main>
    );
  }

  return (
    <main className={styles.screen} data-theme="dark">
      {chrome}

      <section className={styles.metrics} aria-label="Relief operation measures">
        <div>
          <strong>{claimsState === "ready" ? claims.length : "—"}</strong>
          <span>Redacted claims received</span>
        </div>
        <div>
          <strong>{claimsState === "ready" ? verified : "—"}</strong>
          <span>Verified · eligible for allocation</span>
        </div>
        <div>
          <strong data-alert={safetyOfLife > 0 ? "true" : undefined}>
            {claimsState === "ready" ? safetyOfLife : "—"}
          </strong>
          <span>Safety-of-life priority</span>
        </div>
        <div>
          <strong>{ledgerState === "ready" ? ledgerAggregate?.count ?? 0 : "—"}</strong>
          <span>Simulated confirmations · no real funds</span>
        </div>
      </section>

      <div className={styles.workspace}>
        <section className={styles.claims}>
          <div className={styles.sectionHead}>
            <div>
              <span className={styles.eyebrow}>Act 2 · intake</span>
              <h1>WhatsApp claim queue</h1>
            </div>
            <span className={styles.sync}>Refreshes every 15 seconds</span>
          </div>

          {claimsState === "locked" ? (
            <p className={styles.empty}>
              Protected queue locked. Present a short-lived operator credential to read claims.
            </p>
          ) : claimsState === "error" ? (
            <p className={styles.error}>{claimsError}</p>
          ) : claimsState === "loading" ? (
            <p className={styles.empty}>Reading the intake queue…</p>
          ) : claims.length === 0 ? (
            <p className={styles.empty}>
              No claims yet. A signed Twilio WhatsApp message creates the first durable claim.
            </p>
          ) : (
            <div className={styles.claimTable} aria-label="Redacted claims">
              <div className={styles.tableHead} aria-hidden="true">
                <span>Claim</span><span>Place</span><span>Triage</span>
                <span>Evidence</span><span>State</span>
              </div>
              {claims.map((claim) => (
                <button
                  type="button"
                  className={styles.claimRow}
                  data-selected={claim.id === selectedId ? "true" : undefined}
                  data-sol={claim.sol ? "true" : undefined}
                  aria-pressed={claim.id === selectedId}
                  disabled={approving}
                  onClick={() => {
                    setSelectedId(claim.id);
                    setApproval(null);
                    setApprovalError(null);
                  }}
                  key={claim.id}
                >
                  <span>
                    <b>{claim.claim_ref}</b>
                    <small>{claim.damage_type?.replaceAll("_", " ") ?? "damage details pending"}</small>
                  </span>
                  <span>
                    {claim.community ?? "Location pending"}
                    <small>{claim.parish ?? "Parish unconfirmed"}</small>
                  </span>
                  <span className={styles.triage} data-severity={claim.severity ?? undefined}>
                    {claim.sol ? "SOL" : claim.severity ?? "—"}
                    <small className="lh-data">
                      {claim.triage_rank === null ? "not triaged" : `rank ${claim.triage_rank}`}
                    </small>
                  </span>
                  <span className={styles.data}>{claim.evidence_count}</span>
                  <span className={styles.state} data-state={claim.status}>
                    {claim.sol ? "SOL · " : ""}{claim.status}
                    <small>{claim.verification_state.replaceAll("_", " ")}</small>
                  </span>
                </button>
              ))}
            </div>
          )}
          <p className={styles.privacy}>
            Redacted operations view. Phone numbers, names, message bodies, transcripts, media URLs,
            and raw provider payloads never leave the API.
          </p>
        </section>

        <aside className={styles.approval}>
          <span className={styles.eyebrow}>Act 3 · human gate</span>
          <h2>Approve relief allocation</h2>
          <label className={styles.field}>
            <span>Confirm your password</span>
            <input
              type="password"
              value={operatorPassword}
              autoComplete="current-password"
              spellCheck={false}
              disabled={approving}
              onChange={(event) => setOperatorPassword(event.target.value)}
              placeholder="Password"
            />
          </label>
          <button
            type="button"
            className={`${styles.approveButton} ${styles.openButton}`}
            disabled={!operatorPassword || claimsState === "loading" || approving}
            onClick={async () => {
              setStepUpError(null);
              try {
                const token = await stepUp(operatorPassword);
                setOperatorPassword("");
                setActiveToken(token);
                await loadClaims(token);
              } catch (failure) {
                setStepUpError(
                  failure instanceof Error ? failure.message : "Could not confirm your password.",
                );
              }
            }}
          >
            {claimsState === "loading" ? "Opening…" : activeToken ? "Refresh protected queue" : "Open protected queue"}
          </button>
          {stepUpError ? (
            <p className={styles.signInError} role="alert">{stepUpError}</p>
          ) : null}
          <p className={styles.noMovement}>
            The credential stays in this tab&apos;s memory only and expires after five minutes.
          </p>
          {selected ? (
            <>
              <dl className={styles.selection}>
                <div><dt>Claim</dt><dd>{selected.claim_ref}</dd></div>
                <div><dt>Eligibility</dt><dd>{selected.status}</dd></div>
                <div><dt>Resource</dt><dd>{money.format(45_000)} cash grant</dd></div>
                <div><dt>Payer</dt><dd>Government relief</dd></div>
              </dl>
              {detailState === "loading" ? (
                <p className={styles.empty}>Reading redacted evidence…</p>
              ) : claimDetail?.verification && hasCompleteSignalBundle ? (
                <div className={styles.verification}>
                  <div className={styles.verificationHead}>
                    <span>Verification</span>
                    <b>{claimDetail.verification.verdict.replaceAll("_", " ")}</b>
                  </div>
                  <div className={styles.signals}>
                    {verificationSignals.map(([name, signal]) => {
                      const evidence = evidenceSummary(signal?.evidence);
                      return (
                        <div key={name}>
                          <span>
                            {name.replaceAll("_", " ")}
                            {signal?.note ? <small>{signal.note}</small> : null}
                            {evidence ? <small>{evidence}</small> : null}
                          </span>
                          <b>
                            {!signal?.present
                              ? "Absent"
                              : typeof signal.score === "number"
                                ? signal.score.toFixed(2)
                                : "Present"}
                          </b>
                        </div>
                      );
                    })}
                  </div>
                  <p>
                    Combined confidence {claimDetail.verification.confidence.toFixed(2)} · shown with
                    every recorded signal, never as a score on its own.
                  </p>
                </div>
              ) : detailState === "error" ? (
                <p className={styles.error} role="alert">
                  {detailError ?? "Claim evidence is unavailable; approval is blocked."}
                </p>
              ) : claimDetail?.verification ? (
                <p className={styles.limit}>
                  A {claimDetail.verification.verdict.replaceAll("_", " ")} verdict is recorded,
                  but the complete five-signal breakdown is unavailable here; confidence and approval
                  are withheld.
                </p>
              ) : detailState === "ready" ? (
                <p className={styles.limit}>
                  Verification evidence is still pending; this claim cannot be approved yet.
                </p>
              ) : null}
              {claimDetail?.verification
              && ["REVIEW", "FLAGGED"].includes(claimDetail.verification.verdict)
              && selected.status === "FILED" ? (
                <div className={styles.reviewGate}>
                  <span className={styles.eyebrow}>Act 2 · Review Clerk decision</span>
                  <p className={styles.limit}>
                    The agent did not auto-verify this claim. Review all five signals and record a
                    reason. This decision does not allocate or move relief.
                  </p>
                  <label className={styles.field}>
                    <span>Review reason · required</span>
                    <textarea
                      value={reviewNote}
                      onChange={(event) => setReviewNote(event.target.value)}
                      minLength={10}
                      maxLength={500}
                      disabled={reviewing}
                      placeholder="Evidence reviewed; explain the approval or rejection."
                    />
                  </label>
                  <div className={styles.gateActions}>
                    <button
                      type="button"
                      className={styles.approveButton}
                      disabled={!reviewReady || reviewing}
                      onClick={() => void reviewClaim("APPROVED")}
                    >
                      {reviewing ? "Recording…" : "Approve claim"}
                    </button>
                    <button
                      type="button"
                      className={`${styles.approveButton} ${styles.rejectButton}`}
                      disabled={!reviewReady || reviewing}
                      onClick={() => void reviewClaim("REJECTED")}
                    >
                      Reject claim
                    </button>
                  </div>
                  {reviewNotice ? <p className={styles.successLine} role="status">{reviewNotice}</p> : null}
                  {reviewError ? <p className={styles.error} role="alert">{reviewError}</p> : null}
                </div>
              ) : null}
              {claimDetail?.damage_assessment ? (
                <div className={styles.reviewGate}>
                  <span className={styles.eyebrow}>Act 2 · Director estimate decision</span>
                  <dl className={styles.estimate}>
                    <div>
                      <dt>Band</dt>
                      <dd>{claimDetail.damage_assessment.band}</dd>
                    </div>
                    <div>
                      <dt>Range</dt>
                      <dd className="lh-data">
                        {claimDetail.damage_assessment.currency}{" "}
                        {claimDetail.damage_assessment.estimate_low.toFixed(2)}
                        {" \u2013 "}
                        {claimDetail.damage_assessment.estimate_high.toFixed(2)}
                      </dd>
                    </div>
                    <div>
                      <dt>Photos read</dt>
                      <dd className="lh-data">{claimDetail.damage_assessment.evidence_count}</dd>
                    </div>
                    <div>
                      <dt>Confidence</dt>
                      <dd className="lh-data">
                        {claimDetail.damage_assessment.confidence.toFixed(2)}
                      </dd>
                    </div>
                  </dl>
                  {claimDetail.damage_assessment.rationale ? (
                    <p className={styles.limit}>{claimDetail.damage_assessment.rationale}</p>
                  ) : null}
                  {claimDetail.damage_assessment.decided ? (
                    <p className={styles.limit}>
                      A Director has already recorded{" "}
                      {claimDetail.damage_assessment.verdict.toLowerCase()} on this estimate.
                      A further decision would be a second signature on the same figure.
                    </p>
                  ) : (
                    <>
                      <p className={styles.limit}>
                        A range, not a figure. The estimate does not size the grant \u2014 relief is a
                        flat J$45,000 \u2014 and nothing is released by this decision.
                      </p>
                      <label className={styles.field}>
                        <span>Decision reason \u00b7 required</span>
                        <textarea
                          value={damageNote}
                          onChange={(event) => setDamageNote(event.target.value)}
                          minLength={10}
                          maxLength={500}
                          disabled={deciding}
                          placeholder="Photos reviewed; explain the approval or rejection."
                        />
                      </label>
                      <div className={styles.gateActions}>
                        <button
                          type="button"
                          className={styles.approveButton}
                          disabled={deciding || damageNote.trim().length < 10}
                          onClick={() => void decideDamage("APPROVED")}
                        >
                          {deciding ? "Recording\u2026" : "Approve estimate"}
                        </button>
                        <button
                          type="button"
                          className={`${styles.approveButton} ${styles.rejectButton}`}
                          disabled={deciding || damageNote.trim().length < 10}
                          onClick={() => void decideDamage("REJECTED")}
                        >
                          Reject estimate
                        </button>
                      </div>
                    </>
                  )}
                  {damageNotice ? (
                    <p className={styles.successLine} role="status">{damageNotice}</p>
                  ) : null}
                  {damageError ? <p className={styles.error} role="alert">{damageError}</p> : null}
                </div>
              ) : null}
              {claimDetail?.routing ? (
                <div className={styles.routing}>
                  <span className={styles.eyebrow}>Payer route</span>
                  <p className={styles.limit}>
                    {claimDetail.routing.route.replaceAll("_", " ")}
                    {claimDetail.routing.insurer_name
                      ? ` \u00b7 ${claimDetail.routing.insurer_name}`
                      : " \u00b7 no insurer-sharing consent on file"}
                  </p>
                  {claimDetail.routing.fnol_available ? (
                    <a
                      className={styles.fnolLink}
                      href={`/api/lighthouse/v1/claims/${encodeURIComponent(selected.id)}/fnol.pdf`}
                      target="_blank"
                      rel="noreferrer"
                    >
                      Open FNOL packet (PDF)
                    </a>
                  ) : null}
                </div>
              ) : null}
              <label className={styles.field}>
                <span>Decision note · optional</span>
                <textarea
                  value={note}
                  onChange={(event) => setNote(event.target.value)}
                  maxLength={500}
                  placeholder="Evidence reviewed; standard emergency grant."
                />
              </label>
              {selected.status !== "VERIFIED" ? (
                <p className={styles.limit}>Only verified claims can cross this gate.</p>
              ) : null}
              <button
                type="button"
                className={styles.approveButton}
                disabled={approving || !approvalReady}
                onClick={() => void approve()}
              >
                {approving ? "Signing…" : `Approve ${money.format(45_000)}`}
              </button>
              <p className={styles.noMovement}>
                This records an approved allocation. It does not create or claim a bank transfer,
                mobile-money payment, voucher, or delivery.
              </p>
            </>
          ) : (
            <p className={styles.empty}>Select a verified claim to open the human gate.</p>
          )}

          {approval ? (
            <div className={styles.success} role="status">
              <strong>Allocation approved · no money moved</strong>
              <span>Claim {approvalClaim?.claim_ref ?? approval.allocation.claim_id}</span>
              <span>Signed by {approval.approval.approved_by.display_name}</span>
              <span>Ledger #{approval.ledger.seq} · {shortHash(approval.ledger.hash)}</span>
              <span>{approval.idempotent_replay ? "Existing decision replayed safely" : "New immutable decision recorded"}</span>
            </div>
          ) : null}
          {approvalError ? <p className={styles.error} role="alert">{approvalError}</p> : null}
        </aside>
      </div>

      <SettlementWorkbench onLedgerChanged={loadLedger} />

      <section className={styles.ledger}>
        <div className={styles.sectionHead}>
          <div>
            <span className={styles.eyebrow}>Public proof · identity removed</span>
            <h2>Append-only allocation ledger</h2>
          </div>
          <span className={styles.sync} data-valid={ledgerChain?.valid ? "true" : undefined}>
            {ledgerChain
              ? `${ledgerChain.valid ? "Full chain valid" : "Chain check failed"} · ${ledgerChain.algorithm} · head ${ledgerChain.head_seq ?? "—"}`
              : "Hash chained in database order"}
          </span>
        </div>
        {ledgerState === "error" ? (
          <p className={styles.error}>{ledgerError}</p>
        ) : ledgerState === "loading" ? (
          <p className={styles.empty}>Verifying the public ledger…</p>
        ) : ledger.length === 0 ? (
          <p className={styles.empty}>No public allocation approvals have been recorded.</p>
        ) : (
          <div className={styles.ledgerTable}>
            <div className={styles.ledgerHead} aria-hidden="true">
              <span>Seq</span><span>Action</span><span>Relief</span><span>Recorded (UTC)</span><span>Hash</span>
            </div>
            {[...ledger].reverse().map((entry) => {
              const release = entry.allocation ?? entry.settlement ?? entry;
              const amount = release.amount == null ? null : Number(release.amount);
              const recorded = entry.recorded_at ?? entry.ts;
              return (
                <div className={styles.ledgerRow} key={`${entry.seq}:${entry.hash}`}>
                  <span className={styles.data}>{entry.seq}</span>
                  <span>
                    {entry.action.replaceAll(".", " · ")}
                    <small>{entry.money_movement?.status?.replaceAll("_", " ") ?? "Audited milestone"}</small>
                  </span>
                  <span>
                    {amount == null ? release.resource ?? "—" : money.format(amount)}
                    <small>
                      {entry.settlement
                        ? `${entry.settlement.channel?.replaceAll("_", " ") ?? "Channel withheld"} · SIMULATED`
                        : release.payer_route?.replaceAll("_", " ") ?? "Public identity withheld"}
                    </small>
                  </span>
                  <span className={styles.data}>
                    {entry.recorded_on
                      ? onDate.format(new Date(`${entry.recorded_on}T00:00:00Z`))
                      : recorded
                        ? when.format(new Date(recorded))
                        : "—"}
                  </span>
                  <span className={styles.hash}>{shortHash(entry.hash)}</span>
                </div>
              );
            })}
          </div>
        )}
      </section>
    </main>
  );
}
