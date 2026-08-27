"""Worker registration for the Alert Agent.

Propose only, and of all the propose-only agents this is the one where that
matters most: an alert is the single agent output that reaches a household
directly. It holds no transition authority (transitions.md) and it cannot
send. Gate G1 — a Director's signature — is what makes a cascade sendable, and
the outbound channel that would carry it does not exist yet either.

Woken by a posture change, which is where alert cascades hang off: people act
on the posture, not on the advisory arriving.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from lighthouse_contracts import ActorKind, AgentName, Event

from app import ledger
from app.alert_service import NothingToAlert, propose_cascade
from app.models import Advisory, HazardEvent
from app.worker import register, register_terminal_failure

#: A cascade that cannot be drafted at ACT is a household that does not get
#: warned, so an exhausted job here is worth an anomaly a human sees.
ALERT_RECONCILE_JOB_TYPE = "alert_terminal_reconcile"


class AlertNotRunnable(RuntimeError):
    """Safe, non-PII failure."""


@register(AgentName.ALERT_AGENT)
def handle(session: Session, payload: dict) -> None:
    """Draft a cascade for the posture this event is now at.

    Posture is read from the event row rather than the payload. The payload
    records what the posture was when the job was queued; by the time it runs
    the storm may have moved, and a household should be warned about where
    things actually stand.
    """
    advisory_id = uuid.UUID(str(payload["advisory_id"]))
    advisory = session.get(Advisory, advisory_id)
    if advisory is None:
        raise AlertNotRunnable("advisory does not exist")
    event = session.get(HazardEvent, advisory.hazard_event_id)
    if event is None:
        raise AlertNotRunnable("advisory has no hazard event")
    try:
        propose_cascade(session, event, advisory)
    except NothingToAlert:
        # Nobody in an alertable band, or the posture fell back to QUIET
        # between queueing and running. Both are ordinary.
        return


register_terminal_failure(AgentName.ALERT_AGENT, ALERT_RECONCILE_JOB_TYPE)


@register(ALERT_RECONCILE_JOB_TYPE)
def handle_terminal_failure(session: Session, payload: dict) -> None:
    """Flag an event whose cascade could not be drafted."""
    event_id = str(payload.get("hazard_event_id") or "")
    ledger.append(
        session,
        action=str(Event.ANOMALY_FLAGGED),
        subject_type="hazard_event",
        subject_id=uuid.UUID(event_id) if event_id else None,
        payload={
            "kind": "ALERT_TERMINAL_FAILURE",
            "hazard_event_id": event_id,
            "posture": payload.get("posture"),
            "terminal_error_code": payload.get("terminal_error_code"),
        },
        actor_kind=ActorKind.SYSTEM,
    )


__all__ = [
    "ALERT_RECONCILE_JOB_TYPE",
    "AlertNotRunnable",
    "handle",
    "handle_terminal_failure",
]
