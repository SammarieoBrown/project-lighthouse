"""Sending an approved cascade, and recording who it reached (ALT-02).

This is the only module in the platform that talks *to* a household, so it is
the one with the most ways to do harm. Three properties hold it down.

**Nothing sends without a signature.** ``alert_delivery.approval_id`` is NOT
NULL and the approval must be a real, Director-signed G1 for the cascade being
sent. That is gate G1 made structural, the same way ``disbursement.approval_id``
makes G3 structural — an alert row cannot exist without the signature that
authorised it.

**Nothing sends for real by default.** The sender is fail-closed on config, the
same shape as the disbursement executor and the vision provider. With no
channel configured every row is written ``simulated`` and no message leaves the
process. For the whole buildathon that is the intended state: the registry is
synthetic, the phone numbers are synthetic, and a real message to a real number
is the one mistake here that cannot be taken back.

**Nothing is recorded by phone number.** Households are identified by
``phone_hash`` and Storm File id. A log of who we messaged is exactly the kind
of table that quietly becomes a directory, and the no-PII rule is not suspended
because the table is operational.

ALT-02's tiering is WhatsApp first, SMS for the numbers that never confirmed
within ten minutes. The fallback is a second row rather than an overwrite,
because "we tried WhatsApp, it did not confirm, we sent an SMS" is three facts
and a status column can hold one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from lighthouse_contracts import (
    ActorKind,
    AlertChannel,
    AlertDeliveryStatus,
    Event,
    GateKind,
    StormFileState,
)

from app import ledger
from app.config import get_settings
from app.models import AlertDelivery, Approval, LedgerEntry, RiskAssessment, StormFile

#: ALT-02's fallback window, stated once. Ten minutes is long enough that a
#: phone in a blackout has a real chance to come back, and short enough that
#: the SMS still arrives before landfall matters.
FALLBACK_AFTER = timedelta(minutes=10)


class AlertDispatchError(RuntimeError):
    """Base class for safe, non-PII dispatch failures."""


class CascadeNotApproved(AlertDispatchError):
    pass


class ChannelUnavailable(AlertDispatchError):
    pass


@dataclass(frozen=True, slots=True)
class DispatchResult:
    approval_id: uuid.UUID
    queued: int
    already_sent: int
    simulated: bool


class SimulatedAlertSender:
    """Writes a delivery record and sends nothing.

    The default, and for this release the only one. It exists as a class
    rather than an `if` so that a real sender is a substitution at one seam
    rather than an edit threaded through the dispatch logic.
    """

    simulated = True

    def send(self, *, channel: AlertChannel, phone_hash: str, body: str) -> str:
        # A reference that is obviously local. If one of these ever turns up in
        # a provider's dashboard, something is wired wrong.
        return f"simulated:{channel.value.lower()}:{phone_hash[:12]}"


def _sender(settings) -> SimulatedAlertSender:
    mode = str(getattr(settings, "alert_channel_mode", "simulated") or "simulated")
    if mode != "simulated":
        # There is no real sender in this release, and failing loudly is much
        # better than a mode name that silently does nothing.
        raise ChannelUnavailable(
            "no live alert channel is implemented; alert_channel_mode must be "
            "'simulated' in this release"
        )
    return SimulatedAlertSender()


def _signed_cascade(session: Session, approval_id: uuid.UUID) -> LedgerEntry:
    approval = session.get(Approval, approval_id)
    if approval is None or approval.gate is not GateKind.ALERT_CASCADE:
        raise CascadeNotApproved("no signed alert cascade for that approval")
    proposal = session.scalar(
        select(LedgerEntry).where(LedgerEntry.id == approval.subject_id)
    )
    if proposal is None or proposal.action != str(Event.ALERT_CASCADE_PROPOSED):
        raise CascadeNotApproved("the approval does not point at a cascade draft")
    return proposal


def _recipients(session: Session, proposal: LedgerEntry):
    """The households the signed drafts actually cover.

    Resolved from the drafts rather than re-queried from scratch, so the set
    that gets messaged is the set the Director read a recipient count for. If
    the storm moved between signature and send, that is a new cascade and a new
    signature, not a quietly wider send.
    """
    cascade = (proposal.payload or {}).get("cascade") or {}
    advisory_id = (proposal.payload or {}).get("advisory_id")
    areas = {
        (draft.get("parish"), draft.get("community"))
        for draft in cascade.get("drafts", [])
    }
    if not areas or advisory_id is None:
        return []
    rows = session.execute(
        select(StormFile, RiskAssessment.predicted_band)
        .join(RiskAssessment, RiskAssessment.storm_file_id == StormFile.id)
        .where(
            RiskAssessment.advisory_id == uuid.UUID(str(advisory_id)),
            StormFile.state != StormFileState.SETTLED,
            StormFile.phone_hash.is_not(None),
        )
    ).all()
    return [
        storm_file
        for storm_file, _band in rows
        if (storm_file.parish, storm_file.community) in areas
        or (storm_file.parish or "UNSPECIFIED", storm_file.community) in areas
    ]


def dispatch_cascade(
    session: Session, approval_id: uuid.UUID, *, now: datetime | None = None
) -> DispatchResult:
    """Send an approved cascade on the first channel and record every attempt."""
    current = now or datetime.now(UTC)
    proposal = _signed_cascade(session, approval_id)
    sender = _sender(get_settings())
    cascade = (proposal.payload or {}).get("cascade") or {}
    bodies = {
        (draft.get("parish"), draft.get("community")): draft.get("text_patois")
        or draft.get("text_en")
        or ""
        for draft in cascade.get("drafts", [])
    }

    queued = 0
    already = 0
    for storm_file in _recipients(session, proposal):
        existing = session.scalar(
            select(AlertDelivery).where(
                AlertDelivery.approval_id == approval_id,
                AlertDelivery.storm_file_id == storm_file.id,
                AlertDelivery.channel == AlertChannel.WHATSAPP,
            )
        )
        if existing is not None:
            already += 1
            continue
        body = bodies.get((storm_file.parish, storm_file.community)) or bodies.get(
            (storm_file.parish or "UNSPECIFIED", storm_file.community), ""
        )
        reference = sender.send(
            channel=AlertChannel.WHATSAPP, phone_hash=storm_file.phone_hash, body=body
        )
        session.add(
            AlertDelivery(
                approval_id=approval_id,
                storm_file_id=storm_file.id,
                phone_hash=storm_file.phone_hash,
                parish=storm_file.parish,
                community=storm_file.community,
                channel=AlertChannel.WHATSAPP,
                status=AlertDeliveryStatus.SENT,
                simulated=sender.simulated,
                provider_ref=reference,
                attempted_at=current,
            )
        )
        queued += 1
    session.flush()

    ledger.append(
        session,
        action=str(Event.ALERT_CASCADE_APPROVED),
        subject_type="alert_delivery",
        subject_id=approval_id,
        payload={
            "approval_id": str(approval_id),
            "proposal_id": str(proposal.id),
            "channel": str(AlertChannel.WHATSAPP),
            "recipients": queued,
            "already_attempted": already,
            "simulated": sender.simulated,
        },
        actor_kind=ActorKind.SYSTEM,
    )
    return DispatchResult(
        approval_id=approval_id,
        queued=queued,
        already_sent=already,
        simulated=sender.simulated,
    )


def sweep_fallbacks(
    session: Session, *, now: datetime | None = None
) -> list[AlertDelivery]:
    """SMS to every number WhatsApp never confirmed (ALT-02).

    The WhatsApp attempt is marked SUPERSEDED rather than deleted or failed:
    it is not a failure, it is an attempt that did not confirm, and that
    distinction is the entire justification for sending a second message to
    someone in a storm.
    """
    current = now or datetime.now(UTC)
    cutoff = current - FALLBACK_AFTER
    stale = session.scalars(
        select(AlertDelivery).where(
            AlertDelivery.channel == AlertChannel.WHATSAPP,
            AlertDelivery.status == AlertDeliveryStatus.SENT,
            AlertDelivery.attempted_at < cutoff,
        )
    ).all()
    if not stale:
        return []
    sender = _sender(get_settings())

    created: list[AlertDelivery] = []
    for attempt in stale:
        already = session.scalar(
            select(AlertDelivery).where(
                AlertDelivery.approval_id == attempt.approval_id,
                AlertDelivery.storm_file_id == attempt.storm_file_id,
                AlertDelivery.channel == AlertChannel.SMS,
            )
        )
        if already is not None:
            continue
        fallback = AlertDelivery(
            approval_id=attempt.approval_id,
            storm_file_id=attempt.storm_file_id,
            phone_hash=attempt.phone_hash,
            parish=attempt.parish,
            community=attempt.community,
            channel=AlertChannel.SMS,
            status=AlertDeliveryStatus.SENT,
            simulated=sender.simulated,
            provider_ref=sender.send(
                channel=AlertChannel.SMS, phone_hash=attempt.phone_hash, body=""
            ),
            attempted_at=current,
        )
        attempt.status = AlertDeliveryStatus.SUPERSEDED
        session.add(fallback)
        created.append(fallback)
    session.flush()
    return created


def confirm_delivery(
    session: Session, delivery_id: uuid.UUID, *, now: datetime | None = None
) -> AlertDelivery:
    """Record that a message actually arrived, which stops the fallback."""
    delivery = session.get(AlertDelivery, delivery_id)
    if delivery is None:
        raise AlertDispatchError("delivery does not exist")
    delivery.status = AlertDeliveryStatus.CONFIRMED
    delivery.confirmed_at = now or datetime.now(UTC)
    session.flush()
    return delivery


__all__ = [
    "FALLBACK_AFTER",
    "AlertDispatchError",
    "CascadeNotApproved",
    "ChannelUnavailable",
    "DispatchResult",
    "SimulatedAlertSender",
    "confirm_delivery",
    "dispatch_cascade",
    "sweep_fallbacks",
]
