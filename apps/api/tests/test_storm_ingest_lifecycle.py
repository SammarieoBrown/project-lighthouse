"""Historical storm replacement preserves durable identities."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.models import Advisory, HazardEvent
from app.nhc.fstadv import ForecastAdvisory, Position
from app.storms import synthesize


class _Session:
    def __init__(self, scalar_results):
        self._scalar_results = iter(scalar_results)
        self.executed: list[str] = []
        self.added: list[object] = []
        self.deleted: list[object] = []

    def scalars(self, _statement):
        return next(self._scalar_results)

    def execute(self, statement, _parameters=None):
        self.executed.append(str(statement))

    def add(self, value):
        self.added.append(value)

    def delete(self, value):
        self.deleted.append(value)

    def flush(self):
        return None


def _engine_stubs(monkeypatch: pytest.MonkeyPatch):
    at = datetime(2020, 9, 1, tzinfo=UTC)
    position = Position(
        valid_at=at,
        lat=18.0,
        lon=-77.0,
        kind="observed",
        max_wind_kt=90,
    )
    source = {
        34: {quadrant: "measured" for quadrant in synthesize.QUADRANTS},
    }
    forecast = ForecastAdvisory(
        storm_id="AL992020",
        storm_name="Tester",
        storm_type="HURRICANE",
        advisory_number="1",
        issued_at=at,
        current=position,
        forecasts=(),
        movement_deg=270,
        movement_kt=10,
        pressure_mb=980,
    )
    monkeypatch.setattr(
        synthesize, "_fill_radii", lambda _track: ([position], [source])
    )
    monkeypatch.setattr(
        synthesize,
        "advisories_from_track",
        lambda _track, *, _filled_positions: [forecast],
    )
    monkeypatch.setattr(
        synthesize,
        "_wind_fields",
        lambda _session, _advisory: {
            "wind_field_34": "field-34",
            "wind_field_50": None,
            "wind_field_64": None,
        },
    )
    monkeypatch.setattr(
        synthesize,
        "_raw_payload",
        lambda *_args: {"positions": [{}]},
    )
    return SimpleNamespace(storm_id="AL992020", name="TESTER", year=2020), at


def test_replace_keeps_event_and_matching_advisory_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    track, at = _engine_stubs(monkeypatch)
    event_id = UUID("11111111-1111-1111-1111-111111111111")
    advisory_id = UUID("22222222-2222-2222-2222-222222222222")
    event = HazardEvent(
        id=event_id,
        name="Old name",
        external_ref="al992020",
        replay=True,
    )
    advisory = Advisory(
        id=advisory_id,
        hazard_event_id=event_id,
        advisory_number="1",
        issued_at=at,
        observed=False,
        raw={"synthesized": True},
    )
    stale = Advisory(
        id=UUID("33333333-3333-3333-3333-333333333333"),
        hazard_event_id=event_id,
        advisory_number="2",
        issued_at=at,
        observed=False,
        raw={"synthesized": True},
    )
    session = _Session([[event], [advisory, stale]])

    returned = synthesize.ingest_track(
        session,
        track,
        external_ref="AL992020",
        name="Updated name",
        replace_existing=True,
    )

    assert returned is event
    assert returned.id == event_id
    assert event.name == "Updated name"
    assert advisory.id == advisory_id
    assert advisory.raw["hindcast"] is True
    assert advisory.raw["size_source"] == "measured"
    assert stale in session.deleted
    assert not any(isinstance(value, HazardEvent) for value in session.deleted)
    assert any("DELETE FROM place_exposure_build" in sql for sql in session.executed)
    assert any("DELETE FROM risk_assessment" in sql for sql in session.executed)
    assert any("DELETE FROM place_exposure" in sql for sql in session.executed)


def test_replace_refuses_to_overwrite_authoritative_advisories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    track, at = _engine_stubs(monkeypatch)
    event_id = UUID("11111111-1111-1111-1111-111111111111")
    event = HazardEvent(
        id=event_id,
        name="Published storm",
        external_ref="al992020",
        replay=True,
    )
    advisory = Advisory(
        id=UUID("22222222-2222-2222-2222-222222222222"),
        hazard_event_id=event_id,
        advisory_number="1",
        issued_at=at,
        observed=False,
        raw={"synthesized": False},
    )
    session = _Session([[event], [advisory]])

    with pytest.raises(ValueError, match="authoritative advisories"):
        synthesize.ingest_track(session, track, replace_existing=True)

    assert session.executed == []
