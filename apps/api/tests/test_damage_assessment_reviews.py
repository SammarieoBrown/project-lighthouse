"""Authenticated Director API over immutable damage assessment proposals."""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager

from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from lighthouse_contracts import ActorKind, AppRole, ClaimStatus, StormFileState
from lighthouse_contracts.agents import DamageAssessmentOutput, DamagePhotoFinding

from app import damage_assessment_reviews
from app.approval_credentials import issue_human_credential, set_human_password
from app.damage_assessment_service import (
    DeterministicDamageAssessor,
    DeterministicPhotoStore,
    run_damage_assessment,
)
from app.intake.media import FetchedMedia
from app.models import DamageAssessment
from app.web import app
from factories import make_claim, make_event, make_storm_file, make_user

POINT = "POINT(-77.1000 18.1000)"


def _credential(session, role: AppRole):
    user = make_user(session, role)
    password = "correct horse director battery"
    set_human_password(session, email=user.email, password=password)
    issued = issue_human_credential(session, email=user.email, password=password)
    return user, issued.token


def _client(monkeypatch, session) -> TestClient:
    @contextmanager
    def scoped():
        yield session
        session.flush()

    monkeypatch.setattr(damage_assessment_reviews, "session_scope", scoped)
    return TestClient(app)


def _photo_evidence(session, claim_id: uuid.UUID) -> uuid.UUID:
    evidence_id = uuid.uuid4()
    digest = uuid.uuid4().hex.ljust(64, "0")
    session.execute(
        text(
            """
            INSERT INTO evidence (id, claim_id, kind, uri, payload, sha256)
            VALUES (:id, :claim_id, 'PHOTO', :uri, CAST(:payload AS jsonb), :sha256)
            """
        ),
        {
            "id": evidence_id,
            "claim_id": claim_id,
            "uri": f"r2://lighthouse-private/intake/sha256/{digest}",
            "payload": json.dumps({"media_state": "STORED", "content_type": "image/jpeg"}),
            "sha256": digest,
        },
    )
    return evidence_id


def _proposed_assessment(session):
    event = make_event(session)
    sf = make_storm_file(session, state=StormFileState.VERIFIED)
    claim = make_claim(session, sf, event, status=ClaimStatus.VERIFIED, damage_type="roof_loss")
    session.execute(
        text("UPDATE claim SET location = ST_GeogFromText(:wkt) WHERE id = :id"),
        {"wkt": f"SRID=4326;{POINT}", "id": claim.id},
    )
    evidence_id = _photo_evidence(session, claim.id)
    session.flush()

    uri, sha256 = session.execute(
        text("SELECT uri, sha256 FROM evidence WHERE id = :id"), {"id": evidence_id}
    ).one()
    object_key = uri.removeprefix("r2://").split("/", 1)[1]
    store = DeterministicPhotoStore(
        media_by_key={
            object_key: FetchedMedia(
                data=b"fake-jpeg-bytes", content_type="image/jpeg", sha256=sha256
            )
        }
    )
    output = DamageAssessmentOutput(
        band="MAJOR",
        estimate_low=40000.0,
        estimate_high=90000.0,
        currency="JMD",
        confidence=0.75,
        findings=[
            DamagePhotoFinding(
                evidence_id=evidence_id,
                observed_damage="roof sheeting torn off",
                band="MAJOR",
                confidence=0.8,
            )
        ],
        location_source="claim",
        model_version="test-model-v1",
        rationale="Roof damage clearly visible.",
    )
    assessor = DeterministicDamageAssessor(output=output)
    result = run_damage_assessment(session, claim.id, assessor=assessor, store=store)
    return claim, result.assessment


def _body(assessment: DamageAssessment, verdict: str = "APPROVED") -> dict:
    return {
        "assessment_id": str(assessment.id),
        "verdict": verdict,
        "rationale": "Reviewed the photos against the field estimate.",
    }


def test_review_route_requires_active_director(session, monkeypatch):
    claim, assessment = _proposed_assessment(session)
    _, clerk_token = _credential(session, AppRole.REVIEW_CLERK)
    client = _client(monkeypatch, session)
    path = f"/v1/claims/{claim.id}/damage-assessment/review"

    missing = client.post(path, json=_body(assessment))
    forbidden = client.post(
        path,
        json=_body(assessment),
        headers={"Authorization": f"Bearer {clerk_token}"},
    )

    assert missing.status_code == 401
    assert forbidden.status_code == 403
    assert session.scalar(select(func.count()).select_from(DamageAssessment)) == 1


def test_director_approval_is_append_only_and_idempotent(session, monkeypatch):
    claim, assessment = _proposed_assessment(session)
    director, token = _credential(session, AppRole.DIRECTOR)
    client = _client(monkeypatch, session)
    path = f"/v1/claims/{claim.id}/damage-assessment/review"
    headers = {"Authorization": f"Bearer {token}"}

    created = client.post(path, json=_body(assessment), headers=headers)
    replay = client.post(path, json=_body(assessment), headers=headers)

    assert created.status_code == 201
    assert replay.status_code == 200
    assert replay.json() == {**created.json(), "idempotent_replay": True}
    body = created.json()
    assert body["assessment"]["verdict"] == "APPROVED"
    assert body["assessment"]["overrides_id"] == str(assessment.id)
    assert body["assessment"]["decided_by"] == {"id": str(director.id), "role": "DIRECTOR"}
    assert body["claim"]["status"] == "VERIFIED"
    assert claim.status is ClaimStatus.VERIFIED
    assert session.scalar(select(func.count()).select_from(DamageAssessment)) == 2

    override = session.scalar(
        select(DamageAssessment).where(DamageAssessment.overrides_id == assessment.id)
    )
    assert override is not None
    assert override.actor_kind is ActorKind.HUMAN
    assert override.actor_id == director.id


def test_review_rejects_conflicting_decisions(session, monkeypatch):
    claim, assessment = _proposed_assessment(session)
    _, token = _credential(session, AppRole.DIRECTOR)
    client = _client(monkeypatch, session)
    path = f"/v1/claims/{claim.id}/damage-assessment/review"
    headers = {"Authorization": f"Bearer {token}"}

    approved = client.post(path, json=_body(assessment), headers=headers)
    changed = client.post(
        path, json={**_body(assessment), "verdict": "REJECTED"}, headers=headers
    )

    assert approved.status_code == 201
    assert changed.status_code == 409
    assert session.scalar(select(func.count()).select_from(DamageAssessment)) == 2
