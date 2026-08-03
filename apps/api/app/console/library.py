"""Export every replayable storm, and an index the console can choose from.

`export.py` writes one storm to one file, which was the right shape when there
was one storm. The console fetches that file by a constant path and has no way
to ask what else exists.

This writes the same payload once per hazard event, plus a small index beside
them. The per-storm files are unchanged in shape — the console's reader,
validator and every derivation keep working, and the only new idea is that the
URL is now chosen rather than hardcoded.

    cd apps/api && uv run python -m app.console.library

**Committed, like the single file before them.** Vercel builds the console with
`npm run build` and has no Python and no `DATABASE_URL`, so a generated file
that is not in the repo is a file production does not have. Determinism is what
makes that safe: the same database produces the same bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.console.export import (
    DEFAULT_EVENT,
    DEFAULT_OUTPUT,
    ExportError,
    build_replay,
    serialise,
)

REPO_ROOT = Path(__file__).parents[4]
REPLAY_DIR = REPO_ROOT / "apps" / "console" / "public" / "replay"

#: The storm the console opens on when nothing is chosen. Melissa, because she
#: is the one with real NHC advisories, real watch and warning geography and a
#: real forecast cone — the others are hindcasts and say so.
DEFAULT_STORM = DEFAULT_EVENT
_SAFE_EXTERNAL_REF = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")


class ReleaseGateError(RuntimeError):
    """The replay is valid for investigation, but incomplete for publication."""


@dataclass(frozen=True)
class TrustedArtifact:
    path: Path
    sha256: str


# Verified after the completed Melissa building/exposure build. Updating the
# legacy replay is an evidence change and must update this pin deliberately.
DEFAULT_FALLBACK = TrustedArtifact(
    path=DEFAULT_OUTPUT,
    sha256="9c4baf388a717b9f80eebde0256e3a9f78a7b8c973646e8652b3f9d18aa5bec3",
)


def require_releaseable(payload: dict[str, Any], *, external_ref: str) -> None:
    """Require the mapped-building evidence promised by a library replay.

    ``build_replay`` intentionally permits missing mapped inventory so the
    single-storm console can still show household analysis while a building
    build is unavailable. The public storm *library* has a stronger contract:
    every listed artifact must carry the independently validated structure
    denominator and event exposure for every frame. ``build_replay`` only emits
    those fields after its completion-marker and digest checks pass, so presence
    here is evidence of provenance rather than a manufactured zero.
    """
    event = payload.get("event")
    event_id = event.get("id") if isinstance(event, dict) else None
    if str(event_id or "").lower() != external_ref.lower():
        raise ReleaseGateError(
            f"artifact event {event_id!r} does not match {external_ref!r}"
        )

    districts = payload.get("districts")
    if not isinstance(districts, list) or not districts:
        raise ReleaseGateError("artifact has no districts")
    if any(
        not isinstance(district, dict)
        or isinstance(district.get("structures"), bool)
        or not isinstance(district.get("structures"), int)
        or district["structures"] < 0
        for district in districts
    ):
        raise ReleaseGateError("mapped structure inventory is incomplete")

    frames = payload.get("frames")
    if not isinstance(frames, list) or not frames:
        raise ReleaseGateError("artifact has no frames")
    for frame in frames:
        exposed = frame.get("district_exposed") if isinstance(frame, dict) else None
        if not isinstance(exposed, list) or len(exposed) != len(districts):
            raise ReleaseGateError("mapped exposure is incomplete")
        if any(
            not isinstance(row, list)
            or len(row) != 3
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in row
            )
            for row in exposed
        ):
            raise ReleaseGateError("mapped exposure contains an invalid band row")


def _read_releaseable(
    artifact: TrustedArtifact, *, external_ref: str
) -> tuple[dict[str, Any], bytes]:
    try:
        encoded = artifact.path.read_bytes()
        payload = json.loads(encoded)
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseGateError(
            f"cannot read fallback artifact {artifact.path}: {exc}"
        ) from exc
    digest = hashlib.sha256(encoded).hexdigest()
    if digest != artifact.sha256:
        raise ReleaseGateError(
            f"fallback artifact {artifact.path} has SHA-256 {digest}, "
            f"expected {artifact.sha256}"
        )
    if not isinstance(payload, dict):
        raise ReleaseGateError(
            f"fallback artifact {artifact.path} is not a JSON object"
        )
    require_releaseable(payload, external_ref=external_ref)
    return payload, encoded


def _write_atomic(path: Path, encoded: bytes) -> None:
    """Publish one complete artifact, never a partly-written response."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _rollup_size_source(values: list[str] | None) -> str:
    """Reduce per-advisory evidence without discarding uncertainty."""
    raw_sources = set(values or [])
    if not raw_sources:
        # NHC advisories publish their radii; only synthesized rows carry the
        # explicit source field introduced by the simulator.
        return "measured"
    # Below-threshold fixes have no applicable 34/50/64 kt size to source. They
    # must not downgrade evidence from the hurricane portion of the same storm.
    size_sources = raw_sources - {"not_applicable"}
    allowed_sources = {"measured", "modelled", "mixed", "unavailable"}
    if not size_sources:
        return "unavailable"
    if not size_sources <= allowed_sources or "unavailable" in size_sources:
        return "unavailable"
    if "mixed" in size_sources or size_sources == {"measured", "modelled"}:
        return "mixed"
    return next(iter(size_sources))


def _describe(session: Session, external_ref: str) -> dict[str, Any]:
    """What the picker needs, and what the screen must disclose.

    `kind` is the load-bearing field. An advisory replay is what forecasters
    published at the time; a hindcast projects the track the storm actually
    took, which is perfect foresight and a different claim. `size_source` says
    whether the wind field's extent was measured or came from our model. The
    console shows both — a storm list that presented these as equivalent would
    be the exact failure the evidence contract exists to prevent.
    """
    row = session.execute(
        text(
            """
            SELECT e.name,
                   count(*) FILTER (WHERE NOT a.observed) AS advisories,
                   -- Forecast rows only. The best-track row's issued_at is set
                   -- to now() at ingest, because a post-season reanalysis has
                   -- no advisory time of its own — leave it in and Melissa's
                   -- date range ends today rather than in October 2025.
                   min(a.issued_at) FILTER (WHERE NOT a.observed) AS first_at,
                   max(a.issued_at) FILTER (WHERE NOT a.observed) AS last_at,
                   bool_or(coalesce((a.raw->>'hindcast')::boolean, false)) AS hindcast,
                   array_agg(
                     DISTINCT coalesce(a.raw->>'size_source', 'unavailable')
                   ) FILTER (
                     WHERE coalesce((a.raw->>'synthesized')::boolean, false)
                   ) AS size_sources
            FROM hazard_event e
            LEFT JOIN advisory a ON a.hazard_event_id = e.id
            WHERE e.external_ref = :ref
            GROUP BY e.name
            """
        ),
        {"ref": external_ref},
    ).one()

    return {
        "id": external_ref,
        "name": row.name,
        "advisories": int(row.advisories or 0),
        "from": row.first_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "to": row.last_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "file": f"{external_ref}.json",
        "kind": "hindcast" if row.hindcast else "advisory",
        "size_source": _rollup_size_source(row.size_sources),
    }


def replayable(session: Session) -> list[str]:
    """Events with advisories and risk assessments — anything else cannot draw.

    An event that has been ingested but never scored would export frames whose
    every count is zero, which looks like a storm that did no damage rather
    than one that has not been run.
    """
    rows = (
        session.execute(
            text(
                """
            SELECT e.external_ref
            FROM hazard_event e
            JOIN advisory a ON a.hazard_event_id = e.id AND NOT a.observed
            JOIN risk_assessment r ON r.advisory_id = a.id
            WHERE e.external_ref IS NOT NULL
            GROUP BY e.external_ref
            ORDER BY min(a.issued_at) DESC
            """
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


def export_all(
    session: Session,
    *,
    directory: Path = REPLAY_DIR,
    fallbacks: Mapping[str, TrustedArtifact] | None = None,
) -> list[dict[str, Any]]:
    """Export only releaseable storms and write the index beside the artifacts.

    Melissa's committed ``replay.json`` is the last known-good complete artifact
    from before the library split. It is an explicit fallback, not a source of
    fabricated zeros: it must pass the same structural release gate and match
    the requested event before it can be copied forward.
    """
    directory.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    trusted_fallbacks = (
        {DEFAULT_STORM: DEFAULT_FALLBACK} if fallbacks is None else dict(fallbacks)
    )

    for ref in replayable(session):
        if _SAFE_EXTERNAL_REF.fullmatch(ref) is None:
            print(f"  {ref:<34} skipped — unsafe external_ref for an artifact path")
            continue
        target = directory / f"{ref}.json"
        artifact_source = "generated"
        try:
            payload = build_replay(session, external_ref=ref)
            require_releaseable(payload, external_ref=ref)
            encoded = serialise(payload)
        except (ExportError, ReleaseGateError) as unavailable:
            fallback_artifact = trusted_fallbacks.get(ref)
            if fallback_artifact is None:
                print(f"  {ref:<34} skipped — {unavailable}")
                continue
            try:
                _, encoded = _read_releaseable(fallback_artifact, external_ref=ref)
            except ReleaseGateError as fallback_error:
                print(
                    f"  {ref:<34} skipped — {unavailable}; "
                    f"fallback unavailable: {fallback_error}"
                )
                continue
            artifact_source = "last_known_good"

        _write_atomic(target, encoded)
        entry = _describe(session, ref)
        entry["bytes"] = target.stat().st_size
        entry["artifact_source"] = artifact_source
        entries.append(entry)
        print(
            f"  {entry['name']:<34} {entry['advisories']:>3} advisories  "
            f"{entry['bytes'] / 1024:>7.0f} KB  {entry['kind']}/{entry['size_source']}"
        )

    index = {
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "default": DEFAULT_STORM
        if any(e["id"] == DEFAULT_STORM for e in entries)
        else (entries[0]["id"] if entries else None),
        "storms": entries,
    }
    index_path = directory / "index.json"
    _write_atomic(
        index_path,
        (json.dumps(index, indent=2, sort_keys=True) + "\n").encode(),
    )
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.console.library",
        description="Export every replayable storm plus the index the console picks from.",
    )
    parser.add_argument("--directory", default=None, help=f"default: {REPLAY_DIR}")
    args = parser.parse_args(argv)

    from app.db import session_scope

    directory = Path(args.directory) if args.directory else REPLAY_DIR
    with session_scope() as session:
        entries = export_all(session, directory=directory)

    print(f"\n{len(entries)} storm(s) — index at {directory / 'index.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
