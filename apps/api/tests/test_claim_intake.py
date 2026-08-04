"""Signed Twilio edge -> identity -> claim/evidence -> verification queue."""

from __future__ import annotations

import importlib
import json
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from lighthouse_contracts import (
    SOL_PRIORITY,
    AgentName,
    ClaimStatus,
    Event,
    JobStatus,
    Posture,
    StormFileState,
)

from app.intake.service import phone_hash, process_intake_job
from app.intake.twilio import public_request_url, signature_for, signature_is_valid
from app.models import AgentJob, Claim, HazardEvent, LedgerEntry, StormFile
from app.web import app


TOKEN = "test-primary-auth-token"
PUBLIC_BASE_URL = "https://lighthouse.example"
WHATSAPP_PATH = "/webhooks/twilio/whatsapp"
STATUS_PATH = "/webhooks/twilio/status"
PHONE = "+18765550199"


def _sid(hex_digit: str) -> str:
    return "SM" + hex_digit * 32


def _settings(token: str | None = TOKEN):
    return SimpleNamespace(
        twilio_auth_token=token,
        public_base_url=PUBLIC_BASE_URL,
        intake_hazard_external_ref=None,
    )


def _install_edge(monkeypatch: pytest.MonkeyPatch, session, *, token: str | None = TOKEN):
    module = importlib.import_module("app.intake.router")

    @contextmanager
    def test_session_scope():
        yield session

    monkeypatch.setattr(module, "get_settings", lambda: _settings(token))
    monkeypatch.setattr(module, "session_scope", test_session_scope)
    return TestClient(app, raise_server_exceptions=False)


def _signed_post(client: TestClient, path: str, form: dict[str, str]):
    signature = signature_for(PUBLIC_BASE_URL + path, form, TOKEN)
    return client.post(path, data=form, headers={"X-Twilio-Signature": signature})


def _text_form(*, sid: str, body: str) -> dict[str, str]:
    return {
        "MessageSid": sid,
        "From": f"whatsapp:{PHONE}",
        "To": "whatsapp:+14155238886",
        "ProfileName": "Must not persist in the queue",
        "Body": body,
        "NumMedia": "0",
    }


def _active_event(session) -> HazardEvent:
    event = HazardEvent(
        name="Melissa live intake test",
        external_ref=f"intake-{_sid('e')}",
        current_posture=Posture.ACT,
        replay=True,
    )
    session.add(event)
    session.flush()
    return event


def test_signature_matches_twilio_documented_vector():
    """Independent official vector catches URL/ordering/HMAC drift."""
    url = "https://example.com/myapp.php?foo=1&bar=2"
    params = {
        "Digits": "1234",
        "To": "+18005551212",
        "From": "+14158675310",
        "Caller": "+14158675310",
        "CallSid": "CA1234567890ABCDE",
    }
    expected = "L/OH5YylLD5NRKLltdqwSvS0BnU="
    assert signature_for(url, params, "12345") == expected
    assert signature_is_valid(
        url=url,
        params=params,
        auth_token="12345",
        supplied_signature=expected,
    )
    assert not signature_is_valid(
        url=url,
        params=params,
        auth_token="12345",
        supplied_signature="invalid",
    )


def test_public_signature_url_uses_configured_origin_path_and_query():
    assert public_request_url(
        public_base_url="https://api.example/",
        path=WHATSAPP_PATH,
        query="event=al132025",
    ) == "https://api.example/webhooks/twilio/whatsapp?event=al132025"


def test_both_twilio_routes_fail_closed(monkeypatch, session):
    client = _install_edge(monkeypatch, session)
    inbound = _text_form(sid=_sid("a"), body="Mi roof gone")
    status = {"MessageSid": _sid("b"), "MessageStatus": "delivered"}

    assert client.post(WHATSAPP_PATH, data=inbound).status_code == 403
    assert client.post(STATUS_PATH, data=status).status_code == 403
    assert client.post(
        WHATSAPP_PATH,
        data=inbound,
        headers={"X-Twilio-Signature": "forged"},
    ).status_code == 403

    disabled = _install_edge(monkeypatch, session, token=None)
    assert disabled.post(WHATSAPP_PATH, data=inbound).status_code == 503
    assert disabled.post(STATUS_PATH, data=status).status_code == 503


def test_claim_reads_require_recent_human_bearer(monkeypatch, session):
    module = importlib.import_module("app.intake.claims")

    @contextmanager
    def test_session_scope():
        yield session

    monkeypatch.setattr(module, "session_scope", test_session_scope)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/api/claims").status_code == 401
    assert client.get(f"/api/claims/{uuid.uuid4()}").status_code == 401


def test_signed_status_callback_is_accepted(monkeypatch, session):
    client = _install_edge(monkeypatch, session)
    status = {
        "MessageSid": _sid("c"),
        "MessageStatus": "delivered",
        "ErrorCode": "",
        "To": f"whatsapp:{PHONE}",
        "Body": "must not persist",
    }
    first = _signed_post(client, STATUS_PATH, status)
    second = _signed_post(client, STATUS_PATH, status)
    assert first.status_code == second.status_code == 204
    assert first.text == ""

    jobs = list(
        session.scalars(select(AgentJob).where(AgentJob.job_type == "twilio_delivery_status"))
    )
    assert len(jobs) == 1
    assert jobs[0].payload["provider_message_sid"] == status["MessageSid"]
    assert jobs[0].payload["message_status"] == "delivered"
    assert jobs[0].payload["reconciliation_state"] == "PENDING_HANDLER"
    serialized = json.dumps(jobs[0].payload)
    assert PHONE not in serialized
    assert status["Body"] not in serialized


def test_signed_text_is_identity_linked_and_queue_deduplicated(
    monkeypatch, session, caplog
):
    client = _install_edge(monkeypatch, session)
    body = "Mi roof gone and we need water"
    form = _text_form(sid=_sid("1"), body=body)

    first = _signed_post(client, WHATSAPP_PATH, form)
    second = _signed_post(client, WHATSAPP_PATH, form)
    assert first.status_code == second.status_code == 200
    assert first.text == "<Response></Response>"
    assert first.headers["content-type"].startswith("application/xml")

    storm_files = list(
        session.scalars(select(StormFile).where(StormFile.phone_hash == phone_hash(PHONE)))
    )
    assert len(storm_files) == 1
    assert storm_files[0].phone == PHONE
    assert storm_files[0].state is StormFileState.AFFECTED
    assert storm_files[0].thin is True
    assert storm_files[0].synthetic is False

    jobs = list(
        session.scalars(
            select(AgentJob).where(AgentJob.job_type == str(AgentName.INTAKE_AGENT))
        )
    )
    assert len(jobs) == 1
    payload = jobs[0].payload
    assert payload["storm_file_id"] == str(storm_files[0].id)
    assert payload["provider_message_sid"] == form["MessageSid"]
    assert payload["body"] == body
    serialized = json.dumps(payload)
    assert PHONE not in serialized
    assert "whatsapp:" not in serialized
    assert "ProfileName" not in serialized
    assert "form" not in payload

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert PHONE not in messages
    assert body not in messages


def test_text_job_persists_claim_evidence_ledger_and_verification(session):
    _active_event(session)
    form = _text_form(sid=_sid("2"), body="Mi roof gone and need water")

    # Exercise the same edge service without HTTP so this test can inspect the
    # worker transaction directly.
    from app.intake.service import enqueue_twilio_inbound
    from app.intake.twilio import parse_inbound

    enqueued = enqueue_twilio_inbound(session, parse_inbound(form))
    intake_job = session.get(AgentJob, enqueued.job_id)
    assert intake_job is not None

    result = process_intake_job(session, dict(intake_job.payload))
    claim = session.get(Claim, result.claim_id)
    assert claim is not None
    assert claim.storm_file_id == enqueued.storm_file_id
    assert claim.status is ClaimStatus.FILED
    assert claim.transcript == form["Body"]
    assert claim.damage_type == "roof_damage"
    assert claim.reported_needs == ["water"]
    assert claim.partial is True
    assert result.verification_state == "QUEUED"

    evidence = session.execute(
        text(
            """
            SELECT kind::text, uri, payload, sha256
            FROM evidence WHERE claim_id = :claim_id
            """
        ),
        {"claim_id": claim.id},
    ).one()
    assert evidence.kind == "TRANSCRIPT"
    assert evidence.uri is None
    assert evidence.payload["provider_message_sid"] == form["MessageSid"]
    assert form["Body"] not in json.dumps(evidence.payload)
    assert len(evidence.sha256) == 64

    verification_jobs = list(
        session.scalars(
            select(AgentJob).where(
                AgentJob.job_type == str(AgentName.VERIFICATION_AGENT),
                AgentJob.payload["claim_id"].astext == str(claim.id),
            )
        )
    )
    assert len(verification_jobs) == 1
    assert verification_jobs[0].status is JobStatus.QUEUED
    assert verification_jobs[0].priority == 0

    created_entry = session.scalar(
        select(LedgerEntry).where(
            LedgerEntry.action == str(Event.CLAIM_CREATED),
            LedgerEntry.subject_id == claim.id,
        )
    )
    assert created_entry is not None
    assert created_entry.payload["verification_state"] == "QUEUED"
    assert created_entry.payload["evidence_count"] == 1

    duplicate = process_intake_job(session, dict(intake_job.payload))
    assert duplicate.duplicate is True
    assert duplicate.claim_id == claim.id
    assert session.scalar(select(func.count()).select_from(Claim)) == 1
    assert session.execute(
        text("SELECT count(*) FROM evidence WHERE claim_id = :claim_id"),
        {"claim_id": claim.id},
    ).scalar_one() == 1


def test_authenticated_claim_reads_are_redacted(monkeypatch, session):
    _active_event(session)
    from app.intake.service import enqueue_twilio_inbound
    from app.intake.twilio import parse_inbound

    body = "Mi roof gone and need insulin"
    enqueued = enqueue_twilio_inbound(
        session,
        parse_inbound(_text_form(sid=_sid("6"), body=body)),
    )
    intake_job = session.get(AgentJob, enqueued.job_id)
    assert intake_job is not None
    result = process_intake_job(session, dict(intake_job.payload))

    module = importlib.import_module("app.intake.claims")

    @contextmanager
    def test_session_scope():
        yield session

    seen: list[set] = []

    def allow(_session, authorization, *, allowed_roles):
        assert authorization == "Bearer test-operator"
        seen.append(set(allowed_roles))
        return SimpleNamespace(user=None, credential=None)

    monkeypatch.setattr(module, "session_scope", test_session_scope)
    monkeypatch.setattr(module, "authenticate_human", allow)
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"Authorization": "Bearer test-operator"}

    listed = client.get("/api/claims?limit=100", headers=headers)
    detailed = client.get(f"/api/claims/{result.claim_id}", headers=headers)
    assert listed.status_code == detailed.status_code == 200
    assert len(seen) == 2

    list_body = listed.json()
    detail_body = detailed.json()
    assert list_body["claims"][0]["id"] == str(result.claim_id)
    assert detail_body["id"] == str(result.claim_id)
    assert detail_body["verification_state"] == "QUEUED"
    assert detail_body["evidence"][0]["has_uri"] is False
    assert detail_body["verification"] is None
    serialized = json.dumps({"list": list_body, "detail": detail_body})
    assert PHONE not in serialized
    assert phone_hash(PHONE) not in serialized
    assert body not in serialized
    assert "provider_message_sid" not in serialized
    assert "uri" not in detail_body["evidence"][0]


def test_voice_note_files_then_defers_verification_to_durable_media_job(session):
    _active_event(session)
    from app.intake.service import enqueue_twilio_inbound
    from app.intake.twilio import parse_inbound

    sid = _sid("3")
    media_url = (
        "https://api.twilio.com/2010-04-01/Accounts/ACtest/"
        f"Messages/{sid}/Media/ME123"
    )
    form = {
        "MessageSid": sid,
        "From": f"whatsapp:{PHONE}",
        "Body": "",
        "NumMedia": "1",
        "MediaUrl0": media_url,
        "MediaContentType0": "audio/ogg",
    }
    enqueued = enqueue_twilio_inbound(session, parse_inbound(form))
    intake_job = session.get(AgentJob, enqueued.job_id)
    assert intake_job is not None
    result = process_intake_job(session, dict(intake_job.payload))

    claim = session.get(Claim, result.claim_id)
    assert claim is not None
    assert claim.transcript is None
    assert claim.partial is True
    assert claim.status is ClaimStatus.FILED

    evidence = session.execute(
        text(
            """
            SELECT kind::text, uri, payload
            FROM evidence WHERE claim_id = :claim_id
            """
        ),
        {"claim_id": claim.id},
    ).one()
    assert evidence.kind == "AUDIO"
    assert evidence.uri == media_url
    assert evidence.payload["media_state"] == "PENDING_FETCH"
    assert evidence.payload["transcription_state"] == "PENDING"
    assert result.verification_state == "MEDIA_PENDING"
    assert session.scalar(
        select(func.count()).select_from(AgentJob).where(
            AgentJob.job_type == str(AgentName.VERIFICATION_AGENT),
            AgentJob.payload["claim_id"].astext == str(claim.id),
        )
    ) == 0
    media_job = session.scalar(
        select(AgentJob).where(
            AgentJob.job_type == "intake_media_enrichment",
            AgentJob.payload["claim_id"].astext == str(claim.id),
        )
    )
    assert media_job is not None and media_job.status is JobStatus.QUEUED


def test_configured_event_ref_wins_when_multiple_hazards_are_open(session):
    from app.intake.service import enqueue_twilio_inbound
    from app.intake.twilio import parse_inbound

    gilbert = HazardEvent(
        name="Gilbert",
        external_ref="al081988",
        current_posture=Posture.ACT,
        replay=True,
    )
    melissa = HazardEvent(
        name="Melissa",
        external_ref="al132025-intake-test",
        current_posture=Posture.ACT,
        replay=True,
    )
    session.add_all([gilbert, melissa])
    session.flush()

    enqueued = enqueue_twilio_inbound(
        session,
        parse_inbound(_text_form(sid=_sid("5"), body="Mi roof gone")),
        hazard_external_ref=melissa.external_ref,
    )
    intake_job = session.get(AgentJob, enqueued.job_id)
    assert intake_job is not None
    assert intake_job.payload["hazard_external_ref"] == melissa.external_ref
    result = process_intake_job(session, dict(intake_job.payload))
    claim = session.get(Claim, result.claim_id)
    assert claim is not None
    assert claim.hazard_event_id == melissa.id


def test_existing_phone_is_reused_and_sol_rides_priority_queue(session):
    event = _active_event(session)
    existing = StormFile(
        phone=PHONE,
        phone_hash=phone_hash(PHONE),
        state=StormFileState.REGISTERED,
        thin=False,
        synthetic=True,
        parish="St Elizabeth",
    )
    session.add(existing)
    session.flush()

    from app.intake.service import enqueue_twilio_inbound
    from app.intake.twilio import parse_inbound

    form = _text_form(sid=_sid("4"), body="I am trapped and bleeding, roof gone")
    enqueued = enqueue_twilio_inbound(session, parse_inbound(form))
    assert enqueued.storm_file_id == existing.id
    assert enqueued.created is True
    intake_job = session.get(AgentJob, enqueued.job_id)
    assert intake_job is not None
    assert intake_job.priority == SOL_PRIORITY
    assert existing.state is StormFileState.REGISTERED

    result = process_intake_job(
        session,
        {**dict(intake_job.payload), "hazard_event_id": str(event.id)},
    )
    assert existing.state is StormFileState.AFFECTED
    claim = session.get(Claim, result.claim_id)
    assert claim is not None
    assert claim.storm_file_id == existing.id
    assert claim.sol is True
    assert claim.claim_ref.startswith("SE-")

    verification_job = session.scalar(
        select(AgentJob).where(
            AgentJob.job_type == str(AgentName.VERIFICATION_AGENT),
            AgentJob.payload["claim_id"].astext == str(claim.id),
        )
    )
    assert verification_job is not None
    assert verification_job.priority == SOL_PRIORITY
    assert session.scalar(
        select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.action == str(Event.CLAIM_SOL_RAISED),
            LedgerEntry.subject_id == claim.id,
        )
    ) == 1
