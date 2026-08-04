"""Real-Postgres proof that media enrichment gates, but never verifies, claims."""

from __future__ import annotations

import hashlib
import io
import json

from PIL import Image
from sqlalchemy import func, select, text

from lighthouse_contracts import SOL_PRIORITY, AgentName, ClaimStatus, Event, JobStatus, Posture

from app.intake.media import FetchedMedia, StoredMedia, TWILIO_MEDIA_JOB_TYPE
from app.intake.service import (
    enqueue_twilio_inbound,
    process_intake_job,
    process_media_job,
    reconcile_media_failure,
)
from app.intake.transcription import DeterministicTranscriber, TranscriptionResult
from app.intake.twilio import parse_inbound
from app.models import AgentJob, Claim, HazardEvent, LedgerEntry


ACCOUNT_SID = "AC" + "a" * 32
MESSAGE_SID = "SM" + "c" * 32
MEDIA_SID = "ME" + "d" * 32
MEDIA_URL = (
    f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}/"
    f"Messages/{MESSAGE_SID}/Media/{MEDIA_SID}"
)
PHONE = "+18765550201"
OGG = b"OggS" + b"\x00" * 128 + b"OpusHead" + b"\x00" * 32
DIGEST = hashlib.sha256(OGG).hexdigest()


class _Fetcher:
    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []

    def fetch(self, url, *, message_sid, expected_content_type):
        self.calls.append((url, message_sid, expected_content_type))
        return FetchedMedia(OGG, "audio/ogg", DIGEST)


class _Store:
    def __init__(self):
        self.calls: list[str] = []

    def put(self, media):
        self.calls.append(media.sha256)
        key = f"intake/sha256/{media.sha256[:2]}/{media.sha256}"
        return StoredMedia(
            uri=f"r2://lighthouse-media/{key}",
            object_key=key,
            content_type=media.content_type,
            sha256=media.sha256,
            size_bytes=media.size_bytes,
        )


class _PhotoFetcher:
    def __init__(self, data: bytes):
        self.media = FetchedMedia(data, "image/png", hashlib.sha256(data).hexdigest())

    def fetch(self, url, *, message_sid, expected_content_type):
        assert url.startswith("https://api.twilio.com/")
        assert message_sid.startswith("SM")
        assert expected_content_type == "image/png"
        return self.media


def test_voice_note_is_hashed_transcribed_extracted_then_queued_once(session):
    event = HazardEvent(
        name="Voice intake event",
        external_ref="voice-intake-flow",
        current_posture=Posture.ACT,
        replay=False,
    )
    session.add(event)
    session.flush()
    inbound = parse_inbound(
        {
            "MessageSid": MESSAGE_SID,
            "From": f"whatsapp:{PHONE}",
            "Body": "",
            "NumMedia": "1",
            "MediaUrl0": MEDIA_URL,
            "MediaContentType0": "audio/ogg",
        }
    )
    enqueued = enqueue_twilio_inbound(
        session,
        inbound,
        hazard_external_ref=event.external_ref,
    )
    intake_job = session.get(AgentJob, enqueued.job_id)
    assert intake_job is not None
    filed = process_intake_job(session, dict(intake_job.payload))
    assert filed.verification_state == "MEDIA_PENDING"
    session.refresh(intake_job)
    minimized_payload = json.dumps(intake_job.payload)
    assert MEDIA_URL not in minimized_payload
    assert "body" not in intake_job.payload
    assert "media" not in intake_job.payload
    assert intake_job.payload["retention_state"] == "MINIMIZED_AFTER_CLAIM"

    claim = session.get(Claim, filed.claim_id)
    assert claim is not None
    assert claim.status is ClaimStatus.FILED
    assert claim.transcript is None
    assert claim.partial is True
    assert session.scalar(
        select(func.count()).select_from(AgentJob).where(
            AgentJob.job_type == str(AgentName.VERIFICATION_AGENT),
            AgentJob.payload["claim_id"].astext == str(claim.id),
        )
    ) == 0

    media_job = session.scalar(
        select(AgentJob).where(
            AgentJob.job_type == TWILIO_MEDIA_JOB_TYPE,
            AgentJob.payload["claim_id"].astext == str(claim.id),
        )
    )
    assert media_job is not None and media_job.status is JobStatus.QUEUED
    assert media_job.priority == SOL_PRIORITY
    assert media_job.payload["voice_priority"] is True
    pending = session.execute(
        text(
            "SELECT id, uri, sha256, payload FROM evidence "
            "WHERE claim_id=:claim_id AND kind='AUDIO'"
        ),
        {"claim_id": claim.id},
    ).mappings().one()
    assert pending["uri"] == MEDIA_URL
    assert pending["sha256"] is None
    assert pending["payload"]["media_state"] == "PENDING_FETCH"

    fetcher, store = _Fetcher(), _Store()
    transcriber = DeterministicTranscriber(
        TranscriptionResult(
            text="Mi trapped. Di roof blow off and wi need wata fi drink.",
            lang="jam",
            provider="deterministic_test",
            model="patois-fixture-v1",
        )
    )
    enriched = process_media_job(
        session,
        dict(media_job.payload),
        fetcher=fetcher,
        store=store,
        transcriber=transcriber,
    )
    assert enriched.verification_state == "QUEUED"
    assert fetcher.calls == [(MEDIA_URL, MESSAGE_SID, "audio/ogg")]
    assert store.calls == [DIGEST]
    assert transcriber.calls == [DIGEST]

    session.refresh(claim)
    assert claim.status is ClaimStatus.FILED  # This lane never auto-verifies.
    assert claim.transcript == "Mi trapped. Di roof blow off and wi need wata fi drink."
    assert claim.lang == "jam"
    assert claim.damage_type == "roof_damage"
    assert claim.reported_needs == ["water"]
    assert claim.partial is False
    assert claim.sol is True

    rows = session.execute(
        text(
            "SELECT kind::text AS kind, uri, payload, sha256, phash "
            "FROM evidence WHERE claim_id=:claim_id ORDER BY created_at, id"
        ),
        {"claim_id": claim.id},
    ).mappings().all()
    audio = next(row for row in rows if row["kind"] == "AUDIO")
    transcript = next(row for row in rows if row["kind"] == "TRANSCRIPT")
    assert audio["uri"].startswith("r2://lighthouse-media/intake/sha256/")
    assert audio["sha256"] == DIGEST
    assert audio["payload"]["media_state"] == "STORED"
    assert audio["payload"]["transcription_state"] == "COMPLETE"
    assert transcript["uri"] is None
    assert transcript["sha256"] == hashlib.sha256(claim.transcript.encode()).hexdigest()
    serialized = json.dumps(rows, default=str)
    assert MEDIA_URL not in serialized
    assert claim.transcript not in serialized

    verification_jobs = list(
        session.scalars(
            select(AgentJob).where(
                AgentJob.job_type == str(AgentName.VERIFICATION_AGENT),
                AgentJob.payload["claim_id"].astext == str(claim.id),
            )
        )
    )
    assert len(verification_jobs) == 1
    assert verification_jobs[0].priority == SOL_PRIORITY
    assert session.scalar(
        select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.action == str(Event.CLAIM_SOL_RAISED),
            LedgerEntry.subject_id == claim.id,
        )
    ) == 1

    duplicate = process_media_job(
        session,
        dict(media_job.payload),
        fetcher=fetcher,
        store=store,
        transcriber=transcriber,
    )
    assert duplicate.duplicate is True
    assert len(fetcher.calls) == len(store.calls) == len(transcriber.calls) == 1
    assert session.scalar(
        select(func.count()).select_from(AgentJob).where(
            AgentJob.job_type == str(AgentName.VERIFICATION_AGENT),
            AgentJob.payload["claim_id"].astext == str(claim.id),
        )
    ) == 1


def test_photo_gets_real_phash_before_verification_is_queued(session):
    event = HazardEvent(
        name="Photo intake event",
        external_ref="photo-intake-flow",
        current_posture=Posture.ACT,
        replay=False,
    )
    session.add(event)
    session.flush()
    message_sid = "SM" + "e" * 32
    media_sid = "ME" + "f" * 32
    media_url = (
        f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}/"
        f"Messages/{message_sid}/Media/{media_sid}"
    )
    image = Image.new("RGB", (20, 20))
    for x in range(20):
        for y in range(20):
            image.putpixel((x, y), (x * 10, y * 10, (x + y) * 5))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    photo = buffer.getvalue()

    enqueued = enqueue_twilio_inbound(
        session,
        parse_inbound(
            {
                "MessageSid": message_sid,
                "From": "whatsapp:+18765550202",
                "Body": "Mi wall fall and need shelter",
                "NumMedia": "1",
                "MediaUrl0": media_url,
                "MediaContentType0": "image/png",
            }
        ),
        hazard_external_ref=event.external_ref,
    )
    intake_job = session.get(AgentJob, enqueued.job_id)
    assert intake_job is not None
    filed = process_intake_job(session, dict(intake_job.payload))
    media_job = session.scalar(
        select(AgentJob).where(
            AgentJob.job_type == TWILIO_MEDIA_JOB_TYPE,
            AgentJob.payload["claim_id"].astext == str(filed.claim_id),
        )
    )
    assert media_job is not None
    fetcher = _PhotoFetcher(photo)
    process_media_job(
        session,
        dict(media_job.payload),
        fetcher=fetcher,
        store=_Store(),
    )
    row = session.execute(
        text(
            "SELECT uri, sha256, phash, payload FROM evidence "
            "WHERE claim_id=:claim_id AND kind='PHOTO'"
        ),
        {"claim_id": filed.claim_id},
    ).mappings().one()
    assert row["uri"].startswith("r2://")
    assert row["sha256"] == fetcher.media.sha256
    assert len(row["phash"]) == 16
    assert row["payload"]["phash_state"] == "COMPUTED"
    assert session.scalar(
        select(func.count()).select_from(AgentJob).where(
            AgentJob.job_type == str(AgentName.VERIFICATION_AGENT),
            AgentJob.payload["claim_id"].astext == str(filed.claim_id),
        )
    ) == 1


def test_terminal_media_failure_scrubs_provider_url_and_queues_safe_review(session):
    event = HazardEvent(
        name="Failed media event",
        external_ref="failed-media-flow",
        current_posture=Posture.ACT,
        replay=False,
    )
    session.add(event)
    session.flush()
    message_sid = "SM" + "1" * 32
    media_sid = "ME" + "2" * 32
    media_url = (
        f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}/"
        f"Messages/{message_sid}/Media/{media_sid}"
    )
    enqueued = enqueue_twilio_inbound(
        session,
        parse_inbound(
            {
                "MessageSid": message_sid,
                "From": "whatsapp:+18765550203",
                "Body": "Mi roof gone and need water",
                "NumMedia": "1",
                "MediaUrl0": media_url,
                "MediaContentType0": "image/jpeg",
            }
        ),
        hazard_external_ref=event.external_ref,
    )
    intake_job = session.get(AgentJob, enqueued.job_id)
    assert intake_job is not None
    filed = process_intake_job(session, dict(intake_job.payload))
    media_job = session.scalar(
        select(AgentJob).where(
            AgentJob.job_type == TWILIO_MEDIA_JOB_TYPE,
            AgentJob.payload["claim_id"].astext == str(filed.claim_id),
        )
    )
    assert media_job is not None

    reconcile_media_failure(
        session,
        {
            **dict(media_job.payload),
            "terminal_error_code": "handler_error:MediaBoundaryError",
        },
    )

    failed = session.execute(
        text(
            "SELECT uri, sha256, phash, payload FROM evidence "
            "WHERE claim_id=:claim_id AND kind='PHOTO'"
        ),
        {"claim_id": filed.claim_id},
    ).mappings().one()
    assert failed["uri"] is None
    assert failed["sha256"] is None
    assert failed["phash"] is None
    assert failed["payload"]["media_state"] == "FAILED"
    assert failed["payload"]["failure_code"] == "handler_error:MediaBoundaryError"
    assert media_url not in json.dumps(failed, default=str)
    verification_job = session.scalar(
        select(AgentJob).where(
            AgentJob.job_type == str(AgentName.VERIFICATION_AGENT),
            AgentJob.payload["claim_id"].astext == str(filed.claim_id),
        )
    )
    assert verification_job is not None
    assert session.scalar(
        select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.action == "intake.media_failed",
            LedgerEntry.subject_id == filed.claim_id,
        )
    ) == 1
