"""Worker registration for the Logistics Agent.

Propose only (transitions.md): it holds no transition authority and cannot
decrement a shelf. It reads verified claims in triage order, matches them to
cash and stock, and writes the proposal to the ledger. Gate G2 — a Director's
signature in ``approvals.py`` — is what turns any of it into an allocation.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from lighthouse_contracts import ActorKind, AgentName, Event

from app import ledger
from app.logistics_service import NothingToPlan, propose_allocation_plan
from app.models import Claim
from app.worker import register, register_terminal_failure

#: Logistics is enqueued per triaged claim but plans for the whole event, so a
#: failure here strands every waiting household rather than one. That is worth
#: an anomaly a human sees.
LOGISTICS_RECONCILE_JOB_TYPE = "logistics_terminal_reconcile"


@register(AgentName.LOGISTICS_AGENT)
def handle(session: Session, payload: dict) -> None:
    """Propose a plan for the event this claim belongs to.

    Triage enqueues one job per claim, but an allocation plan is an event-wide
    object: planning per claim would propose the last tarpaulin to several
    households at once, each job blind to the others. So the job identifies the
    event, and a run that finds nothing waiting is a success — it means an
    earlier job already covered this claim.
    """
    claim_id = uuid.UUID(str(payload["claim_id"]))
    claim = session.get(Claim, claim_id)
    if claim is None:
        raise LogisticsNotRunnable("claim does not exist")
    try:
        propose_allocation_plan(session, claim.hazard_event_id)
    except NothingToPlan:
        return


class LogisticsNotRunnable(RuntimeError):
    """Safe, non-PII failure."""


register_terminal_failure(AgentName.LOGISTICS_AGENT, LOGISTICS_RECONCILE_JOB_TYPE)


@register(LOGISTICS_RECONCILE_JOB_TYPE)
def handle_terminal_failure(session: Session, payload: dict) -> None:
    """Flag an event whose plan could not be built after exhausted retries."""
    claim_id = str(payload.get("claim_id") or "")
    ledger.append(
        session,
        action=str(Event.ANOMALY_FLAGGED),
        subject_type="claim",
        subject_id=uuid.UUID(claim_id) if claim_id else None,
        payload={
            "kind": "LOGISTICS_TERMINAL_FAILURE",
            "claim_id": claim_id,
            "terminal_error_code": payload.get("terminal_error_code"),
        },
        actor_kind=ActorKind.SYSTEM,
    )


__all__ = [
    "LOGISTICS_RECONCILE_JOB_TYPE",
    "LogisticsNotRunnable",
    "handle",
    "handle_terminal_failure",
]
