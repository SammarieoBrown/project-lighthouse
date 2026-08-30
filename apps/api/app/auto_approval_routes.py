"""Setting and revoking the Director's standing authorization.

Delegating authority is itself an act of authority, so it takes the same
password the Director gives to approve one claim — and revoking is deliberately
the cheaper of the two paths to reach, because withdrawing authority should
never be harder than granting it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from lighthouse_contracts import ActorKind, AppRole, PayerRoute

from app import ledger
from app.auto_approval_service import active_policy
from app.db import session_scope
from app.human_auth import authenticate_human
from app.models import AutoApprovalPolicy, DonationPool, HazardEvent

router = APIRouter(prefix="/v1/auto-approval", tags=["auto-approval"])


class PolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hazard_event_id: uuid.UUID
    max_amount: Decimal = Field(gt=0, le=Decimal("1000000.00"), decimal_places=2)
    min_confidence: Decimal = Field(ge=0, le=1)
    min_signals: int = Field(ge=1, le=5)
    requires_assessment: bool = True
    payer_route: Literal["GOV_RELIEF", "DONOR_POOL"]
    pool_id: uuid.UUID | None = None
    note: str | None = Field(default=None, max_length=500)


class PolicyResponse(BaseModel):
    id: uuid.UUID
    hazard_event_id: uuid.UUID
    max_amount: Decimal
    min_confidence: Decimal
    min_signals: int
    requires_assessment: bool
    payer_route: str
    pool_id: uuid.UUID | None
    pool_name: str | None
    authorized_by: str
    created_at: datetime
    revoked_at: datetime | None


def _serialize(session: Session, policy: AutoApprovalPolicy) -> dict:
    pool = session.get(DonationPool, policy.pool_id) if policy.pool_id else None
    author = policy.created_by
    return {
        "id": policy.id,
        "hazard_event_id": policy.hazard_event_id,
        "max_amount": policy.max_amount,
        "min_confidence": policy.min_confidence,
        "min_signals": policy.min_signals,
        "requires_assessment": policy.requires_assessment,
        "payer_route": str(policy.payer_route),
        "pool_id": policy.pool_id,
        "pool_name": pool.name if pool else None,
        # The identity behind delegated authority is an operator fact, not a
        # household one, so the id is enough and the name stays out of it.
        "authorized_by": str(author),
        "created_at": policy.created_at,
        "revoked_at": policy.revoked_at,
    }


@router.get("/policies")
def list_policies_route(
    response: Response,
    authorization: str | None = Header(default=None),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    with session_scope() as session:
        authenticate_human(
            session, authorization, allowed_roles={AppRole.DIRECTOR, AppRole.AUDITOR}
        )
        rows = session.scalars(
            select(AutoApprovalPolicy)
            .order_by(AutoApprovalPolicy.created_at.desc())
            .limit(20)
        ).all()
        return {"policies": [_serialize(session, row) for row in rows]}


@router.post("/policies", status_code=status.HTTP_201_CREATED)
def create_policy_route(
    request: PolicyRequest,
    response: Response,
    authorization: str | None = Header(default=None),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    if (request.payer_route == "DONOR_POOL") != (request.pool_id is not None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="pool_id is required for DONOR_POOL and not allowed otherwise",
        )
    with session_scope() as session:
        human = authenticate_human(
            session, authorization, allowed_roles={AppRole.DIRECTOR}
        )
        if session.get(HazardEvent, request.hazard_event_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="hazard event not found"
            )
        if request.pool_id is not None and session.get(DonationPool, request.pool_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="donation pool not found"
            )

        # One authorization in force at a time: two overlapping ceilings would
        # leave nobody able to say what an agent was allowed to do.
        superseded = active_policy(session, request.hazard_event_id)
        if superseded is not None:
            superseded.revoked_at = datetime.now(superseded.created_at.tzinfo)
            superseded.revoked_by = human.user.id
            session.flush()

        policy = AutoApprovalPolicy(
            hazard_event_id=request.hazard_event_id,
            max_amount=request.max_amount,
            min_confidence=request.min_confidence,
            min_signals=request.min_signals,
            requires_assessment=request.requires_assessment,
            payer_route=PayerRoute(request.payer_route),
            pool_id=request.pool_id,
            created_by=human.user.id,
            role_at_time=human.user.role,
            reauth_at=human.credential.reauthenticated_at,
        )
        session.add(policy)
        session.flush()
        ledger.append(
            session,
            action="auto_approval.authorized",
            subject_type="auto_approval_policy",
            subject_id=policy.id,
            actor_kind=ActorKind.HUMAN,
            actor_id=human.user.id,
            payload={
                "max_amount": f"{policy.max_amount:.2f}",
                "currency": "JMD",
                "min_confidence": f"{policy.min_confidence:.3f}",
                "min_signals": policy.min_signals,
                "requires_assessment": policy.requires_assessment,
                "payer_route": str(policy.payer_route),
                "pool_id": str(policy.pool_id) if policy.pool_id else None,
                "supersedes": str(superseded.id) if superseded else None,
                "note": request.note,
                "money_movement": "NOT_INITIATED",
            },
        )
        return _serialize(session, policy)


@router.post("/policies/{policy_id}/revoke")
def revoke_policy_route(
    policy_id: uuid.UUID,
    response: Response,
    authorization: str | None = Header(default=None),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    with session_scope() as session:
        human = authenticate_human(
            session, authorization, allowed_roles={AppRole.DIRECTOR}
        )
        policy = session.get(AutoApprovalPolicy, policy_id)
        if policy is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="policy not found"
            )
        if policy.revoked_at is None:
            policy.revoked_at = datetime.now(policy.created_at.tzinfo)
            policy.revoked_by = human.user.id
            session.flush()
            ledger.append(
                session,
                action="auto_approval.revoked",
                subject_type="auto_approval_policy",
                subject_id=policy.id,
                actor_kind=ActorKind.HUMAN,
                actor_id=human.user.id,
                payload={"money_movement": "NOT_INITIATED"},
            )
        return _serialize(session, policy)


__all__ = ["router"]
