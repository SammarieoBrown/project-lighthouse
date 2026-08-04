"""Worker registration for the stripped WhatsApp Intake Agent."""

from __future__ import annotations

from sqlalchemy.orm import Session

from lighthouse_contracts import AgentName

from app.intake.media import TWILIO_MEDIA_JOB_TYPE, TWILIO_MEDIA_RECONCILE_JOB_TYPE
from app.intake.service import (
    process_intake_job,
    process_media_job,
    reconcile_media_failure,
)
from app.worker import register, register_terminal_failure


@register(AgentName.INTAKE_AGENT)
def handle(session: Session, payload: dict) -> None:
    """Turn one durable provider job into claim, evidence, and verification work."""
    process_intake_job(session, payload)


@register(TWILIO_MEDIA_JOB_TYPE)
def handle_media(session: Session, payload: dict) -> None:
    """Replace provider URLs with hashed private media before verification."""
    process_media_job(session, payload)


register_terminal_failure(TWILIO_MEDIA_JOB_TYPE, TWILIO_MEDIA_RECONCILE_JOB_TYPE)


@register(TWILIO_MEDIA_RECONCILE_JOB_TYPE)
def handle_media_terminal_failure(session: Session, payload: dict) -> None:
    """Scrub an exhausted media capability and unblock conservative review."""
    reconcile_media_failure(session, payload)


__all__ = ["handle", "handle_media", "handle_media_terminal_failure"]
