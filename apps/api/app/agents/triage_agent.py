"""Worker registration for the Triage Agent.

Autonomous, and holds no transition authority at all (transitions.md): it
annotates severity and queue order and cannot move a claim. That is
deliberate — an agent that cannot move a file cannot lose one.

Jobs for this handler have been queuing since verification landed. Until now
the worker parked every one of them, which was the intended behaviour for an
agent that did not exist yet; registering the handler drains that backlog.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from lighthouse_contracts import ActorKind, AgentName, Event

from app import ledger
from app.triage_service import run_triage
from app.worker import register, register_terminal_failure

#: Triage makes no network call, so an exhausted job here means the claim or
#: its Storm File could not be read — a data problem a human has to look at,
#: not something a retry will fix. Same fail-closed shape as the intake and
#: damage assessment reconcilers.
TRIAGE_RECONCILE_JOB_TYPE = "triage_terminal_reconcile"


@register(AgentName.TRIAGE_AGENT)
def handle(session: Session, payload: dict) -> None:
    """Score one verified claim."""
    claim_id = uuid.UUID(str(payload["claim_id"]))
    confidence = payload.get("verification_confidence")
    run_triage(
        session,
        claim_id,
        verification_confidence=float(confidence) if confidence is not None else None,
    )


register_terminal_failure(AgentName.TRIAGE_AGENT, TRIAGE_RECONCILE_JOB_TYPE)


@register(TRIAGE_RECONCILE_JOB_TYPE)
def handle_terminal_failure(session: Session, payload: dict) -> None:
    """Flag a claim that could not be triaged after exhausted retries.

    An untriaged verified claim is invisible in a queue sorted by severity, so
    it has to be loud somewhere. The ledger is that somewhere.
    """
    claim_id = str(payload.get("claim_id") or "")
    ledger.append(
        session,
        action=str(Event.ANOMALY_FLAGGED),
        subject_type="claim",
        subject_id=uuid.UUID(claim_id) if claim_id else None,
        payload={
            "kind": "TRIAGE_TERMINAL_FAILURE",
            "claim_id": claim_id,
            "terminal_error_code": payload.get("terminal_error_code"),
        },
        actor_kind=ActorKind.SYSTEM,
    )


__all__ = ["TRIAGE_RECONCILE_JOB_TYPE", "handle", "handle_terminal_failure"]
