"""The intake conversation: location shares, follow-ups, and replies.

One household, one open claim: messages after the first fill the claim in
rather than filing siblings, a WhatsApp location share pins the Storm File
and the claim, and the reply engine asks only for what is still missing —
never more than three times, and never at all unless deliberately enabled.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from lighthouse_contracts import AgentName, JobStatus

from app.config import get_settings
from app.intake import replies
from app.intake.service import enqueue_twilio_inbound, process_intake_job
from app.intake.twilio import InvalidTwilioPayload, parse_inbound
from app.models import AgentJob, Claim, HazardEvent, LedgerEntry, StormFile

PHONE = "+18765550142"

#: Black River, Saint Elizabeth — inside the COD admin-1 boundary.
LATITUDE, LONGITUDE = "18.0292", "-77.8508"


def _sid(hex_digit: str) -> str:
    return "SM" + hex_digit * 32


def _form(
    *,
    sid: str,
    body: str = "",
    latitude: str | None = None,
    longitude: str | None = None,
) -> dict[str, str]:
    form = {
        "MessageSid": sid,
        "From": f"whatsapp:{PHONE}",
        "To": "whatsapp:+14155238886",
        "Body": body,
        "NumMedia": "0",
    }
    if latitude is not None:
        form["Latitude"] = latitude
    if longitude is not None:
        form["Longitude"] = longitude
    return form


def _event(session) -> HazardEvent:
    event = HazardEvent(name="Melissa conversation test", replay=True)
    session.add(event)
    session.flush()
    return event


def _process(session, form: dict[str, str]):
    enqueued = enqueue_twilio_inbound(session, parse_inbound(form))
    job = session.get(AgentJob, enqueued.job_id)
    return process_intake_job(session, dict(job.payload))


def test_location_share_parses_and_lone_or_bad_coordinates_are_rejected():
    inbound = parse_inbound(
        _form(sid=_sid("a"), latitude=LATITUDE, longitude=LONGITUDE)
    )
    assert inbound.latitude == pytest.approx(18.0292)
    assert inbound.longitude == pytest.approx(-77.8508)

    with pytest.raises(InvalidTwilioPayload, match="missing a coordinate"):
        parse_inbound(_form(sid=_sid("b"), latitude=LATITUDE))
    with pytest.raises(InvalidTwilioPayload, match="not numeric"):
        parse_inbound(_form(sid=_sid("c"), latitude="north", longitude=LONGITUDE))
    with pytest.raises(InvalidTwilioPayload, match="out of range"):
        parse_inbound(_form(sid=_sid("d"), latitude="91.0", longitude=LONGITUDE))
    with pytest.raises(InvalidTwilioPayload, match="no text, media, or location"):
        parse_inbound(_form(sid=_sid("e")))


def test_location_only_message_files_a_claim_pinned_to_a_parish(session):
    _event(session)
    result = _process(
        session, _form(sid=_sid("1"), latitude=LATITUDE, longitude=LONGITUDE)
    )

    claim = session.get(Claim, result.claim_id)
    storm_file = session.get(StormFile, result.storm_file_id)
    assert claim.location is not None
    assert storm_file.location is not None
    assert storm_file.parish == "Saint Elizabeth"
    assert claim.partial is True


def test_follow_ups_fill_the_open_claim_instead_of_filing_new_ones(session):
    _event(session)
    first = _process(session, _form(sid=_sid("2"), body="Mi roof gone"))
    second = _process(
        session, _form(sid=_sid("3"), latitude=LATITUDE, longitude=LONGITUDE)
    )
    third = _process(session, _form(sid=_sid("4"), body="need water fi drink"))

    assert first.claim_id == second.claim_id == third.claim_id
    claims = session.scalars(select(Claim)).all()
    assert len(claims) == 1
    claim = claims[0]
    assert claim.location is not None
    assert claim.damage_type == "roof_damage"
    assert "water" in claim.reported_needs
    assert "Mi roof gone" in claim.transcript and "need water" in claim.transcript

    follow_ups = session.scalars(
        select(LedgerEntry).where(LedgerEntry.action == "claim.follow_up_applied")
    ).all()
    assert len(follow_ups) == 2


def test_new_evidence_requeues_verification_after_a_completed_run(session):
    _event(session)
    result = _process(session, _form(sid=_sid("5"), body="Mi roof gone"))

    def verification_jobs():
        return session.scalars(
            select(AgentJob).where(
                AgentJob.job_type == str(AgentName.VERIFICATION_AGENT),
                AgentJob.payload["claim_id"].astext == str(result.claim_id),
            )
        ).all()

    jobs = verification_jobs()
    assert len(jobs) == 1

    # While the first run is still queued, a follow-up must not duplicate it.
    _process(session, _form(sid=_sid("6"), latitude=LATITUDE, longitude=LONGITUDE))
    assert len(verification_jobs()) == 1

    # Once it has run, later evidence earns a fresh verification.
    jobs[0].status = JobStatus.DONE
    session.flush()
    _process(session, _form(sid=_sid("7"), body="walls crack too"))
    assert len(verification_jobs()) == 2


def _live_replies(monkeypatch):
    sent: list[tuple[str, str]] = []
    live = get_settings().model_copy(update={"intake_reply_mode": "live"})
    monkeypatch.setattr(replies, "get_settings", lambda: live)
    monkeypatch.setattr(
        replies,
        "_send",
        lambda *, to_phone, body: sent.append((to_phone, body)),
    )
    return sent


def test_reply_acknowledges_and_asks_for_location_first(session, monkeypatch):
    sent = _live_replies(monkeypatch)
    _event(session)
    result = _process(session, _form(sid=_sid("8"), body="Mi roof gone"))

    assert len(sent) == 1
    to_phone, body = sent[0]
    assert to_phone == PHONE
    assert result.claim_ref in body
    assert "share your location" in body.lower()

    entry = session.scalar(
        select(LedgerEntry).where(LedgerEntry.action == replies.REPLY_SENT_ACTION)
    )
    assert entry.payload["asked"] == "location"
    assert entry.payload["acknowledged_creation"] is True


def test_reply_confirms_location_then_asks_for_a_photo(session, monkeypatch):
    sent = _live_replies(monkeypatch)
    _event(session)
    _process(session, _form(sid=_sid("9"), body="Mi roof gone"))
    _process(session, _form(sid=_sid("a"), latitude=LATITUDE, longitude=LONGITUDE))

    assert len(sent) == 2
    _, body = sent[1]
    assert "location received" in body.lower()
    assert "photo" in body.lower()


def test_reply_stops_asking_after_the_follow_up_cap(session, monkeypatch):
    sent = _live_replies(monkeypatch)
    _event(session)
    result = _process(session, _form(sid=_sid("b"), body="Mi roof gone"))
    claim = session.get(Claim, result.claim_id)
    storm_file = session.get(StormFile, result.storm_file_id)

    for _ in range(replies.MAX_FOLLOW_UPS - 1):
        replies.maybe_reply(
            session, claim=claim, storm_file=storm_file, created=False, applied=["message"]
        )
    assert sum("share your location" in body.lower() for _, body in sent) == 3

    composed = replies.compose_reply(
        session, claim=claim, storm_file=storm_file, created=False, applied=["message"]
    )
    assert composed is not None
    body, asked = composed
    assert asked is None
    assert "share your location" not in body.lower()


def test_replies_stay_silent_when_disabled(session, monkeypatch):
    def explode(**_kwargs):
        raise AssertionError("a disabled reply engine must not send")

    monkeypatch.setattr(replies, "_send", explode)
    _event(session)
    _process(session, _form(sid=_sid("c"), body="Mi roof gone"))

    assert (
        session.scalar(
            select(LedgerEntry).where(LedgerEntry.action == replies.REPLY_SENT_ACTION)
        )
        is None
    )


def test_reply_failure_never_fails_intake(session, monkeypatch):
    live = get_settings().model_copy(update={"intake_reply_mode": "live"})
    monkeypatch.setattr(replies, "get_settings", lambda: live)

    def explode(**_kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(replies, "_send", explode)
    _event(session)
    result = _process(session, _form(sid=_sid("d"), body="Mi roof gone"))

    assert session.get(Claim, result.claim_id) is not None
    assert (
        session.scalar(
            select(LedgerEntry).where(LedgerEntry.action == replies.REPLY_SENT_ACTION)
        )
        is None
    )
