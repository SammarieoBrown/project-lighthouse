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
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from lighthouse_contracts import ActorKind, AgentName

from .models import LedgerEntry

#: Arbitrary but stable key for the advisory lock that serialises appends.
_LOCK_KEY = 8_140_251_105_301_001

# A verified head is immutable: the database refuses ledger UPDATE and DELETE,
# and every append creates a different ``(seq, hash)`` pair.  Keep only the
# most-recent successful proof for a few seconds so health probes and public
# reads do not repeatedly walk the entire chain.  One entry is deliberately
# enough (and is an absolute memory bound); a new head replaces the old one.
_CHAIN_CACHE_TTL_SECONDS = 5.0


@dataclass(frozen=True)
class _ChainVerificationCache:
    head: tuple[int | None, str | None]
    expires_at: float


_chain_verification_cache: _ChainVerificationCache | None = None
_chain_verification_lock = threading.Lock()


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


def chain_head(session: Session) -> tuple[int | None, str | None]:
    """Return the immutable identity of the current ledger head."""
    head = session.execute(
        select(LedgerEntry.seq, LedgerEntry.hash)
        .order_by(LedgerEntry.seq.desc())
        .limit(1)
    ).one_or_none()
    return (head.seq, head.hash) if head is not None else (None, None)


def clear_verify_chain_cache() -> None:
    """Clear the successful-proof cache (primarily for deterministic tests)."""
    global _chain_verification_cache
    with _chain_verification_lock:
        _chain_verification_cache = None


def cached_verify_chain(
    session: Session,
    *,
    head: tuple[int | None, str | None] | None = None,
) -> bool:
    """Verify the full chain, reusing only a recent successful head proof.

    Failures and exceptions are never cached.  The latter are converted to a
    false result so callers fail closed instead of publishing receipts or
    reporting readiness when integrity could not be established.
    """
    global _chain_verification_cache

    try:
        resolved_head = head if head is not None else chain_head(session)
    except Exception:
        clear_verify_chain_cache()
        return False

    now = time.monotonic()
    with _chain_verification_lock:
        cached = _chain_verification_cache
        if (
            cached is not None
            and cached.head == resolved_head
            and cached.expires_at > now
        ):
            return True

        try:
            valid = verify_chain(session)
        except Exception:
            _chain_verification_cache = None
            return False

        if not valid:
            _chain_verification_cache = None
            return False

        _chain_verification_cache = _ChainVerificationCache(
            head=resolved_head,
            expires_at=now + _CHAIN_CACHE_TTL_SECONDS,
        )
        return True
