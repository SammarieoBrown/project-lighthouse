"""The pre-landfall list (ALT-04).

The most sensitive object the platform produces: a ranked register of
vulnerable people and where they live. Most of these tests are about who
cannot see it.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from lighthouse_contracts import AppRole, DamageBand, Posture

from app import anticipatory_routes
from app.anticipatory import AnticipatoryListUnavailable, build_list, to_csv
from app.approval_credentials import issue_human_credential, set_human_password
from app.web import app

from factories import make_user
from test_alert_agent import _advisory, _at_risk, _event_at


def _ready_event(session, *, posture=Posture.READY, households=3):
    event = _event_at(session, posture)
    advisory = _advisory(session, event)
    files = []
    for index in range(households):
        storm_file = _at_risk(session, advisory, band=DamageBand.MAJOR)
        storm_file.vuln_score = 20 + index * 30  # 20, 50, 80
        files.append(storm_file)
    session.flush()
    return event, files


def _client(monkeypatch, session) -> TestClient:
    @contextmanager
    def scoped():
        yield session
        session.flush()

    monkeypatch.setattr(anticipatory_routes, "session_scope", scoped)
    return TestClient(app)


def _token(session, role):
    user = make_user(session, role)
    set_human_password(session, email=user.email, password="correct horse lighthouse")
    return issue_human_credential(
        session, email=user.email, password="correct horse lighthouse"
    ).token


# -- who may see it ----------------------------------------------------------


def test_only_a_director_may_open_the_list(session, monkeypatch):
    """PRD 11.4. Publishing this would invert the privacy posture every other
    surface argues for."""
    event, _ = _ready_event(session)
    client = _client(monkeypatch, session)

    for role in (AppRole.REVIEW_CLERK, AppRole.FINANCE_OFFICER, AppRole.AUDITOR):
        denied = client.get(
            f"/v1/hazard-events/{event.id}/anticipatory-list",
            headers={"Authorization": f"Bearer {_token(session, role)}"},
        )
        assert denied.status_code == 403, role

    allowed = client.get(
        f"/v1/hazard-events/{event.id}/anticipatory-list",
        headers={"Authorization": f"Bearer {_token(session, AppRole.DIRECTOR)}"},
    )
    assert allowed.status_code == 200
    assert allowed.json()["visibility"] == "DIRECTOR_ONLY_NEVER_PUBLIC"


def test_the_list_is_never_cached(session, monkeypatch):
    event, _ = _ready_event(session)
    client = _client(monkeypatch, session)

    response = client.get(
        f"/v1/hazard-events/{event.id}/anticipatory-list",
        headers={"Authorization": f"Bearer {_token(session, AppRole.DIRECTOR)}"},
    )

    assert response.headers["cache-control"] == "no-store"


# -- when it exists ----------------------------------------------------------


def test_nothing_is_anticipated_below_ready(session):
    """At WATCH there is genuinely nothing to anticipate yet, which is a
    different statement from "nobody is at risk"."""
    event, _ = _ready_event(session, posture=Posture.WATCH)

    with pytest.raises(AnticipatoryListUnavailable, match="generated from READY"):
        build_list(session, event.id)


def test_act_still_produces_a_list(session):
    event, _ = _ready_event(session, posture=Posture.ACT)

    assert len(build_list(session, event.id)) == 3


def test_an_unknown_event_is_refused(session):
    with pytest.raises(AnticipatoryListUnavailable, match="does not exist"):
        build_list(session, uuid.uuid4())


# -- the ordering ------------------------------------------------------------


def test_ranking_is_vulnerability_times_probability(session):
    """Neither term alone is useful: a fragile household the storm will miss
    does not need pre-positioning, and a sturdy one in the eye can wait."""
    event, files = _ready_event(session)

    rows = build_list(session, event.id)

    scores = [row["rank_score"] for row in rows]
    assert scores == sorted(scores, reverse=True)
    # Every household shares p34 here, so vulnerability decides the order.
    assert [row["vulnerability"] for row in rows] == [80, 50, 20]


def test_the_list_can_be_trimmed(session):
    event, _ = _ready_event(session, households=3)

    assert len(build_list(session, event.id, limit=2)) == 2


# -- what it carries ---------------------------------------------------------


def test_it_carries_counts_rather_than_people(session):
    """Enough to size a delivery, not enough to identify anyone. The name is
    absent even on a Director-only surface."""
    event, files = _ready_event(session)

    row = build_list(session, event.id)[0]

    assert row["household_size"] == 5
    assert row["elderly"] == 1
    assert "head_name" not in row
    assert "phone" not in row
    blob = str(row)
    assert all(storm_file.phone not in blob for storm_file in files)
    assert all((storm_file.head_name or "") not in blob for storm_file in files)


def test_it_exports_as_csv_because_pre_positioning_happens_in_a_truck(
    session, monkeypatch
):
    event, _ = _ready_event(session)
    client = _client(monkeypatch, session)

    response = client.get(
        f"/v1/hazard-events/{event.id}/anticipatory-list.csv",
        headers={"Authorization": f"Bearer {_token(session, AppRole.DIRECTOR)}"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "anticipatory-list.csv" in response.headers["content-disposition"]
    lines = response.text.strip().splitlines()
    assert lines[0].startswith("storm_file_id,parish,community,vulnerability")
    assert len(lines) == 4  # header plus three households


def test_the_csv_has_no_name_or_number_column(session):
    event, _ = _ready_event(session)

    header = to_csv(build_list(session, event.id)).splitlines()[0]

    assert "phone" not in header
    assert "name" not in header
