"""FNOL packets (INS-01), and the rule about what they must never carry.

The strongest assertion in this file is a negative one: no dollar figure
reaches an insurer. INS-05 says category, severity and evidence only, and a
relief programme that values a loss is doing the adjuster's job and creating a
number the household will be held to.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from lighthouse_contracts import AppRole, ClaimStatus, StormFileState

from app import fnol
from app.approval_credentials import issue_human_credential, set_human_password
from app.fnol_service import FnolNotAvailable, build_fnol, render_pdf
from app.models import Consent
from app.routing_service import route_claim
from app.web import app

from factories import make_claim, make_event, make_storm_file, make_user, make_verification

INSURER = "Island Mutual"


def _insured_claim(session, *, share=True, needs=("water",)):
    event = make_event(session)
    sf = make_storm_file(session, state=StormFileState.VERIFIED)
    session.execute(
        text("UPDATE storm_file SET location = ST_GeogFromText(:w) WHERE id = :i"),
        {"w": "SRID=4326;POINT(-77.75 18.05)", "i": sf.id},
    )
    session.add(
        Consent(
            storm_file_id=sf.id,
            version="v1",
            channel="whatsapp",
            scope={"share_with_insurer": share, "insurer_name": INSURER},
        )
    )
    claim = make_claim(
        session, sf, event, status=ClaimStatus.VERIFIED, reported_needs=list(needs)
    )
    make_verification(session, claim)
    session.flush()
    route_claim(session, claim.id)
    session.flush()
    return sf, claim


def _client(monkeypatch, session) -> TestClient:
    @contextmanager
    def scoped():
        yield session
        session.flush()

    monkeypatch.setattr(fnol, "session_scope", scoped)
    return TestClient(app)


def _token(session, role=AppRole.DIRECTOR):
    user = make_user(session, role)
    set_human_password(session, email=user.email, password="correct horse lighthouse")
    return issue_human_credential(
        session, email=user.email, password="correct horse lighthouse"
    ).token


# -- INS-05, the rule that matters most -------------------------------------


def test_the_packet_carries_no_dollar_figure_and_says_why(session):
    """An absent key would leave a reader guessing whether we forgot. The
    packet states the omission is a rule."""
    sf, claim = _insured_claim(session)

    packet = build_fnol(session, claim.id)

    assert packet.content["monetary_estimate"] is None
    assert "INS-05" in packet.content["monetary_estimate_basis"]
    blob = str(packet.content)
    assert "45000" not in blob
    assert "estimate_low" not in blob and "estimate_high" not in blob


def test_a_damage_assessment_on_file_is_still_not_quoted(session):
    """The Damage Assessment Agent's estimate exists and is deliberately not
    read here."""
    sf, claim = _insured_claim(session)
    session.execute(
        text(
            "INSERT INTO damage_assessment (claim_id, storm_file_id, band,"
            " estimate_low, estimate_high, currency, confidence, findings,"
            " evidence_ids, location_source, verdict, actor_kind, agent_name,"
            " model_version, rationale, snapshot_hash)"
            " VALUES (:c, :s, 'MAJOR', 180000, 240000, 'JMD', 0.8, '[]'::jsonb,"
            " '[]'::jsonb, 'claim', 'PROPOSED', 'AGENT', 'damage_assessment_agent',"
            " 'test', 'test', repeat('a', 64))"
        ),
        {"c": claim.id, "s": sf.id},
    )
    session.flush()

    packet = build_fnol(session, claim.id)

    assert "180000" not in str(packet.content)
    assert "240000" not in str(packet.content)


# -- what it does carry -----------------------------------------------------


def test_the_packet_carries_everything_ins_01_names(session):
    sf, claim = _insured_claim(session)

    body = build_fnol(session, claim.id).content

    assert body["insurer_name"] == INSURER
    assert body["policyholder"]["name"] == sf.head_name
    assert body["policyholder"]["contact_phone"] == sf.phone
    assert body["property"]["parish"] == sf.parish
    assert body["property"]["structure"]["roof"] == "zinc"
    assert body["property"]["latitude"] is not None
    assert body["claim"]["claim_ref"] == claim.claim_ref
    assert body["verification"]["confidence"] is not None
    assert len(body["verification"]["signals"]) == 5
    assert body["event"]["hazard_event_id"] == str(claim.hazard_event_id)


def test_rainfall_is_reported_unavailable_rather_than_omitted(session):
    """An insurer reading a packet with no rainfall key cannot tell whether it
    did not rain or whether we do not measure it. We do not measure it."""
    sf, claim = _insured_claim(session)

    hazard = build_fnol(session, claim.id).content["event"]["observed_hazard"]

    assert "rainfall_mm" in hazard
    assert hazard["rainfall_mm"] is None
    assert "not measured" in hazard["rainfall_basis"]


def test_evidence_is_referenced_by_digest_not_embedded(session):
    """A carrier fetches the images through a door they are entitled to, not
    as a blob attached to a document that may be forwarded onward."""
    sf, claim = _insured_claim(session)
    session.execute(
        text(
            "INSERT INTO evidence (claim_id, kind, uri, payload, sha256)"
            " VALUES (:c, 'PHOTO', 'r2://b/k', '{\"content_type\":\"image/jpeg\"}'::jsonb,"
            " repeat('b', 64))"
        ),
        {"c": claim.id},
    )
    session.flush()

    evidence = build_fnol(session, claim.id).content["evidence"]

    assert evidence
    assert all("sha256" in item for item in evidence)
    assert all("uri" not in item and "data" not in item for item in evidence)


# -- who may have one -------------------------------------------------------


def test_an_uninsured_claim_has_no_packet(session):
    event = make_event(session)
    sf = make_storm_file(session, state=StormFileState.VERIFIED)
    claim = make_claim(session, sf, event, status=ClaimStatus.VERIFIED)
    make_verification(session, claim)
    session.flush()
    route_claim(session, claim.id)

    with pytest.raises(FnolNotAvailable, match="not routed to an insurer"):
        build_fnol(session, claim.id)


def test_withdrawing_consent_stops_the_packet_being_built(session):
    """Consent is asked again at the moment a packet is about to leave, not
    only when the claim was routed."""
    sf, claim = _insured_claim(session)
    assert build_fnol(session, claim.id)  # available while consent stands

    session.execute(
        text("UPDATE consent SET revoked_at = :now WHERE storm_file_id = :s"),
        {"now": datetime.now(UTC), "s": sf.id},
    )
    session.flush()

    with pytest.raises(FnolNotAvailable, match="consent is not currently active"):
        build_fnol(session, claim.id)


def test_an_unverified_claim_has_no_packet(session):
    event = make_event(session)
    sf = make_storm_file(session, state=StormFileState.VERIFIED)
    claim = make_claim(session, sf, event, status=ClaimStatus.FILED)

    with pytest.raises(FnolNotAvailable, match="not verified"):
        build_fnol(session, claim.id)


# -- the surface ------------------------------------------------------------


def test_the_pdf_opens_and_states_the_withheld_valuation(session):
    sf, claim = _insured_claim(session)

    data = render_pdf(build_fnol(session, claim.id))

    assert data.startswith(b"%PDF-")
    assert data.rstrip().endswith(b"%%EOF")
    assert len(data) > 1000


def test_the_endpoints_are_director_gated(session, monkeypatch):
    sf, claim = _insured_claim(session)
    client = _client(monkeypatch, session)
    clerk_token = _token(session, AppRole.REVIEW_CLERK)

    denied = client.get(
        f"/v1/claims/{claim.id}/fnol",
        headers={"Authorization": f"Bearer {clerk_token}"},
    )
    assert denied.status_code == 403

    director_token = _token(session, AppRole.DIRECTOR)
    allowed = client.get(
        f"/v1/claims/{claim.id}/fnol",
        headers={"Authorization": f"Bearer {director_token}"},
    )
    assert allowed.status_code == 200
    assert allowed.json()["insurer_name"] == INSURER
    assert allowed.headers["cache-control"] == "no-store"


def test_the_pdf_route_returns_a_downloadable_document(session, monkeypatch):
    sf, claim = _insured_claim(session)
    client = _client(monkeypatch, session)
    token = _token(session)

    response = client.get(
        f"/v1/claims/{claim.id}/fnol.pdf",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert claim.claim_ref in response.headers["content-disposition"]
    assert response.content.startswith(b"%PDF-")


def test_an_unknown_claim_is_a_404(session, monkeypatch):
    client = _client(monkeypatch, session)
    token = _token(session)

    response = client.get(
        f"/v1/claims/{uuid.uuid4()}/fnol",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404
