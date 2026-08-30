"""Tell the household their relief is ready, on the channel they claimed on.

The last mile of PAY: a confirmed disbursement is worthless to a household
that never hears about it. No payment rail exists in this release, so what
travels is a voucher reference the household can quote — and the message says
plainly that this is a simulated confirmation, because a text claiming money
is waiting when none has moved would be the one lie this system cannot tell.

The notice is env-gated, fail-open, and ledgered: settlement never fails
because a phone was unreachable, and a notice that was never sent leaves no
receipt claiming it was.
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
from app.models import Claim, Disbursement, StormFile

log = logging.getLogger(__name__)

NOTICE_SENT_ACTION = "relief.notice_sent"


def voucher_code(disbursement: Disbursement) -> str | None:
    """A short reference derived from the provider confirmation.

    Derived rather than stored so it cannot drift from the confirmation it
    names, and absent until a confirmation exists — there is nothing to quote
    before the rail has answered.
    """
    reference = (disbursement.external_ref or "").strip()
    if not reference:
        return None
    return f"LH-{reference.rsplit('-', 1)[-1][-8:].upper()}"


def _body(*, claim: Claim, amount: str, code: str, channel: str) -> str:
    return (
        f"Lighthouse relief for claim {claim.claim_ref}: J${amount} approved "
        f"and confirmed. Your voucher reference is {code}. Quote it with your "
        f"ID at a partner agent ({channel.replace('_', ' ').lower()}). "
        "This is a Lighthouse demonstration: the confirmation is simulated and "
        "no real funds have moved."
    )


def _send(*, to_phone: str, body: str) -> None:
    settings = get_settings()
    account_sid = settings.twilio_account_sid or ""
    auth_token = settings.twilio_auth_token or ""
    sender = settings.twilio_whatsapp_from or ""
    if not account_sid or not auth_token or not sender:
        raise RuntimeError("relief notices are live but Twilio sending is not configured")
    response = httpx.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json",
        auth=(account_sid, auth_token),
        data={"To": f"whatsapp:{to_phone}", "From": sender, "Body": body},
        timeout=10.0,
    )
    response.raise_for_status()


def _already_sent(session: Session, disbursement_id: uuid.UUID) -> bool:
    return bool(
        session.execute(
            text(
                """
                SELECT 1 FROM ledger_entry
                 WHERE action = :action
                   AND subject_type = 'disbursement'
                   AND subject_id = :subject_id
                 LIMIT 1
                """
            ),
            {"action": NOTICE_SENT_ACTION, "subject_id": disbursement_id},
        ).scalar_one_or_none()
    )


def notify_relief_confirmed(
    session: Session,
    *,
    claim: Claim,
    storm_file: StormFile,
    disbursement: Disbursement,
    amount: str,
) -> str | None:
    """Send one voucher notice for one confirmed disbursement.

    Never raises. An unreachable phone is an operational fact for a human to
    chase, not a reason to unwind a signed settlement.
    """
    if get_settings().relief_notice_mode != "live":
        return None
    code = voucher_code(disbursement)
    if code is None or not storm_file.phone:
        return None
    if _already_sent(session, disbursement.id):
        return None

    body = _body(
        claim=claim, amount=amount, code=code, channel=str(disbursement.channel)
    )
    try:
        _send(to_phone=storm_file.phone, body=body)
    except Exception:
        # The number never reaches the log line; the claim is enough to trace.
        log.warning("relief notice failed claim=%s", claim.id, exc_info=True)
        return None

    ledger.append(
        session,
        action=NOTICE_SENT_ACTION,
        subject_type="disbursement",
        subject_id=disbursement.id,
        payload={
            "claim_ref": claim.claim_ref,
            "voucher_reference": code,
            "channel": str(disbursement.channel),
            "simulated": True,
            "no_real_money_moved": True,
        },
        actor_kind=ActorKind.AGENT,
        agent=AgentName.LEDGER_AGENT,
    )
    return code


__all__ = ["NOTICE_SENT_ACTION", "notify_relief_confirmed", "voucher_code"]
