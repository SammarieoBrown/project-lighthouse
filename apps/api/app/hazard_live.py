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

It also carries NHC's Tropical Weather Outlook — the product that watches the
lifecycle stages advisories do not: disturbances that have not formed yet and
remnants that may regenerate. The outlook publishes prose and formation
chances, not fixes, so that is exactly what is served; no position or
intensity is ever invented for an outlook area.

Absence is kept distinct from failure: an empty basin returns ``storms: []``
with ``status: "ok"``, an unreachable feed returns ``storms: null`` with
``status: "unreachable"``. Both are 200s — a quiet Atlantic and a broken feed
are answers, not errors. The outlook adds one more state, ``"unparsed"``: the
product was fetched but its format has drifted past the parser, which is a
different claim from either quiet or unreachable and is reported as itself.
"""

from __future__ import annotations

import logging
import re
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

# The raw Atlantic Tropical Weather Outlook, from the NWS product feed rather
# than an HTML or RSS wrapper: genuine line structure, no markup to strip, and
# the most format-stable form the product has.
TWO_ATLANTIC_URL = "https://tgftp.nws.noaa.gov/data/raw/ab/abnt20.knhc.two.at.txt"
FETCH_TIMEOUT_S = 5.0

# One fetch per five minutes, shared across requests. NHC updates the storms
# product on advisory cadence (hours) and the outlook four times a day;
# polling either per page load would be rude and would put a third party on
# this endpoint's latency path.
CACHE_TTL_S = 300.0

_cache_lock = threading.Lock()
_cache: tuple[float, list[dict[str, Any]]] | None = None
_two_cache: tuple[float, dict[str, Any]] | None = None


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


def fetch_two_text() -> str:
    """Read the raw Atlantic outlook text. Raises on transport failure."""
    response = httpx.get(TWO_ATLANTIC_URL, timeout=FETCH_TIMEOUT_S)
    response.raise_for_status()
    return response.text


# Second provider boundary, same rule: injectable, never live in tests.
_fetch_two = fetch_two_text


_ISSUED = re.compile(r"^\d{3,4} [AP]M [A-Z]{2,5} [A-Za-z]{3} [A-Za-z]{3} \d{1,2} \d{4}$", re.M)
_CHANCE_48H = re.compile(
    r"\*\s*Formation chance through 48 hours[^A-Za-z0-9]*([A-Za-z]+)[^0-9]*(\d+)\s*percent",
    re.I,
)
_CHANCE_7DAY = re.compile(
    r"\*\s*Formation chance through 7 days[^A-Za-z0-9]*([A-Za-z]+)[^0-9]*(\d+)\s*percent",
    re.I,
)


def _chance(pattern: re.Pattern[str], block: str) -> dict[str, Any] | None:
    match = pattern.search(block)
    if not match:
        return None
    return {"band": match.group(1).lower(), "percent": int(match.group(2))}


def parse_outlook(text: str) -> dict[str, Any]:
    """Read the outlook's areas out of the product text.

    Pure and total: format drift produces ``areas: None`` with
    ``status: "unparsed"`` rather than an exception, and a genuinely quiet
    outlook produces ``areas: []``. An area whose chance lines have drifted is
    kept with ``None`` chances — dropping it would silently narrow the basin.
    """
    issued_match = _ISSUED.search(text)
    issued = issued_match.group(0) if issued_match else None

    # Everything after the sign-off is the forecaster's name, not the outlook.
    body = text.split("$$", 1)[0]

    areas: list[dict[str, Any]] = []
    for index, block in enumerate(re.split(r"\n\s*\n", body)):
        block = block.strip()
        # An outlook area is any block carrying the 7-day formation line; the
        # header, the Active Systems paragraph and the quiet-season sentence
        # all lack it and fall through untouched.
        if not _CHANCE_7DAY.search(block):
            continue
        # A real area is one paragraph and two bullets. Two 7-day lines in one
        # "block", or a block the size of the whole product, mean the
        # blank-line structure is gone — and one garbage area titled by a
        # fallback would be worse than saying unparsed below.
        if len(_CHANCE_7DAY.findall(block)) > 1 or len(block) > 2500:
            log.warning("outlook block is not a single area (%d chars)", len(block))
            continue
        lines = block.splitlines()
        first = lines[0].strip() if lines else ""
        titled = first.endswith(":") and 3 <= len(first) <= 90
        title = first.rstrip(":") if titled else f"Outlook area {len(areas) + 1}"
        prose_lines = [
            line.strip()
            for line in (lines[1:] if titled else lines)
            if line.strip() and not line.lstrip().startswith("*")
        ]
        areas.append({
            "title": title,
            "text": " ".join(prose_lines),
            "chance_48h": _chance(_CHANCE_48H, block),
            "chance_7day": _chance(_CHANCE_7DAY, block),
        })

    if not areas and _CHANCE_7DAY.search(body):
        # The formation line exists but no block yielded an area: the format
        # has drifted past the block splitter. Say so rather than reporting a
        # quiet basin that may not be quiet.
        log.warning("outlook text carries formation lines the parser could not segment")
        return {"status": "unparsed", "issued": issued, "areas": None}

    return {"status": "ok", "issued": issued, "areas": areas}


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


def _outlook() -> dict[str, Any]:
    global _two_cache
    now = time.monotonic()
    with _cache_lock:
        cached = _two_cache
    if cached is not None and now - cached[0] < CACHE_TTL_S:
        return cached[1]

    try:
        text = _fetch_two()
    except Exception as error:  # noqa: BLE001 — any failure means one thing here
        log.warning("outlook feed unreachable type=%s", type(error).__name__)
        if cached is not None:
            stale = dict(cached[1])
            stale["status"] = "stale"
            return stale
        return {"status": "unreachable", "issued": None, "areas": None}

    parsed = parse_outlook(text)
    if parsed["status"] == "ok":
        with _cache_lock:
            _two_cache = (now, parsed)
    return parsed


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
        "outlook": _outlook(),
    }
