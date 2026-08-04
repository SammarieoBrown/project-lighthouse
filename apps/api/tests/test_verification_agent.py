"""Evidence, idempotency, and T6/T7 tests for the Verification Agent."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from lighthouse_contracts import (
    ActorKind,
    AgentName,
    AppRole,
    ClaimStatus,
    Event,
    StormFileState,
    Verdict,
)

from app import ledger
from app.models import AgentJob, LedgerEntry, Verification
from app.verification_service import (
    ReviewDecisionConflict,
    record_review_decision,
    run_verification,
)
from app.worker import HANDLERS, load_handlers

from factories import make_claim, make_event, make_storm_file, make_user


POINT = "POINT(-77.1000 18.1000)"


def _set_location(session, table: str, row_id: uuid.UUID, wkt: str = POINT) -> None:
    assert table in {"claim", "storm_file"}
    session.execute(
        text(
            f"UPDATE {table} "
            "SET location = ST_GeogFromText(:wkt) WHERE id = :row_id"
        ),
        {"wkt": f"SRID=4326;{wkt}", "row_id": row_id},
    )


def _evidence(
    session,
    claim_id: uuid.UUID,
    *,
    kind: str,
    payload: dict,
    uri: str | None = None,
    sha256: str | None = None,
    phash: str | None = None,
) -> uuid.UUID:
    evidence_id = uuid.uuid4()
    session.execute(
        text(
            """
            INSERT INTO evidence (id, claim_id, kind, uri, payload, sha256, phash)
            VALUES (
              :id, :claim_id, CAST(:kind AS evidence_kind), :uri,
              CAST(:payload AS jsonb), :sha256, :phash
            )
            """
        ),
        {
            "id": evidence_id,
            "claim_id": claim_id,
            "kind": kind,
            "uri": uri,
            "payload": json.dumps(payload),
            "sha256": sha256,
            "phash": phash,
        },
    )
    return evidence_id


def _observed_wind(session, event_id: uuid.UUID) -> uuid.UUID:
    advisory_id = uuid.uuid4()
    session.execute(
        text(
            """
            INSERT INTO advisory (
              id, hazard_event_id, advisory_number, issued_at, observed,
              wind_field_34, wind_field_50, wind_field_64, raw
            ) VALUES (
              :id, :event_id, 'OBS-VERIFY', now(), true,
              ST_Multi(ST_Buffer(ST_GeogFromText(:point), 5000)::geometry)::geography,
              ST_Multi(ST_Buffer(ST_GeogFromText(:point), 3000)::geometry)::geography,
              ST_Multi(ST_Buffer(ST_GeogFromText(:point), 1000)::geometry)::geography,
              '{}'::jsonb
            )
            """
        ),
        {
            "id": advisory_id,
            "event_id": event_id,
            "point": f"SRID=4326;{POINT}",
        },
    )
    return advisory_id


def _add_neighbours(session, event, *, count: int = 4) -> None:
    for index in range(count):
        sf = make_storm_file(session, state=StormFileState.AFFECTED)
        claim = make_claim(session, sf, event, damage_type="roof_damage")
        lon = -77.1000 + (index + 1) * 0.0002
        _set_location(session, "storm_file", sf.id, f"POINT({lon} 18.1000)")
        assert claim.status is ClaimStatus.FILED


def _complete_claim(session, *, include_satellite: bool = True):
    event = make_event(session)
    sf = make_storm_file(session, state=StormFileState.AFFECTED)
    sf.thin = False
    sf.structure = {"roof": "zinc", "walls": "timber", "floors": 1}
    claim = make_claim(session, sf, event, damage_type="roof_damage")
    _set_location(session, "storm_file", sf.id)
    session.expire(sf, ["location"])
    _observed_wind(session, event.id)
    _add_neighbours(session, event)
    if include_satellite:
        _evidence(
            session,
            claim.id,
            kind="SATELLITE",
            payload={
                "source": "sentinel-2-change-v1",
                "change_score": 0.95,
                "cloud_fraction": 0.05,
            },
            sha256="a" * 64,
        )
    _evidence(
        session,
        claim.id,
        kind="PHOTO",
        uri="r2://lighthouse-private/intake/sha256/photo",
        payload={"media_state": "STORED", "phash_state": "COMPLETE"},
        sha256="b" * 64,
        phash="0123456789abcdef",
    )
    session.flush()
    return event, sf, claim


def test_pending_media_and_missing_sources_force_review_without_invention(session):
    event = make_event(session)
    sf = make_storm_file(session, state=StormFileState.AFFECTED)
    sf.thin = True
    sf.structure = {}
    claim = make_claim(session, sf, event, partial=True)
    _evidence(
        session,
        claim.id,
        kind="AUDIO",
        uri="https://api.twilio.com/pending-private-media",
        payload={"media_state": "PENDING_FETCH"},
    )

    result = run_verification(session, claim.id)

    assert result.created is True
    assert result.transitioned is False
    assert result.output.verdict is Verdict.REVIEW
    assert result.output.capped is True
    assert set(result.output.missing_signals) == {
        "hazard_sufficiency",
        "satellite_change",
        "neighbour_corroboration",
        "registry_match",
        "media_integrity",
    }
    for signal in result.output.signals.model_dump().values():
        assert signal["present"] is False
        assert signal["score"] is None
    assert claim.status is ClaimStatus.FILED
    assert sf.state is StormFileState.AFFECTED
    assert result.verification.snapshot_hash

    replay = run_verification(session, claim.id)
    assert replay.created is False
    assert replay.verification.id == result.verification.id
    assert session.scalar(
        select(func.count()).select_from(Verification).where(
            Verification.claim_id == claim.id
        )
    ) == 1
    assert ledger.verify_chain(session) is True


def test_all_five_persisted_signals_auto_verify_and_emit_t6_once(session):
    _, sf, claim = _complete_claim(session)

    result = run_verification(session, claim.id)

    assert result.output.verdict is Verdict.AUTO_VERIFIED
    assert result.output.capped is False
    assert result.output.missing_signals == []
    assert result.output.confidence >= 0.85
    assert claim.status is ClaimStatus.VERIFIED
    assert sf.state is StormFileState.VERIFIED
    assert claim.verified_at is not None

    transitions = session.execute(
        select(LedgerEntry).where(
            LedgerEntry.action == str(Event.CLAIM_VERIFIED),
            LedgerEntry.subject_type == "storm_file",
            LedgerEntry.subject_id == sf.id,
        )
    ).scalars().all()
    assert len(transitions) == 1
    assert transitions[0].payload["transition"] == "T6"
    assert transitions[0].actor_kind is ActorKind.AGENT
    assert transitions[0].agent_name == str(AgentName.VERIFICATION_AGENT)

    triage_jobs = session.execute(
        select(AgentJob).where(
            AgentJob.job_type == str(AgentName.TRIAGE_AGENT),
            AgentJob.payload["claim_id"].astext == str(claim.id),
        )
    ).scalars().all()
    assert len(triage_jobs) == 1

    replay = run_verification(session, claim.id)
    assert replay.created is False
    assert replay.verification.id == result.verification.id
    assert ledger.verify_chain(session) is True


def test_missing_satellite_never_auto_verifies_even_with_four_strong_signals(session):
    _, sf, claim = _complete_claim(session, include_satellite=False)

    result = run_verification(session, claim.id)

    satellite = result.output.signals.satellite_change
    assert satellite.present is False
    assert satellite.score is None
    assert result.output.verdict is Verdict.REVIEW
    assert result.output.capped is True
    assert result.output.confidence <= 0.80
    assert claim.status is ClaimStatus.FILED
    assert sf.state is StormFileState.AFFECTED


def test_exact_media_reuse_is_evidence_against_auto_verification(session):
    event, sf, claim = _complete_claim(session)
    other_sf = make_storm_file(session, state=StormFileState.AFFECTED)
    other = make_claim(session, other_sf, event)
    _evidence(
        session,
        other.id,
        kind="PHOTO",
        uri="r2://lighthouse-private/intake/sha256/reused",
        payload={"media_state": "STORED", "phash_state": "COMPLETE"},
        sha256="b" * 64,
        phash="fedcba9876543210",
    )

    result = run_verification(session, claim.id)

    media = result.output.signals.media_integrity
    assert media.present is True
    assert media.score == 0.0
    assert media.evidence["exact_duplicate_claim_ids"] == [str(other.id)]
    assert result.output.verdict is Verdict.REVIEW
    assert claim.status is ClaimStatus.FILED
    assert sf.state is StormFileState.AFFECTED


def test_review_clerk_approval_appends_override_and_performs_t7_idempotently(session):
    event = make_event(session)
    sf = make_storm_file(session, state=StormFileState.AFFECTED)
    claim = make_claim(session, sf, event, partial=True)
    agent_run = run_verification(session, claim.id)
    clerk = make_user(session, AppRole.REVIEW_CLERK)

    approved = record_review_decision(
        session,
        claim_id=claim.id,
        verification_id=agent_run.verification.id,
        clerk_id=clerk.id,
        verdict=Verdict.APPROVED,
        rationale="Caller identity and field evidence were reviewed by the clerk.",
    )

    assert approved.created is True
    assert approved.verification.overrides_id == agent_run.verification.id
    assert approved.verification.actor_kind is ActorKind.HUMAN
    assert approved.verification.actor_id == clerk.id
    assert approved.verification.agent_name is None
    assert claim.status is ClaimStatus.VERIFIED
    assert sf.state is StormFileState.VERIFIED

    transition = session.scalar(
        select(LedgerEntry).where(
            LedgerEntry.subject_type == "storm_file",
            LedgerEntry.subject_id == sf.id,
            LedgerEntry.action == str(Event.CLAIM_VERIFIED),
        )
    )
    assert transition is not None
    assert transition.payload["transition"] == "T7"
    assert transition.actor_kind is ActorKind.HUMAN
    assert transition.actor_id == clerk.id
    assert transition.agent_name is None

    retry = record_review_decision(
        session,
        claim_id=claim.id,
        verification_id=agent_run.verification.id,
        clerk_id=clerk.id,
        verdict=Verdict.APPROVED,
        rationale="Caller identity and field evidence were reviewed by the clerk.",
    )
    assert retry.created is False
    assert retry.verification.id == approved.verification.id
    assert session.scalar(
        select(func.count()).select_from(Verification).where(
            Verification.claim_id == claim.id
        )
    ) == 2


def test_review_override_rejects_non_clerk_and_stale_or_conflicting_decisions(session):
    event = make_event(session)
    sf = make_storm_file(session, state=StormFileState.AFFECTED)
    claim = make_claim(session, sf, event)
    agent_run = run_verification(session, claim.id)
    director = make_user(session, AppRole.DIRECTOR)

    with pytest.raises(ReviewDecisionConflict, match="Review Clerk"):
        record_review_decision(
            session,
            claim_id=claim.id,
            verification_id=agent_run.verification.id,
            clerk_id=director.id,
            verdict=Verdict.APPROVED,
            rationale="Not authorised.",
        )


def test_database_rejects_forged_foreign_and_agent_override_rows(session):
    event = make_event(session)
    sf = make_storm_file(session, state=StormFileState.AFFECTED)
    claim = make_claim(session, sf, event)
    parent = run_verification(session, claim.id).verification
    clerk = make_user(session, AppRole.REVIEW_CLERK)

    changed_signals = json.loads(json.dumps(parent.signals))
    changed_signals["hazard_sufficiency"] = {
        "present": True,
        "score": 1.0,
        "evidence": {"forged": True},
    }
    with pytest.raises(DBAPIError), session.begin_nested():
        session.add(
            Verification(
                claim_id=claim.id,
                signals=changed_signals,
                confidence=float(parent.confidence),
                verdict=Verdict.APPROVED,
                actor_kind=ActorKind.HUMAN,
                actor_id=clerk.id,
                agent_name=None,
                model_version=parent.model_version,
                threshold_version=parent.threshold_version,
                rationale="Forged evidence snapshot.",
                capped=parent.capped,
                overrides_id=parent.id,
            )
        )
        session.flush()

    other_sf = make_storm_file(session, state=StormFileState.AFFECTED)
    other_claim = make_claim(session, other_sf, event)
    with pytest.raises(DBAPIError), session.begin_nested():
        session.add(
            Verification(
                claim_id=other_claim.id,
                signals=parent.signals,
                confidence=float(parent.confidence),
                verdict=Verdict.APPROVED,
                actor_kind=ActorKind.HUMAN,
                actor_id=clerk.id,
                agent_name=None,
                model_version=parent.model_version,
                threshold_version=parent.threshold_version,
                rationale="Foreign-claim parent.",
                capped=parent.capped,
                overrides_id=parent.id,
            )
        )
        session.flush()

    with pytest.raises(DBAPIError), session.begin_nested():
        session.add(
            Verification(
                claim_id=claim.id,
                signals=parent.signals,
                confidence=float(parent.confidence),
                verdict=Verdict.REVIEW,
                actor_kind=ActorKind.AGENT,
                actor_id=None,
                agent_name=str(AgentName.VERIFICATION_AGENT),
                model_version=parent.model_version,
                threshold_version=parent.threshold_version,
                rationale="Agent attempted to override itself.",
                capped=parent.capped,
                overrides_id=parent.id,
            )
        )
        session.flush()


def test_worker_registers_verification_handler():
    handlers = load_handlers()
    assert str(AgentName.VERIFICATION_AGENT) in handlers
