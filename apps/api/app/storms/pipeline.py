"""Reproducible catalogue-storm to console-replay orchestration.

The default invocation is a read-only plan. Database writes, the expensive
building exposure build, and artifact publication happen only with ``--apply``:

    cd apps/api
    uv run python -m app.storms.pipeline AL081988
    uv run python -m app.storms.pipeline AL081988 --apply

Rebuilding an existing event additionally requires ``--replace``. Replacement
keeps the hazard event's UUID stable; only derived advisories, risk rows and
exposure are regenerated inside explicit transaction boundaries.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.storms.catalogue import CatalogueError, archive_provenance, load, summarise

PIPELINE_SCHEMA = "lighthouse.storm-pipeline-plan.v1"
_EXTERNAL_REF = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")


def normalise_external_ref(value: str) -> str:
    ref = value.strip().lower()
    if _EXTERNAL_REF.fullmatch(ref) is None:
        raise ValueError(
            "external_ref must be 3-64 lowercase letters, digits, underscores or hyphens"
        )
    return ref


def build_plan(
    storm_id: str,
    *,
    external_ref: str | None = None,
    directory: Path,
) -> dict[str, Any]:
    """Resolve and validate every archive-only input without opening a database."""
    track = load(storm_id)
    sources = archive_provenance()
    summary = summarise(track)
    ref = normalise_external_ref(external_ref or track.storm_id)
    return {
        "schema": PIPELINE_SCHEMA,
        "mode": "dry-run",
        "storm": {
            "id": track.storm_id,
            "name": summary.name.title(),
            "year": summary.year,
            "positions": summary.points,
            "peak_wind_kt": summary.peak_wind_kt,
            "closest_km": summary.closest_km,
            "wind_extent_provenance": summary.provenance,
        },
        "sources": sources,
        "external_ref": ref,
        "output_directory": str(directory),
        "replacement_policy": "fail_if_exists_unless_replace_is_explicit",
        "stages": [
            "ingest_identity_stable_event_and_advisories",
            "score_household_risk",
            "build_mapped_structure_exposure",
            "publish_only_if_release_gate_passes",
        ],
    }


def _score_event(session: Any, event: Any) -> tuple[int, int]:
    from sqlalchemy import select

    from app.agents.risk_mapper import assess
    from app.models import Advisory

    advisories = list(
        session.scalars(
            select(Advisory)
            .where(Advisory.hazard_event_id == event.id, Advisory.observed.is_(False))
            .order_by(Advisory.issued_at, Advisory.advisory_number)
        )
    )
    assessed = 0
    for advisory in advisories:
        assessed += assess(session, advisory).assessed
    return len(advisories), assessed


def apply_pipeline(
    plan: dict[str, Any],
    *,
    replace_existing: bool,
    session_factory: Callable[[], Any] | None = None,
    ingest: Callable[..., Any] | None = None,
    score: Callable[[Any, Any], tuple[int, int]] | None = None,
    exposure_builder: Callable[..., Any] | None = None,
    library_exporter: Callable[..., list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Apply a validated plan through restartable, fail-closed stages.

    The ingest and risk work commit together. Exposure is a separate atomic
    build because it uses DuckDB plus its own Postgres transaction. A failure
    between stages leaves the event absent from the library: ``export_all``
    requires the completed building/exposure markers before publication.
    """
    if plan.get("schema") != PIPELINE_SCHEMA:
        raise ValueError("unsupported or missing pipeline plan schema")
    storm = plan.get("storm")
    if not isinstance(storm, dict) or not isinstance(storm.get("id"), str):
        raise ValueError("pipeline plan has no storm id")
    ref = normalise_external_ref(str(plan.get("external_ref", "")))
    directory = Path(str(plan.get("output_directory", "")))
    track = load(storm["id"])

    if session_factory is None:
        from app.db import session_scope

        session_factory = session_scope
    if ingest is None:
        from app.storms.synthesize import ingest_track

        ingest = ingest_track
    if score is None:
        score = _score_event
    if exposure_builder is None:
        from app.registry.buildings import build

        exposure_builder = build
    if library_exporter is None:
        from app.console.library import export_all

        library_exporter = export_all

    with session_factory() as session:
        event = ingest(
            session,
            track,
            external_ref=ref,
            replace_existing=replace_existing,
        )
        event_id = str(event.id)
        advisory_count, assessments = score(session, event)

    exposure_builder(external_ref=ref)

    with session_factory() as session:
        entries = library_exporter(session, directory=directory)

    published = next((entry for entry in entries if entry.get("id") == ref), None)
    if published is None:
        raise RuntimeError(
            f"{ref} completed processing but failed the replay release gate; "
            "inspect the exposure completion markers before retrying"
        )
    return {
        "schema": "lighthouse.storm-pipeline-result.v1",
        "mode": "applied",
        "external_ref": ref,
        "hazard_event_id": event_id,
        "advisories": advisory_count,
        "risk_assessments": assessments,
        "artifact": published,
        "index": str(directory / "index.json"),
    }


def _json(document: dict[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    from app.console.library import REPLAY_DIR

    parser = argparse.ArgumentParser(
        prog="python -m app.storms.pipeline",
        description="Plan or apply one pinned Atlantic storm through the replay pipeline.",
    )
    parser.add_argument("storm", help="archive id, for example AL081988")
    parser.add_argument(
        "--event", help="hazard event external_ref (defaults to storm id)"
    )
    parser.add_argument("--directory", type=Path, default=REPLAY_DIR)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform database, exposure and artifact writes; omitted means dry-run",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="allow regeneration of an existing event while preserving its UUID",
    )
    args = parser.parse_args(argv)
    if args.replace and not args.apply:
        parser.error("--replace has no effect without --apply")

    try:
        plan = build_plan(
            args.storm,
            external_ref=args.event,
            directory=args.directory,
        )
    except (CatalogueError, KeyError, ValueError) as exc:
        parser.error(str(exc))

    if not args.apply:
        print(_json(plan))
        return 0

    result = apply_pipeline(plan, replace_existing=args.replace)
    print(_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
