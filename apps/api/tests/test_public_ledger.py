"""Privacy, pagination, and integrity-cache tests for the public ledger."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from lighthouse_contracts import AppRole, ClaimStatus, StormFileState

from app import ledger, public_ledger
from app.models import LedgerEntry
from app.web import app
from factories import make_claim, make_event, make_storm_file, make_user, settle_with_signature


VALID_PAYLOAD = {
    "gate": "ALLOCATION_PLAN",
    "parish": "Saint Elizabeth",
    "need_category": "ROOF_DAMAGE",
    "resource": "CASH",
    "amount": "45000.00",
    "currency": "JMD",
    "payer_route": "GOV_RELIEF",
    "synthetic": True,
    "money_movement": "NOT_INITIATED_AT_APPROVAL",
}


@pytest.fixture(autouse=True)
def _clean_chain_verification_cache():
    ledger.clear_verify_chain_cache()
    yield
    ledger.clear_verify_chain_cache()


def _client(monkeypatch: pytest.MonkeyPatch, session) -> TestClient:
    @contextmanager
    def scoped():
        yield session
        session.flush()

    monkeypatch.setattr(public_ledger, "session_scope", scoped)
    return TestClient(app)


def _append_public(session, **payload_overrides):
    if payload_overrides:
        raise ValueError("database-backed approval receipts cannot be overridden")
    storm_file = make_storm_file(session, state=StormFileState.VERIFIED)
    claim = make_claim(
        session,
        storm_file,
        make_event(session),
        status=ClaimStatus.VERIFIED,
    )
    disbursement = settle_with_signature(
        session, claim, make_user(session, AppRole.FINANCE_OFFICER)
    )
    return session.scalar(
        select(LedgerEntry).where(
            LedgerEntry.action == "allocation.approved",
            LedgerEntry.subject_id == disbursement.allocation_id,
        )
    )


def test_public_receipt_is_redacted_and_uses_utc_date(session, monkeypatch):
    entry = _append_public(session)
    internal_ids = {
        str(entry.id),
        str(entry.subject_id),
        entry.payload["claim_id"],
        entry.payload["allocation_id"],
        entry.payload["approval_id"],
    }
    response = _client(monkeypatch, session).get("/v1/public/ledger")

    assert response.status_code == 200
    receipt = response.json()["entries"][0]
    assert receipt == {
        "seq": entry.seq,
        "prev_hash": entry.prev_hash,
        "hash": entry.hash,
        "payload_hash": entry.payload_hash,
        "action": "allocation.approved",
        "recorded_on": entry.ts.date().isoformat(),
        "allocation": {
            "resource": "CASH",
            "amount": "45000.00",
            "currency": "JMD",
            "payer_route": "GOV_RELIEF",
            "synthetic": True,
        },
        "approval": {"gate": "ALLOCATION_PLAN"},
        "money_movement": {"status": "NOT_INITIATED_AT_APPROVAL"},
    }
    assert "T" not in receipt["recorded_on"]
    assert "parish" not in response.text
    assert "need_category" not in response.text
    assert "Saint Elizabeth" not in response.text
    assert "ROOF_DAMAGE" not in response.text
    assert all(identifier not in response.text for identifier in internal_ids)


def test_latest_selects_newest_receipts_and_returns_them_chronologically(
    session, monkeypatch
):
    entries = [_append_public(session) for _ in range(4)]

    response = _client(monkeypatch, session).get(
        "/v1/public/ledger?latest=true&limit=2"
    )

    assert response.status_code == 200
    body = response.json()
    assert [row["seq"] for row in body["entries"]] == [
        entries[-2].seq,
        entries[-1].seq,
    ]
    assert body["page"] == {
        "after_seq": 0,
        "limit": 2,
        "next_after_seq": entries[-1].seq,
        "has_more": True,
    }


def test_normal_after_seq_pagination_remains_forward_ordered(session, monkeypatch):
    entries = [_append_public(session) for _ in range(3)]

    response = _client(monkeypatch, session).get(
        f"/v1/public/ledger?after_seq={entries[0].seq}&limit=1"
    )

    assert response.status_code == 200
    body = response.json()
    assert [row["seq"] for row in body["entries"]] == [entries[1].seq]
    assert body["page"]["next_after_seq"] == entries[1].seq
    assert body["page"]["has_more"] is True


def test_publication_revalidates_redacted_taxonomy_and_fails_closed(
    session, monkeypatch
):
    row = SimpleNamespace(
        seq=99,
        subject_id=uuid.uuid4(),
        ts=_append_public(session).ts,
        payload={**VALID_PAYLOAD, "parish": "Household of Jane Doe"},
    )

    with pytest.raises(HTTPException) as exc:
        public_ledger._public_entry(row)

    assert exc.value.status_code == 503
    assert exc.value.detail == "public ledger entry 99 is not publishable"
    assert "Jane Doe" not in exc.value.detail


def test_public_ledger_withholds_entries_when_cached_verification_fails(
    session, monkeypatch
):
    _append_public(session)
    monkeypatch.setattr(
        public_ledger.ledger,
        "cached_verify_chain",
        lambda _session, **_kwargs: False,
    )

    response = _client(monkeypatch, session).get("/v1/public/ledger")

    assert response.status_code == 503
    assert response.json() == {
        "detail": "ledger integrity check failed; public entries withheld"
    }
    assert "allocation.approved" not in response.text


def test_cached_verification_reuses_same_head_and_invalidates_on_append(
    session, monkeypatch
):
    _append_public(session)
    original_verify = ledger.verify_chain
    calls = 0

    def counted_verify(value):
        nonlocal calls
        calls += 1
        return original_verify(value)

    monkeypatch.setattr(ledger, "verify_chain", counted_verify)

    assert ledger.cached_verify_chain(session) is True
    assert ledger.cached_verify_chain(session) is True
    assert calls == 1

    _append_public(session)
    assert ledger.cached_verify_chain(session) is True
    assert calls == 2


def test_cached_verification_never_caches_failure(session, monkeypatch):
    _append_public(session)
    calls = 0

    def broken(_session):
        nonlocal calls
        calls += 1
        return False

    monkeypatch.setattr(ledger, "verify_chain", broken)

    assert ledger.cached_verify_chain(session) is False
    assert ledger.cached_verify_chain(session) is False
    assert calls == 2


def test_cached_verification_expires_after_short_ttl(session, monkeypatch):
    _append_public(session)
    original_verify = ledger.verify_chain
    calls = 0
    now = 100.0

    def counted_verify(value):
        nonlocal calls
        calls += 1
        return original_verify(value)

    monkeypatch.setattr(ledger, "verify_chain", counted_verify)
    monkeypatch.setattr(ledger.time, "monotonic", lambda: now)

    assert ledger.cached_verify_chain(session) is True
    assert ledger.cached_verify_chain(session) is True
    assert calls == 1

    now += ledger._CHAIN_CACHE_TTL_SECONDS + 0.001
    assert ledger.cached_verify_chain(session) is True
    assert calls == 2
