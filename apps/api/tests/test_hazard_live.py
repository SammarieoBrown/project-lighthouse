"""The live hazard board never touches the network in tests, never reports a
replay's posture as live, and keeps an empty basin distinct from a broken feed."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import hazard_live
from app.web import app
from factories import make_event


@pytest.fixture(autouse=True)
def clear_basin_cache():
    hazard_live._cache = None
    hazard_live._two_cache = None
    hazard_live._gtwo_cache = None
    yield
    hazard_live._cache = None
    hazard_live._two_cache = None
    hazard_live._gtwo_cache = None


# The Atlantic outlook of 2026-08-30 12z, verbatim: the morning the remnants
# of Tropical Storm Dolly were the reason this parser got written.
OUTLOOK_SAMPLE = """\
ABNT20 KNHC 301136
TWOAT

Tropical Weather Outlook
NWS National Hurricane Center Miami FL
800 AM EDT Sun Aug 30 2026

For the North Atlantic...Caribbean Sea and the Gulf of America:

East of the Leeward Islands (Remnants of Dolly):
A strong tropical wave, the remnants of Tropical Storm Dolly, is
producing an area of showers and thunderstorms from Puerto Rico
eastward across the Virgin Islands and the northern Leeward Islands.
Conditions may become more conducive for redevelopment by the middle
of the week as the system reaches the southeastern Gulf of America.
* Formation chance through 48 hours...low...near 0 percent.
* Formation chance through 7 days...low...20 percent.

Northern Gulf of America (AL97):
Showers and thunderstorms associated with an area of low pressure
have become more concentrated. Interests along the Louisiana and
Upper Texas coasts should monitor the progress of this system.
* Formation chance through 48 hours...low...30 percent.
* Formation chance through 7 days...low...30 percent.

$$
Forecaster Beven
"""

QUIET_OUTLOOK = """\
ABNT20 KNHC 151136
TWOAT

Tropical Weather Outlook
NWS National Hurricane Center Miami FL
800 AM EST Sun Nov 15 2026

For the North Atlantic...Caribbean Sea and the Gulf of America:

Tropical cyclone formation is not expected during the next 7 days.

$$
Forecaster Beven
"""


def test_outlook_parses_areas_titles_and_chances():
    parsed = hazard_live.parse_outlook(OUTLOOK_SAMPLE)

    assert parsed["status"] == "ok"
    assert parsed["issued"] == "800 AM EDT Sun Aug 30 2026"
    titles = [area["title"] for area in parsed["areas"]]
    assert titles == [
        "East of the Leeward Islands (Remnants of Dolly)",
        "Northern Gulf of America (AL97)",
    ]
    dolly, invest = parsed["areas"]
    # "near 0 percent" reads as 0, not as a parse failure.
    assert dolly["chance_48h"] == {"band": "low", "percent": 0}
    assert dolly["chance_7day"] == {"band": "low", "percent": 20}
    assert invest["chance_7day"] == {"band": "low", "percent": 30}
    assert "remnants of Tropical Storm Dolly" in dolly["text"]
    # The sign-off never leaks into an area.
    assert "Forecaster" not in invest["text"]


def test_quiet_outlook_is_zero_areas_not_a_failure():
    parsed = hazard_live.parse_outlook(QUIET_OUTLOOK)

    assert parsed["status"] == "ok"
    assert parsed["areas"] == []


def test_area_with_drifted_chance_lines_is_kept_with_null_chances():
    mangled = OUTLOOK_SAMPLE.replace(
        "* Formation chance through 48 hours...low...near 0 percent.",
        "* Formation chance through 48 hours...uncertain at this time.",
    )

    parsed = hazard_live.parse_outlook(mangled)

    dolly = parsed["areas"][0]
    assert dolly["chance_48h"] is None
    assert dolly["chance_7day"] == {"band": "low", "percent": 20}


GTWO_FIXTURE = Path(__file__).parent / "fixtures" / "gtwo_sample.zip"


def test_graphical_outlook_serves_atlantic_geometry_only():
    parsed = hazard_live.parse_gtwo(GTWO_FIXTURE.read_bytes())

    areas = parsed["areas"]["features"]
    points = parsed["points"]["features"]
    # The 2026-08-30 bundle holds two Atlantic and two Pacific areas; only the
    # Atlantic pair may reach an Atlantic board.
    assert len(areas) == 2
    assert {a["properties"]["prob_7day"] for a in areas} == {20, 30}
    assert all(a["geometry"]["type"] == "Polygon" for a in areas)
    assert len(points) == 2
    assert all(p["geometry"]["type"] == "Point" for p in points)
    # "30%" strings became integers; risk bands became lowercase words.
    al97 = next(a for a in areas if a["properties"]["prob_48h"] == 30)
    assert al97["properties"]["risk_7day"] == "low"


def test_graphical_outlook_failure_leaves_text_outlook_standing(session, monkeypatch):
    def boom() -> bytes:
        raise RuntimeError("refused")

    monkeypatch.setattr(hazard_live, "_fetch", lambda: [])
    monkeypatch.setattr(hazard_live, "_fetch_two", lambda: OUTLOOK_SAMPLE)
    monkeypatch.setattr(hazard_live, "_fetch_gtwo", boom)

    body = _client(monkeypatch, session).get("/v1/hazard/live").json()

    assert body["outlook"]["features"] is None
    assert len(body["outlook"]["areas"]) == 2


def test_endpoint_carries_geometry_beside_the_text(session, monkeypatch):
    monkeypatch.setattr(hazard_live, "_fetch", lambda: [])
    monkeypatch.setattr(hazard_live, "_fetch_two", lambda: OUTLOOK_SAMPLE)
    monkeypatch.setattr(hazard_live, "_fetch_gtwo", GTWO_FIXTURE.read_bytes)

    body = _client(monkeypatch, session).get("/v1/hazard/live").json()

    assert len(body["outlook"]["features"]["areas"]["features"]) == 2
    assert len(body["outlook"]["areas"]) == 2


def test_unsegmentable_outlook_reports_unparsed_not_quiet():
    # Formation lines present but the block structure gone: the parser must
    # not report a quiet basin it cannot actually read.
    flattened = " ".join(OUTLOOK_SAMPLE.split())

    parsed = hazard_live.parse_outlook(flattened)

    assert parsed["status"] == "unparsed"
    assert parsed["areas"] is None


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
