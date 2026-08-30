"""Twilio request authentication and inbound WhatsApp parsing.

Twilio signs the exact public URL followed by the sorted form fields with
HMAC-SHA1.  The service is behind Render, so reconstructing the URL from the
ASGI request's internal scheme is unsafe and usually wrong; callers provide the
configured public base URL instead.

This module deliberately has no dependency on the Twilio SDK.  Signature
verification is a small, stable protocol primitive and keeping it here avoids
adding a large messaging client to the inbound-only path.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit


class InvalidTwilioPayload(ValueError):
    """A signed request is not a supported inbound WhatsApp message."""


@dataclass(frozen=True, slots=True)
class TwilioMedia:
    index: int
    url: str
    content_type: str | None


@dataclass(frozen=True, slots=True)
class TwilioInbound:
    message_sid: str
    from_phone: str
    body: str
    media: tuple[TwilioMedia, ...]
    #: A WhatsApp location share, when the household sent one. Both set or
    #: both ``None`` — a lone coordinate is a malformed payload, not a point.
    latitude: float | None = None
    longitude: float | None = None


@dataclass(frozen=True, slots=True)
class TwilioDeliveryStatus:
    message_sid: str
    message_status: str
    error_code: str | None


_SID_RE = re.compile(r"^[A-Za-z0-9]{8,64}$")
_E164_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")

# PRD INT-04.  Phrase matching is intentionally conservative: a false positive
# only raises priority and human attention; it never changes verification or
# payment authority.
_SOL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("trapped", re.compile(r"\btrapp?ed\b", re.IGNORECASE)),
    ("injured", re.compile(r"\binjur(?:ed|y|ies)\b", re.IGNORECASE)),
    ("bleeding", re.compile(r"\bbleed(?:ing|in)?\b", re.IGNORECASE)),
    (
        "cannot_breathe",
        re.compile(r"\b(?:can(?:not|'t|t)|cyan)\s+breathe\b", re.IGNORECASE),
    ),
    ("dying", re.compile(r"\bdying\b", re.IGNORECASE)),
    ("baby_sick", re.compile(r"\bbaby\s+(?:is\s+)?sick\b", re.IGNORECASE)),
    (
        "no_water_for_days",
        re.compile(r"\bno\s+water\s+(?:for|fi)\s+(?:[2-9]|\w+)\s+days?\b", re.IGNORECASE),
    ),
)


def _parameter_pairs(params: object) -> Iterable[tuple[str, str]]:
    """Yield all form values, including repeated fields.

    Starlette's ``FormData`` exposes ``multi_items``.  The mapping fallback is
    useful for unit tests and keeps this protocol helper framework-neutral.
    """
    multi_items = getattr(params, "multi_items", None)
    if callable(multi_items):
        for name, value in multi_items():
            yield str(name), str(value)
        return

    if not isinstance(params, Mapping):
        raise TypeError("Twilio signature parameters must be form data")
    for name, value in params.items():
        if isinstance(value, (list, tuple)):
            for item in value:
                yield str(name), str(item)
        else:
            yield str(name), str(value)


def signature_for(url: str, params: object, auth_token: str) -> str:
    """Return the signature Twilio should have sent for one form request."""
    values: dict[str, list[str]] = defaultdict(list)
    for name, value in _parameter_pairs(params):
        values[name].append(value)

    material = url
    for name in sorted(values):
        # Twilio's helper sorts and de-duplicates repeated values.
        for value in sorted(set(values[name])):
            material += name + value

    digest = hmac.new(
        auth_token.encode("utf-8"), material.encode("utf-8"), hashlib.sha1
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def signature_is_valid(
    *, url: str, params: object, auth_token: str, supplied_signature: str | None
) -> bool:
    """Constant-time Twilio signature comparison; missing values fail closed."""
    if not auth_token or not supplied_signature:
        return False
    expected = signature_for(url, params, auth_token)
    return hmac.compare_digest(expected, supplied_signature.strip())


def public_request_url(*, public_base_url: str, path: str, query: str = "") -> str:
    """Build the exact externally configured URL used in Twilio's signature."""
    base = public_base_url.rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("PUBLIC_BASE_URL must be an absolute HTTP(S) URL")
    if not path.startswith("/"):
        raise ValueError("request path must be absolute")
    url = f"{base}{path}"
    return f"{url}?{query}" if query else url


def normalize_whatsapp_phone(value: str) -> str:
    """Return E.164 without Twilio's ``whatsapp:`` transport prefix."""
    candidate = value.strip()
    if candidate.lower().startswith("whatsapp:"):
        candidate = candidate.split(":", 1)[1]
    if not _E164_RE.fullmatch(candidate):
        raise InvalidTwilioPayload("invalid WhatsApp sender")
    return candidate


def _media_url(value: str) -> str:
    url = value.strip()
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "twilio.com" or host.endswith(".twilio.com")):
        raise InvalidTwilioPayload("invalid Twilio media URL")
    return url


def parse_inbound(form: Mapping[str, object]) -> TwilioInbound:
    """Validate and reduce a signed Twilio form to the fields intake needs."""
    sid = str(form.get("MessageSid") or "").strip()
    if not _SID_RE.fullmatch(sid):
        raise InvalidTwilioPayload("invalid message SID")

    phone = normalize_whatsapp_phone(str(form.get("From") or ""))
    body = str(form.get("Body") or "")
    if len(body) > 16_000:
        raise InvalidTwilioPayload("message body is too large")

    try:
        media_count = int(str(form.get("NumMedia") or "0"))
    except ValueError as exc:
        raise InvalidTwilioPayload("invalid media count") from exc
    if not 0 <= media_count <= 10:
        raise InvalidTwilioPayload("invalid media count")

    media: list[TwilioMedia] = []
    for index in range(media_count):
        raw_url = str(form.get(f"MediaUrl{index}") or "")
        if not raw_url:
            raise InvalidTwilioPayload("missing media URL")
        content_type = str(form.get(f"MediaContentType{index}") or "").strip().lower()
        media.append(
            TwilioMedia(
                index=index,
                url=_media_url(raw_url),
                content_type=content_type or None,
            )
        )

    latitude, longitude = _location(form)

    if not body.strip() and not media and latitude is None:
        raise InvalidTwilioPayload("message has no text, media, or location")

    return TwilioInbound(
        message_sid=sid,
        from_phone=phone,
        body=body,
        media=tuple(media),
        latitude=latitude,
        longitude=longitude,
    )


def _location(form: Mapping[str, object]) -> tuple[float | None, float | None]:
    """Parse a WhatsApp location share's coordinates, if the form carries one."""
    raw_lat = str(form.get("Latitude") or "").strip()
    raw_lon = str(form.get("Longitude") or "").strip()
    if not raw_lat and not raw_lon:
        return None, None
    if not raw_lat or not raw_lon:
        raise InvalidTwilioPayload("location share is missing a coordinate")
    try:
        latitude, longitude = float(raw_lat), float(raw_lon)
    except ValueError as exc:
        raise InvalidTwilioPayload("location coordinates are not numeric") from exc
    if not (
        math.isfinite(latitude)
        and math.isfinite(longitude)
        and -90.0 <= latitude <= 90.0
        and -180.0 <= longitude <= 180.0
    ):
        raise InvalidTwilioPayload("location coordinates are out of range")
    return latitude, longitude


def parse_delivery_status(form: Mapping[str, object]) -> TwilioDeliveryStatus:
    """Reduce a signed status callback to its non-PII reconciliation fields."""
    sid = str(form.get("MessageSid") or "").strip()
    if not _SID_RE.fullmatch(sid):
        raise InvalidTwilioPayload("invalid message SID")
    message_status = str(form.get("MessageStatus") or "").strip().lower()
    if not re.fullmatch(r"[a-z_]{2,32}", message_status):
        raise InvalidTwilioPayload("invalid message status")
    error_code = str(form.get("ErrorCode") or "").strip() or None
    if error_code is not None and not re.fullmatch(r"[0-9]{3,8}", error_code):
        raise InvalidTwilioPayload("invalid delivery error code")
    return TwilioDeliveryStatus(
        message_sid=sid,
        message_status=message_status,
        error_code=error_code,
    )


def safety_of_life_matches(text: str) -> list[str]:
    """Return the canonical INT-04 phrases found in text, without the text."""
    return [name for name, pattern in _SOL_PATTERNS if pattern.search(text)]


__all__ = [
    "InvalidTwilioPayload",
    "TwilioInbound",
    "TwilioDeliveryStatus",
    "TwilioMedia",
    "normalize_whatsapp_phone",
    "parse_inbound",
    "parse_delivery_status",
    "public_request_url",
    "safety_of_life_matches",
    "signature_for",
    "signature_is_valid",
]
