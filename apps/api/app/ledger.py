"""The append-only, hash-chained ledger.

Every state transition, posture change, threshold change, approval, allocation,
disbursement and agent verdict lands here (LGR-01). The chain is what lets an
auditor prove nothing was quietly edited after the fact, which is the entire
reason this platform expects to be trusted with public money.

Two properties are load-bearing:

* **Append-only.** ``schema.sql`` installs rules that make UPDATE and DELETE
  silently do nothing, so tampering is impossible through the ORM, through psql,
  and through a panicked developer at 2am.
* **Chained.** Each row's hash covers the previous row's hash, so altering any
  historical row invalidates every row after it. ``verify_chain()`` is the proof
  and must pass at all times.

Appends are serialised with a transaction-scoped advisory lock. Without it two
concurrent writers could read the same ``prev_hash`` and fork the chain — which
would be caught later by ``verify_chain()``, but only after the damage.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from lighthouse_contracts import ActorKind, AgentName

from .models import LedgerEntry

#: Arbitrary but stable key for the advisory lock that serialises appends.
_LOCK_KEY = 8_140_251_105_301_001


def canonical_json(payload: dict) -> str:
    """Stable serialisation. Key order and separators must never vary, or a
    replayed hash will not match a stored one."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def hash_payload(payload: dict) -> str:
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def _row_hash(
    *,
    prev_hash: str | None,
    actor_kind: str,
    actor_id: uuid.UUID | None,
    agent_name: str | None,
    action: str,
    subject_type: str,
    subject_id: uuid.UUID | None,
    payload_hash: str,
    ts: datetime,
) -> str:
    parts = [
        prev_hash or "",
        actor_kind,
        str(actor_id or ""),
        agent_name or "",
        action,
        subject_type,
        str(subject_id or ""),
        payload_hash,
        ts.isoformat(),
    ]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def append(
    session: Session,
    *,
    action: str,
    subject_type: str,
    subject_id: uuid.UUID | None = None,
    payload: dict | None = None,
    actor_kind: ActorKind = ActorKind.SYSTEM,
    actor_id: uuid.UUID | None = None,
    agent: AgentName | None = None,
) -> LedgerEntry:
    """Append one entry. Caller owns the transaction.

    This is deliberately not auto-committing: the entry must land in the same
    transaction as whatever it records, or the ledger becomes a description of
    what probably happened.
    """
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})

    prev = session.execute(
        select(LedgerEntry.hash).order_by(LedgerEntry.seq.desc()).limit(1)
    ).scalar_one_or_none()

    payload = payload or {}
    ph = hash_payload(payload)
    ts = datetime.now(UTC)

    entry = LedgerEntry(
        prev_hash=prev,
        hash=_row_hash(
            prev_hash=prev,
            actor_kind=str(actor_kind),
            actor_id=actor_id,
            agent_name=str(agent) if agent else None,
            action=action,
            subject_type=subject_type,
            subject_id=subject_id,
            payload_hash=ph,
            ts=ts,
        ),
        actor_kind=actor_kind,
        actor_id=actor_id,
        agent_name=str(agent) if agent else None,
        action=action,
        subject_type=subject_type,
        subject_id=subject_id,
        payload=payload,
        payload_hash=ph,
        ts=ts,
    )
    session.add(entry)
    session.flush()
    return entry


class ChainBroken(Exception):
    """Raised with the sequence number of the first bad row."""


def verify_chain(session: Session, *, raise_on_error: bool = False) -> bool:
    """Walk the whole chain and recompute every hash.

    Returns True if intact. This is cheap at buildathon scale and is asserted at
    the end of every replay; if it ever returns False in production, stop and
    find out why rather than regenerating anything.
    """
    prev_hash: str | None = None
    rows = session.execute(select(LedgerEntry).order_by(LedgerEntry.seq)).scalars()

    for row in rows:
        if row.prev_hash != prev_hash:
            if raise_on_error:
                raise ChainBroken(f"seq {row.seq}: prev_hash does not match predecessor")
            return False

        if hash_payload(row.payload or {}) != row.payload_hash:
            if raise_on_error:
                raise ChainBroken(f"seq {row.seq}: payload does not match payload_hash")
            return False

        expected = _row_hash(
            prev_hash=row.prev_hash,
            actor_kind=str(row.actor_kind),
            actor_id=row.actor_id,
            agent_name=row.agent_name,
            action=row.action,
            subject_type=row.subject_type,
            subject_id=row.subject_id,
            payload_hash=row.payload_hash,
            ts=row.ts,
        )
        if expected != row.hash:
            if raise_on_error:
                raise ChainBroken(f"seq {row.seq}: hash mismatch")
            return False

        prev_hash = row.hash

    return True
