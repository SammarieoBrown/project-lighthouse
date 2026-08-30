"""Authenticated claim operations read API.

Even parish/community claim rows can re-identify a household when volume is
low.  These routes therefore require the same short-lived human credential as
approval actions and never return phones, names, provider forms, media URIs,
or raw evidence payloads.

Revised 2026-08-30, deliberately: the operator view now carries the
household's message text (``transcript``) and serves photo/audio evidence
bytes through an authenticated route, because a clerk reviewing a claim needs
to read what the household actually said.  The public portal keeps the full
redaction — nothing here is reachable without an operator credential.
"""

from __future__ import annotations

import math
import re
import uuid

from fastapi import APIRouter, Cookie, Header, HTTPException, Query, Response, status
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from lighthouse_contracts import (
    ActorKind,
    AgentName,
    AppRole,
    ClaimStatus,
    JobStatus,
    PayerRoute,
    Verdict,
)

from app.auth_session import read_session
from app.config import get_settings
from app.db import session_scope
from app.human_auth import authenticate_human
from app.intake.media import (
    TWILIO_MEDIA_JOB_TYPE,
    MediaBoundaryError,
    MediaConfigurationError,
    R2MediaStore,
)
from app.models import (
    AgentJob,
    Claim,
    DamageAssessment,
    RoutingDecision,
    Verification,
)

router = APIRouter(prefix="/api", tags=["claims"])
_CLAIM_ROLES = {AppRole.DIRECTOR, AppRole.REVIEW_CLERK}
_VERIFICATION_SIGNAL_NAMES = frozenset(
    {
        "hazard_sufficiency",
        "satellite_change",
        "neighbour_corroboration",
        "registry_match",
        "media_integrity",
    }
)
_SAFE_SIGNAL_EVIDENCE_FIELDS = frozenset(
    {
        "source",
        "source_count",
        "observed_advisories_evaluated",
        "highest_wind_band_kt",
        "cloud_fraction",
        "radius_metres",
        "independent_households",
        "matching_damage_households",
        "synthetic_provenance",
        "damage_category",
        "matched_component",
        "media_count",
        "ready_count",
        "pending_reasons",
        "content_hash_count",
        "perceptual_hash_count",
        "forensic_processor_count",
        "selected",
    }
)
_SAFE_REASON = re.compile(r"^[a-z0-9_]{1,64}$")


def _verification_state(session: Session, claim: Claim) -> str:
    if claim.status in {ClaimStatus.VERIFIED, ClaimStatus.SETTLED}:
        return "VERIFIED"

    latest = session.scalar(
        select(Verification)
        .where(Verification.claim_id == claim.id)
        .order_by(Verification.created_at.desc(), Verification.id.desc())
        .limit(1)
    )
    if latest is not None:
        if latest.verdict in {Verdict.AUTO_VERIFIED, Verdict.APPROVED}:
            return "VERIFIED"
        if latest.verdict is Verdict.REVIEW:
            return "REVIEW"
        if latest.verdict in {Verdict.FLAGGED, Verdict.REJECTED}:
            return "FLAGGED"
        return "COMPLETED"

    job = session.scalar(
        select(AgentJob)
        .where(
            AgentJob.job_type == str(AgentName.VERIFICATION_AGENT),
            AgentJob.payload["claim_id"].astext == str(claim.id),
        )
        .order_by(AgentJob.created_at.desc(), AgentJob.id.desc())
        .limit(1)
    )
    if job is None:
        media_job = session.scalar(
            select(AgentJob)
            .where(
                AgentJob.job_type == TWILIO_MEDIA_JOB_TYPE,
                AgentJob.payload["claim_id"].astext == str(claim.id),
            )
            .order_by(AgentJob.created_at.desc(), AgentJob.id.desc())
            .limit(1)
        )
        if media_job is None:
            return "FAILED"
        return {
            JobStatus.QUEUED: "MEDIA_PENDING",
            JobStatus.RUNNING: "MEDIA_PROCESSING",
            JobStatus.DONE: "MEDIA_FAILED",
            JobStatus.FAILED: "MEDIA_FAILED",
            JobStatus.DEAD: "MEDIA_FAILED",
        }[media_job.status]
    return {
        JobStatus.QUEUED: "QUEUED",
        JobStatus.RUNNING: "RUNNING",
        JobStatus.DONE: "COMPLETED",
        JobStatus.FAILED: "FAILED",
        JobStatus.DEAD: "FAILED",
    }[job.status]


def _summary(session: Session, row) -> dict:
    if "verification_state" in row._mapping:
        verification_state = row.verification_state
    else:
        claim = session.get(Claim, row.id)
        if claim is None:  # Same transaction; only defensive.
            raise RuntimeError("claim disappeared while serialising")
        verification_state = _verification_state(session, claim)
    return {
        "id": row.id,
        "claim_ref": row.claim_ref,
        "status": str(row.status),
        "verification_state": verification_state,
        "damage_type": row.damage_type,
        "reported_needs": list(row.reported_needs or []),
        "parish": row.parish,
        "community": row.community,
        "sol": row.sol,
        # TRI-02: the queue is live-sorted on the console and SOL pins to the
        # top regardless. The Triage Agent computes both of these and nothing
        # was carrying them out to the screen that needs them.
        "severity": str(row.severity) if row.severity else None,
        "triage_rank": row.triage_rank,
        "partial": row.partial,
        "channel": row.channel,
        "filed_at": row.filed_at,
        "evidence_count": row.evidence_count,
        "transcript": row.transcript,
        "hazard_event_id": row.hazard_event_id,
    }


def list_redacted_claims(
    session: Session,
    *,
    hazard_event_id: uuid.UUID | None = None,
    limit: int = 100,
) -> list[dict]:
    rows = session.execute(
        text(
            """
            SELECT c.id, c.claim_ref, c.status::text AS status, c.damage_type,
                   c.reported_needs, sf.parish, sf.community, c.sol, c.partial,
                   c.severity, c.triage_rank, c.hazard_event_id,
                   c.channel, c.filed_at, c.transcript,
                   (SELECT count(*) FROM evidence e WHERE e.claim_id = c.id)::int
                     AS evidence_count,
                   CASE
                     WHEN c.status::text IN ('VERIFIED', 'SETTLED') THEN 'VERIFIED'
                     WHEN latest_verification.verdict IN ('AUTO_VERIFIED', 'APPROVED')
                       THEN 'VERIFIED'
                     WHEN latest_verification.verdict = 'REVIEW' THEN 'REVIEW'
                     WHEN latest_verification.verdict IN ('FLAGGED', 'REJECTED')
                       THEN 'FLAGGED'
                     WHEN latest_verification.id IS NOT NULL THEN 'COMPLETED'
                     WHEN verification_job.status = 'QUEUED' THEN 'QUEUED'
                     WHEN verification_job.status = 'RUNNING' THEN 'RUNNING'
                     WHEN verification_job.status = 'DONE' THEN 'COMPLETED'
                     WHEN verification_job.id IS NOT NULL THEN 'FAILED'
                     WHEN media_job.status = 'QUEUED' THEN 'MEDIA_PENDING'
                     WHEN media_job.status = 'RUNNING' THEN 'MEDIA_PROCESSING'
                     ELSE 'FAILED'
                   END AS verification_state
            FROM claim c
            JOIN storm_file sf ON sf.id = c.storm_file_id
            LEFT JOIN LATERAL (
              SELECT v.id, v.verdict::text AS verdict
                FROM verification v
               WHERE v.claim_id = c.id
               ORDER BY v.created_at DESC, v.id DESC
               LIMIT 1
            ) latest_verification ON TRUE
            LEFT JOIN LATERAL (
              SELECT j.id, j.status::text AS status
                FROM agent_job j
               WHERE j.job_type = :verification_job_type
                 AND j.payload->>'claim_id' = c.id::text
               ORDER BY j.created_at DESC, j.id DESC
               LIMIT 1
            ) verification_job ON TRUE
            LEFT JOIN LATERAL (
              SELECT j.id, j.status::text AS status
                FROM agent_job j
               WHERE j.job_type = :media_job_type
                 AND j.payload->>'claim_id' = c.id::text
               ORDER BY j.created_at DESC, j.id DESC
               LIMIT 1
            ) media_job ON TRUE
            WHERE (CAST(:event_id AS uuid) IS NULL
                   OR c.hazard_event_id = CAST(:event_id AS uuid))
            -- TRI-02's order, computed once here rather than in the browser:
            -- safety-of-life first, then the rank the Triage Agent assigned,
            -- then newest. A claim triage has not reached yet sorts after the
            -- ones it has rather than jumping the queue on recency alone.
            ORDER BY c.sol DESC, c.triage_rank ASC NULLS LAST,
                     c.filed_at DESC, c.id
            LIMIT :limit
            """
        ),
        {
            "event_id": hazard_event_id,
            "limit": limit,
            "verification_job_type": str(AgentName.VERIFICATION_AGENT),
            "media_job_type": TWILIO_MEDIA_JOB_TYPE,
        },
    ).all()
    return [_summary(session, row) for row in rows]


def _safe_signal_evidence(value: object, *, depth: int = 0) -> dict:
    """Expose decision context while withholding household and evidence IDs."""
    if not isinstance(value, dict) or depth > 1:
        return {}
    safe: dict[str, object] = {}
    for name, raw in value.items():
        if name not in _SAFE_SIGNAL_EVIDENCE_FIELDS:
            continue
        if name == "selected":
            nested = _safe_signal_evidence(raw, depth=depth + 1)
            if nested:
                safe[name] = nested
        elif isinstance(raw, bool):
            safe[name] = raw
        elif isinstance(raw, int) and not isinstance(raw, bool) and abs(raw) <= 1_000_000:
            safe[name] = raw
        elif isinstance(raw, float) and math.isfinite(raw) and abs(raw) <= 1_000_000:
            safe[name] = raw
        elif isinstance(raw, str) and len(raw) <= 120:
            safe[name] = raw
        elif name == "pending_reasons" and isinstance(raw, list):
            safe[name] = [
                reason for reason in raw[:10]
                if isinstance(reason, str) and _SAFE_REASON.fullmatch(reason)
            ]
    return safe


def _safe_signals(signals: object) -> dict:
    """Expose only valid public-safe fields for the frozen five signals."""
    if not isinstance(signals, dict):
        return {}
    safe: dict[str, dict] = {}
    for name, value in signals.items():
        if name not in _VERIFICATION_SIGNAL_NAMES or not isinstance(value, dict):
            continue
        signal: dict[str, object] = {}
        if isinstance(value.get("present"), bool):
            signal["present"] = value["present"]
        score = value.get("score")
        if (
            isinstance(score, (int, float))
            and not isinstance(score, bool)
            and math.isfinite(float(score))
            and 0.0 <= float(score) <= 1.0
        ):
            signal["score"] = float(score)
        if signal:
            note = value.get("note")
            if isinstance(note, str) and 0 < len(note) <= 240:
                signal["note"] = note
            evidence = _safe_signal_evidence(value.get("evidence"))
            if evidence:
                signal["evidence"] = evidence
            safe[name] = signal
    return safe


def get_redacted_claim(session: Session, claim_id: uuid.UUID) -> dict | None:
    row = session.execute(
        text(
            """
            SELECT c.id, c.claim_ref, c.status::text AS status, c.damage_type,
                   c.reported_needs, sf.parish, sf.community, c.sol, c.partial,
                   c.severity, c.triage_rank, c.hazard_event_id,
                   c.channel, c.filed_at, c.transcript,
                   (SELECT count(*) FROM evidence e WHERE e.claim_id = c.id)::int
                     AS evidence_count
            FROM claim c
            JOIN storm_file sf ON sf.id = c.storm_file_id
            WHERE c.id = :claim_id
            """
        ),
        {"claim_id": claim_id},
    ).one_or_none()
    if row is None:
        return None

    detail = _summary(session, row)
    evidence_rows = session.execute(
        text(
            """
            SELECT id, kind::text AS kind, created_at, (uri IS NOT NULL) AS has_uri,
                   sha256
            FROM evidence WHERE claim_id = :claim_id
            ORDER BY created_at, id
            """
        ),
        {"claim_id": claim_id},
    ).all()
    detail["evidence"] = [
        {
            "id": evidence.id,
            "kind": evidence.kind,
            "created_at": evidence.created_at,
            "has_uri": evidence.has_uri,
            "sha256": evidence.sha256,
        }
        for evidence in evidence_rows
    ]

    latest = session.scalar(
        select(Verification)
        .where(Verification.claim_id == claim_id)
        .order_by(Verification.created_at.desc(), Verification.id.desc())
        .limit(1)
    )
    detail["verification"] = (
        {
            "id": latest.id,
            "confidence": float(latest.confidence),
            "verdict": str(latest.verdict),
            "capped": latest.capped,
            "signals": _safe_signals(latest.signals),
            "created_at": latest.created_at,
        }
        if latest is not None
        else None
    )

    # The standing damage estimate, so a Director can act on it without
    # leaving the claim. Bands and a range only — the figure is a proposal
    # until a Director signs it, and the row says which.
    assessment = session.scalar(
        select(DamageAssessment)
        .where(DamageAssessment.claim_id == claim_id)
        .order_by(DamageAssessment.created_at.desc(), DamageAssessment.id.desc())
        .limit(1)
    )
    detail["damage_assessment"] = (
        {
            "id": assessment.id,
            "verdict": str(assessment.verdict),
            "band": str(assessment.band),
            "estimate_low": float(assessment.estimate_low),
            "estimate_high": float(assessment.estimate_high),
            "currency": assessment.currency,
            "confidence": float(assessment.confidence),
            "rationale": assessment.rationale,
            "evidence_count": len(assessment.evidence_ids or []),
            "decided": assessment.actor_kind is ActorKind.HUMAN,
            "created_at": assessment.created_at,
        }
        if assessment is not None
        else None
    )

    # Who pays, and whether an FNOL packet exists to fetch. The insurer is
    # named because a named third party may receive this household's claim.
    routing = session.scalar(
        select(RoutingDecision)
        .where(RoutingDecision.claim_id == claim_id)
        .order_by(RoutingDecision.decided_at.desc(), RoutingDecision.id.desc())
        .limit(1)
    )
    detail["routing"] = (
        {
            "route": str(routing.route),
            "insurer_name": routing.insurer_name,
            "fnol_available": routing.route in {PayerRoute.INSURER, PayerRoute.BOTH},
            "decided_at": routing.decided_at,
        }
        if routing is not None
        else None
    )
    return detail


def _authenticate_reader(
    session, authorization: str | None, lh_session: str | None
) -> None:
    """An eight-hour shift cookie or a step-up bearer both open the reads.

    This is the split ``auth_session`` documents: signing in opens the queues
    a role permits, and only approving or signing re-proves the password. A
    supplied bearer is still validated strictly — presenting a bad credential
    never falls back to the cookie.
    """
    if authorization:
        authenticate_human(session, authorization, allowed_roles=_CLAIM_ROLES)
        return
    user = read_session(session, lh_session)
    if user.role not in _CLAIM_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="your role cannot read the claim queue",
        )


@router.get("/claims")
def list_claims_route(
    response: Response,
    authorization: str | None = Header(default=None),
    lh_session: str | None = Cookie(default=None),
    hazard_event_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    with session_scope() as session:
        _authenticate_reader(session, authorization, lh_session)
        return {
            "claims": list_redacted_claims(
                session,
                hazard_event_id=hazard_event_id,
                limit=limit,
            )
        }


@router.get("/claims/{claim_id}")
def claim_detail_route(
    claim_id: uuid.UUID,
    response: Response,
    authorization: str | None = Header(default=None),
    lh_session: str | None = Cookie(default=None),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    with session_scope() as session:
        _authenticate_reader(session, authorization, lh_session)
        detail = get_redacted_claim(session, claim_id)
        if detail is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="claim not found")
        return detail


def claim_agent_timeline(session: Session, claim_id: uuid.UUID) -> list[dict]:
    """Every agent's work on one claim, oldest first.

    Four sources, one order: the jobs the workers ran, the verification rows
    they wrote, the damage assessments they proposed, and the ledger entries
    that record what each decision meant. Read together they are the account
    of how a claim reached its current state — including the runs that failed
    and the decisions an agent declined to make.
    """
    events: list[dict] = []

    jobs = session.execute(
        text(
            """
            SELECT job_type, status::text AS status, attempts, last_error,
                   created_at, finished_at, payload
              FROM agent_job
             WHERE payload->>'claim_id' = :claim_id
             ORDER BY created_at, id
            """
        ),
        {"claim_id": str(claim_id)},
    ).all()
    for job in jobs:
        events.append(
            {
                "at": job.finished_at or job.created_at,
                "source": "job",
                "actor": job.job_type,
                "title": job.job_type.replace("_", " "),
                "state": job.status,
                "detail": (job.last_error or "")[:300] or None,
                "data": {
                    "attempts": job.attempts,
                    "queued_at": job.created_at,
                    "trigger": (job.payload or {}).get("trigger"),
                },
            }
        )

    verifications = session.scalars(
        select(Verification)
        .where(Verification.claim_id == claim_id)
        .order_by(Verification.created_at, Verification.id)
    ).all()
    for row in verifications:
        signals = _safe_signals(row.signals)
        scored = [name for name, value in signals.items() if value.get("present")]
        events.append(
            {
                "at": row.created_at,
                "source": "verification",
                "actor": row.agent_name or "review clerk",
                "title": f"verification · {str(row.verdict).replace('_', ' ').lower()}",
                "state": str(row.verdict),
                "detail": row.rationale,
                "data": {
                    "confidence": float(row.confidence),
                    "capped": row.capped,
                    "signals_scored": len(scored),
                    "signals": signals,
                    "actor_kind": str(row.actor_kind),
                    "overrides": str(row.overrides_id) if row.overrides_id else None,
                },
            }
        )

    assessments = session.scalars(
        select(DamageAssessment)
        .where(DamageAssessment.claim_id == claim_id)
        .order_by(DamageAssessment.created_at, DamageAssessment.id)
    ).all()
    for row in assessments:
        findings = row.findings if isinstance(row.findings, list) else []
        events.append(
            {
                "at": row.created_at,
                "source": "damage_assessment",
                "actor": row.agent_name or "director",
                "title": f"damage assessment · {str(row.verdict).lower()}",
                "state": str(row.verdict),
                "detail": row.rationale,
                "data": {
                    "band": str(row.band),
                    "estimate_low": float(row.estimate_low),
                    "estimate_high": float(row.estimate_high),
                    "currency": row.currency,
                    "confidence": float(row.confidence),
                    "model_version": row.model_version,
                    "photos_read": len(row.evidence_ids or []),
                    "findings": [
                        {
                            "observed_damage": str(item.get("observed_damage") or ""),
                            "band": str(item.get("band") or ""),
                            "confidence": item.get("confidence"),
                        }
                        for item in findings
                        if isinstance(item, dict)
                    ],
                },
            }
        )

    entries = session.execute(
        text(
            """
            SELECT seq, action, agent_name, actor_kind::text AS actor_kind,
                   ts, payload
              FROM ledger_entry
             WHERE (subject_type = 'claim' AND subject_id = :claim_id)
                OR payload->>'claim_id' = :claim_id_text
             ORDER BY seq
            """
        ),
        {"claim_id": claim_id, "claim_id_text": str(claim_id)},
    ).all()
    for entry in entries:
        payload = dict(entry.payload or {})
        # The ledger carries operational detail that is safe to show an
        # operator, but it is not a place to re-export household text.
        detail = payload.get("reason") or payload.get("note")
        events.append(
            {
                "at": entry.ts,
                "source": "ledger",
                "actor": entry.agent_name or str(entry.actor_kind).lower(),
                "title": str(entry.action).replace("_", " ").replace(".", " · "),
                "state": None,
                "detail": detail if isinstance(detail, str) else None,
                "data": {
                    "seq": entry.seq,
                    "amount": payload.get("amount"),
                    "currency": payload.get("currency"),
                    "payer_route": payload.get("payer_route"),
                    "severity": payload.get("severity"),
                    "rank": payload.get("rank"),
                    "policy_id": payload.get("policy_id"),
                },
            }
        )

    events.sort(key=lambda event: (event["at"] is None, event["at"]))
    return events


@router.get("/claims/{claim_id}/timeline")
def claim_timeline_route(
    claim_id: uuid.UUID,
    response: Response,
    authorization: str | None = Header(default=None),
    lh_session: str | None = Cookie(default=None),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    with session_scope() as session:
        _authenticate_reader(session, authorization, lh_session)
        if session.get(Claim, claim_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="claim not found"
            )
        return {"events": claim_agent_timeline(session, claim_id)}


@router.get("/claims/{claim_id}/evidence/{evidence_id}/media")
def claim_evidence_media_route(
    claim_id: uuid.UUID,
    evidence_id: uuid.UUID,
    authorization: str | None = Header(default=None),
    lh_session: str | None = Cookie(default=None),
) -> Response:
    """Serve one stored photo or voice note to a signed-in operator.

    The R2 URI itself still never leaves the API; the bytes are read back
    through the same digest check the worker wrote them with, so a mutated
    object serves a 502 rather than a wrong image.
    """
    with session_scope() as session:
        _authenticate_reader(session, authorization, lh_session)
        row = session.execute(
            text(
                """
                SELECT uri, sha256, kind::text AS kind
                FROM evidence
                WHERE id = :evidence_id AND claim_id = :claim_id
                """
            ),
            {"evidence_id": evidence_id, "claim_id": claim_id},
        ).one_or_none()
    if (
        row is None
        or row.kind not in {"PHOTO", "AUDIO"}
        or not row.uri
        or not row.sha256
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="media not found")

    try:
        store = R2MediaStore.from_settings(get_settings())
    except MediaConfigurationError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="media store is not configured",
        ) from None

    prefix = f"r2://{store.bucket}/"
    if not str(row.uri).startswith(prefix):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="media not found")

    try:
        media = store.get(str(row.uri)[len(prefix):], expected_sha256=str(row.sha256))
    except MediaBoundaryError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="stored media failed integrity verification",
        ) from None
    except Exception:
        # An object-store failure must not become a 500 with a stack trace; the
        # operator needs "media unavailable", and the URI stays unlogged.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="stored media is unavailable",
        ) from None

    return Response(
        content=media.data,
        media_type=media.content_type or "application/octet-stream",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
        },
    )


__all__ = [
    "get_redacted_claim",
    "list_redacted_claims",
    "router",
]
