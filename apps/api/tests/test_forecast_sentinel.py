"""Forecast Sentinel: the posture transition, not the posture rules.

The four rules that decide what READY means are exercised against the real
Melissa advisories in ``test_replay.py``. What is tested here is what happens
*because* the posture moved — the ledger entry HAZ-03 requires, the alert job
that hangs off it, and the fact that running twice does not double-record.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, text

from lighthouse_contracts import AgentName, Event, Posture

from app import forecast_sentinel_service
from app.agents.forecast_sentinel import ForecastSentinelNotRunnable
from app.agents.forecast_sentinel import handle as sentinel_handle
from app.forecast_sentinel_service import evaluate_posture
from app.models import Advisory, AgentJob, LedgerEntry
from app.worker import load_handlers

from factories import make_event, make_storm_file

#: A box over St Elizabeth, comfortably around the factory household.
INSIDE = "POLYGON((-78.2 17.6,-77.2 17.6,-77.2 18.5,-78.2 18.5,-78.2 17.6))"
ELSEWHERE = "POLYGON((-60.0 10.0,-59.0 10.0,-59.0 11.0,-60.0 11.0,-60.0 10.0))"


def _advisory(session, event, *, number: str = "1", wind_field: str | None = None):
    advisory = Advisory(
        hazard_event_id=event.id,
        advisory_number=number,
        issued_at=datetime(2025, 10, 21, 15, 0, tzinfo=UTC),
    )
    session.add(advisory)
    session.flush()
    if wind_field is not None:
        session.execute(
            text(
                "UPDATE advisory SET wind_field_34 = ST_GeogFromText(:wkt)"
                " WHERE id = :id"
            ),
            {"wkt": f"SRID=4326;{wind_field}", "id": advisory.id},
        )
        session.refresh(advisory)
    return advisory


def _household_at(session, lon: float, lat: float):
    sf = make_storm_file(session)
    session.execute(
        text("UPDATE storm_file SET location = ST_GeogFromText(:wkt) WHERE id = :id"),
        {"wkt": f"SRID=4326;POINT({lon} {lat})", "id": sf.id},
    )
    session.flush()
    return sf


def _fixed_posture(monkeypatch, posture: Posture) -> None:
    monkeypatch.setattr(
        forecast_sentinel_service, "posture_for", lambda session, advisory: posture
    )


def _posture_entries(session, event) -> list[LedgerEntry]:
    return list(
        session.scalars(
            select(LedgerEntry)
            .where(
                LedgerEntry.action == str(Event.HAZARD_POSTURE_CHANGED),
                LedgerEntry.subject_id == event.id,
            )
            .order_by(LedgerEntry.seq)
        )
    )


def _alert_jobs(session, event) -> int:
    return session.scalar(
        select(func.count())
        .select_from(AgentJob)
        .where(
            AgentJob.job_type == str(AgentName.ALERT_AGENT),
            AgentJob.payload["hazard_event_id"].astext == str(event.id),
        )
    )


def test_a_posture_change_is_a_ledger_event(session, monkeypatch):
    """HAZ-03. For most of this project's life the posture moved a column and
    nothing else, so the moment the country went to READY was unauditable."""
    event = make_event(session)
    advisory = _advisory(session, event, wind_field=INSIDE)
    _household_at(session, -77.75, 18.05)
    _fixed_posture(monkeypatch, Posture.READY)

    decision = evaluate_posture(session, event, advisory)
    session.flush()

    assert decision.changed is True
    assert decision.previous is Posture.QUIET
    assert decision.posture is Posture.READY
    assert event.current_posture is Posture.READY

    entries = _posture_entries(session, event)
    assert len(entries) == 1
    assert entries[0].payload["previous_posture"] == "QUIET"
    assert entries[0].payload["posture"] == "READY"
    assert entries[0].payload["advisory_number"] == "1"
    assert entries[0].agent_name == str(AgentName.FORECAST_SENTINEL)


def test_a_posture_change_wakes_the_alert_agent(session, monkeypatch):
    event = make_event(session)
    advisory = _advisory(session, event, wind_field=INSIDE)
    _fixed_posture(monkeypatch, Posture.ACT)

    evaluate_posture(session, event, advisory)
    session.flush()

    assert _alert_jobs(session, event) == 1


def test_holding_the_same_posture_records_nothing(session, monkeypatch):
    """The driver calls this synchronously and the worker calls it from a
    queued job. Both landing on one advisory must not double-record."""
    event = make_event(session)
    advisory = _advisory(session, event, wind_field=INSIDE)
    _fixed_posture(monkeypatch, Posture.WATCH)

    first = evaluate_posture(session, event, advisory)
    second = evaluate_posture(session, event, advisory)
    session.flush()

    assert first.changed is True
    assert second.changed is False
    assert second.posture is Posture.WATCH
    assert len(_posture_entries(session, event)) == 1
    assert _alert_jobs(session, event) == 1


def test_only_act_asks_for_a_human(session, monkeypatch):
    event = make_event(session)
    _fixed_posture(monkeypatch, Posture.READY)
    ready = evaluate_posture(session, event, _advisory(session, event, number="1"))
    assert ready.output.escalate_to_human is False

    _fixed_posture(monkeypatch, Posture.ACT)
    act = evaluate_posture(session, event, _advisory(session, event, number="2"))
    assert act.output.escalate_to_human is True


def test_affected_parishes_are_the_ones_with_households_in_the_wind_field(
    session, monkeypatch
):
    """Naming every parish the storm threatens is easy and useless. This names
    the parishes where we actually know someone."""
    event = make_event(session)
    _household_at(session, -77.75, 18.05)
    _fixed_posture(monkeypatch, Posture.ACT)

    covered = evaluate_posture(
        session, event, _advisory(session, event, number="1", wind_field=INSIDE)
    )
    assert covered.output.affected_parishes == ["St Elizabeth"]

    event.current_posture = Posture.QUIET
    missed = evaluate_posture(
        session, event, _advisory(session, event, number="2", wind_field=ELSEWHERE)
    )
    assert missed.output.affected_parishes == []


def test_an_advisory_with_no_wind_field_affects_nobody(session, monkeypatch):
    event = make_event(session)
    _household_at(session, -77.75, 18.05)
    _fixed_posture(monkeypatch, Posture.WATCH)

    decision = evaluate_posture(session, event, _advisory(session, event))

    assert decision.output.affected_parishes == []


def test_the_handler_runs_from_a_queued_payload(session, monkeypatch):
    event = make_event(session)
    advisory = _advisory(session, event, wind_field=INSIDE)
    _fixed_posture(monkeypatch, Posture.ACT)

    sentinel_handle(session, {"advisory_id": str(advisory.id)})
    session.flush()

    assert event.current_posture is Posture.ACT
    assert len(_posture_entries(session, event)) == 1


def test_the_handler_refuses_an_advisory_it_cannot_read(session):
    with pytest.raises(ForecastSentinelNotRunnable, match="advisory does not exist"):
        sentinel_handle(session, {"advisory_id": str(uuid.uuid4())})


def test_worker_registers_the_forecast_sentinel():
    assert str(AgentName.FORECAST_SENTINEL) in load_handlers()
