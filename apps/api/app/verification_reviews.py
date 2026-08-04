"""Authenticated Review Clerk adjudication for low-confidence claims."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from lighthouse_contracts import AppRole, Verdict

from .db import session_scope
from .human_auth import authenticate_human
from .models import Claim, StormFile, Verification
from .verification_service import (
    ClaimNotFound,
    ReviewDecisionConflict,
    VerificationRun,
    record_review_decision,
)

router = APIRouter(prefix="/v1", tags=["verification-review"])


class ReviewDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verification_id: uuid.UUID
    verdict: Literal["APPROVED", "REJECTED"]
    rationale: str = Field(min_length=10, max_length=500)

    @field_validator("rationale")
    @classmethod
    def normalize_rationale(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 10:
            raise ValueError("review rationale must contain at least 10 characters")
        return normalized


class ReviewDecisionResponse(BaseModel):
    verification: dict
    claim: dict
    storm_file: dict
    money_movement: dict
    idempotent_replay: bool


def _response(
    session: Session,
    outcome: VerificationRun,
    *,
    reviewer_id: uuid.UUID,
) -> dict:
    verification = outcome.verification
    claim = session.get(Claim, verification.claim_id)
    storm_file = session.get(StormFile, claim.storm_file_id) if claim else None
    if claim is None or storm_file is None:
        raise RuntimeError("review decision lost its claim lifecycle records")
    return {
        "verification": {
            "id": verification.id,
            "claim_id": verification.claim_id,
            "overrides_id": verification.overrides_id,
            "verdict": str(verification.verdict),
            "confidence": float(verification.confidence),
            "capped": verification.capped,
            "snapshot_hash": verification.snapshot_hash,
            "reviewed_by": {
                "id": reviewer_id,
                "role": "REVIEW_CLERK",
            },
            "created_at": verification.created_at,
        },
        "claim": {
            "id": claim.id,
            "claim_ref": claim.claim_ref,
            "status": str(claim.status),
        },
        "storm_file": {"state": str(storm_file.state)},
        "money_movement": {
            "status": "NOT_AUTHORIZED_BY_VERIFICATION_REVIEW",
            "amount": None,
            "currency": None,
        },
        "idempotent_replay": not outcome.created,
    }


@router.post(
    "/claims/{claim_id}/verification/review",
    response_model=ReviewDecisionResponse,
    status_code=status.HTTP_201_CREATED,
)
def review_claim_route(
    claim_id: uuid.UUID,
    request: ReviewDecisionRequest,
    response: Response,
    authorization: str | None = Header(default=None),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    with session_scope() as session:
        human = authenticate_human(
            session,
            authorization,
            allowed_roles={AppRole.REVIEW_CLERK},
        )
        try:
            outcome = record_review_decision(
                session,
                claim_id=claim_id,
                verification_id=request.verification_id,
                clerk_id=human.user.id,
                verdict=Verdict(request.verdict),
                rationale=request.rationale,
            )
        except ClaimNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="claim not found",
            ) from exc
        except ReviewDecisionConflict as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        if not outcome.created:
            response.status_code = status.HTTP_200_OK
        return _response(session, outcome, reviewer_id=human.user.id)


__all__ = [
    "ReviewDecisionRequest",
    "ReviewDecisionResponse",
    "review_claim_route",
    "router",
]
