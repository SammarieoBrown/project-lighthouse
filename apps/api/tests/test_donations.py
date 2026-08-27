"""Donations (DON-01 to DON-04).

Two things these hold hardest. Nothing public names a household or a donor,
and no figure escapes without saying it is simulated — a number on a public
page that looks like real money and is not is the worst thing this surface
could publish.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from lighthouse_contracts import ClaimStatus, Event, StormFileState

from app import donations
from app.donations_service import (
    MIN_AGGREGATION_BUCKET,
    DonationRejected,
    PoolNotFound,
    create_pool,
    donor_journey,
    draw_down,
    record_donation,
)
from app.models import Donation, DonationPool, LedgerEntry
from app.web import app

from factories import (
    make_claim,
    make_event,
    make_storm_file,
    make_verification,
)


def _pool(session, name="St Elizabeth pool", scope="PARISH", value="St Elizabeth"):
    return create_pool(session, name=name, scope_kind=scope, scope_value=value)


def _client(monkeypatch, session) -> TestClient:
    @contextmanager
    def scoped():
        yield session
        session.flush()

    monkeypatch.setattr(donations, "session_scope", scoped)
    return TestClient(app)


# -- intake and pooling -----------------------------------------------------


def test_a_donation_credits_the_pool_in_one_transaction(session):
    """The public balance can never show money with no donation behind it."""
    pool = _pool(session)

    receipt = record_donation(
        session, pool_id=pool.id, donor_handle="Auntie M", amount=10000
    )

    assert receipt.pool_balance == Decimal("10000.00")
    assert session.scalar(select(DonationPool.balance)) == Decimal("10000.00")
    assert session.scalar(select(Donation.amount)) == Decimal("10000.00")


def test_every_donation_is_a_ledger_entry_with_a_handle_not_a_name(session):
    """DON-02. A public record of what arrived is a different product from a
    public record of who gave it."""
    pool = _pool(session)

    receipt = record_donation(
        session, pool_id=pool.id, donor_handle="diaspora-42", amount=5000
    )
    session.flush()

    entry = session.scalar(
        select(LedgerEntry).where(
            LedgerEntry.action == str(Event.DONATION_RECEIVED),
            LedgerEntry.subject_id == receipt.donation.id,
        )
    )
    assert entry is not None
    assert entry.payload["donor_handle"] == "diaspora-42"
    assert entry.payload["amount"] == "5000.00"
    assert entry.payload["pool_balance_after"] == "5000.00"
    assert entry.payload["simulated"] is True


def test_pools_are_event_or_parish_and_nothing_narrower(session):
    """PRD 11.3: finer pools fragment the money and constrain the allocation
    agent before there is volume to justify either."""
    with pytest.raises(DonationRejected, match="pool scope"):
        create_pool(session, name="Roofs only", scope_kind="CATEGORY", scope_value="ROOF")

    with pytest.raises(DonationRejected, match="must name its parish"):
        create_pool(session, name="Nameless", scope_kind="PARISH")


def test_a_donation_must_be_positive_and_attributed(session):
    pool = _pool(session)

    with pytest.raises(DonationRejected, match="positive"):
        record_donation(session, pool_id=pool.id, donor_handle="x", amount=0)
    with pytest.raises(DonationRejected, match="handle"):
        record_donation(session, pool_id=pool.id, donor_handle="   ", amount=10)
    with pytest.raises(PoolNotFound):
        record_donation(session, pool_id=uuid.uuid4(), donor_handle="x", amount=10)


# -- draw-down --------------------------------------------------------------


def test_a_pool_cannot_be_overdrawn(session):
    """The last thing standing between a signed allocation and money that was
    never given."""
    pool = _pool(session)
    record_donation(session, pool_id=pool.id, donor_handle="Auntie M", amount=45000)

    remaining = draw_down(session, pool.id, Decimal("45000.00"))
    assert remaining == Decimal("0.00")

    with pytest.raises(DonationRejected, match="pool holds"):
        draw_down(session, pool.id, Decimal("0.01"))


def test_the_database_refuses_a_negative_balance_even_if_python_is_bypassed(session):
    pool = _pool(session)

    with pytest.raises(Exception, match="donation_pool_balance_chk"):
        with session.begin_nested():
            session.execute(
                text("UPDATE donation_pool SET balance = -1 WHERE id = :i"),
                {"i": pool.id},
            )


# -- the donor journey ------------------------------------------------------


def _funded_household(session, pool):
    event = make_event(session)
    sf = make_storm_file(session, state=StormFileState.VERIFIED)
    claim = make_claim(session, sf, event, status=ClaimStatus.VERIFIED)
    make_verification(session, claim)
    session.flush()
    return claim


def test_the_journey_traces_received_pooled_allocated(session):
    pool = _pool(session)
    receipt = record_donation(
        session, pool_id=pool.id, donor_handle="Auntie M", amount=100000
    )

    journey = donor_journey(session, receipt.donation.id)

    assert journey["received"]["amount"] == "100000.00"
    assert journey["pooled"]["pool_name"] == "St Elizabeth pool"
    assert journey["allocated"]["household_count"] == 0
    assert journey["disbursed_and_confirmed"]["confirmed_count"] == 0
    assert journey["simulated"] is True


def test_the_journey_never_names_a_household(session):
    pool = _pool(session)
    receipt = record_donation(
        session, pool_id=pool.id, donor_handle="Auntie M", amount=100000
    )
    claim = _funded_household(session, pool)

    journey = donor_journey(session, receipt.donation.id)

    blob = str(journey)
    assert claim.claim_ref not in blob
    assert "head_name" not in blob


def test_parishes_are_withheld_below_the_aggregation_bucket(session):
    """"Reached 2 households in Black River" is close to naming an address."""
    pool = _pool(session)
    receipt = record_donation(
        session, pool_id=pool.id, donor_handle="Auntie M", amount=100000
    )

    journey = donor_journey(session, receipt.donation.id)

    assert journey["allocated"]["parishes"] == []
    assert journey["allocated"]["parishes_withheld_until_bucket"] is True
    assert MIN_AGGREGATION_BUCKET >= 10


# -- the public surface -----------------------------------------------------


def test_the_public_pool_view_is_aggregate_and_says_it_is_simulated(
    session, monkeypatch
):
    pool = _pool(session)
    record_donation(session, pool_id=pool.id, donor_handle="Auntie M", amount=25000)
    client = _client(monkeypatch, session)

    response = client.get("/v1/public/pools")

    assert response.status_code == 200
    body = response.json()
    assert body["simulated"] is True
    row = body["pools"][0]
    assert row["balance"] == "25000.00"
    assert row["donation_count"] == 1
    assert "donor_handle" not in row  # aggregate only


def test_donating_through_the_public_route_records_custody_honestly(
    session, monkeypatch
):
    """The platform records and directs; the fiscal sponsor holds funds."""
    pool = _pool(session)
    client = _client(monkeypatch, session)

    response = client.post(
        "/v1/public/donations",
        json={
            "pool_id": str(pool.id),
            "donor_handle": "diaspora-42",
            "amount": "10000.00",
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["simulated"] is True
    assert "fiscal sponsor" in body["custody"]
    assert body["pool_balance"] == "10000.00"


def test_the_public_donation_route_refuses_a_bad_amount(session, monkeypatch):
    pool = _pool(session)
    client = _client(monkeypatch, session)

    response = client.post(
        "/v1/public/donations",
        json={"pool_id": str(pool.id), "donor_handle": "x", "amount": "-5.00"},
    )

    assert response.status_code == 422


def test_the_journey_route_is_public_and_needs_no_account(session, monkeypatch):
    """A journey view a donor cannot open without an account is not one."""
    pool = _pool(session)
    receipt = record_donation(
        session, pool_id=pool.id, donor_handle="Auntie M", amount=10000
    )
    client = _client(monkeypatch, session)

    response = client.get(f"/v1/public/donations/{receipt.donation.id}/journey")

    assert response.status_code == 200
    assert response.json()["donor_handle"] == "Auntie M"
    assert response.headers["cache-control"] == "no-store"


def test_an_unknown_donation_journey_is_a_404(session, monkeypatch):
    client = _client(monkeypatch, session)
    response = client.get(f"/v1/public/donations/{uuid.uuid4()}/journey")
    assert response.status_code == 404
