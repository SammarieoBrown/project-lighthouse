"""The live hazard board never touches the network in tests, never reports a
replay's posture as live, and keeps an empty basin distinct from a broken feed."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app import hazard_live
from app.web import app
from factories import make_event


@pytest.fixture(autouse=True)
def clear_basin_cache():
    hazard_live._cache = None
    yield
    hazard_live._cache = None


def _client(monkeypatch: pytest.MonkeyPatch, session) -> TestClient:
    @contextmanager
    def scoped():
        yield session

    monkeypatch.setattr(hazard_live, "session_scope", scoped)
    return TestClient(app)


def test_live_board_reports_storms_and_no_replay_posture(session, monkeypatch):
    # A replayed storm in the database must not lend the live board a posture.
    make_event(session)
    monkeypatch.setattr(
        hazard_live,
        "_fetch",
        lambda: [
            {
                "id": "al052026",
                "name": "Ernesto",
                "classification": "HU",
                "intensity": "85",
                "pressure": "968",
                "latitudeNumeric": 16.2,
                "longitudeNumeric": -61.5,
                "movementDir": 285,
                "movementSpeed": "12",
                "lastUpdate": "2026-08-30T15:00:00Z",
            },
            # No numeric position: cannot be placed, must be skipped, and the
            # skip must not take the listed storm down with it.
            {"id": "al062026", "name": "Unplaced", "intensity": "30"},
            # East Pacific: a real storm in the wrong basin for this board.
            {
                "id": "ep112026",
                "name": "Karina",
                "classification": "HU",
                "intensity": "70",
                "latitudeNumeric": 17.3,
                "longitudeNumeric": -122.1,
            },
        ],
    )

    body = _client(monkeypatch, session).get("/v1/hazard/live").json()

    assert body["basin"]["status"] == "ok"
    assert [storm["name"] for storm in body["basin"]["storms"]] == ["Ernesto"]
    storm = body["basin"]["storms"][0]
    assert storm["intensity_kt"] == 85.0
    assert storm["lat"] == 16.2 and storm["lon"] == -61.5
    assert body["posture"]["level"] == "QUIET"
    assert body["posture"]["source"] == "no live hazard event"


def test_unreachable_feed_is_a_status_not_an_error(session, monkeypatch):
    def boom():
        raise RuntimeError("refused")

    monkeypatch.setattr(hazard_live, "_fetch", boom)

    response = _client(monkeypatch, session).get("/v1/hazard/live")

    assert response.status_code == 200
    body = response.json()
    assert body["basin"]["status"] == "unreachable"
    assert body["basin"]["storms"] is None


def test_empty_basin_is_zero_storms_not_a_failure(session, monkeypatch):
    monkeypatch.setattr(hazard_live, "_fetch", lambda: [])

    body = _client(monkeypatch, session).get("/v1/hazard/live").json()

    assert body["basin"]["status"] == "ok"
    assert body["basin"]["storms"] == []
