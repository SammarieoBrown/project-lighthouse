"""The FNOL surface: JSON for an insurer's intake, PDF for a person (INS-01).

Director-gated in this release. INS-02's insurer portal — authenticated
carriers receiving only their own policyholders' packets — is P1, and until
per-carrier scoping exists, handing an ``INSURER_USER`` a route that can name
any claim id would be the wrong door to open first.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Header, HTTPException, Response, status

from lighthouse_contracts import AppRole

from .db import session_scope
from .fnol_service import ClaimNotFound, FnolNotAvailable, build_fnol, render_pdf
from .human_auth import authenticate_human

router = APIRouter(prefix="/v1", tags=["fnol"])


def _packet(claim_id: uuid.UUID, authorization: str | None):
    with session_scope() as session:
        authenticate_human(session, authorization, allowed_roles={AppRole.DIRECTOR})
        try:
            return build_fnol(session, claim_id)
        except ClaimNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="claim not found"
            ) from exc
        except FnolNotAvailable as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            ) from exc


@router.get("/claims/{claim_id}/fnol")
def fnol_json_route(
    claim_id: uuid.UUID,
    response: Response,
    authorization: str | None = Header(default=None),
) -> dict:
    # The packet carries a policyholder's name, phone and address. It is never
    # cached anywhere between here and the person who asked for it.
    response.headers["Cache-Control"] = "no-store"
    return _packet(claim_id, authorization).content


@router.get("/claims/{claim_id}/fnol.pdf")
def fnol_pdf_route(
    claim_id: uuid.UUID,
    authorization: str | None = Header(default=None),
) -> Response:
    packet = _packet(claim_id, authorization)
    return Response(
        content=render_pdf(packet),
        media_type="application/pdf",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": (
                f'attachment; filename="fnol-{packet.content["claim"]["claim_ref"]}.pdf"'
            ),
        },
    )


__all__ = ["fnol_json_route", "fnol_pdf_route", "router"]
