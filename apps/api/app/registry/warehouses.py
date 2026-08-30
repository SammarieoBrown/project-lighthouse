"""Put real shelves behind the goods half of PAY-06.

The allocation path has always accepted an ITEM: a SKU, a count, and the
warehouse it comes off, with the stock decrement enforced in the database.
What never existed was a shelf to decrement. This seeds the parish depots and
their opening counts, so "two tarpaulins for a destroyed roof" is a movement
of something real rather than a row with nothing behind it.

Idempotent: re-running tops each SKU back up to its opening count rather than
stacking duplicates, because a demo re-seed should be a reset, not a windfall.

Usage:

    uv run python -m app.registry.warehouses --list
    uv run python -m app.registry.warehouses --seed
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from lighthouse_contracts import ActorKind

from app import ledger
from app.db import session_scope
from app.models import StockItem, Warehouse

#: One depot per replay parish, plus the national store the others draw from.
DEPOTS: tuple[tuple[str, str | None, tuple[float, float]], ...] = (
    ("Black River depot", "Saint Elizabeth", (-77.8508, 18.0292)),
    ("Savanna-la-Mar depot", "Westmoreland", (-78.1333, 18.2167)),
    ("Kingston national store", None, (-76.7936, 17.9714)),
)

#: Opening counts, in the SKUs ``logistics_service.BASKETS`` allocates.
OPENING_STOCK: dict[str, int] = {
    "tarpaulin": 400,
    "water": 2000,
    "food_pack": 800,
    "med_kit": 120,
}


def seed_warehouses(session) -> dict[str, int]:
    """Create missing depots and top every SKU up to its opening count."""
    created = 0
    topped = 0
    for name, parish, (lon, lat) in DEPOTS:
        warehouse = session.scalar(select(Warehouse).where(Warehouse.name == name))
        if warehouse is None:
            warehouse = Warehouse(
                name=name,
                parish=parish,
                location=f"SRID=4326;POINT({lon} {lat})",
            )
            session.add(warehouse)
            session.flush()
            created += 1
        for sku, quantity in OPENING_STOCK.items():
            item = session.scalar(
                select(StockItem).where(
                    StockItem.warehouse_id == warehouse.id, StockItem.sku == sku
                )
            )
            if item is None:
                session.add(
                    StockItem(warehouse_id=warehouse.id, sku=sku, quantity=quantity)
                )
                topped += 1
            elif item.quantity < quantity:
                item.quantity = quantity
                topped += 1
    session.flush()
    ledger.append(
        session,
        action="warehouse.stock_seeded",
        subject_type="warehouse",
        payload={
            "depots_created": created,
            "skus_topped_up": topped,
            "synthetic": True,
            "money_movement": "NOT_INITIATED",
        },
        actor_kind=ActorKind.SYSTEM,
    )
    return {"depots_created": created, "skus_topped_up": topped}


def stock_on_hand(session) -> list[dict]:
    """Every depot with its counts, for the console's goods selector."""
    rows = session.execute(
        select(Warehouse.id, Warehouse.name, Warehouse.parish, StockItem.sku, StockItem.quantity)
        .join(StockItem, StockItem.warehouse_id == Warehouse.id, isouter=True)
        .order_by(Warehouse.name, StockItem.sku)
    ).all()
    depots: dict[str, dict] = {}
    for row in rows:
        depot = depots.setdefault(
            str(row.id),
            {"warehouse_id": row.id, "name": row.name, "parish": row.parish, "stock": []},
        )
        if row.sku is not None:
            depot["stock"].append({"sku": row.sku, "quantity": row.quantity})
    return list(depots.values())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.registry.warehouses")
    parser.add_argument("--seed", action="store_true", help="create depots and top up stock")
    parser.add_argument("--list", action="store_true", help="print stock on hand")
    args = parser.parse_args(argv)
    if not (args.seed or args.list):
        parser.print_help()
        return 2
    with session_scope() as session:
        if args.seed:
            report = seed_warehouses(session)
            print(
                f"depots created: {report['depots_created']}, "
                f"SKUs topped up: {report['skus_topped_up']}"
            )
        if args.list:
            for depot in stock_on_hand(session):
                counts = ", ".join(
                    f"{item['sku']} x{item['quantity']}" for item in depot["stock"]
                ) or "empty"
                print(f"{depot['name']} ({depot['parish'] or 'national'}): {counts}")
    return 0


if __name__ == "__main__":  # pragma: no cover - operator command
    raise SystemExit(main())


__all__ = ["DEPOTS", "OPENING_STOCK", "seed_warehouses", "stock_on_hand"]
