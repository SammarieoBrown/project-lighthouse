"""Evidence, idempotency, and Director-gate tests for the Damage Assessment Agent."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from lighthouse_contracts import (
    ActorKind,
    AgentName,
    AppRole,
    ClaimStatus,
    DamageAssessmentVerdict,
    DamageBand,
    Event,
    StormFileState,
)
from lighthouse_contracts.agents import (
    AGENT_IO,
    PROPOSE_ONLY,
    DamageAssessmentOutput,
    DamagePhotoFinding,
)

from app import verification_service
from app.damage_assessment_service import (
    CURRENCY,
    MAX_ASSESSMENT_BYTES,
    MAX_ASSESSMENT_PHOTOS,
    ClaudeDamageAssessor,
    DamageAssessmentNotRunnable,
    DamageAssessmentProviderDisabled,
    DeterministicDamageAssessor,
    DeterministicPhotoStore,
    ReviewDecisionConflict,
    record_damage_assessment_decision,
    run_damage_assessment,
)
from app.intake.media import FetchedMedia
from app.models import AgentJob, DamageAssessment, LedgerEntry
from app.worker import load_handlers

from factories import make_claim, make_event, make_storm_file, make_user


POINT = "POINT(-77.1000 18.1000)"


def _set_location(session, table: str, row_id: uuid.UUID, wkt: str = POINT) -> None:
    assert table in {"claim", "storm_file"}
    session.execute(
        text(f"UPDATE {table} SET location = ST_GeogFromText(:wkt) WHERE id = :row_id"),
        {"wkt": f"SRID=4326;{wkt}", "row_id": row_id},
    )


def _photo_evidence(
    session,
    claim_id: uuid.UUID,
    *,
    content_type: str = "image/jpeg",
    sha256: str | None = None,
) -> uuid.UUID:
    evidence_id = uuid.uuid4()
    digest = sha256 or uuid.uuid4().hex.ljust(64, "0")
    session.execute(
        text(
            """
            INSERT INTO evidence (id, claim_id, kind, uri, payload, sha256)
            VALUES (
              :id, :claim_id, 'PHOTO', :uri, CAST(:payload AS jsonb), :sha256
            )
            """
        ),
        {
            "id": evidence_id,
            "claim_id": claim_id,
            "uri": f"r2://lighthouse-private/intake/sha256/{digest}",
            "payload": json.dumps({"media_state": "STORED", "content_type": content_type}),
            "sha256": digest,
        },
    )
    return evidence_id


def _verified_claim_with_photo(session, *, n_photos: int = 1):
    event = make_event(session)
    sf = make_storm_file(session, state=StormFileState.VERIFIED)
    claim = make_claim(session, sf, event, status=ClaimStatus.VERIFIED, damage_type="roof_loss")
    _set_location(session, "claim", claim.id)
    evidence_ids = [_photo_evidence(session, claim.id) for _ in range(n_photos)]
    session.flush()
    return sf, claim, evidence_ids


def _assessor(band="MAJOR", low=40000.0, high=90000.0, evidence_ids=None):
    findings = [
        DamagePhotoFinding(
            evidence_id=eid,
            observed_damage="roof sheeting torn off, exposed rafters",
            band=band,
            confidence=0.8,
        )
        for eid in (evidence_ids or [])
    ]
    output = DamageAssessmentOutput(
        band=band,
        estimate_low=low,
        estimate_high=high,
        currency="JMD",
        confidence=0.75,
        findings=findings,
        location_source="claim",
        model_version="test-model-v1",
        rationale="Roof damage clearly visible across all submitted photos.",
    )
    return DeterministicDamageAssessor(output=output)


def _seed_store(session, claim_id: uuid.UUID, evidence_ids: list[uuid.UUID]) -> DeterministicPhotoStore:
    store = DeterministicPhotoStore()
    rows = session.execute(
        text("SELECT id, uri, sha256 FROM evidence WHERE claim_id = :claim_id"),
        {"claim_id": claim_id},
    ).all()
    for row in rows:
        key = row.uri.removeprefix("r2://").split("/", 1)[1]
        store.media_by_key[key] = FetchedMedia(
            data=b"fake-jpeg-bytes", content_type="image/jpeg", sha256=row.sha256
        )
    return store


def test_no_photo_evidence_is_not_runnable(session):
    event = make_event(session)
    sf = make_storm_file(session, state=StormFileState.VERIFIED)
    claim = make_claim(session, sf, event, status=ClaimStatus.VERIFIED)

    with pytest.raises(DamageAssessmentNotRunnable, match="no readable photo evidence"):
        run_damage_assessment(session, claim.id, assessor=_assessor(), store=DeterministicPhotoStore())


def test_unverified_claim_is_not_runnable(session):
    event = make_event(session)
    sf = make_storm_file(session, state=StormFileState.AFFECTED)
    claim = make_claim(session, sf, event, status=ClaimStatus.FILED)
    _photo_evidence(session, claim.id)
    session.flush()

    with pytest.raises(DamageAssessmentNotRunnable, match="assessable state"):
        run_damage_assessment(session, claim.id, assessor=_assessor(), store=DeterministicPhotoStore())


def test_no_usable_location_is_not_runnable(session):
    event = make_event(session)
    sf = make_storm_file(session, state=StormFileState.VERIFIED)
    claim = make_claim(session, sf, event, status=ClaimStatus.VERIFIED)
    _photo_evidence(session, claim.id)
    session.flush()

    with pytest.raises(DamageAssessmentNotRunnable, match="usable point location"):
        run_damage_assessment(session, claim.id, assessor=_assessor(), store=DeterministicPhotoStore())


def test_disabled_provider_is_refused_when_no_assessor_injected(session):
    sf, claim, evidence_ids = _verified_claim_with_photo(session)

    with pytest.raises(DamageAssessmentProviderDisabled):
        run_damage_assessment(session, claim.id)


def test_happy_path_stores_proposal_ties_location_and_never_transitions_claim(session):
    sf, claim, evidence_ids = _verified_claim_with_photo(session)
    store = _seed_store(session, claim.id, evidence_ids)
    assessor = _assessor(evidence_ids=evidence_ids)

    result = run_damage_assessment(session, claim.id, assessor=assessor, store=store)

    assert result.created is True
    assert result.assessment.actor_kind is ActorKind.AGENT
    assert result.assessment.agent_name == str(AgentName.DAMAGE_ASSESSMENT_AGENT)
    assert str(result.assessment.verdict) == "PROPOSED"
    assert result.assessment.location_source == "claim"
    assert float(result.assessment.estimate_low) == 40000.0
    assert float(result.assessment.estimate_high) == 90000.0
    assert result.output.model_version == "anthropic:claude-opus-5"
    assert claim.status is ClaimStatus.VERIFIED  # untouched
    assert assessor.calls == [(claim.id, evidence_ids)]
    assert set(store.calls) == {
        key for key in store.media_by_key
    }

    replay = run_damage_assessment(session, claim.id, assessor=assessor, store=store)
    assert replay.created is False
    assert replay.assessment.id == result.assessment.id
    assert session.scalar(
        select(func.count()).select_from(DamageAssessment).where(
            DamageAssessment.claim_id == claim.id
        )
    ) == 1
    # The replay short-circuits before ever calling the assessor again.
    assert len(assessor.calls) == 1


def test_findings_outside_the_claim_are_rejected(session):
    sf, claim, evidence_ids = _verified_claim_with_photo(session)
    store = _seed_store(session, claim.id, evidence_ids)
    assessor = _assessor(evidence_ids=[uuid.uuid4()])

    with pytest.raises(DamageAssessmentNotRunnable, match="outside this claim"):
        run_damage_assessment(session, claim.id, assessor=assessor, store=store)


def test_director_approval_appends_override_and_can_adjust_the_range(session):
    sf, claim, evidence_ids = _verified_claim_with_photo(session)
    store = _seed_store(session, claim.id, evidence_ids)
    proposal = run_damage_assessment(
        session, claim.id, assessor=_assessor(evidence_ids=evidence_ids), store=store
    )
    director = make_user(session, AppRole.DIRECTOR)

    approved = record_damage_assessment_decision(
        session,
        claim_id=claim.id,
        assessment_id=proposal.assessment.id,
        director_id=director.id,
        verdict="APPROVED",
        rationale="Confirmed against the field photos; adjusted to match the quote.",
        confirmed_low=50000.0,
        confirmed_high=85000.0,
    )

    assert approved.created is True
    assert approved.assessment.overrides_id == proposal.assessment.id
    assert approved.assessment.actor_kind is ActorKind.HUMAN
    assert approved.assessment.actor_id == director.id
    assert float(approved.assessment.estimate_low) == 50000.0
    assert float(approved.assessment.estimate_high) == 85000.0
    assert approved.assessment.band == proposal.assessment.band  # copied, not adjustable
    assert claim.status is ClaimStatus.VERIFIED  # still never transitions the claim

    retry = record_damage_assessment_decision(
        session,
        claim_id=claim.id,
        assessment_id=proposal.assessment.id,
        director_id=director.id,
        verdict="APPROVED",
        rationale="Confirmed against the field photos; adjusted to match the quote.",
        confirmed_low=50000.0,
        confirmed_high=85000.0,
    )
    assert retry.created is False
    assert retry.assessment.id == approved.assessment.id


def test_non_director_cannot_decide(session):
    sf, claim, evidence_ids = _verified_claim_with_photo(session)
    store = _seed_store(session, claim.id, evidence_ids)
    proposal = run_damage_assessment(
        session, claim.id, assessor=_assessor(evidence_ids=evidence_ids), store=store
    )
    clerk = make_user(session, AppRole.REVIEW_CLERK)

    with pytest.raises(ReviewDecisionConflict, match="Director"):
        record_damage_assessment_decision(
            session,
            claim_id=claim.id,
            assessment_id=proposal.assessment.id,
            director_id=clerk.id,
            verdict="APPROVED",
            rationale="Not authorised to decide this.",
        )


def test_agent_refuses_to_run_once_a_director_decision_is_latest(session):
    sf, claim, evidence_ids = _verified_claim_with_photo(session)
    store = _seed_store(session, claim.id, evidence_ids)
    proposal = run_damage_assessment(
        session, claim.id, assessor=_assessor(evidence_ids=evidence_ids), store=store
    )
    director = make_user(session, AppRole.DIRECTOR)
    record_damage_assessment_decision(
        session,
        claim_id=claim.id,
        assessment_id=proposal.assessment.id,
        director_id=director.id,
        verdict="REJECTED",
        rationale="Photos do not support the claimed severity.",
    )

    with pytest.raises(DamageAssessmentNotRunnable, match="already latest"):
        run_damage_assessment(
            session, claim.id, assessor=_assessor(evidence_ids=evidence_ids), store=store
        )


def test_worker_registers_damage_assessment_handler():
    handlers = load_handlers()
    assert str(AgentName.DAMAGE_ASSESSMENT_AGENT) in handlers


# ---------------------------------------------------------------------------
# The contract's own invariant. No database, so this one still runs when the
# Neon branch is unreachable — which is exactly when a frozen-contract drift
# would otherwise go unnoticed.
# ---------------------------------------------------------------------------


def test_every_propose_only_agent_output_carries_requires_approval():
    """PROPOSE_ONLY documents itself as "their outputs carry
    ``requires_approval``". An agent added to the set without the field reads
    as gated and answers AttributeError when something checks."""
    for agent in PROPOSE_ONLY:
        output_contract = AGENT_IO[agent][1]
        assert "requires_approval" in output_contract.model_fields, agent
    assert AgentName.DAMAGE_ASSESSMENT_AGENT in PROPOSE_ONLY
    field = DamageAssessmentOutput.model_fields["requires_approval"]
    assert field.default is True


# ---------------------------------------------------------------------------
# Database-enforced guarantees. These live in triggers rather than in Python
# on purpose, so they are tested through SQL that bypasses the service.
# ---------------------------------------------------------------------------


def test_stored_assessments_cannot_be_edited_or_deleted(session):
    sf, claim, evidence_ids = _verified_claim_with_photo(session)
    store = _seed_store(session, claim.id, evidence_ids)
    proposal = run_damage_assessment(
        session, claim.id, assessor=_assessor(evidence_ids=evidence_ids), store=store
    )

    with pytest.raises(DBAPIError), session.begin_nested():
        session.execute(
            text("UPDATE damage_assessment SET rationale = 'tampered' WHERE id = :id"),
            {"id": proposal.assessment.id},
        )

    with pytest.raises(DBAPIError), session.begin_nested():
        session.execute(
            text("DELETE FROM damage_assessment WHERE id = :id"),
            {"id": proposal.assessment.id},
        )


def test_database_refuses_forged_agent_rows(session):
    sf, claim, evidence_ids = _verified_claim_with_photo(session)

    # An agent that signs with someone else's authority.
    with pytest.raises(DBAPIError), session.begin_nested():
        session.add(
            _raw_assessment(
                claim, sf, agent_name=str(AgentName.VERIFICATION_AGENT)
            )
        )
        session.flush()

    # An agent that decides instead of proposing.
    with pytest.raises(DBAPIError), session.begin_nested():
        session.add(
            _raw_assessment(claim, sf, verdict=DamageAssessmentVerdict.APPROVED)
        )
        session.flush()


def test_database_refuses_forged_director_overrides(session):
    sf, claim, evidence_ids = _verified_claim_with_photo(session)
    store = _seed_store(session, claim.id, evidence_ids)
    parent = run_damage_assessment(
        session, claim.id, assessor=_assessor(evidence_ids=evidence_ids), store=store
    ).assessment
    clerk = make_user(session, AppRole.REVIEW_CLERK)
    director = make_user(session, AppRole.DIRECTOR)

    # A Review Clerk is not a Director. Money-adjacent means Director only.
    with pytest.raises(DBAPIError), session.begin_nested():
        session.add(_raw_override(claim, parent, clerk.id))
        session.flush()

    # A Director who rewrites what the photos showed rather than the range.
    with pytest.raises(DBAPIError), session.begin_nested():
        session.add(_raw_override(claim, parent, director.id, band=DamageBand.DESTROYED))
        session.flush()

    # A decision floating free of the proposal it is supposed to dispose of.
    with pytest.raises(DBAPIError), session.begin_nested():
        override = _raw_override(claim, parent, director.id)
        override.overrides_id = None
        session.add(override)
        session.flush()


def _raw_assessment(claim, sf, **kw) -> DamageAssessment:
    """A proposal built by hand, so the trigger is what rejects it."""
    defaults = dict(
        claim_id=claim.id,
        storm_file_id=sf.id,
        band=DamageBand.MAJOR,
        estimate_low=Decimal("1000.00"),
        estimate_high=Decimal("2000.00"),
        currency=CURRENCY,
        confidence=0.5,
        findings=[],
        evidence_ids=[],
        location_source="claim",
        verdict=DamageAssessmentVerdict.PROPOSED,
        actor_kind=ActorKind.AGENT,
        actor_id=None,
        agent_name=str(AgentName.DAMAGE_ASSESSMENT_AGENT),
        model_version="test",
        rationale="hand-built",
    )
    defaults.update(kw)
    return DamageAssessment(**defaults)


def _raw_override(claim, parent, actor_id, **kw) -> DamageAssessment:
    defaults = dict(
        claim_id=claim.id,
        storm_file_id=parent.storm_file_id,
        band=parent.band,
        estimate_low=parent.estimate_low,
        estimate_high=parent.estimate_high,
        currency=parent.currency,
        confidence=float(parent.confidence),
        findings=parent.findings,
        evidence_ids=parent.evidence_ids,
        location_source=parent.location_source,
        verdict=DamageAssessmentVerdict.APPROVED,
        actor_kind=ActorKind.HUMAN,
        actor_id=actor_id,
        agent_name=None,
        model_version=parent.model_version,
        rationale="hand-built override",
        overrides_id=parent.id,
    )
    defaults.update(kw)
    return DamageAssessment(**defaults)


# ---------------------------------------------------------------------------
# The seam that connects this agent to the claim pipeline.
# ---------------------------------------------------------------------------


def _enqueued(session, claim) -> int:
    return session.scalar(
        select(func.count())
        .select_from(AgentJob)
        .where(
            AgentJob.job_type == str(AgentName.DAMAGE_ASSESSMENT_AGENT),
            AgentJob.payload["claim_id"].astext == str(claim.id),
        )
    )


def _provider(monkeypatch, value: str) -> None:
    patched = verification_service.get_settings().model_copy(
        update={"damage_assessment_provider": value}
    )
    monkeypatch.setattr(verification_service, "get_settings", lambda: patched)


def test_a_readable_photo_and_a_live_provider_enqueue_the_agent(session, monkeypatch):
    sf, claim, evidence_ids = _verified_claim_with_photo(session)
    _provider(monkeypatch, "anthropic")

    verification_service._enqueue_damage_assessment(session, claim, sf)
    session.flush()

    assert _enqueued(session, claim) == 1


def test_a_disabled_provider_enqueues_nothing(session, monkeypatch):
    """The shipped default. A job queued here cannot succeed, and its dead
    body becomes an ANOMALY_FLAGGED that names a claim needing nothing."""
    sf, claim, evidence_ids = _verified_claim_with_photo(session)
    _provider(monkeypatch, "disabled")

    verification_service._enqueue_damage_assessment(session, claim, sf)
    session.flush()

    assert _enqueued(session, claim) == 0


def test_an_undecodable_photo_enqueues_nothing(session, monkeypatch):
    """The enqueue gate and the handler have to agree on "readable". A HEIC
    photo passes a media_state check and fails the handler every time."""
    event = make_event(session)
    sf = make_storm_file(session, state=StormFileState.VERIFIED)
    claim = make_claim(session, sf, event, status=ClaimStatus.VERIFIED)
    _set_location(session, "claim", claim.id)
    _photo_evidence(session, claim.id, content_type="image/heic")
    session.flush()
    _provider(monkeypatch, "anthropic")

    verification_service._enqueue_damage_assessment(session, claim, sf)
    session.flush()

    assert _enqueued(session, claim) == 0


# ---------------------------------------------------------------------------
# Money-shaped facts we do not take the model's word for.
# ---------------------------------------------------------------------------


def test_the_currency_is_pinned_server_side(session):
    """A silent "USD" is a 150x error, and the immutability trigger would
    then require every override to copy it forward unchanged."""
    sf, claim, evidence_ids = _verified_claim_with_photo(session)
    store = _seed_store(session, claim.id, evidence_ids)
    assessor = _assessor(evidence_ids=evidence_ids)
    assessor.output = assessor.output.model_copy(update={"currency": "USD"})

    result = run_damage_assessment(session, claim.id, assessor=assessor, store=store)

    assert result.assessment.currency == "JMD"
    assert result.output.currency == "JMD"


def test_the_vision_call_sends_the_media_type_the_allow_list_checked(session, monkeypatch):
    """``_readable_photo_evidence`` validates the evidence row's content type.
    R2 hands back whatever ContentType survived on the object, which for an
    object stored without one is the empty string."""
    sf, claim, evidence_ids = _verified_claim_with_photo(session)
    store = _seed_store(session, claim.id, evidence_ids)
    for key, media in store.media_by_key.items():
        store.media_by_key[key] = FetchedMedia(
            data=media.data, content_type="", sha256=media.sha256
        )

    sent: dict = {}

    class _FakeMessages:
        def parse(self, **kw):
            sent.update(kw)
            return SimpleNamespace(
                parsed_output=_assessor(evidence_ids=evidence_ids).output
            )

    class _FakeAnthropic:
        def __init__(self, **kw):
            self.messages = _FakeMessages()

    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropic)

    run_damage_assessment(
        session,
        claim.id,
        assessor=ClaudeDamageAssessor(api_key="test-key"),
        store=store,
    )

    images = [
        block
        for block in sent["messages"][0]["content"]
        if block.get("type") == "image"
    ]
    assert images
    assert {block["source"]["media_type"] for block in images} == {"image/jpeg"}


# ---------------------------------------------------------------------------
# The ledger has to remember what was disposed, not just that something was.
# ---------------------------------------------------------------------------


def test_the_ledger_records_which_way_the_director_decided(session):
    sf, claim, evidence_ids = _verified_claim_with_photo(session)
    store = _seed_store(session, claim.id, evidence_ids)
    proposal = run_damage_assessment(
        session, claim.id, assessor=_assessor(evidence_ids=evidence_ids), store=store
    )
    director = make_user(session, AppRole.DIRECTOR)

    decision = record_damage_assessment_decision(
        session,
        claim_id=claim.id,
        assessment_id=proposal.assessment.id,
        director_id=director.id,
        verdict="REJECTED",
        rationale="Photos do not support the claimed severity.",
    )
    session.flush()

    entry = session.scalar(
        select(LedgerEntry)
        .where(
            LedgerEntry.action == str(Event.DAMAGE_ASSESSMENT_DECIDED),
            LedgerEntry.subject_id == decision.assessment.id,
        )
        .order_by(LedgerEntry.seq.desc())
        .limit(1)
    )
    assert entry is not None
    # Without the verdict this entry is a dollar range and no disposition.
    assert entry.payload["verdict"] == "REJECTED"
    assert entry.payload["snapshot_hash"] == decision.assessment.snapshot_hash
    assert entry.payload["overrides_id"] == str(proposal.assessment.id)


# ---------------------------------------------------------------------------
# What the proposal was made from is recorded by the service, not inferred
# from what the model chose to talk about.
# ---------------------------------------------------------------------------


def _ordered_evidence_ids(session, claim_id: uuid.UUID) -> list[str]:
    """The order ``_readable_photo_evidence`` sees, which is the table's."""
    rows = session.execute(
        text(
            "SELECT id FROM evidence WHERE claim_id = :claim_id AND kind = 'PHOTO'"
            " ORDER BY created_at, id"
        ),
        {"claim_id": claim_id},
    ).all()
    return [str(row.id) for row in rows]


def test_a_replay_reuses_the_row_even_when_findings_skip_a_photo(session):
    """The model may legitimately have nothing to say about one photo.
    Keying the replay check on ``findings`` meant that proposal could never
    match its own evidence set: every redelivery spent another paid vision
    call, appended a duplicate PROPOSED row, and made the Director's
    assessment_id stale."""
    sf, claim, evidence_ids = _verified_claim_with_photo(session, n_photos=3)
    store = _seed_store(session, claim.id, evidence_ids)
    # Findings for two of the three photos.
    assessor = _assessor(evidence_ids=evidence_ids[:2])

    first = run_damage_assessment(session, claim.id, assessor=assessor, store=store)
    assert first.created is True
    assert len(first.assessment.findings) == 2
    assert first.assessment.evidence_ids == _ordered_evidence_ids(session, claim.id)

    replay = run_damage_assessment(session, claim.id, assessor=assessor, store=store)

    assert replay.created is False
    assert replay.assessment.id == first.assessment.id
    assert len(assessor.calls) == 1
    assert session.scalar(
        select(func.count())
        .select_from(DamageAssessment)
        .where(DamageAssessment.claim_id == claim.id)
    ) == 1


def test_a_new_photo_produces_a_new_proposal(session):
    sf, claim, evidence_ids = _verified_claim_with_photo(session, n_photos=1)
    store = _seed_store(session, claim.id, evidence_ids)
    first = run_damage_assessment(
        session, claim.id, assessor=_assessor(evidence_ids=evidence_ids), store=store
    )

    evidence_ids.append(_photo_evidence(session, claim.id))
    session.flush()
    store = _seed_store(session, claim.id, evidence_ids)

    second = run_damage_assessment(
        session, claim.id, assessor=_assessor(evidence_ids=evidence_ids), store=store
    )

    assert second.created is True
    assert second.assessment.id != first.assessment.id
    assert len(second.assessment.evidence_ids) == 2


def test_the_photo_count_is_trimmed_and_the_row_says_which_were_read(session):
    """A trim is not a refusal — twelve photos of a roof should still get an
    estimate — but it is not silent either: the row names the exact set."""
    sf, claim, evidence_ids = _verified_claim_with_photo(session, n_photos=12)
    store = _seed_store(session, claim.id, evidence_ids)
    expected = _ordered_evidence_ids(session, claim.id)[:MAX_ASSESSMENT_PHOTOS]
    assessor = _assessor(evidence_ids=[uuid.UUID(eid) for eid in expected])

    result = run_damage_assessment(session, claim.id, assessor=assessor, store=store)

    assert result.assessment.evidence_ids == expected
    assert len(result.assessment.evidence_ids) == MAX_ASSESSMENT_PHOTOS
    sent_claim_id, sent_photo_ids = assessor.calls[0]
    assert [str(pid) for pid in sent_photo_ids] == expected


def test_oversized_photo_evidence_never_reaches_the_paid_call(session):
    sf, claim, evidence_ids = _verified_claim_with_photo(session, n_photos=2)
    store = _seed_store(session, claim.id, evidence_ids)
    oversized = b"\x00" * (MAX_ASSESSMENT_BYTES // 2 + 1)
    for key, media in store.media_by_key.items():
        store.media_by_key[key] = FetchedMedia(
            data=oversized, content_type="image/jpeg", sha256=media.sha256
        )
    assessor = _assessor(evidence_ids=evidence_ids)

    with pytest.raises(DamageAssessmentNotRunnable, match="byte boundary"):
        run_damage_assessment(session, claim.id, assessor=assessor, store=store)

    assert assessor.calls == []


def test_the_provider_rechecks_the_boundary_it_was_handed():
    """The fetch-side cap is not the provider's cap. A test double or a future
    store adapter must not be able to hand `assess` more than the API takes."""
    assessor = ClaudeDamageAssessor(api_key="test-key")
    too_many = [
        FetchedMedia(data=b"x", content_type="image/jpeg", sha256="0" * 64)
        for _ in range(MAX_ASSESSMENT_PHOTOS + 1)
    ]

    with pytest.raises(DamageAssessmentNotRunnable, match="photo count"):
        assessor.assess(claim=None, photos=[], media=too_many)

    with pytest.raises(DamageAssessmentNotRunnable, match="photo count"):
        assessor.assess(claim=None, photos=[], media=[])

    too_big = [
        FetchedMedia(
            data=b"x" * (MAX_ASSESSMENT_BYTES + 1),
            content_type="image/jpeg",
            sha256="0" * 64,
        )
    ]
    with pytest.raises(DamageAssessmentNotRunnable, match="byte boundary"):
        assessor.assess(claim=None, photos=[], media=too_big)
