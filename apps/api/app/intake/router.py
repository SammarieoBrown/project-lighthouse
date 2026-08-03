"""Signed provider webhooks for claim intake."""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qsl

from fastapi import APIRouter, HTTPException, Request, Response
from starlette.datastructures import FormData

from app.config import get_settings
from app.db import session_scope

from .service import enqueue_twilio_delivery_status, enqueue_twilio_inbound
from .twilio import (
    InvalidTwilioPayload,
    parse_delivery_status,
    parse_inbound,
    public_request_url,
    signature_is_valid,
)

log = logging.getLogger("lighthouse.intake")
router = APIRouter()

_MAX_FORM_BYTES = 64 * 1024
_MAX_FORM_FIELDS = 64
_MALFORMED_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


async def _bounded_urlencoded_form(request: Request) -> FormData:
    """Read one strict Twilio form without ever buffering an unbounded body."""
    content_type = request.headers.get("content-type", "")
    parts = [part.strip() for part in content_type.split(";")]
    if not parts or parts[0].lower() != "application/x-www-form-urlencoded":
        raise HTTPException(status_code=415, detail="unsupported webhook content type")
    for parameter in parts[1:]:
        normalized = parameter.lower().replace('"', "")
        if normalized != "charset=utf-8":
            raise HTTPException(status_code=415, detail="unsupported webhook content type")

    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            length = int(declared_length)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid content length") from None
        if length < 0:
            raise HTTPException(status_code=400, detail="invalid content length")
        if length > _MAX_FORM_BYTES:
            raise HTTPException(status_code=413, detail="webhook body is too large")

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _MAX_FORM_BYTES:
            raise HTTPException(status_code=413, detail="webhook body is too large")
        chunks.append(chunk)

    raw = b"".join(chunks)
    try:
        encoded = raw.decode("ascii")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="malformed webhook form") from None
    if _MALFORMED_PERCENT_ESCAPE.search(encoded):
        raise HTTPException(status_code=400, detail="malformed webhook form")
    if encoded and encoded.count("&") + 1 > _MAX_FORM_FIELDS:
        raise HTTPException(status_code=413, detail="webhook form has too many fields")

    try:
        pairs = parse_qsl(
            encoded,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=_MAX_FORM_FIELDS,
        )
    except (UnicodeError, ValueError):
        raise HTTPException(status_code=400, detail="malformed webhook form") from None
    return FormData(pairs)


async def _authenticated_twilio_form(request: Request) -> FormData:
    """Return form data only after fail-closed provider authentication."""
    # Bound and structurally validate hostile input before any multipart/form
    # parser or signature work. This also covers chunked bodies with no declared
    # Content-Length.
    form = await _bounded_urlencoded_form(request)
    settings = get_settings()
    if not settings.twilio_auth_token:
        # A disabled webhook must be loud to operations and retryable to Twilio;
        # accepting unsigned reports would make forged claims look legitimate.
        log.error("twilio webhook disabled: auth token is not configured")
        raise HTTPException(status_code=503, detail="provider webhook unavailable")

    try:
        signed_url = public_request_url(
            public_base_url=settings.public_base_url,
            path=request.url.path,
            query=request.url.query,
        )
    except ValueError:
        log.error("twilio webhook disabled: public base URL is invalid")
        raise HTTPException(status_code=503, detail="provider webhook unavailable") from None

    if not signature_is_valid(
        url=signed_url,
        params=form,
        auth_token=settings.twilio_auth_token,
        supplied_signature=request.headers.get("X-Twilio-Signature"),
    ):
        # Do not log form fields: From and Body are household PII.
        log.warning("rejected request with invalid twilio signature path=%s", request.url.path)
        raise HTTPException(status_code=403, detail="invalid provider signature")
    return form


@router.post("/webhooks/twilio/whatsapp")
async def twilio_whatsapp(request: Request) -> Response:
    """Authenticate and durable-queue an inbound WhatsApp text or voice note."""
    form = await _authenticated_twilio_form(request)
    try:
        inbound = parse_inbound(form)
    except InvalidTwilioPayload as exc:
        # The exception identifies the invalid field class, never its value.
        raise HTTPException(status_code=400, detail=str(exc)) from None

    with session_scope() as session:
        result = enqueue_twilio_inbound(
            session,
            inbound,
            hazard_external_ref=get_settings().intake_hazard_external_ref,
        )

    log.info(
        "inbound whatsapp message sid=%s intake_job=%s disposition=%s",
        inbound.message_sid,
        result.job_id,
        "queued" if result.created else "duplicate",
    )
    # Valid empty TwiML tells Twilio deliberately not to send an automatic
    # reply. An empty text/plain body is accepted by some clients but is not a
    # valid Messaging webhook response.
    return Response(
        content="<Response></Response>",
        status_code=200,
        media_type="application/xml",
    )


@router.post("/webhooks/twilio/status")
async def twilio_status(request: Request) -> Response:
    """Authenticate and durable-queue delivery status before acknowledging it."""
    form = await _authenticated_twilio_form(request)
    try:
        callback = parse_delivery_status(form)
    except InvalidTwilioPayload as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    with session_scope() as session:
        result = enqueue_twilio_delivery_status(session, callback)

    # Status reconciliation belongs to the outbound delivery module. Until its
    # handler lands, this job parks visibly with PENDING_HANDLER instead of a
    # successful HTTP response erasing Twilio's only copy.
    log.info(
        "delivery status sid=%s status=%s job=%s disposition=%s",
        callback.message_sid,
        callback.message_status,
        result.job_id,
        "queued" if result.created else "duplicate",
    )
    return Response(status_code=204)


__all__ = ["router"]


# Kept at the bottom to make the provider edge readable as one unit. Claim reads
# share this top-level router but have their own mandatory human authentication.
from .claims import router as claims_router  # noqa: E402

router.include_router(claims_router)
