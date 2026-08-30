"""What is on the shelves, for the operator deciding what to send.

Stock counts are operational, not public: a depot's holdings are exactly the
information that would tell someone where to go looting after a storm, so this
sits behind the same credential as the claim queue.
"""

from __future__ import annotations

from fastapi import APIRouter, Cookie, Header, Response

from lighthouse_contracts import AppRole

from app.auth_session import read_session
from app.db import session_scope
from app.human_auth import authenticate_human
from app.registry.warehouses import stock_on_hand

router = APIRouter(prefix="/v1", tags=["logistics"])
_STOCK_ROLES = {AppRole.DIRECTOR, AppRole.REVIEW_CLERK, AppRole.AUDITOR}


@router.get("/warehouses")
def warehouses_route(
    response: Response,
    authorization: str | None = Header(default=None),
    lh_session: str | None = Cookie(default=None),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    with session_scope() as session:
        if authorization:
            authenticate_human(session, authorization, allowed_roles=_STOCK_ROLES)
        else:
            user = read_session(session, lh_session)
            if user.role not in _STOCK_ROLES:
                from fastapi import HTTPException, status

                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="your role cannot read depot stock",
                )
        return {"warehouses": stock_on_hand(session)}


__all__ = ["router"]
