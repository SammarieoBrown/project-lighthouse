"""Authenticated Director adjudication of proposed damage estimates."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from lighthouse_contracts import AppRole

from .damage_assessment_service import (
    ClaimNotFound,
    DamageAssessmentRun,
    ReviewDecisionConflict,
    record_damage_assessment_decision,
)
from .db import session_scope
from .human_auth import authenticate_human
from .models import Claim

router = APIRouter(prefix="/v1", tags=["damage-assessment-review"])


class DamageAssessmentDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assessment_id: uuid.UUID
    verdict: Literal["APPROVED", "REJECTED"]
    rationale: str = Field(min_length=10, max_length=500)
    confirmed_low: float | None = Field(default=None, ge=0)
    confirmed_high: float | None = Field(default=None, ge=0)

    @field_validator("rationale")
    @classmethod
    def normalize_rationale(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 10:
            raise ValueError("decision rationale must contain at least 10 characters")
        return normalized


class DamageAssessmentDecisionResponse(BaseModel):
    assessment: dict
    claim: dict
    idempotent_replay: bool


def _response(outcome: DamageAssessmentRun, claim: Claim, *, director_id: uuid.UUID) -> dict:
    assessment = outcome.assessment
    return {
        "assessment": {
            "id": assessment.id,
            "claim_id": assessment.claim_id,
            "overrides_id": assessment.overrides_id,
            "verdict": str(assessment.verdict),
            "band": str(assessment.band),
            "estimate_low": float(assessment.estimate_low),
            "estimate_high": float(assessment.estimate_high),
            "currency": assessment.currency,
            "snapshot_hash": assessment.snapshot_hash,
            "decided_by": {"id": director_id, "role": "DIRECTOR"},
            "created_at": assessment.created_at,
        },
        "claim": {
            "id": claim.id,
            "claim_ref": claim.claim_ref,
            "status": str(claim.status),
        },
        "idempotent_replay": not outcome.created,
    }


@router.post(
    "/claims/{claim_id}/damage-assessment/review",
    response_model=DamageAssessmentDecisionResponse,
    status_code=status.HTTP_201_CREATED,
)
def review_damage_assessment_route(
    claim_id: uuid.UUID,
    request: DamageAssessmentDecisionRequest,
    response: Response,
    authorization: str | None = Header(default=None),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    with session_scope() as session:
        human = authenticate_human(
            session,
            authorization,
            allowed_roles={AppRole.DIRECTOR},
        )
        try:
            outcome = record_damage_assessment_decision(
                session,
                claim_id=claim_id,
                assessment_id=request.assessment_id,
                director_id=human.user.id,
                verdict=request.verdict,
                rationale=request.rationale,
                confirmed_low=request.confirmed_low,
                confirmed_high=request.confirmed_high,
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
        claim = session.get(Claim, outcome.assessment.claim_id)
        if claim is None:
            raise RuntimeError("decision lost its claim record")
        if not outcome.created:
            response.status_code = status.HTTP_200_OK
        return _response(outcome, claim, director_id=human.user.id)


__all__ = [
    "DamageAssessmentDecisionRequest",
    "DamageAssessmentDecisionResponse",
    "review_damage_assessment_route",
    "router",
]
