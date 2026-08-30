"""FastAPI entrypoint — ``uvicorn app.web:app``.

Health, ledger integrity, and signed provider webhooks that durably record what
arrived and return immediately.

The webhook does not process anything. NFR-A-02 requires inbound messages to be
durable-queued at the edge before processing, so the handler's only job is to
get the message into Postgres and get out — if every agent downstream is broken,
we still have not lost a household's message.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lighthouse_contracts import __version__ as contracts_version

from . import ledger, queue
from .alert_approvals import router as alert_approvals_router
from .anticipatory_routes import router as anticipatory_router
from .approvals import router as approvals_router
from .auto_approval_routes import router as auto_approval_router
from .logistics_routes import router as logistics_router
from .config import get_settings
from .damage_assessment_reviews import router as damage_assessment_reviews_router
from .db import session_scope
from .disbursements import router as disbursements_router
from .donations import router as donations_router
from .fnol import router as fnol_router
from .intake import router as intake_router
from .auth_session import router as auth_router
from .hazard_live import router as hazard_live_router
from .public_ledger import router as public_ledger_router
from .verification_reviews import router as verification_reviews_router

log = logging.getLogger("lighthouse.web")

_MAX_APPROVAL_BODY_BYTES = 16 * 1024


class BoundedApprovalBodyMiddleware:
    """Bound human-decision bodies before FastAPI buffers or parses them."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    @staticmethod
    def _applies(scope: Scope) -> bool:
        path = str(scope.get("path", ""))
        if scope.get("type") != "http" or scope.get("method") != "POST":
            return False
        return any(
            (
                path.startswith(prefix)
                and path.endswith(suffix)
                and len(path) > len(prefix) + len(suffix)
            )
            for prefix, suffix in (
                ("/v1/claims/", "/allocations/approve"),
                ("/v1/claims/", "/verification/review"),
                ("/v1/allocations/", "/disbursements/sign"),
                ("/v1/disbursements/", "/execute"),
            )
        )

    @staticmethod
    async def _reject(
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        status_code: int,
        detail: str,
    ) -> None:
        await JSONResponse({"detail": detail}, status_code=status_code)(
            scope, receive, send
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._applies(scope):
            await self.app(scope, receive, send)
            return

        declared_lengths = [
            value
            for name, value in scope.get("headers", [])
            if name.lower() == b"content-length"
        ]
        if len(declared_lengths) > 1:
            await self._reject(
                scope,
                receive,
                send,
                status_code=400,
                detail="invalid content length",
            )
            return
        if declared_lengths:
            try:
                declared = int(declared_lengths[0].decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                declared = -1
            if declared < 0:
                await self._reject(
                    scope,
                    receive,
                    send,
                    status_code=400,
                    detail="invalid content length",
                )
                return
            if declared > _MAX_APPROVAL_BODY_BYTES:
                await self._reject(
                    scope,
                    receive,
                    send,
                    status_code=413,
                    detail="request body is too large",
                )
                return

        chunks: list[bytes] = []
        received = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            received += len(chunk)
            if received > _MAX_APPROVAL_BODY_BYTES:
                await self._reject(
                    scope,
                    receive,
                    send,
                    status_code=413,
                    detail="request body is too large",
                )
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break

        body = b"".join(chunks)
        delivered = False

        async def replay_receive() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay_receive, send)


#: Namespace for the advisory lock the pool seeder takes. Arbitrary, but fixed:
#: two instances booting together must contend on the same number.
_POOL_SEED_LOCK = 0x4C48_0001


def _ensure_donation_pools() -> None:
    """Open the demo donation pools if they are not open already (DON-01).

    This lives on boot rather than only in the deploy hook because the deploy
    hook turned out not to run: the service auto-deploys on commit and does not
    read ``render.yaml``, so a ``preDeployCommand`` added there is inert until
    somebody sinks the Blueprint by hand. The portal's give form renders only
    when a pool exists, so that left a public surface permanently in its empty
    state — the exact failure the hook was written to end.

    Guarded by a transaction-scoped advisory lock because ``donation_pool.name``
    carries no unique constraint; two instances booting at once would otherwise
    race to mint the same pot twice.

    Never fatal. A pool that cannot be opened is a portal that cannot take a
    donation, which is bad; an API that will not boot is every other surface
    down as well, which is worse.
    """
    try:
        from .replay.seed_claims import seed_pools

        with session_scope() as session:
            session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _POOL_SEED_LOCK})
            created = seed_pools(session)
        if created:
            log.info("donation pools opened count=%d", created)
    except Exception as exc:  # noqa: BLE001 - boot must survive any failure here
        log.error("donation pool seeding failed type=%s", type(exc).__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    _ensure_donation_pools()
    yield


app = FastAPI(title="Lighthouse API", version="0.1.0", lifespan=lifespan)
app.add_middleware(BoundedApprovalBodyMiddleware)
router = APIRouter()


@router.get("/health")
def health() -> JSONResponse:
    settings = get_settings()
    with session_scope() as session:
        chain_ok = ledger.cached_verify_chain(session)
        backlog = queue.pending_count(session)
    return JSONResponse(
        {
            "status": "ok" if chain_ok else "error",
            "environment": settings.environment,
            "contracts": contracts_version,
            "ledger_chain_valid": chain_ok,
            "queue_backlog": backlog,
        },
        status_code=200 if chain_ok else 503,
    )


@app.exception_handler(Exception)
async def unhandled(_: Request, exc: Exception) -> JSONResponse:
    # SQLAlchemy exceptions include bound parameters in their string form. An
    # intake failure can therefore contain a phone or message body even when no
    # application log statement mentions either. Preserve the error class for
    # operations without serialising the exception or provider payload.
    log.error("unhandled error type=%s", type(exc).__name__)
    return JSONResponse({"detail": "internal error"}, status_code=500)


app.include_router(router)
app.include_router(intake_router)
app.include_router(approvals_router)
app.include_router(alert_approvals_router)
app.include_router(anticipatory_router)
app.include_router(disbursements_router)
app.include_router(donations_router)
app.include_router(fnol_router)
app.include_router(verification_reviews_router)
app.include_router(damage_assessment_reviews_router)
app.include_router(public_ledger_router)
app.include_router(auth_router)
app.include_router(auto_approval_router)
app.include_router(logistics_router)
app.include_router(hazard_live_router)
