"""Logistics: matching in triage order, and the goods half of PAY-06.

The agent proposes and nothing else. What these tests hold it to is that the
order it serves claims in is the order triage computed, that the basket a
household gets is the one its severity earns, that stock it cannot find is
named rather than dropped, and that it never writes an allocation.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select, text

from lighthouse_contracts import (
    AgentName,
    ClaimStatus,
    Event,
    ResourceKind,
    Severity,
    StormFileState,
)

from app.agents.logistics_agent import handle as logistics_handle
from app.logistics_service import (
    BASKETS,
    CASH_GRANT,
    NothingToPlan,
    build_proposal,
    propose_allocation_plan,
)
from app.models import Allocation, LedgerEntry, StockItem, Warehouse
from app.worker import load_handlers

from factories import make_claim, make_event, make_storm_file


def _warehouse(session, *, name="St Elizabeth Depot", parish="St Elizabeth", **stock):
    warehouse = Warehouse(name=name, parish=parish)
    session.add(warehouse)
    session.flush()
    for sku, quantity in stock.items():
        session.add(
            StockItem(warehouse_id=warehouse.id, sku=sku, quantity=quantity)
        )
    session.flush()
    return warehouse


def _verified_claim(session, event, *, severity=Severity.HIGH, rank=1000, **kw):
    sf = make_storm_file(session, state=StormFileState.VERIFIED)
    claim = make_claim(
        session, sf, event, status=ClaimStatus.VERIFIED, severity=severity, **kw
    )
    claim.triage_rank = rank
    session.flush()
    return claim


def _full_shelf(session, **overrides):
    stock = {"tarpaulin": 50, "water": 200, "food_pack": 100, "med_kit": 20}
    stock.update(overrides)
    return _warehouse(session, **stock)


# -- ordering ---------------------------------------------------------------


def test_claims_are_served_in_the_order_triage_put_them(session):
    """The whole point of computing a rank. When the last tarpaulin goes it
    should go to the household the ordering rule put first."""
    event = make_event(session)
    _full_shelf(session)
    last = _verified_claim(session, event, severity=Severity.MED, rank=3000)
    first = _verified_claim(session, event, severity=Severity.URGENT, rank=10)
    middle = _verified_claim(session, event, severity=Severity.HIGH, rank=1500)

    output = build_proposal(session, event.id)

    order = []
    for draft in output.allocations:
        if draft.claim_id not in order:
            order.append(draft.claim_id)
    assert order == [first.id, middle.id, last.id]


def test_a_safety_of_life_claim_is_served_before_a_better_ranked_one(session):
    event = make_event(session)
    _full_shelf(session)
    ranked = _verified_claim(session, event, severity=Severity.URGENT, rank=0)
    sol = _verified_claim(session, event, severity=Severity.MED, rank=3000, sol=True)

    output = build_proposal(session, event.id)

    assert output.allocations[0].claim_id == sol.id
    assert ranked.id in {d.claim_id for d in output.allocations}


# -- what each household is offered ------------------------------------------


def test_every_verified_claim_gets_the_flat_grant_and_nothing_else_decides_it(session):
    """PAY-06 fixes cash so no agent performs a per-claim valuation. Three
    households at three severities, one amount."""
    event = make_event(session)
    _full_shelf(session)
    for severity in (Severity.URGENT, Severity.HIGH, Severity.MED):
        _verified_claim(session, event, severity=severity)

    output = build_proposal(session, event.id)

    cash = [d for d in output.allocations if d.resource is ResourceKind.CASH]
    assert len(cash) == 3
    assert {d.amount for d in cash} == {CASH_GRANT}


def test_the_basket_is_the_one_the_severity_earns(session):
    event = make_event(session)
    _full_shelf(session)
    urgent = _verified_claim(session, event, severity=Severity.URGENT)

    output = build_proposal(session, event.id)

    goods = {
        (d.sku, d.quantity)
        for d in output.allocations
        if d.resource is ResourceKind.ITEM and d.claim_id == urgent.id
    }
    assert goods == set(BASKETS[Severity.URGENT])


def test_an_untriaged_claim_still_gets_something(session):
    """Reaching logistics untriaged is an upstream bug. It must not become a
    household that receives nothing."""
    event = make_event(session)
    _full_shelf(session)
    claim = _verified_claim(session, event, severity=None, rank=None)

    output = build_proposal(session, event.id)

    goods = {
        (d.sku, d.quantity)
        for d in output.allocations
        if d.resource is ResourceKind.ITEM and d.claim_id == claim.id
    }
    assert goods == set(BASKETS[Severity.MED])
    assert "untriaged" in output.rationale


# -- stock -------------------------------------------------------------------


def test_the_proposal_never_promises_stock_twice(session):
    event = make_event(session)
    _full_shelf(session, tarpaulin=1)
    _verified_claim(session, event, severity=Severity.HIGH, rank=1)
    _verified_claim(session, event, severity=Severity.HIGH, rank=2)

    output = build_proposal(session, event.id)

    tarps = [
        d for d in output.allocations
        if d.resource is ResourceKind.ITEM and d.sku == "tarpaulin"
    ]
    assert len(tarps) == 1
    assert any("tarpaulin" in need for need in output.unmet_needs)


def test_stock_it_cannot_find_is_named_not_dropped(session):
    """An unmet need is the signal a Director needs in order to go and find
    stock. Burying it would be the worst possible kindness."""
    event = make_event(session)
    _warehouse(session, water=100)  # no tarpaulins at all
    claim = _verified_claim(session, event, severity=Severity.HIGH)

    output = build_proposal(session, event.id)

    assert any(claim.claim_ref in need for need in output.unmet_needs)
    assert any("tarpaulin" in need for need in output.unmet_needs)


def test_a_household_appears_once_per_run_sheet_however_many_items(session):
    """A van visits a house once."""
    event = make_event(session)
    _full_shelf(session)
    _verified_claim(session, event, severity=Severity.URGENT)

    output = build_proposal(session, event.id)

    assert len(output.run_sheets) == 1
    sheet = output.run_sheets[0]
    assert len(sheet.stops) == 1
    assert len(sheet.stops[0].items) == len(BASKETS[Severity.URGENT])


# -- propose only ------------------------------------------------------------


def test_proposing_writes_a_raw_ledger_record_and_no_allocation(session):
    event = make_event(session)
    _full_shelf(session)
    _verified_claim(session, event, severity=Severity.HIGH)

    proposal = propose_allocation_plan(session, event.id)
    session.flush()

    entry = session.scalar(
        select(LedgerEntry).where(
            LedgerEntry.action == str(Event.ALLOCATION_PLAN_PROPOSED),
            LedgerEntry.subject_id == event.id,
        )
    )
    assert entry is not None
    assert entry.agent_name == str(AgentName.LOGISTICS_AGENT)
    assert entry.payload["requires_approval"] is True
    # Stored whole: this is the eval set, not a summary of it.
    assert entry.payload["proposal"]["allocations"]
    assert proposal.output.requires_approval is True

    # Nothing was released and no shelf moved.
    assert session.scalar(select(func.count()).select_from(Allocation)) == 0
    assert session.scalar(
        select(StockItem.quantity).where(StockItem.sku == "tarpaulin")
    ) == 50


def test_an_already_served_claim_is_not_proposed_again(session):
    event = make_event(session)
    _full_shelf(session)
    _verified_claim(session, event)

    build_proposal(session, event.id)  # still unserved — no allocation written
    session.execute(
        text("UPDATE claim SET status = 'SETTLED' WHERE hazard_event_id = :e"),
        {"e": event.id},
    )
    session.flush()

    with pytest.raises(NothingToPlan):
        build_proposal(session, event.id)


def test_the_handler_plans_for_the_event_not_the_claim(session):
    """Triage enqueues one job per claim; planning per claim would promise the
    same last tarpaulin to several households, each job blind to the others."""
    event = make_event(session)
    _full_shelf(session)
    first = _verified_claim(session, event, rank=1)
    _verified_claim(session, event, rank=2)

    logistics_handle(session, {"claim_id": str(first.id)})
    session.flush()

    entry = session.scalar(
        select(LedgerEntry)
        .where(LedgerEntry.action == str(Event.ALLOCATION_PLAN_PROPOSED))
        .order_by(LedgerEntry.seq.desc())
        .limit(1)
    )
    assert entry.payload["claim_count"] == 2


def test_worker_registers_the_logistics_agent():
    assert str(AgentName.LOGISTICS_AGENT) in load_handlers()
