"""Readiness semantics for the Render health check."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import web


@pytest.mark.parametrize(
    ("chain_ok", "expected_status", "expected_label"),
    [(True, 200, "ok"), (False, 503, "error")],
)
def test_health_fails_readiness_when_ledger_chain_is_broken(
    monkeypatch: pytest.MonkeyPatch,
    chain_ok: bool,
    expected_status: int,
    expected_label: str,
) -> None:
    session = object()

    @contextmanager
    def fake_session_scope():
        yield session

    monkeypatch.setattr(
        web, "get_settings", lambda: SimpleNamespace(environment="test")
    )
    monkeypatch.setattr(web, "session_scope", fake_session_scope)
    monkeypatch.setattr(web.ledger, "cached_verify_chain", lambda value: chain_ok)
    monkeypatch.setattr(web.queue, "pending_count", lambda value: 7)

    response = TestClient(web.app).get("/health")

    assert response.status_code == expected_status
    assert response.json() == {
        "status": expected_label,
        "environment": "test",
        "contracts": web.contracts_version,
        "ledger_chain_valid": chain_ok,
        "queue_backlog": 7,
    }
