"""Goods relief comes off a named shelf, and the shelf notices."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from lighthouse_contracts import (
    AppRole,
    ClaimStatus,
    PayerRoute,
    ResourceKind,
    StormFileState,
)

from app.approvals import (
    AllocationApprovalRequest,
    ApprovalServiceError,
    approve_claim_allocation,
)
from app.approval_credentials import issue_human_credential, set_human_password
from app.human_auth import authenticate_human
from app.models import Allocation, StockItem, Warehouse
from app.registry.warehouses import OPENING_STOCK, seed_warehouses, stock_on_hand

from factories import (
    make_claim,
    make_event,
    make_storm_file,
    make_user,
    make_verification,
)


def _director(session):
    user = make_user(session, role=AppRole.DIRECTOR)
    set_human_password(session, email=user.email, password="depot-test-password")
    issued = issue_human_credential(
        session, email=user.email, password="depot-test-password"
    )
    return authenticate_human(
        session, f"Bearer {issued.token}", allowed_roles={AppRole.DIRECTOR}
    )


def _verified_claim(session):
    storm_file = make_storm_file(session, state=StormFileState.VERIFIED)
    event = make_event(session)
    claim = make_claim(session, storm_file, event, status=ClaimStatus.VERIFIED)
    make_verification(session, claim)
    return claim


def test_seeding_is_idempotent_and_tops_shelves_back_up(session):
    first = seed_warehouses(session)
    assert first["depots_created"] == 3

    depot = session.scalar(select(Warehouse).where(Warehouse.name == "Black River depot"))
    item = session.scalar(
        select(StockItem).where(
            StockItem.warehouse_id == depot.id, StockItem.sku == "tarpaulin"
        )
    )
    item.quantity = 5
    session.flush()

    second = seed_warehouses(session)

    # A re-seed is a reset, not a windfall: no new depots, and the drawn-down
    # shelf goes back to its opening count rather than gaining another 400.
    assert second["depots_created"] == 0
    assert item.quantity == OPENING_STOCK["tarpaulin"]


def test_approving_goods_takes_them_off_the_named_shelf(session):
    seed_warehouses(session)
    human = _director(session)
    claim = _verified_claim(session)
    depot = session.scalar(select(Warehouse).where(Warehouse.name == "Black River depot"))
    item = session.scalar(
        select(StockItem).where(
            StockItem.warehouse_id == depot.id, StockItem.sku == "tarpaulin"
        )
    )
    before = item.quantity

    approve_claim_allocation(
        session,
        claim_id=claim.id,
        request=AllocationApprovalRequest(
            resource=ResourceKind.ITEM,
            sku="tarpaulin",
            quantity=2,
            warehouse_id=depot.id,
            payer_route=PayerRoute.GOV_RELIEF,
            note="Two tarpaulins for a roof opened to the sky.",
        ),
        idempotency_key=str(uuid.uuid4()),
        human=human,
    )

    allocation = session.scalar(select(Allocation))
    assert allocation.resource is ResourceKind.ITEM
    assert allocation.quantity == 2
    assert allocation.amount is None
    session.refresh(item)
    assert item.quantity == before - 2


def test_a_depot_cannot_sign_out_stock_it_does_not_hold(session):
    seed_warehouses(session)
    human = _director(session)
    claim = _verified_claim(session)
    depot = session.scalar(select(Warehouse).where(Warehouse.name == "Black River depot"))

    with pytest.raises((ApprovalServiceError, DBAPIError)):
        approve_claim_allocation(
            session,
            claim_id=claim.id,
            request=AllocationApprovalRequest(
                resource=ResourceKind.ITEM,
                sku="tarpaulin",
                quantity=OPENING_STOCK["tarpaulin"] + 1,
                warehouse_id=depot.id,
                payer_route=PayerRoute.GOV_RELIEF,
                note="more tarpaulins than exist",
            ),
            idempotency_key=str(uuid.uuid4()),
            human=human,
        )


def test_stock_on_hand_reports_every_depot(session):
    seed_warehouses(session)

    depots = stock_on_hand(session)

    assert {depot["name"] for depot in depots} == {
        "Black River depot",
        "Savanna-la-Mar depot",
        "Kingston national store",
    }
    black_river = next(d for d in depots if d["name"] == "Black River depot")
    assert black_river["parish"] == "Saint Elizabeth"
    assert {item["sku"] for item in black_river["stock"]} == set(OPENING_STOCK)
