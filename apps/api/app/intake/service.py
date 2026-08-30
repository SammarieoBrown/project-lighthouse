"""Transactional claim intake service.

The webhook persists only a provider-minimal job and a Storm File identity. A
worker files the claim immediately. Text-only reports proceed to verification;
media reports first pass through a separate durable enrichment job so a raw,
unhashed provider URL can never be mistaken for verified media evidence.
"""

from __future__ import annotations

import hashlib
import re
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
from app.config import get_settings
from app.models import AgentJob, Claim, HazardEvent, StormFile
from app.registry.geography import parish_at

from . import replies
from .extraction import extract_claim_fields
from .media import (
    MAX_AUDIO_BYTES,
    MAX_MEDIA_BYTES,
    TWILIO_MEDIA_JOB_TYPE,
    MediaBoundaryError,
    MediaFetcher,
    MediaStore,
    R2MediaStore,
    TwilioMediaFetcher,
    image_perceptual_hash,
    is_audio_content_type,
    is_image_content_type,
    is_supported_content_type,
)
from .transcription import (
    CloudflareWhisperTranscriber,
    Transcriber,
    TranscriptionResult,
)
from .twilio import TwilioDeliveryStatus, TwilioInbound, safety_of_life_matches


MAX_MEDIA_JOB_BYTES = 2 * MAX_MEDIA_BYTES
MAX_AUDIO_ITEMS_PER_JOB = 3
MAX_AUDIO_BYTES_PER_STORM_FILE_24H = 3 * MAX_AUDIO_BYTES
_SAFE_TERMINAL_ERROR = re.compile(r"^handler_error:[A-Za-z_][A-Za-z0-9_]{0,63}$")


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


@dataclass(frozen=True, slots=True)
class MediaEnrichmentResult:
    claim_id: UUID
    media_count: int
    transcript_count: int
    verification_state: str
    duplicate: bool = False


_INSERT_EVIDENCE = text(
    """
    INSERT INTO evidence (id, claim_id, kind, uri, payload, sha256, phash)
    VALUES (:id, :claim_id, CAST(:kind AS evidence_kind), :uri, :payload, :sha256, :phash)
    """
).bindparams(bindparam("payload", type_=JSONB))

_UPDATE_EVIDENCE = text(
    """
    UPDATE evidence
       SET uri = :uri,
           payload = :payload,
           sha256 = :sha256,
           phash = :phash
     WHERE id = :evidence_id AND claim_id = :claim_id
    """
).bindparams(bindparam("payload", type_=JSONB))


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
            "latitude": inbound.latitude,
            "longitude": inbound.longitude,
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


def _minimize_intake_job(session: Session, *, sid: str, claim: Claim) -> None:
    """Erase provider body/media capabilities once durable claim rows exist."""
    job = session.scalar(
        select(AgentJob)
        .where(
            AgentJob.job_type == str(AgentName.INTAKE_AGENT),
            AgentJob.payload["provider_message_sid"].astext == sid,
        )
        .order_by(AgentJob.created_at, AgentJob.id)
        .limit(1)
    )
    if job is None:
        return
    job.payload = {
        "provider": "twilio",
        "provider_message_sid": sid,
        "storm_file_id": str(claim.storm_file_id),
        "claim_id": str(claim.id),
        "retention_state": "MINIMIZED_AFTER_CLAIM",
    }
    session.flush()


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
    extracted = extract_claim_fields(text_value)
    return extracted.damage_type, list(extracted.reported_needs)


def _insert_evidence(
    session: Session,
    *,
    claim_id: UUID,
    kind: EvidenceKind,
    uri: str | None,
    payload: dict,
    sha256: str | None = None,
    phash: str | None = None,
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
            "phash": phash,
        },
    )
    return evidence_id


def _media_kind(content_type: str | None) -> EvidenceKind | None:
    if is_audio_content_type(content_type):
        return EvidenceKind.AUDIO
    if is_image_content_type(content_type):
        return EvidenceKind.PHOTO
    return None


def _pipeline_state(session: Session, claim_id: UUID) -> str:
    verification = session.scalar(
        select(AgentJob)
        .where(
            AgentJob.job_type == str(AgentName.VERIFICATION_AGENT),
            AgentJob.payload["claim_id"].astext == str(claim_id),
        )
        .order_by(AgentJob.created_at.desc(), AgentJob.id.desc())
        .limit(1)
    )
    if verification is not None:
        return {
            JobStatus.QUEUED: "QUEUED",
            JobStatus.RUNNING: "RUNNING",
            JobStatus.DONE: "COMPLETED",
            JobStatus.FAILED: "FAILED",
            JobStatus.DEAD: "FAILED",
        }[verification.status]

    media_job = session.scalar(
        select(AgentJob)
        .where(
            AgentJob.job_type == TWILIO_MEDIA_JOB_TYPE,
            AgentJob.payload["claim_id"].astext == str(claim_id),
        )
        .order_by(AgentJob.created_at.desc(), AgentJob.id.desc())
        .limit(1)
    )
    if media_job is None:
        return "FAILED"
    return {
        JobStatus.QUEUED: "MEDIA_PENDING",
        JobStatus.RUNNING: "MEDIA_PROCESSING",
        JobStatus.DONE: "MEDIA_FAILED",
        JobStatus.FAILED: "MEDIA_FAILED",
        JobStatus.DEAD: "MEDIA_FAILED",
    }[media_job.status]


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
        _minimize_intake_job(session, sid=sid, claim=existing)
        return IntakeResult(
            claim_id=existing.id,
            claim_ref=existing.claim_ref,
            storm_file_id=existing.storm_file_id,
            evidence_count=count,
            verification_state=_pipeline_state(session, existing.id),
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

    supported_media: list[dict] = []
    for item in media:
        if not isinstance(item, dict):
            raise ValueError("intake media item must be an object")
        if not is_supported_content_type(str(item.get("content_type") or "")):
            continue
        try:
            index = int(item["index"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("intake media index is invalid") from exc
        url = str(item.get("url") or "")
        if not 0 <= index <= 9 or not url:
            raise ValueError("intake media identity is invalid")
        supported_media.append(
            {"index": index, "url": url, "content_type": str(item["content_type"])}
        )
    point: str | None = None
    parish: str | None = None
    raw_latitude, raw_longitude = payload.get("latitude"), payload.get("longitude")
    if raw_latitude is not None and raw_longitude is not None:
        latitude, longitude = float(raw_latitude), float(raw_longitude)
        point = f"SRID=4326;POINT({longitude} {latitude})"
        parish = parish_at(longitude, latitude)

    if not body and not supported_media and point is None:
        raise UnsupportedInboundMedia("message has no supported text, audio, or image evidence")

    sol_keywords = list(payload.get("sol_keywords") or safety_of_life_matches(body))
    damage_type, reported_needs = _extract_text(body)

    if point is not None:
        storm_file.location = point
        # A registered household's parish is authoritative; a thin file's
        # parish is whatever the share proves.
        if parish is not None and (storm_file.thin or not storm_file.parish):
            storm_file.parish = parish

    open_claim = _open_claim(
        session, storm_file_id=storm_file.id, hazard_event_id=event.id
    )
    if open_claim is not None:
        return _apply_follow_up(
            session,
            claim=open_claim,
            storm_file=storm_file,
            sid=sid,
            body=body,
            supported_media=supported_media,
            sol_keywords=sol_keywords,
            damage_type=damage_type,
            reported_needs=reported_needs,
            point=point,
        )

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
        location=point,
        transcript=body or None,
        transcript_alt=None,
        lang=None,
        channel="whatsapp",
        sol=bool(sol_keywords),
        # Language is unknown until a voice note is transcribed or a future
        # text-language step runs. Unknown stays partial; extraction completion
        # is not the same thing as verification.
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

    media_evidence: list[tuple[UUID, EvidenceKind]] = []
    for item in supported_media:
        kind = _media_kind(item.get("content_type"))
        assert kind is not None
        evidence_id = _insert_evidence(
            session,
            claim_id=claim.id,
            kind=kind,
            uri=str(item["url"]),
            payload={
                "provider": "twilio",
                "provider_message_sid": sid,
                "media_index": int(item.get("index", 0)),
                "content_type": item.get("content_type"),
                "media_state": "PENDING_FETCH",
                "transcription_state": "PENDING" if kind is EvidenceKind.AUDIO else None,
                "phash_state": "PENDING" if kind is EvidenceKind.PHOTO else None,
            },
        )
        media_evidence.append((evidence_id, kind))
        evidence_count += 1

    verification_state = "MEDIA_PENDING" if media_evidence else "QUEUED"
    statemachine.record_claim_creation(
        session,
        claim,
        enqueue_verification=not media_evidence,
        payload={
            "provider_message_sid": sid,
            "evidence_count": evidence_count,
            "verification_state": verification_state,
        },
    )
    for evidence_id, kind in media_evidence:
        voice_priority = kind is EvidenceKind.AUDIO
        queue.enqueue(
            session,
            job_type=TWILIO_MEDIA_JOB_TYPE,
            # Audio-only reports cannot be screened for safety-of-life until
            # transcription. Each attachment is an independent bounded job so
            # one slow object cannot outlive the worker lease or duplicate all
            # other provider calls on retry.
            priority=SOL_PRIORITY if claim.sol or voice_priority else 0,
            payload={
                "claim_id": str(claim.id),
                "evidence_ids": [str(evidence_id)],
                "voice_priority": voice_priority,
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

    _minimize_intake_job(session, sid=sid, claim=claim)

    applied = ["location"] if point is not None else []
    replies.maybe_reply(
        session, claim=claim, storm_file=storm_file, created=True, applied=applied
    )

    return IntakeResult(
        claim_id=claim.id,
        claim_ref=claim.claim_ref,
        storm_file_id=storm_file.id,
        evidence_count=evidence_count,
        verification_state=verification_state,
    )


def _open_claim(
    session: Session, *, storm_file_id: UUID, hazard_event_id: UUID
) -> Claim | None:
    """The household's claim still being assembled for this event, if any.

    Only ``FILED`` qualifies: once a claim is verified or beyond, a new report
    is a new claim rather than an edit to a decided one.
    """
    return session.scalar(
        select(Claim)
        .where(
            Claim.storm_file_id == storm_file_id,
            Claim.hazard_event_id == hazard_event_id,
            Claim.status == ClaimStatus.FILED,
        )
        .order_by(Claim.filed_at.desc(), Claim.id.desc())
        .limit(1)
    )


def _apply_follow_up(
    session: Session,
    *,
    claim: Claim,
    storm_file: StormFile,
    sid: str,
    body: str,
    supported_media: list[dict],
    sol_keywords: list[str],
    damage_type: str | None,
    reported_needs: list[str],
    point: str | None,
) -> IntakeResult:
    """Fold one follow-up message into the household's open claim.

    One conversation, one claim: the spec's intake loop fills a single claim
    over several messages rather than filing a claim per message. Every field
    only ever gains information — nothing a household already said is erased
    by a later message.
    """
    applied: list[str] = []

    if point is not None:
        claim.location = point
        applied.append("location")

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
        claim.transcript = f"{claim.transcript}\n{body}" if claim.transcript else body
        if damage_type and not claim.damage_type:
            claim.damage_type = damage_type
        if reported_needs:
            merged = list(claim.reported_needs or [])
            merged.extend(n for n in reported_needs if n not in merged)
            claim.reported_needs = merged
        applied.append("message")

    media_evidence: list[tuple[UUID, EvidenceKind]] = []
    for item in supported_media:
        kind = _media_kind(item.get("content_type"))
        assert kind is not None
        evidence_id = _insert_evidence(
            session,
            claim_id=claim.id,
            kind=kind,
            uri=str(item["url"]),
            payload={
                "provider": "twilio",
                "provider_message_sid": sid,
                "media_index": int(item.get("index", 0)),
                "content_type": item.get("content_type"),
                "media_state": "PENDING_FETCH",
                "transcription_state": "PENDING" if kind is EvidenceKind.AUDIO else None,
                "phash_state": "PENDING" if kind is EvidenceKind.PHOTO else None,
            },
        )
        media_evidence.append((evidence_id, kind))
        applied.append("voice note" if kind is EvidenceKind.AUDIO else "photo")

    if sol_keywords and not claim.sol:
        claim.sol = True
        ledger.append(
            session,
            action=str(Event.CLAIM_SOL_RAISED),
            subject_type="claim",
            subject_id=claim.id,
            payload={"keywords": sol_keywords, "priority": SOL_PRIORITY},
            agent=AgentName.INTAKE_AGENT,
        )

    session.flush()
    ledger.append(
        session,
        action="claim.follow_up_applied",
        subject_type="claim",
        subject_id=claim.id,
        payload={
            "claim_ref": claim.claim_ref,
            "provider_message_sid": sid,
            "applied": sorted(set(applied)),
        },
        agent=AgentName.INTAKE_AGENT,
    )

    for evidence_id, kind in media_evidence:
        voice_priority = kind is EvidenceKind.AUDIO
        queue.enqueue(
            session,
            job_type=TWILIO_MEDIA_JOB_TYPE,
            priority=SOL_PRIORITY if claim.sol or voice_priority else 0,
            payload={
                "claim_id": str(claim.id),
                "evidence_ids": [str(evidence_id)],
                "voice_priority": voice_priority,
            },
        )
    # New media defers re-verification to the media pipeline's completion
    # hook, exactly as claim creation does.
    if not media_evidence and applied:
        _ensure_verification_queued(session, claim)

    _minimize_intake_job(session, sid=sid, claim=claim)

    evidence_count = session.execute(
        text("SELECT count(*) FROM evidence WHERE claim_id = :claim_id"),
        {"claim_id": claim.id},
    ).scalar_one()

    replies.maybe_reply(
        session, claim=claim, storm_file=storm_file, created=False, applied=applied
    )

    return IntakeResult(
        claim_id=claim.id,
        claim_ref=claim.claim_ref,
        storm_file_id=storm_file.id,
        evidence_count=evidence_count,
        verification_state=_pipeline_state(session, claim.id),
    )


def _ensure_verification_queued(session: Session, claim: Claim) -> None:
    """Queue a verification run unless one is already waiting to happen.

    Only QUEUED and RUNNING block a new enqueue: a completed run is history,
    not cover. Evidence that arrives after a verdict — a photo, a location
    share — must produce a fresh verification row, because the verdict that
    exists was rendered without it.
    """
    existing = session.scalar(
        select(AgentJob.id)
        .where(
            AgentJob.job_type == str(AgentName.VERIFICATION_AGENT),
            AgentJob.payload["claim_id"].astext == str(claim.id),
            AgentJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
        )
        .limit(1)
    )
    if existing is None:
        statemachine.enqueue_claim_verification(session, claim)


def _media_pipeline_complete(session: Session, claim_id: UUID) -> bool:
    pending = session.execute(
        text(
            """
            SELECT count(*)
              FROM evidence
             WHERE claim_id = :claim_id
               AND kind::text IN ('AUDIO', 'PHOTO')
               AND COALESCE(payload->>'media_state', 'PENDING_FETCH')
                     NOT IN ('STORED', 'FAILED')
            """
        ),
        {"claim_id": claim_id},
    ).scalar_one()
    return int(pending) == 0


def process_media_job(
    session: Session,
    payload: dict,
    *,
    fetcher: MediaFetcher | None = None,
    store: MediaStore | None = None,
    transcriber: Transcriber | None = None,
) -> MediaEnrichmentResult:
    """Fetch, store, transcribe, and extract before verification is queued.

    Database mutation is atomic. External R2 writes are content-addressed, so a
    database retry can safely write the same object key again without creating
    a second piece of evidence or trusting a stale provider URL.
    """
    try:
        claim_id = UUID(str(payload["claim_id"]))
    except (KeyError, ValueError) as exc:
        raise ValueError("media job has invalid claim identity") from exc
    raw_evidence_ids = payload.get("evidence_ids")
    if not isinstance(raw_evidence_ids, list) or not 1 <= len(raw_evidence_ids) <= 10:
        raise ValueError("media job has invalid evidence identities")
    try:
        evidence_ids = [UUID(str(value)) for value in raw_evidence_ids]
    except ValueError as exc:
        raise ValueError("media job has invalid evidence identities") from exc
    if len(set(evidence_ids)) != len(evidence_ids):
        raise ValueError("media job has duplicate evidence identities")

    _advisory_lock(session, "intake-media-claim", str(claim_id))
    claim = session.get(Claim, claim_id)
    if claim is None:
        raise LookupError("media job claim no longer exists")
    rows = session.execute(
        text(
            """
            SELECT id, kind::text AS kind, uri, payload, sha256, phash
              FROM evidence
             WHERE claim_id = :claim_id
               AND id = ANY(CAST(:evidence_ids AS uuid[]))
             ORDER BY CASE kind::text WHEN 'AUDIO' THEN 0 ELSE 1 END,
                      (payload->>'media_index')::int, id
             FOR UPDATE
            """
        ),
        {"claim_id": claim_id, "evidence_ids": evidence_ids},
    ).mappings().all()
    if {row["id"] for row in rows} != set(evidence_ids):
        raise LookupError("media job evidence no longer exists")
    if any(row["kind"] not in {str(EvidenceKind.AUDIO), str(EvidenceKind.PHOTO)} for row in rows):
        raise ValueError("media job references non-media evidence")
    audio_count = sum(row["kind"] == str(EvidenceKind.AUDIO) for row in rows)
    if audio_count > MAX_AUDIO_ITEMS_PER_JOB:
        raise MediaBoundaryError("media job exceeds the bounded voice-note count")

    already_complete = all(
        row["sha256"]
        and str(row["uri"] or "").startswith("r2://")
        and isinstance(row["payload"], dict)
        and row["payload"].get("media_state") == "STORED"
        and (
            row["payload"].get("transcription_state") == "COMPLETE"
            if row["kind"] == str(EvidenceKind.AUDIO)
            else bool(row["phash"])
            or row["payload"].get("phash_state") == "UNSUPPORTED"
        )
        for row in rows
    )
    if already_complete:
        pipeline_complete = _media_pipeline_complete(session, claim.id)
        if pipeline_complete:
            _ensure_verification_queued(session, claim)
        return MediaEnrichmentResult(
            claim_id=claim.id,
            media_count=len(rows),
            transcript_count=sum(row["kind"] == str(EvidenceKind.AUDIO) for row in rows),
            verification_state="QUEUED" if pipeline_complete else "MEDIA_PENDING",
            duplicate=True,
        )

    settings = None
    if fetcher is None or store is None or (
        transcriber is None
        and any(row["kind"] == str(EvidenceKind.AUDIO) for row in rows)
    ):
        settings = get_settings()
    fetcher = fetcher or TwilioMediaFetcher.from_settings(settings)
    store = store or R2MediaStore.from_settings(settings)
    if transcriber is None and any(
        row["kind"] == str(EvidenceKind.AUDIO) for row in rows
    ):
        transcriber = CloudflareWhisperTranscriber.from_settings(settings)

    historical_audio_bytes = session.execute(
        text(
            """
            SELECT COALESCE(sum(
              CASE WHEN e.payload->>'size_bytes' ~ '^[0-9]{1,9}$'
                   THEN (e.payload->>'size_bytes')::bigint ELSE 0 END
            ), 0)::bigint
              FROM evidence e
              JOIN claim prior_claim ON prior_claim.id = e.claim_id
             WHERE prior_claim.storm_file_id = :storm_file_id
               AND e.kind::text = 'AUDIO'
               AND e.id <> ALL(CAST(:evidence_ids AS uuid[]))
               AND e.created_at >= now() - interval '24 hours'
            """
        ),
        {
            "storm_file_id": claim.storm_file_id,
            "evidence_ids": evidence_ids,
        },
    ).scalar_one()

    transcripts: list[tuple[int, TranscriptionResult]] = []
    aggregate_bytes = 0
    new_audio_bytes = 0
    for row in rows:
        evidence_payload = row["payload"]
        if not isinstance(evidence_payload, dict):
            raise ValueError("media evidence payload is invalid")
        provider_url = str(row["uri"] or "")
        message_sid = str(evidence_payload.get("provider_message_sid") or "")
        declared_type = str(evidence_payload.get("content_type") or "")
        fetched = fetcher.fetch(
            provider_url,
            message_sid=message_sid,
            expected_content_type=declared_type,
        )
        aggregate_bytes += fetched.size_bytes
        if aggregate_bytes > MAX_MEDIA_JOB_BYTES:
            raise MediaBoundaryError("media job exceeds the aggregate byte boundary")
        if row["kind"] == str(EvidenceKind.AUDIO):
            new_audio_bytes += fetched.size_bytes
            if (
                historical_audio_bytes + new_audio_bytes
                > MAX_AUDIO_BYTES_PER_STORM_FILE_24H
            ):
                raise MediaBoundaryError(
                    "voice-note transcription quota is exhausted for this Storm File"
                )
        stored = store.put(fetched)
        if (
            stored.sha256 != fetched.sha256
            or stored.size_bytes != fetched.size_bytes
            or stored.content_type != fetched.content_type
            or not stored.uri.startswith("r2://")
        ):
            raise MediaBoundaryError("private media store returned inconsistent identity")
        phash = image_perceptual_hash(fetched) if row["kind"] == str(EvidenceKind.PHOTO) else None
        transcript = (
            transcriber.transcribe(fetched)
            if row["kind"] == str(EvidenceKind.AUDIO) and transcriber is not None
            else None
        )
        old_payload = evidence_payload
        media_index = int(old_payload.get("media_index", 0))
        updated_payload = {
            "provider": "twilio",
            "provider_message_sid": old_payload.get("provider_message_sid"),
            "media_index": media_index,
            "content_type": stored.content_type,
            "size_bytes": stored.size_bytes,
            "media_state": "STORED",
            "storage_backend": "r2",
            "storage_schema": "content-addressed-sha256-v1",
            "transcription_state": "COMPLETE" if transcript is not None else None,
            "phash_state": (
                "COMPUTED"
                if phash is not None
                else "UNSUPPORTED"
                if row["kind"] == str(EvidenceKind.PHOTO)
                else None
            ),
        }
        if transcript is not None:
            updated_payload["transcription_provider"] = transcript.provider
            updated_payload["transcription_model"] = transcript.model
            updated_payload["transcription_language"] = transcript.lang
            transcripts.append((media_index, transcript))
        session.execute(
            _UPDATE_EVIDENCE,
            {
                "evidence_id": row["id"],
                "claim_id": claim.id,
                "uri": stored.uri,
                "payload": updated_payload,
                "sha256": stored.sha256,
                "phash": phash,
            },
        )
        if transcript is not None:
            _insert_evidence(
                session,
                claim_id=claim.id,
                kind=EvidenceKind.TRANSCRIPT,
                uri=None,
                sha256=hashlib.sha256(transcript.text.encode("utf-8")).hexdigest(),
                payload={
                    "source_evidence_id": str(row["id"]),
                    "provider": transcript.provider,
                    "model": transcript.model,
                    "lang": transcript.lang,
                    "content_type": "text/plain; charset=utf-8",
                },
            )

        # Do not retain decoded/provider bytes across attachments. At most one
        # bounded object and its ASR request are resident at a time.
        del fetched

    if transcripts:
        transcripts.sort(key=lambda item: item[0])
        asr_text = "\n".join(item.text for _, item in transcripts)
        typed_text = claim.transcript
        claim.transcript = asr_text
        if typed_text and typed_text != asr_text:
            claim.transcript_alt = typed_text
        languages = {item.lang for _, item in transcripts if item.lang}
        claim.lang = next(iter(languages)) if len(languages) == 1 else "mul" if languages else None

    extraction_text = "\n".join(
        value for value in (claim.transcript, claim.transcript_alt) if value
    )
    extracted = extract_claim_fields(extraction_text)
    if claim.damage_type is None:
        claim.damage_type = extracted.damage_type
    merged_needs = list(claim.reported_needs or [])
    for need in extracted.reported_needs:
        if need not in merged_needs:
            merged_needs.append(need)
    claim.reported_needs = merged_needs
    claim.partial = not extracted.is_complete(
        transcript=claim.transcript,
        lang=claim.lang,
    )

    sol_keywords = safety_of_life_matches(extraction_text)
    if sol_keywords and not claim.sol:
        claim.sol = True
        ledger.append(
            session,
            action=str(Event.CLAIM_SOL_RAISED),
            subject_type="claim",
            subject_id=claim.id,
            payload={"keywords": sol_keywords, "priority": SOL_PRIORITY},
            agent=AgentName.INTAKE_AGENT,
        )
    session.flush()
    pipeline_complete = _media_pipeline_complete(session, claim.id)
    if pipeline_complete:
        _ensure_verification_queued(session, claim)
    return MediaEnrichmentResult(
        claim_id=claim.id,
        media_count=len(rows),
        transcript_count=len(transcripts),
        verification_state="QUEUED" if pipeline_complete else "MEDIA_PENDING",
    )


def reconcile_media_failure(session: Session, payload: dict) -> None:
    """Scrub exhausted provider capabilities and continue to conservative review."""
    try:
        claim_id = UUID(str(payload["claim_id"]))
        evidence_ids = [UUID(str(value)) for value in payload["evidence_ids"]]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("media reconciliation has invalid identities") from exc
    if not evidence_ids or len(evidence_ids) > 10 or len(set(evidence_ids)) != len(evidence_ids):
        raise ValueError("media reconciliation has invalid evidence identities")
    error_code = str(payload.get("terminal_error_code") or "")
    if not _SAFE_TERMINAL_ERROR.fullmatch(error_code):
        error_code = "handler_error:MediaEnrichmentFailed"

    _advisory_lock(session, "intake-media-claim", str(claim_id))
    claim = session.get(Claim, claim_id)
    if claim is None:
        raise LookupError("media reconciliation claim no longer exists")
    rows = session.execute(
        text(
            """
            SELECT id, kind::text AS kind, payload
              FROM evidence
             WHERE claim_id = :claim_id
               AND id = ANY(CAST(:evidence_ids AS uuid[]))
             ORDER BY id
             FOR UPDATE
            """
        ),
        {"claim_id": claim_id, "evidence_ids": evidence_ids},
    ).mappings().all()
    if {row["id"] for row in rows} != set(evidence_ids):
        raise LookupError("media reconciliation evidence no longer exists")

    reconciled: list[str] = []
    for row in rows:
        old = row["payload"] if isinstance(row["payload"], dict) else {}
        if old.get("media_state") == "STORED":
            continue
        safe_payload = {
            "provider": "twilio",
            "provider_message_sid": old.get("provider_message_sid"),
            "media_index": int(old.get("media_index", 0)),
            "content_type": old.get("content_type"),
            "media_state": "FAILED",
            "failure_code": error_code,
            "transcription_state": (
                "FAILED" if row["kind"] == str(EvidenceKind.AUDIO) else None
            ),
            "phash_state": (
                "FAILED" if row["kind"] == str(EvidenceKind.PHOTO) else None
            ),
        }
        session.execute(
            _UPDATE_EVIDENCE,
            {
                "evidence_id": row["id"],
                "claim_id": claim.id,
                "uri": None,
                "payload": safe_payload,
                "sha256": None,
                "phash": None,
            },
        )
        reconciled.append(str(row["id"]))

    claim.partial = True
    if reconciled:
        ledger.append(
            session,
            action="intake.media_failed",
            subject_type="claim",
            subject_id=claim.id,
            payload={
                "evidence_ids": reconciled,
                "failure_code": error_code,
                "verification_disposition": "CONTINUE_WITH_MISSING_SIGNAL",
            },
            agent=AgentName.INTAKE_AGENT,
        )
    session.flush()
    if _media_pipeline_complete(session, claim.id):
        _ensure_verification_queued(session, claim)


__all__ = [
    "EnqueueResult",
    "DeliveryStatusEnqueueResult",
    "IntakeEventUnavailable",
    "IntakeResult",
    "MediaEnrichmentResult",
    "UnsupportedInboundMedia",
    "enqueue_twilio_inbound",
    "enqueue_twilio_delivery_status",
    "ensure_storm_file",
    "phone_hash",
    "process_intake_job",
    "process_media_job",
    "reconcile_media_failure",
    "resolve_hazard_event",
]
