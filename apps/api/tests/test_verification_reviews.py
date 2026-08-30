"""Authenticated Review Clerk API over immutable verification decisions."""

from __future__ import annotations

from contextlib import contextmanager

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from lighthouse_contracts import (
    ActorKind,
    AgentName,
    AppRole,
    ClaimStatus,
    StormFileState,
    Verdict,
)

from app import verification_reviews
from app.approval_credentials import issue_human_credential, set_human_password
from app.models import AgentJob, LedgerEntry, Verification
from app.web import app
from factories import make_claim, make_event, make_storm_file, make_user, make_verification


def _credential(session, role: AppRole):
    user = make_user(session, role)
    password = "correct horse review clerk"
    set_human_password(session, email=user.email, password=password)
    issued = issue_human_credential(session, email=user.email, password=password)
    return user, issued.token


def _client(monkeypatch, session) -> TestClient:
    @contextmanager
    def scoped():
        yield session
        session.flush()

    monkeypatch.setattr(verification_reviews, "session_scope", scoped)
    return TestClient(app)


def _review_source(session):
    storm_file = make_storm_file(session, state=StormFileState.AFFECTED)
    event = make_event(session)
    claim = make_claim(session, storm_file, event, status=ClaimStatus.FILED)
    signals = {
        name: {"present": True, "score": 0.82, "evidence": {}}
        for name in (
            "hazard_sufficiency",
            "satellite_change",
            "neighbour_corroboration",
            "registry_match",
            "media_integrity",
        )
    }
    signals["satellite_change"] = {
        "present": False,
        "score": None,
        "evidence": {},
        "note": "satellite change evidence is not available",
    }
    source = make_verification(
        session,
        claim,
        verdict=Verdict.REVIEW,
        confidence=0.80,
        capped=True,
        signals=signals,
    )
    return storm_file, claim, source


def _body(source: Verification, verdict: str = "APPROVED") -> dict:
    return {
        "verification_id": str(source.id),
        "verdict": verdict,
        "rationale": "Reviewed the complete redacted evidence bundle.",
    }


def test_review_route_requires_review_authority(session, monkeypatch):
    # Since 0015 the Director carries universal gate authority, so the
    # excluded role here is Finance — money hands still cannot judge evidence.
    _, claim, source = _review_source(session)
    _, finance_token = _credential(session, AppRole.FINANCE_OFFICER)
    client = _client(monkeypatch, session)
    path = f"/v1/claims/{claim.id}/verification/review"

    missing = client.post(path, json=_body(source))
    forbidden = client.post(
        path,
        json=_body(source),
        headers={"Authorization": f"Bearer {finance_token}"},
    )

    assert missing.status_code == 401
    assert forbidden.status_code == 403
    assert session.scalar(select(func.count()).select_from(Verification)) == 1


def test_review_approval_is_append_only_idempotent_and_transitions_t7(
    session, monkeypatch
):
    storm_file, claim, source = _review_source(session)
    clerk, token = _credential(session, AppRole.REVIEW_CLERK)
    client = _client(monkeypatch, session)
    path = f"/v1/claims/{claim.id}/verification/review"
    headers = {"Authorization": f"Bearer {token}"}

    created = client.post(path, json=_body(source), headers=headers)
    replay = client.post(path, json=_body(source), headers=headers)

    assert created.status_code == 201
    assert replay.status_code == 200
    assert replay.json() == {**created.json(), "idempotent_replay": True}
    body = created.json()
    assert body["verification"]["verdict"] == "APPROVED"
    assert body["verification"]["overrides_id"] == str(source.id)
    assert body["verification"]["reviewed_by"] == {
        "id": str(clerk.id),
        "role": "REVIEW_CLERK",
    }
    assert body["claim"]["status"] == "VERIFIED"
    assert body["storm_file"]["state"] == "VERIFIED"
    assert body["money_movement"] == {
        "status": "NOT_AUTHORIZED_BY_VERIFICATION_REVIEW",
        "amount": None,
        "currency": None,
    }
    assert body["idempotent_replay"] is False
    assert claim.status is ClaimStatus.VERIFIED
    assert storm_file.state is StormFileState.VERIFIED
    assert session.scalar(select(func.count()).select_from(Verification)) == 2

    override = session.scalar(
        select(Verification).where(Verification.overrides_id == source.id)
    )
    assert override is not None
    assert override.actor_kind is ActorKind.HUMAN
    assert override.actor_id == clerk.id
    assert override.signals == source.signals
    assert session.scalar(
        select(func.count())
        .select_from(LedgerEntry)
        .where(
            LedgerEntry.action == "claim.verified",
            LedgerEntry.subject_id == storm_file.id,
            LedgerEntry.payload["transition"].astext == "T7",
        )
    ) == 1
    assert session.scalar(
        select(func.count())
        .select_from(AgentJob)
        .where(AgentJob.job_type == str(AgentName.TRIAGE_AGENT))
    ) == 1


def test_review_rejection_closes_claim_without_verifying_storm_file(session, monkeypatch):
    storm_file, claim, source = _review_source(session)
    _, token = _credential(session, AppRole.REVIEW_CLERK)
    client = _client(monkeypatch, session)

    response = client.post(
        f"/v1/claims/{claim.id}/verification/review",
        json=_body(source, "REJECTED"),
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 201
    assert response.json()["claim"]["status"] == "REJECTED"
    assert response.json()["storm_file"]["state"] == "AFFECTED"
    assert claim.status is ClaimStatus.REJECTED
    assert claim.closed_reason == _body(source, "REJECTED")["rationale"]
    assert storm_file.state is StormFileState.AFFECTED


def test_review_rejects_stale_or_conflicting_decisions(session, monkeypatch):
    _, claim, source = _review_source(session)
    _, token = _credential(session, AppRole.REVIEW_CLERK)
    newer = make_verification(
        session,
        claim,
        verdict=Verdict.FLAGGED,
        confidence=0.40,
        capped=False,
    )
    client = _client(monkeypatch, session)
    path = f"/v1/claims/{claim.id}/verification/review"
    headers = {"Authorization": f"Bearer {token}"}

    stale = client.post(path, json=_body(source), headers=headers)
    accepted = client.post(path, json=_body(newer), headers=headers)
    changed = client.post(
        path,
        json={**_body(newer), "verdict": "REJECTED"},
        headers=headers,
    )

    assert stale.status_code == 409
    assert accepted.status_code == 201
    assert changed.status_code == 409
    assert session.scalar(select(func.count()).select_from(Verification)) == 3
