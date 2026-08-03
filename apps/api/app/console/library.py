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
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.console.export import DEFAULT_EVENT, build_replay, serialise

REPO_ROOT = Path(__file__).parents[4]
REPLAY_DIR = REPO_ROOT / "apps" / "console" / "public" / "replay"
INDEX = REPLAY_DIR / "index.json"

#: The storm the console opens on when nothing is chosen. Melissa, because she
#: is the one with real NHC advisories, real watch and warning geography and a
#: real forecast cone — the others are hindcasts and say so.
DEFAULT_STORM = DEFAULT_EVENT


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
                   bool_or(a.raw->>'size_source' = 'modelled') AS modelled_size
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
        "size_source": "modelled" if row.modelled_size else "measured",
    }


def replayable(session: Session) -> list[str]:
    """Events with advisories and risk assessments — anything else cannot draw.

    An event that has been ingested but never scored would export frames whose
    every count is zero, which looks like a storm that did no damage rather
    than one that has not been run.
    """
    rows = session.execute(
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
    ).scalars().all()
    return list(rows)


def export_all(session: Session, *, directory: Path = REPLAY_DIR) -> list[dict[str, Any]]:
    directory.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []

    for ref in replayable(session):
        payload = build_replay(session, external_ref=ref)
        target = directory / f"{ref}.json"
        target.write_bytes(serialise(payload))
        entry = _describe(session, ref)
        entry["bytes"] = target.stat().st_size
        entries.append(entry)
        print(f"  {entry['name']:<34} {entry['advisories']:>3} advisories  "
              f"{entry['bytes'] / 1024:>7.0f} KB  {entry['kind']}/{entry['size_source']}")

    index = {
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "default": DEFAULT_STORM if any(e["id"] == DEFAULT_STORM for e in entries)
        else (entries[0]["id"] if entries else None),
        "storms": entries,
    }
    INDEX.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
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

    print(f"\n{len(entries)} storm(s) — index at {INDEX}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
