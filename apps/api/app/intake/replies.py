"""Outbound WhatsApp replies for the intake conversation.

The build spec's intake loop step 3: ask for whatever is missing, at most
three follow-ups, then submit partial. This module is that step. It only ever
messages the phone that just messaged us, it is fail-open in exactly one
direction — a reply that cannot be sent never fails the intake job — and the
ledger records every reply, so the follow-up cap survives worker restarts
without a new table.
"""

from __future__ import annotations

import logging
import uuid

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from lighthouse_contracts import ActorKind, AgentName

from app import ledger
from app.config import get_settings
from app.models import Claim, StormFile

log = logging.getLogger(__name__)

#: Spec: "Max three follow ups, then submit partial."
MAX_FOLLOW_UPS = 3
REPLY_SENT_ACTION = "intake.reply_sent"

_PROMPTS = {
    "location": (
        "Please share your location so we can verify conditions at your "
        "address: tap the attach button in WhatsApp, choose Location, then "
        "Send your current location."
    ),
    "photo": "If you can do it safely, send a photo of the damage.",
    "damage": (
        "Tell us what was damaged — roof, walls, or flooding — and what "
        "your household needs."
    ),
}


def _follow_ups_sent(session: Session, claim_id: uuid.UUID) -> int:
    return int(
        session.execute(
            text(
                """
                SELECT count(*) FROM ledger_entry
                WHERE action = :action
                  AND subject_type = 'claim'
                  AND subject_id = :claim_id
                  AND payload->>'asked' IS NOT NULL
                """
            ),
            {"action": REPLY_SENT_ACTION, "claim_id": claim_id},
        ).scalar_one()
    )


def _has_photo_evidence(session: Session, claim_id: uuid.UUID) -> bool:
    return bool(
        session.execute(
            text(
                "SELECT 1 FROM evidence WHERE claim_id = :claim_id"
                " AND kind IN ('PHOTO', 'AUDIO') LIMIT 1"
            ),
            {"claim_id": claim_id},
        ).scalar_one_or_none()
    )


def _next_ask(session: Session, claim: Claim, storm_file: StormFile) -> str | None:
    if claim.location is None and storm_file.location is None:
        return "location"
    if not _has_photo_evidence(session, claim.id):
        return "photo"
    if not claim.damage_type:
        return "damage"
    return None


def compose_reply(
    session: Session,
    *,
    claim: Claim,
    storm_file: StormFile,
    created: bool,
    applied: list[str],
) -> tuple[str, str | None] | None:
    """The reply body and the field it asks for, or ``None`` for silence.

    A conversation earns a message by having something to say: a new claim is
    acknowledged, an applied update is confirmed, a missing field is asked
    for. A repeat of a message we already answered gets nothing, so a
    household cannot be spammed by its own retries.
    """
    parts: list[str] = []
    if created:
        parts.append(f"Your report is filed as claim {claim.claim_ref}.")
    elif applied:
        received = " and ".join(sorted(applied))
        parts.append(f"{received.capitalize()} received for claim {claim.claim_ref}.")

    ask = _next_ask(session, claim, storm_file)
    if ask is not None and _follow_ups_sent(session, claim.id) >= MAX_FOLLOW_UPS:
        ask = None
    if ask is not None:
        parts.append(_PROMPTS[ask])
    elif created:
        parts.append("A relief officer will review it.")

    if not parts:
        return None
    return " ".join(parts), ask


def _send(*, to_phone: str, body: str) -> None:
    settings = get_settings()
    account_sid = settings.twilio_account_sid or ""
    auth_token = settings.twilio_auth_token or ""
    sender = settings.twilio_whatsapp_from or ""
    if not account_sid or not auth_token or not sender:
        raise RuntimeError("intake replies are live but Twilio sending is not configured")
    response = httpx.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json",
        auth=(account_sid, auth_token),
        data={"To": f"whatsapp:{to_phone}", "From": sender, "Body": body},
        timeout=10.0,
    )
    response.raise_for_status()


def maybe_reply(
    session: Session,
    *,
    claim: Claim,
    storm_file: StormFile,
    created: bool,
    applied: list[str],
) -> str | None:
    """Send at most one reply for one inbound message, and ledger it.

    Never raises: intake must file the claim whether or not the household
    could be answered. The ledger entry is written only after the provider
    accepted the message, so the follow-up count never counts a send that
    did not happen.
    """
    if get_settings().intake_reply_mode != "live":
        return None
    composed = compose_reply(
        session, claim=claim, storm_file=storm_file, created=created, applied=applied
    )
    if composed is None:
        return None
    body, asked = composed
    try:
        _send(to_phone=storm_file.phone, body=body)
    except Exception:
        # The phone number must stay out of the log line.
        log.warning("intake reply failed claim=%s", claim.id, exc_info=True)
        return None
    ledger.append(
        session,
        action=REPLY_SENT_ACTION,
        subject_type="claim",
        subject_id=claim.id,
        payload={
            "claim_ref": claim.claim_ref,
            "asked": asked,
            "applied": sorted(applied),
            "acknowledged_creation": created,
        },
        actor_kind=ActorKind.AGENT,
        agent=AgentName.INTAKE_AGENT,
    )
    return body


__all__ = [
    "MAX_FOLLOW_UPS",
    "REPLY_SENT_ACTION",
    "compose_reply",
    "maybe_reply",
]
