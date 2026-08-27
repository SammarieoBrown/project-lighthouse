"""Worker registration for the Ledger Agent.

Autonomous, and only ever after G3 (transitions.md): it audits a money path
that a Finance Officer has already signed. It holds T8 and C6 — the household's
move to SETTLED on first confirmation — which the confirmation endpoint
exercises at the moment it happens. What this handler adds is the audit
(LGR-04): reconcile what was recorded, and flag what does not add up.

It never resolves what it finds. An auditor that can close its own findings is
not an auditor, and that is precisely why this one is allowed to run without
asking anybody.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from lighthouse_contracts import ActorKind, AgentName, Event

from app import ledger
from app.ledger_agent_service import reconcile
from app.models import Allocation, Claim, Disbursement
from app.worker import register, register_terminal_failure

#: Reconciliation failing is itself worth recording: the audit not running is
#: indistinguishable, from the outside, from the audit finding nothing.
LEDGER_RECONCILE_JOB_TYPE = "ledger_terminal_reconcile"


class LedgerAgentNotRunnable(RuntimeError):
    """Safe, non-PII failure."""


@register(AgentName.LEDGER_AGENT)
def handle(session: Session, payload: dict) -> None:
    """Reconcile the event a confirmed disbursement belongs to."""
    event_id = payload.get("hazard_event_id")
    if event_id is None:
        disbursement_id = payload.get("disbursement_id")
        if disbursement_id is None:
            raise LedgerAgentNotRunnable("job names neither an event nor a payment")
        event_id = session.scalar(
            select_event_for_disbursement(uuid.UUID(str(disbursement_id)))
        )
        if event_id is None:
            raise LedgerAgentNotRunnable("payment does not resolve to a hazard event")
    reconcile(session, uuid.UUID(str(event_id)))


def select_event_for_disbursement(disbursement_id: uuid.UUID):
    """The event a payment belongs to, reached the only way it can be."""
    return (
        select(Claim.hazard_event_id)
        .join(Allocation, Allocation.claim_id == Claim.id)
        .join(Disbursement, Disbursement.allocation_id == Allocation.id)
        .where(Disbursement.id == disbursement_id)
        .limit(1)
    )


register_terminal_failure(AgentName.LEDGER_AGENT, LEDGER_RECONCILE_JOB_TYPE)


@register(LEDGER_RECONCILE_JOB_TYPE)
def handle_terminal_failure(session: Session, payload: dict) -> None:
    """Record that the audit itself could not run."""
    event_id = str(payload.get("hazard_event_id") or "")
    ledger.append(
        session,
        action=str(Event.ANOMALY_FLAGGED),
        subject_type="ledger",
        subject_id=uuid.UUID(event_id) if event_id else None,
        payload={
            "kind": "RECONCILIATION_TERMINAL_FAILURE",
            "hazard_event_id": event_id,
            "terminal_error_code": payload.get("terminal_error_code"),
            "resolution": "REQUIRES_HUMAN",
        },
        actor_kind=ActorKind.SYSTEM,
    )


__all__ = [
    "LEDGER_RECONCILE_JOB_TYPE",
    "LedgerAgentNotRunnable",
    "handle",
    "handle_terminal_failure",
]
