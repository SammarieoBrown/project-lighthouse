"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { LighthouseMark } from "../logo";
import {
  CREDENTIAL_LIFETIME_MS,
  credentialIsDead,
  jsonOrDetail,
} from "./credential";
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
  transcript: string | null;
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

type DonationPool = {
  pool_id: string;
  name: string;
  balance: string;
  simulated: boolean;
};

type LoadState = "locked" | "loading" | "ready" | "error";

/* J$ rather than a bare "$". `Intl` with en-JM renders JMD as "$45,000",
 * which on a screen that also discusses insurers and donor pools could be
 * read as US dollars — and the prose elsewhere on this screen already writes
 * J$45,000, so the figure and the sentence about it disagreed. */
const jmd = new Intl.NumberFormat("en-JM", { maximumFractionDigits: 0 });
const money = { format: (value: number) => `J$${jmd.format(value)}` };
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

export function ReliefOperations() {
  const [claims, setClaims] = useState<Claim[]>([]);
  const [ledger, setLedger] = useState<LedgerEntry[]>([]);
  const [claimsState, setClaimsState] = useState<LoadState>("loading");
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
  const [credentialExpiry, setCredentialExpiry] = useState<number | null>(null);
  const [credentialNotice, setCredentialNotice] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [pools, setPools] = useState<DonationPool[]>([]);
  const [payerChoice, setPayerChoice] = useState<string>("GOV_RELIEF");
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

  /* One way out of an open credential, taken by the expiry timer, by any 401
   * the API answers, and by Sign out. The queue itself stays readable — it
   * belongs to the signed-in session; only the power to decide expires. */
  const closeCredential = useCallback((notice: string | null) => {
    setActiveToken("");
    setCredentialExpiry(null);
    setCredentialNotice(notice);
  }, []);

  useEffect(() => {
    if (!credentialExpiry) return;
    const remaining = credentialExpiry - Date.now();
    const close = () => closeCredential(
      "Your credential expired after five minutes. Confirm your password to approve again.",
    );
    if (remaining <= 0) {
      close();
      return;
    }
    const timer = window.setTimeout(close, remaining);
    return () => window.clearTimeout(timer);
  }, [credentialExpiry, closeCredential]);

  /* The queue reads on the eight-hour session cookie — the proxy forwards it,
   * so no credential header is involved until someone decides something. */
  const loadClaims = useCallback(async () => {
    const requestId = ++claimsRequest.current;
    setClaimsState((current) => (current === "ready" ? current : "loading"));
    setClaimsError(null);
    try {
      const response = await fetch("/api/lighthouse/api/claims?limit=100", {
        cache: "no-store",
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
  }, []);

  /* Pool balances are public by design (DON-02); the selector needs them to
   * say which pool can actually cover a grant before the API refuses one. */
  const loadPools = useCallback(async () => {
    try {
      const response = await fetch("/api/lighthouse/v1/public/pools", { cache: "no-store" });
      const body = (await jsonOrDetail(response)) as { pools?: DonationPool[] };
      setPools(Array.isArray(body.pools) ? body.pools : []);
    } catch {
      setPools([]);
    }
  }, []);

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
    await Promise.all([loadClaims(), loadLedger(), loadPools()]);
  }, [loadClaims, loadLedger, loadPools]);

  useEffect(() => {
    void loadClaims();
    void loadLedger();
    void loadPools();
    const timer = window.setInterval(() => void refresh(), 15_000);
    return () => window.clearInterval(timer);
  }, [loadClaims, loadLedger, loadPools, refresh]);

  useEffect(() => {
    if (!selectedId) {
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
        if (controller.signal.aborted) return;
        setClaimDetail(null);
        setDetailState("error");
        setDetailError(error instanceof Error ? error.message : "Claim evidence is unavailable.");
      });
    return () => controller.abort();
  }, [selectedId, detailRefresh]);

  /* Photo evidence arrives as bytes from an authenticated route; the session
   * cookie rides along on the same-origin fetch, and the object URLs live
   * exactly as long as the detail is on screen. */
  const [photoUrls, setPhotoUrls] = useState<Record<string, string>>({});
  useEffect(() => {
    setPhotoUrls({});
    if (!claimDetail) return;
    const photos = claimDetail.evidence.filter(
      (item) => item.kind === "PHOTO" && item.has_uri,
    );
    if (photos.length === 0) return;
    const controller = new AbortController();
    const created: string[] = [];
    void Promise.all(
      photos.map(async (item) => {
        const response = await fetch(
          `/api/lighthouse/api/claims/${encodeURIComponent(claimDetail.id)}/evidence/${encodeURIComponent(item.id)}/media`,
          {
            cache: "no-store",
            signal: controller.signal,
          },
        );
        if (!response.ok) return null;
        const url = URL.createObjectURL(await response.blob());
        created.push(url);
        return [item.id, url] as const;
      }),
    ).then((entries) => {
      if (controller.signal.aborted) return;
      setPhotoUrls(Object.fromEntries(entries.filter(Boolean) as Array<readonly [string, string]>));
    }).catch(() => {
      /* A missing thumbnail is not an error state; the evidence count and
       * media integrity signal still tell the clerk media exists. */
    });
    return () => {
      controller.abort();
      for (const url of created) URL.revokeObjectURL(url);
    };
  }, [claimDetail]);

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
  const metricReason = claimsState === "loading" ? "reading" : "unavailable";

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
        payer_route: payerChoice === "GOV_RELIEF" ? "GOV_RELIEF" : "DONOR_POOL",
        pool_id: payerChoice === "GOV_RELIEF" ? undefined : payerChoice,
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
      await Promise.all([loadLedger(), loadPools()]);
    } catch (error) {
      setApprovalError(error instanceof Error ? error.message : "Approval failed.");
      if (credentialIsDead(error)) {
        closeCredential("Your credential expired. Confirm your password to continue.");
      }
    } finally {
      setApproving(false);
    }
  }, [selected, approvalReady, activeToken, note, payerChoice, loadLedger, loadPools, closeCredential]);

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
      if (credentialIsDead(error)) {
        closeCredential("Your credential expired. Confirm your password to continue.");
      }
    } finally {
      setDeciding(false);
    }
  }, [selected, activeToken, claimDetail, damageNote, loadLedger, closeCredential]);

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
        `${verdict === "APPROVED" ? "Claim verified" : "Claim rejected"} by your review`
        + (result.idempotent_replay ? " · existing decision replayed safely" : " · immutable decision recorded"),
      );
      await loadClaims();
      setDetailRefresh((value) => value + 1);
    } catch (error) {
      setReviewError(error instanceof Error ? error.message : "Review decision failed.");
      if (credentialIsDead(error)) {
        closeCredential("Your credential expired. Confirm your password to continue.");
      }
    } finally {
      setReviewing(false);
    }
  }, [activeToken, claimDetail, closeCredential, loadClaims, reviewNote, reviewReady, selected]);

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
                closeCredential(null);
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
      <main className={styles.screen} data-theme="light">
        {chrome}
        {sessionState.status === "loading" ? (
          <p className={styles.empty}>Checking your session…</p>
        ) : (
          <div className={styles.signInScreen}>
          <SignIn onSignIn={signIn} />
        </div>
        )}
      </main>
    );
  }

  return (
    <main className={styles.screen} data-theme="light">
      {chrome}

      <section className={styles.metrics} aria-label="Relief operation measures">
        {/* An em-dash on its own reads as broken. These figures are absent for
            a reason the operator can act on — the queue is locked until a
            credential is presented — so the reason is on screen beside the
            dash rather than left to be inferred (rule C4). */}
        <div>
          <strong>{claimsState === "ready" ? claims.length : "—"}</strong>
          <span>Redacted claims received</span>
          {claimsState !== "ready" ? (
            <small className={styles.metricWhy}>{metricReason}</small>
          ) : null}
        </div>
        <div>
          <strong>{claimsState === "ready" ? verified : "—"}</strong>
          <span>Verified · eligible for allocation</span>
          {claimsState !== "ready" ? (
            <small className={styles.metricWhy}>{metricReason}</small>
          ) : null}
        </div>
        <div>
          <strong data-alert={safetyOfLife > 0 ? "true" : undefined}>
            {claimsState === "ready" ? safetyOfLife : "—"}
          </strong>
          <span>Safety-of-life priority</span>
          {claimsState !== "ready" ? (
            <small className={styles.metricWhy}>{metricReason}</small>
          ) : null}
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

          {claimsState === "error" ? (
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
                    {claim.transcript ? (
                      <small className={styles.messageLine}>“{claim.transcript}”</small>
                    ) : null}
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
            Operator view: message text and photo evidence are shown to signed-in operators.
            Phone numbers, names, media URLs, and raw provider payloads never leave the API.
          </p>
        </section>

        <aside className={styles.approval}>
          <span className={styles.eyebrow}>Act 3 · human gate</span>
          <h2>Approve relief allocation</h2>
          {activeToken ? (
            /* An open credential is a state, not a pending action. The field
             * and the filled button leave once they have done their job. The
             * time is the one the console itself set, not a claim about a
             * server it cannot see. */
            <p className={styles.credentialLine}>
              <span>
                Credential active · expires
                {" "}
                <span className={styles.data}>
                  {credentialExpiry
                    ? new Date(credentialExpiry).toLocaleTimeString([], {
                      hour: "2-digit",
                      minute: "2-digit",
                    })
                    : "in five minutes"}
                </span>
              </span>
              <button
                type="button"
                disabled={claimsState === "loading" || approving}
                onClick={() => void loadClaims()}
              >
                {claimsState === "loading" ? "Reading…" : "Refresh queue"}
              </button>
            </p>
          ) : (
            <>
              {credentialNotice ? (
                <p className={styles.credentialNotice} role="status">{credentialNotice}</p>
              ) : null}
              <label className={styles.field}>
                <span>Your password · signs your approval decisions</span>
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
                className={styles.approveButton}
                disabled={!operatorPassword || approving}
                onClick={async () => {
                  setStepUpError(null);
                  try {
                    const token = await stepUp(operatorPassword);
                    setOperatorPassword("");
                    setCredentialNotice(null);
                    setActiveToken(token);
                    setCredentialExpiry(Date.now() + CREDENTIAL_LIFETIME_MS);
                  } catch (failure) {
                    setStepUpError(
                      failure instanceof Error ? failure.message : "Could not confirm your password.",
                    );
                  }
                }}
              >
                Confirm password
              </button>
              <p className={styles.noMovement}>
                The credential stays in this tab&apos;s memory only and expires after five minutes.
              </p>
            </>
          )}
          {stepUpError ? (
            <p className={styles.signInError} role="alert">{stepUpError}</p>
          ) : null}
          {selected ? (
            <>
              <dl className={styles.selection}>
                <div><dt>Claim</dt><dd>{selected.claim_ref}</dd></div>
                <div><dt>Eligibility</dt><dd>{selected.status}</dd></div>
                <div><dt>Resource</dt><dd>{money.format(45_000)} cash grant</dd></div>
                <div>
                  <dt><label htmlFor="payer-route">Payer</label></dt>
                  <dd>
                    <select
                      id="payer-route"
                      className={styles.payerSelect}
                      value={payerChoice}
                      disabled={approving}
                      onChange={(event) => setPayerChoice(event.target.value)}
                    >
                      <option value="GOV_RELIEF">Government relief</option>
                      {pools.map((pool) => {
                        const balance = Number.parseFloat(pool.balance);
                        const short = Number.isFinite(balance) && balance < 45_000;
                        return (
                          <option key={pool.pool_id} value={pool.pool_id} disabled={short}>
                            {pool.name} · {money.format(Number.isFinite(balance) ? balance : 0)}
                            {short ? " · below grant" : ""}
                          </option>
                        );
                      })}
                    </select>
                  </dd>
                </div>
              </dl>
              {selected.transcript ? (
                <div className={styles.householdMessage}>
                  <span>Household message</span>
                  <p>{selected.transcript}</p>
                </div>
              ) : null}
              {Object.keys(photoUrls).length > 0 ? (
                <div className={styles.photoEvidence}>
                  <span>Photo evidence</span>
                  <div>
                    {Object.entries(photoUrls).map(([evidenceId, url], index) => (
                      // eslint-disable-next-line @next/next/no-img-element -- blob
                      // URLs from the authenticated media route; next/image
                      // cannot optimise them and must not proxy them.
                      <img key={evidenceId} src={url} alt={`Photo evidence ${index + 1}`} />
                    ))}
                  </div>
                </div>
              ) : null}
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
                  <span className={styles.eyebrow}>Act 2 · verification review</span>
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
                        A range, not a figure. The estimate does not size the grant: relief is a
                        flat J$45,000, and nothing is released by this decision.
                      </p>
                      <label className={styles.field}>
                        <span>Decision reason · required</span>
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
                          disabled={deciding || !activeToken || damageNote.trim().length < 10}
                          onClick={() => void decideDamage("APPROVED")}
                        >
                          {deciding ? "Recording\u2026" : "Approve estimate"}
                        </button>
                        <button
                          type="button"
                          className={`${styles.approveButton} ${styles.rejectButton}`}
                          disabled={deciding || !activeToken || damageNote.trim().length < 10}
                          onClick={() => void decideDamage("REJECTED")}
                        >
                          Reject estimate
                        </button>
                      </div>
                      {!activeToken ? (
                        <p className={styles.noMovement}>
                          Confirm your password above to record this decision.
                        </p>
                      ) : null}
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
