"""FastAPI entrypoint — ``uvicorn app.web:app``.

Phase 0 scope: prove the wire. Health, ledger integrity, and a WhatsApp webhook
that durably records what arrived and returns immediately.

The webhook does not process anything. NFR-A-02 requires inbound messages to be
durable-queued at the edge before processing, so the handler's only job is to
get the message into Postgres and get out — if every agent downstream is broken,
we still have not lost a household's message.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from lighthouse_contracts import AgentName, __version__ as contracts_version

from . import ledger, queue
from .config import get_settings
from .db import session_scope

log = logging.getLogger("lighthouse.web")

app = FastAPI(title="Lighthouse API", version="0.1.0")
router = APIRouter()


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    with session_scope() as session:
        chain_ok = ledger.verify_chain(session)
        backlog = queue.pending_count(session)
    return {
        "status": "ok",
        "environment": settings.environment,
        "contracts": contracts_version,
        "ledger_chain_valid": chain_ok,
        "queue_backlog": backlog,
    }


@router.post("/webhooks/twilio/whatsapp")
async def twilio_whatsapp(request: Request) -> PlainTextResponse:
    """Inbound WhatsApp message.

    Twilio posts form-encoded, not JSON. Media arrives as MediaUrl0..N.

    NOTE (NFR-S-02): the raw form contains the sender's phone number. It is
    stored on the job payload, which is inside the database, and must never
    reach a log line. Log the message SID, never the body or the sender.
    """
    form = dict(await request.form())
    sid = form.get("MessageSid", "unknown")
    log.info("inbound whatsapp message sid=%s", sid)

    with session_scope() as session:
        queue.enqueue(
            session,
            job_type=AgentName.INTAKE_AGENT,
            payload={"provider": "twilio", "form": form},
        )

    # Twilio expects TwiML or an empty 200. An empty response means "no auto-reply";
    # the Intake Agent replies out-of-band once it has actually read the message.
    return PlainTextResponse("", status_code=200)


@router.post("/webhooks/twilio/status")
async def twilio_status(request: Request) -> PlainTextResponse:
    """Per-recipient delivery status (ALT-02).

    Not decoration: alert delivery reporting is a requirement, and this callback
    is the only place that data exists.
    """
    form = dict(await request.form())
    log.info(
        "delivery status sid=%s status=%s",
        form.get("MessageSid", "unknown"),
        form.get("MessageStatus", "unknown"),
    )
    return PlainTextResponse("", status_code=200)


@app.exception_handler(Exception)
async def unhandled(_: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error", exc_info=exc)
    return JSONResponse({"detail": "internal error"}, status_code=500)


app.include_router(router)
