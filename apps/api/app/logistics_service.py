"""Matching verified claims to stock and cash, in triage order (LGX-02).

Propose only. Nothing here writes an allocation, decrements a shelf, or moves
a shilling — gate G2 does all three, under a Director's signature, in
``approvals.py``. What this produces is a proposal and a raw record of it.

**Cash is not a judgement.** PAY-06 fixes the grant at a flat J$45,000 for
every verified household precisely so that no agent ever performs a per-claim
valuation. The Damage Assessment Agent's estimate informs the Director looking
at the claim; it does not size the payment, and nothing in this module reads
it. If that ever changes it should change in PAY-06 first, not here.

**Goods are where severity earns its keep.** Tiering lives in goods "where
stock constraints already force it", so the basket a household is offered is a
function of the triage severity the Triage Agent computed, and the order
claims are served in is the triage rank. That is the whole reason the queue is
ordered: when the last tarpaulin goes, it should go to the household the
ordering rule put first, not to whichever row the database happened to return.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from lighthouse_contracts import (
    ActorKind,
    AgentName,
    ClaimStatus,
    Event,
    PayerRoute,
    ResourceKind,
    Severity,
)
from lighthouse_contracts.agents import (
    AllocationDraft,
    LogisticsAgentOutput,
    RunSheet,
    RunSheetStop,
)

from app import ledger
from app.models import Allocation, AllocationPlan, Claim, StockItem, StormFile, Warehouse

#: PAY-06's URGENT / HIGH / MED baskets, in the SKUs LGX-01 names. Deliberately
#: a table rather than a rule: a Director asked why a household got two
#: tarpaulins should be answered by pointing at a line, not by reading code.
BASKETS: dict[Severity, tuple[tuple[str, int], ...]] = {
    Severity.URGENT: (("tarpaulin", 2), ("water", 6), ("food_pack", 3), ("med_kit", 1)),
    Severity.HIGH: (("tarpaulin", 1), ("water", 4), ("food_pack", 2)),
    Severity.MED: (("water", 2), ("food_pack", 1)),
}

#: A claim that reached logistics without triage running is a bug upstream, but
#: it must not become a claim that receives nothing. It gets the smallest
#: basket and is visible in the rationale as having arrived untriaged.
UNTRIAGED_BASKET = Severity.MED

CASH_GRANT = 45000.00


class LogisticsServiceError(RuntimeError):
    """Base class for safe, non-PII logistics failures."""


class NothingToPlan(LogisticsServiceError):
    pass


@dataclass(frozen=True, slots=True)
class LogisticsProposal:
    output: LogisticsAgentOutput
    ledger_entry_id: uuid.UUID | None
    created: bool


def _claims_in_triage_order(session: Session, hazard_event_id: uuid.UUID):
    """Verified, unallocated claims, ordered the way triage ordered them.

    SOL first, then triage rank ascending, then filing time. Claims that
    already carry a signed allocation are excluded rather than re-proposed —
    the plan is a proposal for what has not been served yet.
    """
    allocated = (
        select(Allocation.claim_id)
        .join(AllocationPlan, AllocationPlan.id == Allocation.plan_id)
        .where(AllocationPlan.approval_id.is_not(None))
    )
    return session.execute(
        select(Claim, StormFile)
        .join(StormFile, StormFile.id == Claim.storm_file_id)
        .where(
            Claim.hazard_event_id == hazard_event_id,
            Claim.status == ClaimStatus.VERIFIED,
            Claim.id.not_in(allocated),
        )
        .order_by(
            Claim.sol.desc(),
            Claim.triage_rank.asc().nulls_last(),
            Claim.filed_at.asc(),
            Claim.id.asc(),
        )
    ).all()


def _stock_on_hand(session: Session) -> dict[tuple[uuid.UUID, str], int]:
    rows = session.execute(
        select(StockItem.warehouse_id, StockItem.sku, StockItem.quantity)
    ).all()
    return {(row.warehouse_id, row.sku): row.quantity for row in rows}


def _warehouses_for(session: Session) -> dict[str | None, list[Warehouse]]:
    """Warehouses grouped by parish, with a bucket for the ones without one.

    Parish is the whole of the routing rule in this release. LGX-05 makes route
    optimisation an explicit non-goal until P2, so "the warehouse in the same
    parish, else any warehouse" is not a placeholder for something cleverer —
    it is the documented scope.
    """
    grouped: dict[str | None, list[Warehouse]] = defaultdict(list)
    for warehouse in session.scalars(select(Warehouse).order_by(Warehouse.name)):
        grouped[warehouse.parish].append(warehouse)
    return grouped


def _pick_warehouse(
    parish: str | None,
    sku: str,
    quantity: int,
    by_parish: dict[str | None, list[Warehouse]],
    on_hand: dict[tuple[uuid.UUID, str], int],
) -> Warehouse | None:
    same_parish = by_parish.get(parish, [])
    others = [w for p, ws in by_parish.items() if p != parish for w in ws]
    for warehouse in [*same_parish, *others]:
        if on_hand.get((warehouse.id, sku), 0) >= quantity:
            return warehouse
    return None


def build_proposal(
    session: Session, hazard_event_id: uuid.UUID
) -> LogisticsAgentOutput:
    """Match every unserved verified claim to cash and stock. Pure of writes."""
    rows = _claims_in_triage_order(session, hazard_event_id)
    if not rows:
        raise NothingToPlan("no verified claims are waiting for an allocation")

    on_hand = _stock_on_hand(session)
    by_parish = _warehouses_for(session)
    allocations: list[AllocationDraft] = []
    stops_by_warehouse: dict[uuid.UUID | None, list[RunSheetStop]] = defaultdict(list)
    unmet: list[str] = []
    untriaged = 0

    for claim, storm_file in rows:
        allocations.append(
            AllocationDraft(
                claim_id=claim.id,
                resource=ResourceKind.CASH,
                amount=CASH_GRANT,
                payer_route=PayerRoute.GOV_RELIEF,
            )
        )
        severity = claim.severity or UNTRIAGED_BASKET
        if claim.severity is None:
            untriaged += 1

        for sku, quantity in BASKETS[severity]:
            warehouse = _pick_warehouse(
                storm_file.parish, sku, quantity, by_parish, on_hand
            )
            if warehouse is None:
                # Named, not silently dropped. An unmet need is the signal a
                # Director needs to go and find stock, so burying it would be
                # the worst possible kindness.
                unmet.append(f"{sku}x{quantity} for {claim.claim_ref}")
                continue
            on_hand[(warehouse.id, sku)] -= quantity
            allocations.append(
                AllocationDraft(
                    claim_id=claim.id,
                    resource=ResourceKind.ITEM,
                    sku=sku,
                    quantity=quantity,
                    payer_route=PayerRoute.GOV_RELIEF,
                    warehouse_id=warehouse.id,
                )
            )
            stops_by_warehouse[warehouse.id].append(
                RunSheetStop(
                    claim_id=claim.id,
                    community=storm_file.community,
                    items=[f"{sku}x{quantity}"],
                )
            )

    run_sheets = [
        RunSheet(
            label=f"{_warehouse_name(by_parish, warehouse_id)} run sheet",
            warehouse_id=warehouse_id,
            stops=_merge_stops(stops),
        )
        for warehouse_id, stops in stops_by_warehouse.items()
    ]

    claim_count = len(rows)
    goods = sum(1 for draft in allocations if draft.resource is ResourceKind.ITEM)
    rationale = (
        f"{claim_count} verified claim(s) in triage order: "
        f"{claim_count} cash grant(s) at a flat JMD {CASH_GRANT:.2f}, "
        f"{goods} goods line(s) across {len(run_sheets)} run sheet(s)"
    )
    if untriaged:
        rationale += f"; {untriaged} claim(s) arrived untriaged and got the MED basket"
    if unmet:
        rationale += f"; {len(unmet)} need(s) could not be met from stock"

    return LogisticsAgentOutput(
        allocations=allocations,
        run_sheets=run_sheets,
        unmet_needs=unmet,
        rationale=rationale,
    )


def _warehouse_name(
    by_parish: dict[str | None, list[Warehouse]], warehouse_id: uuid.UUID | None
) -> str:
    for warehouses in by_parish.values():
        for warehouse in warehouses:
            if warehouse.id == warehouse_id:
                return warehouse.name
    return "Unassigned"


def _merge_stops(stops: list[RunSheetStop]) -> list[RunSheetStop]:
    """One stop per household, not one per item — a van visits a house once."""
    merged: dict[uuid.UUID, RunSheetStop] = {}
    for stop in stops:
        existing = merged.get(stop.claim_id)
        if existing is None:
            merged[stop.claim_id] = stop
        else:
            merged[stop.claim_id] = existing.model_copy(
                update={"items": [*existing.items, *stop.items]}
            )
    return list(merged.values())


def propose_allocation_plan(
    session: Session, hazard_event_id: uuid.UUID
) -> LogisticsProposal:
    """Build a plan and store it raw, then stop.

    The proposal is a ledger entry rather than a row in ``allocation_plan``,
    for a reason the schema decides rather than this module: an ``allocation``
    may only be inserted in the same transaction as its approval receipt
    (``allocation_ledger_complete_trigger``), so an unsigned plan cannot carry
    its own allocations. The ledger is already the append-only record of what
    agents said, and CLAUDE.md requires every agent output be stored raw
    whether or not a human later overrides it. So it is stored there, whole.
    """
    output = build_proposal(session, hazard_event_id)
    entry = ledger.append(
        session,
        action=str(Event.ALLOCATION_PLAN_PROPOSED),
        subject_type="hazard_event",
        subject_id=hazard_event_id,
        payload={
            "hazard_event_id": str(hazard_event_id),
            "claim_count": len({d.claim_id for d in output.allocations}),
            "cash_lines": sum(
                1 for d in output.allocations if d.resource is ResourceKind.CASH
            ),
            "goods_lines": sum(
                1 for d in output.allocations if d.resource is ResourceKind.ITEM
            ),
            "unmet_needs": output.unmet_needs,
            "requires_approval": output.requires_approval,
            "proposal": output.model_dump(mode="json"),
        },
        actor_kind=ActorKind.AGENT,
        agent=AgentName.LOGISTICS_AGENT,
    )
    return LogisticsProposal(output=output, ledger_entry_id=entry.id, created=True)


__all__ = [
    "BASKETS",
    "CASH_GRANT",
    "LogisticsProposal",
    "LogisticsServiceError",
    "NothingToPlan",
    "UNTRIAGED_BASKET",
    "build_proposal",
    "propose_allocation_plan",
]
