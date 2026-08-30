"""The live hazard board.

``GET /v1/hazard/live`` answers two questions and refuses to blur them:

- what the national posture is right now, read from the most recent hazard
  event that is not a replay — a replayed storm's posture is a recording, and
  reporting it as live would be the exact lie the EOC exists to avoid; and
- what is active in the Atlantic basin right now, read from NHC's public
  ``CurrentStorms.json`` product.

Positions and intensities only. Wind-field polygons, probabilities and the
impact model arrive through advisory ingestion (``app/nhc``), and this
endpoint never fabricates them from a position fix. The console labels the
live view accordingly.

Absence is kept distinct from failure: an empty basin returns ``storms: []``
with ``status: "ok"``, an unreachable feed returns ``storms: null`` with
``status: "unreachable"``. Both are 200s — a quiet Atlantic and a broken feed
are answers, not errors.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter
from sqlalchemy import select

from .db import session_scope
from .models import HazardEvent

log = logging.getLogger(__name__)

router = APIRouter()

CURRENT_STORMS_URL = "https://www.nhc.noaa.gov/CurrentStorms.json"
FETCH_TIMEOUT_S = 5.0

# One fetch per five minutes, shared across requests. NHC updates the product
# on advisory cadence (hours); polling it per page load would be rude and
# would put a third party on this endpoint's latency path.
CACHE_TTL_S = 300.0

_cache_lock = threading.Lock()
_cache: tuple[float, list[dict[str, Any]]] | None = None


def fetch_current_storms() -> list[dict[str, Any]]:
    """Read NHC's active-storm list. Raises on any transport or shape error."""
    response = httpx.get(CURRENT_STORMS_URL, timeout=FETCH_TIMEOUT_S)
    response.raise_for_status()
    body = response.json()
    storms = body.get("activeStorms")
    if not isinstance(storms, list):
        raise ValueError("CurrentStorms.json carried no activeStorms list")
    return storms


# The provider boundary, injectable so tests never touch the network.
_fetch = fetch_current_storms


def _number(value: Any) -> float | None:
    """NHC serialises some numerics as strings; read either, refuse to guess."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _storm_row(raw: dict[str, Any]) -> dict[str, Any] | None:
    # NHC's product carries every basin it forecasts; an East Pacific
    # hurricane on a board titled "Atlantic" would be a true fact in a false
    # place. Basin is the first two letters of the storm id ("al", "ep", "cp").
    storm_id = str(raw.get("id") or "")
    if not storm_id.lower().startswith("al"):
        return None
    lat = _number(raw.get("latitudeNumeric"))
    lon = _number(raw.get("longitudeNumeric"))
    if lat is None or lon is None:
        # A storm the map cannot place is a storm this board cannot honestly
        # list as a position. Skipped, and the skip is logged rather than
        # silently narrowing the basin.
        log.warning("live storm without numeric position skipped id=%s", raw.get("id"))
        return None
    return {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "classification": raw.get("classification"),
        "intensity_kt": _number(raw.get("intensity")),
        "pressure_mb": _number(raw.get("pressure")),
        "lat": lat,
        "lon": lon,
        "movement_dir_deg": _number(raw.get("movementDir")),
        "movement_speed_kt": _number(raw.get("movementSpeed")),
        "last_update": raw.get("lastUpdate"),
    }


def _basin() -> dict[str, Any]:
    global _cache
    now = time.monotonic()
    with _cache_lock:
        cached = _cache
    if cached is not None and now - cached[0] < CACHE_TTL_S:
        return {"status": "ok", "storms": cached[1]}

    try:
        raw = _fetch()
    except Exception as error:  # noqa: BLE001 — any failure means one thing here
        log.warning("live basin feed unreachable type=%s", type(error).__name__)
        if cached is not None:
            # Stale beats absent, said plainly: the console renders the age.
            return {"status": "stale", "storms": cached[1]}
        return {"status": "unreachable", "storms": None}

    storms = [row for row in (_storm_row(item) for item in raw) if row is not None]
    with _cache_lock:
        _cache = (now, storms)
    return {"status": "ok", "storms": storms}


@router.get("/v1/hazard/live")
def hazard_live() -> dict[str, Any]:
    with session_scope() as session:
        event = session.execute(
            select(HazardEvent)
            .where(HazardEvent.replay.is_(False))
            .order_by(HazardEvent.started_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        posture = {
            "level": event.current_posture.value if event else "QUIET",
            "event": (
                {
                    "name": event.name,
                    "since": event.started_at.isoformat(),
                }
                if event
                else None
            ),
            # QUIET-by-absence and QUIET-by-decision are different statements;
            # the source says which one this is.
            "source": "live hazard event" if event else "no live hazard event",
        }

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "posture": posture,
        "basin": _basin(),
    }
