"""The ledger is the platform's claim about itself. These tests are that claim,
made checkable."""

from __future__ import annotations

from sqlalchemy import select, text

from lighthouse_contracts import ActorKind, AgentName

from app import ledger
from app.models import LedgerEntry

from factories import make_storm_file


def test_entries_chain_to_their_predecessor(session):
    sf = make_storm_file(session)
    for i in range(5):
        ledger.append(
            session,
            action="test.event",
            subject_type="storm_file",
            subject_id=sf.id,
            payload={"i": i},
            actor_kind=ActorKind.AGENT,
            agent=AgentName.INTAKE_AGENT,
        )

    rows = session.execute(select(LedgerEntry).order_by(LedgerEntry.seq)).scalars().all()
    assert len(rows) == 5
    assert rows[0].prev_hash is None
    for prev, cur in zip(rows, rows[1:], strict=False):
        assert cur.prev_hash == prev.hash

    assert ledger.verify_chain(session) is True


def test_the_database_refuses_to_update_or_delete_the_ledger(session):
    """LGR-01. Tampering is not caught after the fact — it does not happen.

    The rules make UPDATE and DELETE no-ops rather than errors, which is
    unusual. It means a tampering attempt leaves the record intact and the
    attacker believing they succeeded.
    """
    sf = make_storm_file(session)
    entry = ledger.append(
        session, action="original.action", subject_type="storm_file", subject_id=sf.id
    )
    session.flush()
    original_hash = entry.hash

    session.execute(
        text("UPDATE ledger_entry SET action = 'tampered' WHERE hash = :h"),
        {"h": original_hash},
    )
    session.execute(text("DELETE FROM ledger_entry WHERE hash = :h"), {"h": original_hash})

    still_there = session.execute(
        select(LedgerEntry.action).where(LedgerEntry.hash == original_hash)
    ).scalar_one()

    assert still_there == "original.action"
    assert ledger.verify_chain(session) is True


def test_verify_chain_detects_a_payload_that_no_longer_matches_its_hash(session):
    """If a row's payload were altered by some path we have not thought of,
    the chain must notice. This is the backstop behind the backstop."""
    sf = make_storm_file(session)
    ledger.append(
        session,
        action="claim.created",
        subject_type="storm_file",
        subject_id=sf.id,
        payload={"amount": 45000},
    )
    session.flush()

    # Corrupt payload_hash directly — the one field the append-only rules do not
    # protect us from, since we are simulating a hostile path, not using one.
    session.execute(text("ALTER TABLE ledger_entry DISABLE RULE ledger_no_update"))
    session.execute(
        text("UPDATE ledger_entry SET payload = '{\"amount\": 4500000}'::jsonb")
    )
    session.execute(text("ALTER TABLE ledger_entry ENABLE RULE ledger_no_update"))

    assert ledger.verify_chain(session) is False


def test_payload_hash_is_order_independent(session):
    """Canonical JSON: the same facts must hash the same however they are built,
    or replaying a ledger becomes a coin toss."""
    a = ledger.hash_payload({"b": 2, "a": 1})
    b = ledger.hash_payload({"a": 1, "b": 2})
    assert a == b
