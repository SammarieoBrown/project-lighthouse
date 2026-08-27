"""Worker registration for the photo-based Damage Assessment Agent."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from lighthouse_contracts import ActorKind, AgentName, Event

from app import ledger
from app.damage_assessment_service import run_damage_assessment
from app.worker import register, register_terminal_failure

#: Once retries against the vision provider are exhausted, flag the claim for
#: manual assessment rather than leaving the job silently DEAD — mirrors
#: TWILIO_MEDIA_RECONCILE_JOB_TYPE's fail-closed pattern (intake_agent.py).
DAMAGE_ASSESSMENT_RECONCILE_JOB_TYPE = "damage_assessment_terminal_reconcile"


@register(AgentName.DAMAGE_ASSESSMENT_AGENT)
def handle(session: Session, payload: dict) -> None:
    """Assess one verified claim's photo evidence."""
    claim_id = uuid.UUID(str(payload["claim_id"]))
    run_damage_assessment(session, claim_id)


register_terminal_failure(
    AgentName.DAMAGE_ASSESSMENT_AGENT, DAMAGE_ASSESSMENT_RECONCILE_JOB_TYPE
)


@register(DAMAGE_ASSESSMENT_RECONCILE_JOB_TYPE)
def handle_terminal_failure(session: Session, payload: dict) -> None:
    """Flag a claim for manual damage assessment after exhausted retries.

    The failure itself (a provider outage, a disabled provider, an unreadable
    photo) is already recorded on the dead job row. This job's only purpose is
    to make that visible to a human via the ledger, not to retry the call.
    """
    claim_id = str(payload.get("claim_id") or "")
    ledger.append(
        session,
        action=str(Event.ANOMALY_FLAGGED),
        subject_type="claim",
        subject_id=uuid.UUID(claim_id) if claim_id else None,
        payload={
            "kind": "DAMAGE_ASSESSMENT_TERMINAL_FAILURE",
            "claim_id": claim_id,
            "terminal_error_code": payload.get("terminal_error_code"),
        },
        actor_kind=ActorKind.SYSTEM,
    )


__all__ = ["DAMAGE_ASSESSMENT_RECONCILE_JOB_TYPE", "handle", "handle_terminal_failure"]
