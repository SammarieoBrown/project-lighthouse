"""Network-free intake boundary and redaction tests."""

from __future__ import annotations

import asyncio
import math

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.intake.claims import _safe_signals
from app.intake.router import _MAX_FORM_BYTES, _bounded_urlencoded_form
from app.web import app


PATH = "/webhooks/twilio/whatsapp"


def test_webhook_rejects_unsupported_content_type_before_authentication():
    response = TestClient(app, raise_server_exceptions=False).post(
        PATH,
        content=b'{"MessageSid":"SM123"}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 415


@pytest.mark.parametrize(
    ("body", "expected_status"),
    [
        (b"Body=" + b"x" * _MAX_FORM_BYTES, 413),
        (b"MessageSid=%ZZ&Body=test", 400),
        ("&".join(f"field{i}=value" for i in range(65)).encode("ascii"), 413),
    ],
)
def test_webhook_rejects_oversize_malformed_and_excessive_forms(
    body: bytes, expected_status: int
):
    response = TestClient(app, raise_server_exceptions=False).post(
        PATH,
        content=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == expected_status


def _streaming_request(chunks: list[bytes]) -> Request:
    messages = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks)
    ]

    async def receive():
        return messages.pop(0)

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": PATH,
            "raw_path": PATH.encode("ascii"),
            "query_string": b"",
            # Deliberately no Content-Length: this is the chunked-transfer case.
            "headers": [
                (b"content-type", b"application/x-www-form-urlencoded")
            ],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 443),
        },
        receive,
    )


def test_chunked_body_without_content_length_is_bounded_while_streaming():
    request = _streaming_request(
        [b"a" * (_MAX_FORM_BYTES // 2), b"b" * (_MAX_FORM_BYTES // 2 + 1)]
    )
    with pytest.raises(HTTPException) as caught:
        asyncio.run(_bounded_urlencoded_form(request))
    assert caught.value.status_code == 413


def test_safe_signals_is_exact_allowlist_and_drops_malformed_fields():
    redacted = _safe_signals(
        {
            "hazard_sufficiency": {
                "present": True,
                "score": 0.75,
                "evidence": {"phone": "+18760001111"},
                "extra": "secret",
            },
            "satellite_change": {"present": False},
            "neighbour_corroboration": {"score": 0},
            "registry_match": {"present": "yes", "score": math.nan},
            "media_integrity": {"present": False, "score": 2.0},
            "hazard_sufficiency_extra": {"present": True, "score": 1.0},
            "unknown_signal": {"present": True, "score": 1.0},
        }
    )
    assert redacted == {
        "hazard_sufficiency": {"present": True, "score": 0.75},
        "satellite_change": {"present": False},
        "neighbour_corroboration": {"score": 0.0},
        "media_integrity": {"present": False},
    }
    assert _safe_signals(None) == {}
