"""Machine catalogue and safe selected-storm orchestration."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.storms import catalogue, pipeline, tracks

pytestmark = pytest.mark.skipif(
    not tracks.HURDAT2.exists() or not tracks.EBTRK.exists(),
    reason="storm archives not fetched — run data/storms/fetch_tracks.py",
)


def test_catalogue_document_is_deterministic_and_machine_readable() -> None:
    document = catalogue.catalogue_document()
    encoded = catalogue.serialise_catalogue(document)

    assert document["schema"] == "lighthouse.storm-catalogue.v1"
    assert document["storm_count"] == 195
    assert document["storm_count"] == len(document["storms"])
    assert {source["file"] for source in document["sources"]} == {
        "hurdat2-atlantic.txt",
        "ebtrk-atlantic.txt",
    }
    assert all(len(source["sha256"]) == 64 for source in document["sources"])
    assert json.loads(encoded) == document
    assert encoded == catalogue.serialise_catalogue(catalogue.catalogue_document())
    gilbert = next(storm for storm in document["storms"] if storm["id"] == "al081988")
    assert gilbert["provenance"] == "mixed"
    assert 0 < gilbert["fully_measured_extent_points"] < gilbert["wind_field_points"]


def test_track_library_is_deterministic_and_browser_loadable() -> None:
    document = catalogue.track_library_document()
    encoded = catalogue.serialise_catalogue(document)

    assert document["schema"] == "lighthouse.storm-track-library.v1"
    assert document["storm_count"] == 195
    assert document["storm_count"] == len(document["storms"])
    assert encoded == catalogue.serialise_catalogue(catalogue.track_library_document())

    gilbert = next(storm for storm in document["storms"] if storm["id"] == "al081988")
    assert gilbert["provenance"] == "mixed"
    assert len(gilbert["positions"]) == 49
    assert all(position["at"].endswith("Z") for position in gilbert["positions"])
    assert all(-90 <= position["lat"] <= 90 for position in gilbert["positions"])
    assert all(-180 <= position["lon"] <= 180 for position in gilbert["positions"])


def test_pipeline_plan_is_database_free_and_explicit_about_writes(
    tmp_path: Path,
) -> None:
    plan = pipeline.build_plan("AL081988", directory=tmp_path)

    assert plan["mode"] == "dry-run"
    assert plan["external_ref"] == "al081988"
    assert plan["storm"]["positions"] == 49
    assert plan["replacement_policy"] == "fail_if_exists_unless_replace_is_explicit"
    assert plan["stages"][-1] == "publish_only_if_release_gate_passes"


def test_apply_pipeline_runs_ordered_restartable_stages(
    tmp_path: Path, monkeypatch
) -> None:
    plan = {
        "schema": pipeline.PIPELINE_SCHEMA,
        "storm": {"id": "AL992020"},
        "external_ref": "al992020",
        "output_directory": str(tmp_path),
    }
    track = SimpleNamespace(storm_id="AL992020")
    monkeypatch.setattr(pipeline, "load", lambda _storm_id: track)
    calls: list[tuple] = []
    sessions = iter(["write-session", "export-session"])

    @contextmanager
    def session_factory():
        yield next(sessions)

    event_id = UUID("11111111-1111-1111-1111-111111111111")

    def ingest(session, selected, **kwargs):
        calls.append(("ingest", session, selected, kwargs))
        return SimpleNamespace(id=event_id)

    def score(session, event):
        calls.append(("score", session, event.id))
        return 8, 16_000

    def exposure_builder(*, external_ref):
        calls.append(("exposure", external_ref))

    def library_exporter(session, *, directory):
        calls.append(("export", session, directory))
        return [{"id": "al992020", "file": "al992020.json"}]

    result = pipeline.apply_pipeline(
        plan,
        replace_existing=True,
        session_factory=session_factory,
        ingest=ingest,
        score=score,
        exposure_builder=exposure_builder,
        library_exporter=library_exporter,
    )

    assert [call[0] for call in calls] == ["ingest", "score", "exposure", "export"]
    assert calls[0][3]["replace_existing"] is True
    assert result["hazard_event_id"] == str(event_id)
    assert result["advisories"] == 8
    assert result["risk_assessments"] == 16_000


def test_apply_pipeline_fails_if_export_gate_does_not_publish_selected_storm(
    tmp_path: Path, monkeypatch
) -> None:
    plan = {
        "schema": pipeline.PIPELINE_SCHEMA,
        "storm": {"id": "AL992020"},
        "external_ref": "al992020",
        "output_directory": str(tmp_path),
    }
    monkeypatch.setattr(
        pipeline,
        "load",
        lambda _storm_id: SimpleNamespace(storm_id="AL992020"),
    )

    @contextmanager
    def session_factory():
        yield object()

    with pytest.raises(RuntimeError, match="failed the replay release gate"):
        pipeline.apply_pipeline(
            plan,
            replace_existing=False,
            session_factory=session_factory,
            ingest=lambda *_args, **_kwargs: SimpleNamespace(
                id=UUID("11111111-1111-1111-1111-111111111111")
            ),
            score=lambda *_args: (8, 16_000),
            exposure_builder=lambda **_kwargs: None,
            library_exporter=lambda *_args, **_kwargs: [],
        )


@pytest.mark.parametrize("value", ["../../event", "A", "event.json", "white space"])
def test_external_ref_cannot_escape_the_artifact_directory(value: str) -> None:
    with pytest.raises(ValueError, match="external_ref"):
        pipeline.normalise_external_ref(value)
