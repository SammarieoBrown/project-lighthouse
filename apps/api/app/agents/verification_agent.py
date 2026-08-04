"""Worker registration for the evidence-backed Verification Agent."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from lighthouse_contracts import AgentName

from app.verification_service import run_verification
from app.worker import register


@register(AgentName.VERIFICATION_AGENT)
def handle(session: Session, payload: dict) -> None:
    """Evaluate the claim named by a durable verification job."""
    try:
        claim_id = uuid.UUID(str(payload["claim_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("verification job has invalid claim identity") from exc
    run_verification(session, claim_id)


__all__ = ["handle"]
