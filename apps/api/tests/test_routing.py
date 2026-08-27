"""Payer routing (RTE-01, RTE-02).

The thing these tests protect is narrow and important: nothing sends a
household's claim to a named third party unless that household said it could,
and the record says what they agreed to at the moment it was decided.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from lighthouse_contracts import ClaimStatus, Event, PayerRoute, StormFileState

from app.models import Consent, LedgerEntry, RoutingDecision
from app.routing_service import (
    ClaimNotFound,
    RoutingNotRunnable,
    active_insurer_consent,
    route_claim,
)

from factories import make_claim, make_event, make_storm_file

INSURER = "Island Mutual"


def _consent(session, sf, *, insurer=INSURER, share=True, revoked=False, version="v1",
             granted=None):
    consent = Consent(
        storm_file_id=sf.id,
        version=version,
        channel="whatsapp",
        scope={"share_with_insurer": share, "insurer_name": insurer, "purpose": "FNOL"},
        granted_at=granted or datetime.now(UTC),
        revoked_at=datetime.now(UTC) if revoked else None,
    )
    session.add(consent)
    session.flush()
    return consent


def _claim(session, **kw):
    event = make_event(session)
    sf = make_storm_file(session, state=StormFileState.VERIFIED)
    kw.setdefault("status", ClaimStatus.VERIFIED)
    return sf, make_claim(session, sf, event, **kw)


# -- the four outcomes ------------------------------------------------------


def test_an_uninsured_household_routes_to_relief(session):
    """The default, and the common case. Relief is what the programme is."""
    sf, claim = _claim(session)

    run = route_claim(session, claim.id)

    assert run.decision.route is PayerRoute.GOV_RELIEF
    assert run.decision.insurer_name is None
    assert run.decision.consent_snapshot["granted"] is False


def test_an_insured_household_with_immediate_needs_routes_to_both(session):
    """A household waiting months for an adjuster still needs water tonight.
    Routing them wholly to an insurer would be telling them to wait."""
    sf, claim = _claim(session, reported_needs=["water", "tarpaulin"])
    _consent(session, sf)

    run = route_claim(session, claim.id)

    assert run.decision.route is PayerRoute.BOTH
    assert run.decision.insurer_name == INSURER


def test_an_insured_household_with_no_immediate_needs_routes_to_the_insurer(session):
    sf, claim = _claim(session, reported_needs=[])
    _consent(session, sf)

    run = route_claim(session, claim.id)

    assert run.decision.route is PayerRoute.INSURER
    assert run.decision.insurer_name == INSURER


def test_the_router_never_returns_donor_pool(session):
    """DONOR_POOL is a funding source for a relief-path claim, not a payer.
    Which pool funds an allocation is asked at allocation time."""
    sf, claim = _claim(session)
    _consent(session, sf)

    run = route_claim(session, claim.id)

    assert run.decision.route is not PayerRoute.DONOR_POOL


# -- consent is the gate ----------------------------------------------------


def test_consent_without_a_named_insurer_does_not_route_away_from_relief(session):
    sf, claim = _claim(session)
    _consent(session, sf, insurer=None)

    run = route_claim(session, claim.id)

    assert run.decision.route is PayerRoute.GOV_RELIEF


def test_withheld_consent_keeps_the_claim_on_the_relief_path(session):
    sf, claim = _claim(session)
    _consent(session, sf, share=False)

    run = route_claim(session, claim.id)

    assert run.decision.route is PayerRoute.GOV_RELIEF
    assert run.decision.consent_id is None


def test_revoked_consent_is_not_consent(session):
    """Consent that cannot be withdrawn is not consent, which is the whole
    reason this is a row rather than a flag."""
    sf, claim = _claim(session)
    _consent(session, sf, revoked=True)

    assert active_insurer_consent(session, sf.id) is None
    run = route_claim(session, claim.id)
    assert run.decision.route is PayerRoute.GOV_RELIEF


def test_the_newest_consent_wins(session):
    sf, claim = _claim(session)
    _consent(
        session, sf, insurer="Old Carrier",
        granted=datetime.now(UTC) - timedelta(days=30),
    )
    _consent(session, sf, insurer=INSURER)

    run = route_claim(session, claim.id)

    assert run.decision.insurer_name == INSURER


def test_mentioning_an_insurer_in_a_transcript_routes_nothing(session):
    """A household naming a carrier while describing a collapsed roof has not
    consented to anything."""
    sf, claim = _claim(
        session, transcript=f"Mi have insurance wid {INSURER}, mi roof gone."
    )

    run = route_claim(session, claim.id)

    assert run.decision.route is PayerRoute.GOV_RELIEF
    assert run.decision.insurer_name is None


# -- the record -------------------------------------------------------------


def test_the_decision_is_a_ledger_event_carrying_the_consent_snapshot(session):
    """RTE-02, and the snapshot is a copy rather than a join because an
    auditor asks what we were permitted to do, not what we may do now."""
    sf, claim = _claim(session, reported_needs=["water"])
    consent = _consent(session, sf)

    route_claim(session, claim.id)
    session.flush()

    entry = session.scalar(
        select(LedgerEntry).where(
            LedgerEntry.action == str(Event.CLAIM_ROUTED),
            LedgerEntry.subject_id == claim.id,
        )
    )
    assert entry is not None
    assert entry.payload["route"] == str(PayerRoute.BOTH)
    assert entry.payload["insurer_name"] == INSURER
    assert entry.payload["consent_id"] == str(consent.id)
    assert entry.payload["consent_snapshot"]["granted"] is True
    assert entry.payload["consent_snapshot"]["consent_version"] == "v1"


def test_the_snapshot_survives_the_consent_being_revoked(session):
    sf, claim = _claim(session, reported_needs=["water"])
    consent = _consent(session, sf)
    decision = route_claim(session, claim.id).decision

    consent.revoked_at = datetime.now(UTC)
    session.flush()
    session.expire(decision)

    assert decision.consent_snapshot["granted"] is True
    assert active_insurer_consent(session, sf.id) is None


def test_the_snapshot_carries_no_identifying_detail(session):
    """It answers what was permitted, not who permitted it."""
    sf, claim = _claim(session)
    _consent(session, sf)

    run = route_claim(session, claim.id)

    blob = str(run.decision.consent_snapshot).casefold()
    assert sf.phone.casefold() not in blob
    assert (sf.head_name or "").casefold() not in blob


def test_a_claim_is_routed_once(session):
    """A routing decision is the basis on which a claim may leave the
    programme. Re-deciding it silently would mean the record no longer says
    what was true when the sharing happened."""
    sf, claim = _claim(session)

    first = route_claim(session, claim.id)
    _consent(session, sf)  # answer arrives late
    second = route_claim(session, claim.id)

    assert first.created is True
    assert second.created is False
    assert second.decision.id == first.decision.id
    assert second.decision.route is PayerRoute.GOV_RELIEF
    assert session.scalar(
        select(func.count()).select_from(RoutingDecision)
        .where(RoutingDecision.claim_id == claim.id)
    ) == 1


def test_an_unverified_claim_is_not_routed(session):
    sf, claim = _claim(session, status=ClaimStatus.FILED)

    with pytest.raises(RoutingNotRunnable, match="not verified"):
        route_claim(session, claim.id)


def test_a_missing_claim_is_refused(session):
    with pytest.raises(ClaimNotFound):
        route_claim(session, uuid.uuid4())
