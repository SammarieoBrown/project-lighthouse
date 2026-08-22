"""Evidence, idempotency, and Director-gate tests for the Damage Assessment Agent."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import func, select, text

from lighthouse_contracts import ActorKind, AgentName, AppRole, ClaimStatus, StormFileState
from lighthouse_contracts.agents import DamageAssessmentOutput, DamagePhotoFinding

from app.damage_assessment_service import (
    ClaimNotFound,
    DamageAssessmentNotRunnable,
    DamageAssessmentProviderDisabled,
    DeterministicDamageAssessor,
    DeterministicPhotoStore,
    ReviewDecisionConflict,
    record_damage_assessment_decision,
    run_damage_assessment,
)
from app.intake.media import FetchedMedia
from app.models import AgentJob, DamageAssessment
from app.worker import HANDLERS, load_handlers

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
