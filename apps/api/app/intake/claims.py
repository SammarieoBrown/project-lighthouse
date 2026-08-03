"""Authenticated, redacted claim operations read API.

Even parish/community claim rows can re-identify a household when volume is
low.  These routes therefore require the same short-lived human credential as
approval actions and never return phones, names, transcripts, provider forms,
media URIs, or raw evidence payloads.
"""

from __future__ import annotations

import math
import uuid

from fastapi import APIRouter, Header, HTTPException, Query, Response, status
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from lighthouse_contracts import AgentName, AppRole, ClaimStatus, JobStatus, Verdict

from app.db import session_scope
from app.human_auth import authenticate_human
from app.models import AgentJob, Claim, Verification

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
        return "FAILED"
    return {
        JobStatus.QUEUED: "QUEUED",
        JobStatus.RUNNING: "RUNNING",
        JobStatus.DONE: "COMPLETED",
        JobStatus.FAILED: "FAILED",
        JobStatus.DEAD: "FAILED",
    }[job.status]


def _summary(session: Session, row) -> dict:
    claim = session.get(Claim, row.id)
    if claim is None:  # Same transaction; only defensive.
        raise RuntimeError("claim disappeared while serialising")
    return {
        "id": row.id,
        "claim_ref": row.claim_ref,
        "status": str(row.status),
        "verification_state": _verification_state(session, claim),
        "damage_type": row.damage_type,
        "reported_needs": list(row.reported_needs or []),
        "parish": row.parish,
        "community": row.community,
        "sol": row.sol,
        "partial": row.partial,
        "channel": row.channel,
        "filed_at": row.filed_at,
        "evidence_count": row.evidence_count,
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
                   c.channel, c.filed_at,
                   (SELECT count(*) FROM evidence e WHERE e.claim_id = c.id)::int
                     AS evidence_count
            FROM claim c
            JOIN storm_file sf ON sf.id = c.storm_file_id
            WHERE (CAST(:event_id AS uuid) IS NULL
                   OR c.hazard_event_id = CAST(:event_id AS uuid))
            ORDER BY c.sol DESC, c.filed_at DESC, c.id
            LIMIT :limit
            """
        ),
        {"event_id": hazard_event_id, "limit": limit},
    ).all()
    return [_summary(session, row) for row in rows]


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
            safe[name] = signal
    return safe


def get_redacted_claim(session: Session, claim_id: uuid.UUID) -> dict | None:
    row = session.execute(
        text(
            """
            SELECT c.id, c.claim_ref, c.status::text AS status, c.damage_type,
                   c.reported_needs, sf.parish, sf.community, c.sol, c.partial,
                   c.channel, c.filed_at,
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
            "confidence": float(latest.confidence),
            "verdict": str(latest.verdict),
            "signals": _safe_signals(latest.signals),
            "created_at": latest.created_at,
        }
        if latest is not None
        else None
    )
    return detail


@router.get("/claims")
def list_claims_route(
    response: Response,
    authorization: str | None = Header(default=None),
    hazard_event_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    with session_scope() as session:
        authenticate_human(session, authorization, allowed_roles=_CLAIM_ROLES)
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
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    with session_scope() as session:
        authenticate_human(session, authorization, allowed_roles=_CLAIM_ROLES)
        detail = get_redacted_claim(session, claim_id)
        if detail is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="claim not found")
        return detail


__all__ = [
    "get_redacted_claim",
    "list_redacted_claims",
    "router",
]
