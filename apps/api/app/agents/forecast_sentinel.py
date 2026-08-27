"""Worker registration for the Forecast Sentinel.

Fully autonomous (transitions.md): it sets national posture and records the
change, and holds no authority over any Storm File or claim. The judgement
itself lives in ``forecast_sentinel_service`` because the replay driver calls
the same function synchronously — there is one definition of what READY means
and one place that acts on it.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from lighthouse_contracts import AgentName

from app.forecast_sentinel_service import evaluate_posture
from app.models import Advisory, HazardEvent
from app.worker import register


class ForecastSentinelNotRunnable(RuntimeError):
    """Safe, non-PII failure. The advisory or its event is not readable."""


@register(AgentName.FORECAST_SENTINEL)
def handle(session: Session, payload: dict) -> None:
    """Set posture from one advisory."""
    advisory_id = uuid.UUID(str(payload["advisory_id"]))
    advisory = session.get(Advisory, advisory_id)
    if advisory is None:
        raise ForecastSentinelNotRunnable("advisory does not exist")
    event = session.get(HazardEvent, advisory.hazard_event_id)
    if event is None:
        raise ForecastSentinelNotRunnable("advisory has no hazard event")
    evaluate_posture(session, event, advisory)


__all__ = ["ForecastSentinelNotRunnable", "handle"]
