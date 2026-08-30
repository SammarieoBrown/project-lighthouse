"""Ensure the demo donation pools exist — ``python -m app.replay.seed_pools``.

Runs as the deploy hook, immediately after migrations. The portal's give form
(DON-01) renders only when there is a pool to give to, and pools were created
only inside the full claim replay — a deliberate, by-hand act that production
had never run. The result was a deployed donation surface permanently in its
empty state: honest, and useless, and indistinguishable from "not built" to
anyone reading the page.

This is the smallest cut that fixes that for good. The pools become a deploy
guarantee, the way the schema is, while the synthetic-claim replay stays a
deliberate act — this entrypoint touches the ``donation_pool`` table and
nothing else. Safe to re-run on every deploy because ``seed_pools`` is
idempotent by name and repairs drifted parish scope rather than minting a
duplicate pot.

A failure here propagates and blocks the deploy, the same posture as a failed
migration: a portal without pools is a broken public surface, not a degraded
one.
"""

from __future__ import annotations

from ..db import session_scope
from .seed_claims import seed_pools


def main() -> int:
    with session_scope() as session:
        created = seed_pools(session)
    print(f"donation pools ensured ({created} created)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
