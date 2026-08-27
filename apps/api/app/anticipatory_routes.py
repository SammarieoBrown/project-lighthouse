"""The Director-only pre-landfall list (ALT-04)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Header, HTTPException, Query, Response, status

from lighthouse_contracts import AppRole

from .anticipatory import DEFAULT_TOP_N, AnticipatoryListUnavailable, build_list, to_csv
from .db import session_scope
from .human_auth import authenticate_human

router = APIRouter(prefix="/v1", tags=["anticipatory"])


def _rows(hazard_event_id: uuid.UUID, authorization: str | None, limit: int):
    with session_scope() as session:
        # DIRECTOR only, and only DIRECTOR. This is a ranked register of
        # vulnerable people and where they live.
        authenticate_human(session, authorization, allowed_roles={AppRole.DIRECTOR})
        try:
            return build_list(session, hazard_event_id, limit=limit)
        except AnticipatoryListUnavailable as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            ) from exc


@router.get("/hazard-events/{hazard_event_id}/anticipatory-list")
def anticipatory_list_route(
    hazard_event_id: uuid.UUID,
    response: Response,
    authorization: str | None = Header(default=None),
    limit: int = Query(default=DEFAULT_TOP_N, ge=1, le=500),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    rows = _rows(hazard_event_id, authorization, limit)
    return {
        "households": rows,
        "count": len(rows),
        "visibility": "DIRECTOR_ONLY_NEVER_PUBLIC",
    }


@router.get("/hazard-events/{hazard_event_id}/anticipatory-list.csv")
def anticipatory_csv_route(
    hazard_event_id: uuid.UUID,
    authorization: str | None = Header(default=None),
    limit: int = Query(default=DEFAULT_TOP_N, ge=1, le=500),
) -> Response:
    rows = _rows(hazard_event_id, authorization, limit)
    return Response(
        content=to_csv(rows),
        media_type="text/csv",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'attachment; filename="anticipatory-list.csv"',
        },
    )


__all__ = ["anticipatory_csv_route", "anticipatory_list_route", "router"]
