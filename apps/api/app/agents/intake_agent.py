"""Worker registration for the stripped WhatsApp Intake Agent."""

from __future__ import annotations

from sqlalchemy.orm import Session

from lighthouse_contracts import AgentName

from app.intake.service import process_intake_job
from app.worker import register


@register(AgentName.INTAKE_AGENT)
def handle(session: Session, payload: dict) -> None:
    """Turn one durable provider job into claim, evidence, and verification work."""
    process_intake_job(session, payload)


__all__ = ["handle"]
