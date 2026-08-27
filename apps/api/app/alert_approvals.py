"""Gate G1: a Director signs an alert cascade before it can be sent (ALT-01).

The signature is the whole point of this module. An alert is the one agent
output that reaches a household directly, so ALT-01 makes approval
non-negotiable and ``approval_gate_role_chk`` makes DIRECTOR non-negotiable in
the database rather than here.

Signing does not send. The outbound channel does not exist yet, and when it
does it will read approved cascades rather than being invoked from this path —
a signature and a send are different events with different failure modes, and
collapsing them would mean a delivery failure looked like a missing signature.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from lighthouse_contracts import ActorKind, AppRole, Event, GateKind

from . import ledger
from .db import session_scope
from .human_auth import authenticate_human
from .models import Approval, LedgerEntry

router = APIRouter(prefix="/v1", tags=["alert-approvals"])


class CascadeApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The proposal being signed, by its ledger entry id. Naming the exact
    #: draft matters: a Director signs the wording they read, not "whatever
    #: the latest cascade happens to be" at the moment the request lands.
    proposal_id: uuid.UUID
    note: str = Field(min_length=10, max_length=500)

    @field_validator("note")
    @classmethod
    def normalize_note(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 10:
            raise ValueError("approval note must contain at least 10 characters")
        return normalized


class CascadeApprovalResponse(BaseModel):
    approval: dict
    cascade: dict
    idempotent_replay: bool


@router.post(
    "/hazard-events/{hazard_event_id}/alerts/approve",
    response_model=CascadeApprovalResponse,
    status_code=status.HTTP_201_CREATED,
)
def approve_alert_cascade_route(
    hazard_event_id: uuid.UUID,
    request: CascadeApprovalRequest,
    response: Response,
    authorization: str | None = Header(default=None),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    with session_scope() as session:
        human = authenticate_human(
            session, authorization, allowed_roles={AppRole.DIRECTOR}
        )
        # ``LedgerEntry``'s primary key is its chain position, not its uuid,
        # so the public identifier has to be looked up rather than fetched.
        proposal = session.scalar(
            select(LedgerEntry).where(LedgerEntry.id == request.proposal_id)
        )
        if (
            proposal is None
            or proposal.action != str(Event.ALERT_CASCADE_PROPOSED)
            or proposal.subject_id != hazard_event_id
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="no such cascade proposal for this hazard event",
            )

        existing = session.scalar(
            select(Approval).where(
                Approval.gate == GateKind.ALERT_CASCADE,
                Approval.subject_type == "alert_cascade",
                Approval.subject_id == proposal.id,
            )
        )
        if existing is not None:
            # A cascade is signed once. A second signature would be a second
            # authorisation to reach the same households with the same words.
            response.status_code = status.HTTP_200_OK
            return _response(existing, proposal, replay=True)

        approval = Approval(
            gate=GateKind.ALERT_CASCADE,
            subject_type="alert_cascade",
            subject_id=proposal.id,
            approved_by=human.user.id,
            role_at_time=human.user.role,
            reauth_at=human.credential.reauthenticated_at,
            note=request.note,
        )
        session.add(approval)
        session.flush()

        ledger.append(
            session,
            action=str(Event.ALERT_CASCADE_APPROVED),
            subject_type="alert_cascade",
            subject_id=proposal.id,
            payload={
                "approval_id": str(approval.id),
                "hazard_event_id": str(hazard_event_id),
                "proposal_id": str(proposal.id),
                "gate": str(GateKind.ALERT_CASCADE),
                "posture": proposal.payload.get("posture"),
                "cascade_count": proposal.payload.get("cascade_count"),
                "recipient_count": proposal.payload.get("recipient_count"),
                # Said out loud in the receipt so that no later reader mistakes
                # a signature for a delivery.
                "delivery": "NOT_SENT_AT_APPROVAL",
            },
            actor_kind=ActorKind.HUMAN,
            actor_id=human.user.id,
        )
        return _response(approval, proposal, replay=False)


def _response(approval: Approval, proposal: LedgerEntry, *, replay: bool) -> dict:
    return {
        "approval": {
            "id": approval.id,
            "gate": str(approval.gate),
            "approved_by": approval.approved_by,
            "approved_at": approval.approved_at,
            "reauthenticated_at": approval.reauth_at,
        },
        "cascade": {
            "proposal_id": proposal.id,
            "hazard_event_id": proposal.payload.get("hazard_event_id"),
            "posture": proposal.payload.get("posture"),
            "cascade_count": proposal.payload.get("cascade_count"),
            "recipient_count": proposal.payload.get("recipient_count"),
            "delivery": "NOT_SENT_AT_APPROVAL",
        },
        "idempotent_replay": replay,
    }


__all__ = [
    "CascadeApprovalRequest",
    "CascadeApprovalResponse",
    "approve_alert_cascade_route",
    "router",
]
