"""Apply the Director's standing authorization to one verified claim."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.auto_approval_service import AUTO_APPROVAL_JOB_TYPE, run_auto_approval
from app.worker import register


@register(AUTO_APPROVAL_JOB_TYPE)
def handle(session: Session, payload: dict) -> None:
    """Approve within the authorization, or record why a human must look.

    A deferral is a normal outcome, not an error: the handler returns cleanly
    either way, because a claim the machine declines is the queue working as
    designed rather than a job to retry.
    """
    run_auto_approval(session, uuid.UUID(str(payload["claim_id"])))


__all__ = ["handle"]
