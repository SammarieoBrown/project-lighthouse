"""The Director sizes the grant; donor-funded allocations settle.

The flat J$45,000 was PAY-06's stand-in while no human could size a grant
per claim. The Director now sets the amount at approval, so every guard that
pinned the constant compares against the signed allocation row instead — the
consistency the constant was standing in for, stated directly. Donor-funded
allocations also pass the settlement guards, which had pinned GOV_RELIEF.

Function bodies come from the canonical schema between per-function markers,
the same pattern 0015 uses.
"""

from __future__ import annotations

import re
from pathlib import Path

from alembic import op

from app.config import SCHEMA_SQL

revision = "0016_director_sized_grants"
down_revision = "0015_director_all_gates"
branch_labels = None
depends_on = None

_FUNCTIONS = (
    "allocation_signed_guard",
    "ledger_allocation_approval_guard",
    "disbursement_batch_signed_guard",
    "disbursement_lifecycle_guard",
    "ledger_disbursement_receipt_guard",
)


def _vendored_sql(name: str) -> str:
    """The pre-0016 function bodies, kept beside this file — ``schema.sql`` has
    moved on and no longer contains them."""
    return (Path(__file__).resolve().parent / "downgrade_sql" / name).read_text(
        encoding="utf-8"
    )


def _canonical_function(name: str) -> str:
    source = SCHEMA_SQL.read_text(encoding="utf-8")
    begin, end = f"-- {name.upper()}_FN_BEGIN", f"-- {name.upper()}_FN_END"
    try:
        start = source.index(begin) + len(begin)
        stop = source.index(end, start)
    except ValueError as exc:  # pragma: no cover - release packaging failure
        raise RuntimeError(f"canonical {name} block is missing") from exc
    block = source[start:stop].strip()
    if not re.match(rf"CREATE OR REPLACE FUNCTION {name}\(\)", block):
        raise RuntimeError(f"canonical {name} block does not define {name}")
    return block


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE allocation DROP CONSTRAINT allocation_release_policy_chk;
        ALTER TABLE allocation ADD CONSTRAINT allocation_release_policy_chk CHECK (
          (
            resource = 'CASH'
            AND amount > 0
            AND currency = 'JMD'
            AND sku IS NULL
            AND quantity IS NULL
            AND warehouse_id IS NULL
          ) OR (
            resource = 'ITEM'
            AND amount IS NULL
            AND sku IS NOT NULL
            AND quantity IS NOT NULL
            AND quantity > 0
            AND warehouse_id IS NOT NULL
          )
        );

        ALTER TABLE disbursement_batch
          DROP CONSTRAINT disbursement_batch_release_policy_chk;
        ALTER TABLE disbursement_batch
          ADD CONSTRAINT disbursement_batch_release_policy_chk CHECK (
            total > 0 AND channel IN ('BANK', 'MOBILE_MONEY', 'VOUCHER')
          );
        """
    )
    for name in _FUNCTIONS:
        op.execute(_canonical_function(name))


def downgrade() -> None:
    """Re-pin the flat grant.

    Allocations already signed at another figure would violate the restored
    constraint, so they are removed with the rows that depend on them. This is
    a development path — reverting a policy in production is a decision to make
    forward, with a new migration and a Director behind it.
    """
    op.execute(
        """
        DELETE FROM disbursement;
        DELETE FROM disbursement_batch;
        DELETE FROM allocation WHERE resource = 'CASH' AND amount <> 45000.00;

        ALTER TABLE allocation DROP CONSTRAINT allocation_release_policy_chk;
        ALTER TABLE allocation ADD CONSTRAINT allocation_release_policy_chk CHECK (
          (
            resource = 'CASH'
            AND amount = 45000.00
            AND currency = 'JMD'
            AND sku IS NULL
            AND quantity IS NULL
            AND warehouse_id IS NULL
          ) OR (
            resource = 'ITEM'
            AND amount IS NULL
            AND sku IS NOT NULL
            AND quantity IS NOT NULL
            AND quantity > 0
            AND warehouse_id IS NOT NULL
          )
        );

        ALTER TABLE disbursement_batch
          DROP CONSTRAINT disbursement_batch_release_policy_chk;
        ALTER TABLE disbursement_batch
          ADD CONSTRAINT disbursement_batch_release_policy_chk CHECK (
            total = 45000.00 AND channel IN ('BANK', 'MOBILE_MONEY', 'VOUCHER')
          );
        """
    )
    op.execute(_vendored_sql("0016_downgrade.sql"))
