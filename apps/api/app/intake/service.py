"""Transactional claim intake service.

The webhook persists only a provider-minimal job and a Storm File identity.  A
worker then turns that job into a claim, evidence, ledger entries, and a queued
verification in one transaction.  No external media, transcription, or
messaging call occurs on this stripped production path.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import bindparam, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from lighthouse_contracts import (
    SOL_PRIORITY,
    AgentName,
    ClaimStatus,
    EvidenceKind,
    Event,
    JobStatus,
    Posture,
    StormFileState,
)

from app import ledger, queue, statemachine
from app.models import AgentJob, Claim, HazardEvent, StormFile

from .twilio import TwilioDeliveryStatus, TwilioInbound, safety_of_life_matches


class IntakeEventUnavailable(RuntimeError):
    """No unambiguous hazard event can own the inbound claim."""


class UnsupportedInboundMedia(RuntimeError):
    """The message has no text or media kind this stripped intake supports."""


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    job_id: UUID
    storm_file_id: UUID
    created: bool


@dataclass(frozen=True, slots=True)
class IntakeResult:
    claim_id: UUID
    claim_ref: str
    storm_file_id: UUID
    evidence_count: int
    verification_state: str
    duplicate: bool = False


@dataclass(frozen=True, slots=True)
class DeliveryStatusEnqueueResult:
    job_id: UUID
    created: bool


_INSERT_EVIDENCE = text(
    """
    INSERT INTO evidence (id, claim_id, kind, uri, payload, sha256, phash)
    VALUES (:id, :claim_id, CAST(:kind AS evidence_kind), :uri, :payload, :sha256, :phash)
    """
).bindparams(bindparam("payload", type_=JSONB))

_DAMAGE_RULES: tuple[tuple[str, str], ...] = (
    ("roof", "roof_damage"),
    ("flood", "flooding"),
    ("water come in", "flooding"),
    ("wall", "wall_damage"),
    ("destroyed", "structural_damage"),
    ("house gone", "structural_damage"),
)

_NEED_RULES: tuple[tuple[str, str], ...] = (
    ("tarpaulin", "tarpaulin"),
    ("tarp", "tarpaulin"),
    ("water", "water"),
    ("food", "food"),
    ("shelter", "shelter"),
    ("insulin", "insulin"),
    ("medicine", "medicine"),
    ("medical", "medical_support"),
)


def phone_hash(phone: str) -> str:
    """Match the canonical registry's stable SHA-256 phone identity."""
    return hashlib.sha256(phone.encode("utf-8")).hexdigest()


def _advisory_lock(session: Session, namespace: str, value: str) -> None:
    """Serialise idempotency checks without adding lifecycle schema."""
    digest = hashlib.sha256(f"{namespace}:{value}".encode("utf-8")).digest()
    key = int.from_bytes(digest[:8], byteorder="big", signed=True)
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def ensure_storm_file(session: Session, *, phone: str) -> tuple[StormFile, bool]:
    """Resolve a phone identity, creating an affected thin file if unknown."""
    hashed = phone_hash(phone)
    _advisory_lock(session, "storm-file-phone", hashed)
    existing = session.scalar(select(StormFile).where(StormFile.phone_hash == hashed))
    if existing is not None:
        return existing, False

    storm_file = StormFile(
        phone=phone,
        phone_hash=hashed,
        state=StormFileState.AFFECTED,
        thin=True,
        # This row originated from a live provider identity. Demo content may be
        # staged, but provenance must not claim the household row was seeded.
        synthetic=False,
    )
    session.add(storm_file)
    session.flush()
    statemachine.record_creation(
        session,
        storm_file,
        payload={"channel": "whatsapp", "provider": "twilio"},
    )
    return storm_file, True


def enqueue_twilio_inbound(
    session: Session,
    inbound: TwilioInbound,
    *,
    hazard_external_ref: str | None = None,
) -> EnqueueResult:
    """Durably queue one signed provider message without duplicating retries.

    The clear phone number is consumed here and stored only on ``storm_file``.
    The queued payload carries ``storm_file_id`` and never ``From``/``WaId`` or
    the complete provider form.
    """
    _advisory_lock(session, "twilio-message", inbound.message_sid)
    existing_id = session.execute(
        text(
            """
            SELECT id FROM agent_job
            WHERE job_type = :job_type
              AND payload->>'provider_message_sid' = :sid
            ORDER BY created_at
            LIMIT 1
            """
        ),
        {"job_type": str(AgentName.INTAKE_AGENT), "sid": inbound.message_sid},
    ).scalar_one_or_none()
    if existing_id is not None:
        existing = session.get(AgentJob, existing_id)
        if existing is None:  # The lock makes this defensive, not expected.
            raise RuntimeError("deduplicated intake job disappeared")
        return EnqueueResult(
            job_id=existing.id,
            storm_file_id=UUID(str(existing.payload["storm_file_id"])),
            created=False,
        )

    storm_file, _ = ensure_storm_file(session, phone=inbound.from_phone)
    sol_keywords = safety_of_life_matches(inbound.body)
    job = queue.enqueue(
        session,
        job_type=AgentName.INTAKE_AGENT,
        priority=SOL_PRIORITY if sol_keywords else 0,
        payload={
            "provider": "twilio",
            "provider_message_sid": inbound.message_sid,
            "storm_file_id": str(storm_file.id),
            "channel": "whatsapp",
            "received_at": datetime.now(UTC).isoformat(),
            "body": inbound.body,
            "media": [
                {
                    "index": item.index,
                    "url": item.url,
                    "content_type": item.content_type,
                }
                for item in inbound.media
            ],
            "sol_keywords": sol_keywords,
            "hazard_external_ref": hazard_external_ref,
        },
    )
    return EnqueueResult(job_id=job.id, storm_file_id=storm_file.id, created=True)


def enqueue_twilio_delivery_status(
    session: Session, callback: TwilioDeliveryStatus
) -> DeliveryStatusEnqueueResult:
    """Durably retain one status transition before acknowledging Twilio.

    There is no outbound reconciliation handler yet. The worker will visibly
    park this plain job type rather than discard it, preserving the callback for
    the delivery module that follows.
    """
    identity = f"{callback.message_sid}:{callback.message_status}"
    _advisory_lock(session, "twilio-delivery-status", identity)
    existing_id = session.execute(
        text(
            """
            SELECT id FROM agent_job
            WHERE job_type = 'twilio_delivery_status'
              AND payload->>'provider_message_sid' = :sid
              AND payload->>'message_status' = :message_status
            ORDER BY created_at
            LIMIT 1
            """
        ),
        {
            "sid": callback.message_sid,
            "message_status": callback.message_status,
        },
    ).scalar_one_or_none()
    if existing_id is not None:
        return DeliveryStatusEnqueueResult(job_id=existing_id, created=False)

    job = queue.enqueue(
        session,
        job_type="twilio_delivery_status",
        payload={
            "provider": "twilio",
            "provider_message_sid": callback.message_sid,
            "message_status": callback.message_status,
            "error_code": callback.error_code,
            "reconciliation_state": "PENDING_HANDLER",
            "received_at": datetime.now(UTC).isoformat(),
        },
    )
    return DeliveryStatusEnqueueResult(job_id=job.id, created=True)


def resolve_hazard_event(session: Session, payload: dict) -> HazardEvent:
    """Resolve the claim's event explicitly, or from one unambiguous live event."""
    explicit = payload.get("hazard_event_id")
    if explicit:
        try:
            event_id = UUID(str(explicit))
        except ValueError as exc:
            raise IntakeEventUnavailable("invalid hazard event id") from exc
        event = session.get(HazardEvent, event_id)
        if event is None:
            raise IntakeEventUnavailable("hazard event does not exist")
        return event

    external_ref = str(payload.get("hazard_external_ref") or "").strip()
    if external_ref:
        event = session.scalar(
            select(HazardEvent).where(HazardEvent.external_ref == external_ref)
        )
        if event is None:
            raise IntakeEventUnavailable("configured hazard event does not exist")
        return event

    acting = session.scalars(
        select(HazardEvent)
        .where(HazardEvent.ended_at.is_(None), HazardEvent.current_posture == Posture.ACT)
        .order_by(HazardEvent.started_at.desc(), HazardEvent.id)
        .limit(2)
    ).all()
    if len(acting) == 1:
        return acting[0]
    if len(acting) > 1:
        raise IntakeEventUnavailable("multiple ACT hazard events; explicit id required")

    active = session.scalars(
        select(HazardEvent)
        .where(HazardEvent.ended_at.is_(None))
        .order_by(HazardEvent.started_at.desc(), HazardEvent.id)
        .limit(2)
    ).all()
    if len(active) == 1:
        return active[0]
    if not active:
        raise IntakeEventUnavailable("no active hazard event")
    raise IntakeEventUnavailable("multiple active hazard events; explicit id required")


def _existing_claim_for_message(session: Session, sid: str) -> Claim | None:
    claim_id = session.execute(
        text(
            """
            SELECT claim_id FROM evidence
            WHERE payload->>'provider_message_sid' = :sid
            ORDER BY created_at
            LIMIT 1
            """
        ),
        {"sid": sid},
    ).scalar_one_or_none()
    return session.get(Claim, claim_id) if claim_id is not None else None


def _claim_ref(session: Session, *, storm_file: StormFile, sid: str, event_id: UUID) -> str:
    parish_prefixes = {
        "st elizabeth": "SE",
        "westmoreland": "WE",
        "clarendon": "CL",
        "st james": "SJ",
        "kingston": "KI",
    }
    prefix = parish_prefixes.get((storm_file.parish or "").casefold(), "JM")
    digest = hashlib.sha256(f"{event_id}:{sid}".encode("utf-8")).digest()
    number = int.from_bytes(digest[:8], "big") % 100_000_000
    candidate = f"{prefix}-{number:08d}"
    collision = session.scalar(select(Claim.id).where(Claim.claim_ref == candidate))
    if collision is None:
        return candidate
    # A provider retry was handled before this point.  This branch is only an
    # unrelated 1-in-100M display-ID collision, so retain readability and add a
    # deterministic four-character disambiguator.
    return f"{candidate}-{hashlib.sha256(sid.encode()).hexdigest()[:4].upper()}"


def _extract_text(text_value: str) -> tuple[str | None, list[str]]:
    lowered = text_value.casefold()
    damage = next((value for needle, value in _DAMAGE_RULES if needle in lowered), None)
    needs: list[str] = []
    for needle, value in _NEED_RULES:
        if needle in lowered and value not in needs:
            needs.append(value)
    return damage, needs


def _insert_evidence(
    session: Session,
    *,
    claim_id: UUID,
    kind: EvidenceKind,
    uri: str | None,
    payload: dict,
    sha256: str | None = None,
) -> UUID:
    evidence_id = uuid.uuid4()
    session.execute(
        _INSERT_EVIDENCE,
        {
            "id": evidence_id,
            "claim_id": claim_id,
            "kind": str(kind),
            "uri": uri,
            "payload": payload,
            "sha256": sha256,
            "phash": None,
        },
    )
    return evidence_id


def _media_kind(content_type: str | None) -> EvidenceKind | None:
    value = (content_type or "").casefold()
    if value.startswith("audio/") or value == "application/ogg":
        return EvidenceKind.AUDIO
    if value.startswith("image/"):
        return EvidenceKind.PHOTO
    return None


def process_intake_job(session: Session, payload: dict) -> IntakeResult:
    """Persist a queued text/voice report and explicitly queue verification."""
    if payload.get("provider") != "twilio":
        raise ValueError("unsupported intake provider")
    sid = str(payload.get("provider_message_sid") or "")
    if not sid:
        raise ValueError("intake job is missing provider message SID")

    _advisory_lock(session, "twilio-message", sid)
    if existing := _existing_claim_for_message(session, sid):
        count = session.execute(
            text("SELECT count(*) FROM evidence WHERE claim_id = :claim_id"),
            {"claim_id": existing.id},
        ).scalar_one()
        return IntakeResult(
            claim_id=existing.id,
            claim_ref=existing.claim_ref,
            storm_file_id=existing.storm_file_id,
            evidence_count=count,
            verification_state="QUEUED",
            duplicate=True,
        )

    try:
        storm_file_id = UUID(str(payload["storm_file_id"]))
    except (KeyError, ValueError) as exc:
        raise ValueError("intake job has invalid Storm File identity") from exc
    storm_file = session.get(StormFile, storm_file_id)
    if storm_file is None:
        raise LookupError("intake Storm File no longer exists")

    event = resolve_hazard_event(session, payload)
    body = str(payload.get("body") or "").strip()
    media = payload.get("media") or []
    if not isinstance(media, list):
        raise ValueError("intake media must be a list")

    supported_media = [item for item in media if _media_kind(item.get("content_type"))]
    if not body and not supported_media:
        raise UnsupportedInboundMedia("message has no supported text, audio, or image evidence")

    sol_keywords = list(payload.get("sol_keywords") or safety_of_life_matches(body))
    damage_type, reported_needs = _extract_text(body)

    if storm_file.state in {StormFileState.REGISTERED, StormFileState.AT_RISK}:
        # ``record_claim_creation`` below is the one verification enqueue.  T4's
        # ledger transition still lands, but suppress its otherwise duplicate
        # FOLLOW_ON job.
        statemachine.transition(
            session,
            storm_file,
            StormFileState.AFFECTED,
            agent=AgentName.INTAKE_AGENT,
            enqueue_follow_on=False,
            payload={"provider_message_sid": sid},
        )

    claim = Claim(
        claim_ref=_claim_ref(session, storm_file=storm_file, sid=sid, event_id=event.id),
        storm_file_id=storm_file.id,
        hazard_event_id=event.id,
        status=ClaimStatus.FILED,
        damage_type=damage_type,
        reported_needs=reported_needs,
        transcript=body or None,
        transcript_alt=None,
        lang=None,
        channel="whatsapp",
        sol=bool(sol_keywords),
        # This stripped intake performs no follow-up dialogue or transcription.
        # Filing partial is explicit and safer than presenting missing fields as
        # a complete extraction.
        partial=True,
    )
    session.add(claim)
    session.flush()

    evidence_count = 0
    if body:
        _insert_evidence(
            session,
            claim_id=claim.id,
            kind=EvidenceKind.TRANSCRIPT,
            uri=None,
            sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            payload={
                "provider": "twilio",
                "provider_message_sid": sid,
                "content_type": "text/plain",
            },
        )
        evidence_count += 1

    for item in supported_media:
        kind = _media_kind(item.get("content_type"))
        assert kind is not None
        _insert_evidence(
            session,
            claim_id=claim.id,
            kind=kind,
            uri=str(item["url"]),
            payload={
                "provider": "twilio",
                "provider_message_sid": sid,
                "media_index": int(item.get("index", 0)),
                "content_type": item.get("content_type"),
                "transcription_state": "PENDING" if kind is EvidenceKind.AUDIO else None,
            },
        )
        evidence_count += 1

    statemachine.record_claim_creation(
        session,
        claim,
        payload={
            "provider_message_sid": sid,
            "evidence_count": evidence_count,
            "verification_state": "QUEUED",
        },
    )
    if sol_keywords:
        ledger.append(
            session,
            action=str(Event.CLAIM_SOL_RAISED),
            subject_type="claim",
            subject_id=claim.id,
            payload={"keywords": sol_keywords, "priority": SOL_PRIORITY},
            agent=AgentName.INTAKE_AGENT,
        )

    return IntakeResult(
        claim_id=claim.id,
        claim_ref=claim.claim_ref,
        storm_file_id=storm_file.id,
        evidence_count=evidence_count,
        verification_state="QUEUED",
    )


__all__ = [
    "EnqueueResult",
    "DeliveryStatusEnqueueResult",
    "IntakeEventUnavailable",
    "IntakeResult",
    "UnsupportedInboundMedia",
    "enqueue_twilio_inbound",
    "enqueue_twilio_delivery_status",
    "ensure_storm_file",
    "phone_hash",
    "process_intake_job",
    "resolve_hazard_event",
]
