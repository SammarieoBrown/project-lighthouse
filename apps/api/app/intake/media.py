"""Secure acquisition and private storage for inbound provider media.

The webhook URL is a capability, not evidence.  This module turns it into an
authenticated, bounded byte string and then into a content-addressed object in
private R2.  Provider credentials are sent only to the exact Twilio API host;
the short-lived CDN redirect deliberately receives no ``Authorization``
header.
"""

from __future__ import annotations

import hashlib
import io
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import unquote, urlsplit

import httpx


MAX_MEDIA_BYTES = 16 * 1024 * 1024
# Voice notes are sent to Workers AI as one bounded request. Keeping the raw
# audio at or below 1 MiB caps base64 memory, request time, and paid ASR usage;
# longer recordings need a future chunked pipeline rather than an accidental
# oversized inference call.
MAX_AUDIO_BYTES = 1 * 1024 * 1024
MEDIA_FETCH_DEADLINE_SECONDS = 30.0
TWILIO_MEDIA_JOB_TYPE = "intake_media_enrichment"
TWILIO_MEDIA_RECONCILE_JOB_TYPE = "intake_media_terminal_reconcile"

_TWILIO_API_HOST = "api.twilio.com"
_TWILIO_SECURE_MEDIA_HOST = "mms.twiliocdn.com"
_ACCOUNT_SID_RE = re.compile(r"^AC[0-9a-fA-F]{32}$")
_API_KEY_SID_RE = re.compile(r"^SK[0-9a-fA-F]{32}$")
_MESSAGE_SID_RE = re.compile(r"^(?:SM|MM)[0-9a-fA-F]{32}$")
_MEDIA_SID_RE = re.compile(r"^ME[0-9a-fA-F]{32}$")
_R2_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,126}$")

AUDIO_CONTENT_TYPES = frozenset(
    {
        "audio/aac",
        "audio/amr",
        "audio/m4a",
        "audio/mp4",
        "audio/mpeg",
        "audio/ogg",
        "audio/opus",
        "audio/wav",
        "audio/webm",
        "audio/x-m4a",
        "audio/x-wav",
        "application/ogg",
    }
)
IMAGE_CONTENT_TYPES = frozenset(
    {
        "image/gif",
        "image/heic",
        "image/heif",
        "image/jpeg",
        "image/png",
        "image/webp",
    }
)
SUPPORTED_CONTENT_TYPES = AUDIO_CONTENT_TYPES | IMAGE_CONTENT_TYPES


class MediaBoundaryError(RuntimeError):
    """A provider response failed a security or integrity boundary."""


class MediaConfigurationError(RuntimeError):
    """The worker lacks an explicit credential or private-store setting."""


@dataclass(frozen=True, slots=True)
class FetchedMedia:
    data: bytes
    content_type: str
    sha256: str

    @property
    def size_bytes(self) -> int:
        return len(self.data)


@dataclass(frozen=True, slots=True)
class StoredMedia:
    uri: str
    object_key: str
    content_type: str
    sha256: str
    size_bytes: int


class MediaFetcher(Protocol):
    def fetch(
        self,
        url: str,
        *,
        message_sid: str,
        expected_content_type: str,
    ) -> FetchedMedia: ...


class MediaStore(Protocol):
    def put(self, media: FetchedMedia) -> StoredMedia: ...


def canonical_content_type(value: str | None) -> str:
    """Return a lower-case bare MIME type, preserving only known aliases."""
    raw = (value or "").split(";", 1)[0].strip().lower()
    aliases = {
        "audio/x-wav": "audio/wav",
        "audio/x-m4a": "audio/mp4",
        "audio/m4a": "audio/mp4",
        "audio/opus": "audio/ogg",
        "application/ogg": "audio/ogg",
        "image/jpg": "image/jpeg",
    }
    return aliases.get(raw, raw)


def is_audio_content_type(value: str | None) -> bool:
    return canonical_content_type(value) in {
        canonical_content_type(item) for item in AUDIO_CONTENT_TYPES
    }


def is_image_content_type(value: str | None) -> bool:
    return canonical_content_type(value) in IMAGE_CONTENT_TYPES


def is_supported_content_type(value: str | None) -> bool:
    return is_audio_content_type(value) or is_image_content_type(value)


def image_perceptual_hash(media: FetchedMedia) -> str | None:
    """Return a real 64-bit difference hash when the image can be decoded.

    Base Pillow does not decode HEIC/HEIF. Those files retain their SHA-256 but
    return ``None`` so verification treats the photo signal as absent instead
    of accepting a fabricated perceptual identity.
    """
    if not is_image_content_type(media.content_type):
        return None
    if media.content_type in {"image/heic", "image/heif"}:
        return None

    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(media.data)) as probe:
            if (
                probe.width <= 0
                or probe.height <= 0
                or probe.width * probe.height > 25_000_000
            ):
                raise MediaBoundaryError("image dimensions exceed the decode boundary")
            probe.verify()
        with Image.open(io.BytesIO(media.data)) as decoded:
            grayscale = decoded.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
            get_pixels = getattr(grayscale, "get_flattened_data", grayscale.getdata)
            pixels = list(get_pixels())
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise MediaBoundaryError("image could not be safely decoded") from exc

    bits = 0
    for row in range(8):
        offset = row * 9
        for column in range(8):
            bits = (bits << 1) | int(
                pixels[offset + column] > pixels[offset + column + 1]
            )
    return f"{bits:016x}"


def _validate_initial_twilio_url(
    value: str,
    *,
    account_sid: str,
    message_sid: str,
) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise MediaBoundaryError("media source URL is invalid") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != _TWILIO_API_HOST
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise MediaBoundaryError("media source is not an allowed Twilio API URL")

    # Do not normalize slashes or dot segments. Exact resource paths are much
    # easier to bind to the signed message than URL aliases.
    if (
        parsed.path.endswith("/")
        or "//" in parsed.path
        or "/./" in parsed.path
        or "%" in parsed.path
    ):
        raise MediaBoundaryError("media source has an invalid Twilio resource path")
    parts = parsed.path.split("/")
    if (
        len(parts) != 8
        or parts[1:3] != ["2010-04-01", "Accounts"]
        or parts[4] != "Messages"
        or parts[6] != "Media"
        or unquote(parts[3]) != account_sid
        or unquote(parts[5]) != message_sid
        or not _MEDIA_SID_RE.fullmatch(unquote(parts[7]))
    ):
        raise MediaBoundaryError("media source does not match the signed message")
    return parsed.geturl()


def _validate_secure_redirect(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise MediaBoundaryError("Twilio media redirect URL is invalid") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != _TWILIO_SECURE_MEDIA_HOST
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        raise MediaBoundaryError("Twilio media redirect is not allowlisted")
    return parsed.geturl()


def _magic_matches(data: bytes, content_type: str) -> bool:
    """Small, conservative file-signature check before bytes reach an ASR."""
    if content_type == "audio/ogg":
        return data.startswith(b"OggS") and any(
            marker in data[:65_536] for marker in (b"OpusHead", b"vorbis", b"fLaC")
        )
    if content_type == "audio/wav":
        return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WAVE"
    if content_type == "audio/mpeg":
        return data.startswith(b"ID3") or (
            len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0
        )
    if content_type == "audio/amr":
        return data.startswith((b"#!AMR\n", b"#!AMR-WB\n"))
    if content_type == "audio/webm":
        return data.startswith(b"\x1aE\xdf\xa3")
    if content_type in {"audio/mp4", "audio/aac"}:
        return (
            len(data) >= 12 and data[4:8] == b"ftyp"
        ) or (len(data) >= 2 and data[0] == 0xFF and data[1] & 0xF6 == 0xF0)
    if content_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if content_type == "image/webp":
        return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    if content_type in {"image/heic", "image/heif"}:
        return len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in {
            b"heic",
            b"heix",
            b"hevc",
            b"hevx",
            b"mif1",
            b"msf1",
        }
    return False


class TwilioMediaFetcher:
    """Fetch one message-bound Twilio Media resource without an SSRF surface."""

    def __init__(
        self,
        *,
        account_sid: str,
        credential_sid: str,
        credential_secret: str,
        environment: str,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        max_bytes: int = MAX_MEDIA_BYTES,
    ) -> None:
        if not _ACCOUNT_SID_RE.fullmatch(account_sid):
            raise MediaConfigurationError("Twilio account SID is invalid")
        if not credential_sid or not credential_secret:
            raise MediaConfigurationError("Twilio media credentials are missing")
        if environment.casefold() == "production" and not _API_KEY_SID_RE.fullmatch(
            credential_sid
        ):
            raise MediaConfigurationError(
                "production Twilio media fetch requires an API key credential"
            )
        if max_bytes <= 0 or max_bytes > MAX_MEDIA_BYTES:
            raise MediaConfigurationError("Twilio media byte limit is invalid")
        self.account_sid = account_sid
        self.credential_sid = credential_sid
        self.credential_secret = credential_secret
        self.transport = transport
        self.sleeper = sleeper
        self.clock = clock
        self.max_bytes = max_bytes

    @classmethod
    def from_settings(cls, settings) -> "TwilioMediaFetcher":
        production = str(settings.environment).casefold() == "production"
        api_sid = str(getattr(settings, "twilio_api_key_sid", None) or "")
        api_secret = str(getattr(settings, "twilio_api_key_secret", None) or "")
        if production or (api_sid and api_secret):
            credential_sid, credential_secret = api_sid, api_secret
        else:
            # SID/AuthToken remains useful for the Twilio sandbox and local
            # demos, but is deliberately rejected by __init__ in production.
            credential_sid = str(getattr(settings, "twilio_account_sid", None) or "")
            credential_secret = str(getattr(settings, "twilio_auth_token", None) or "")
        return cls(
            account_sid=str(getattr(settings, "twilio_account_sid", None) or ""),
            credential_sid=credential_sid,
            credential_secret=credential_secret,
            environment=str(settings.environment),
        )

    def _read_response(
        self,
        response: httpx.Response,
        *,
        expected_content_type: str,
        started_at: float,
    ) -> FetchedMedia:
        if response.status_code != 200:
            raise MediaBoundaryError("Twilio media fetch failed")
        encoding = response.headers.get("content-encoding", "identity").casefold()
        if encoding not in {"", "identity"}:
            raise MediaBoundaryError("encoded Twilio media is not accepted")

        actual_type = canonical_content_type(response.headers.get("content-type"))
        expected_type = canonical_content_type(expected_content_type)
        if (
            actual_type not in {canonical_content_type(v) for v in SUPPORTED_CONTENT_TYPES}
            or actual_type != expected_type
        ):
            raise MediaBoundaryError("Twilio media content type does not match the webhook")
        byte_limit = min(
            self.max_bytes,
            MAX_AUDIO_BYTES if is_audio_content_type(actual_type) else MAX_MEDIA_BYTES,
        )

        declared_length = response.headers.get("content-length")
        length: int | None = None
        if declared_length is not None:
            try:
                length = int(declared_length)
            except ValueError as exc:
                raise MediaBoundaryError("Twilio media length is invalid") from exc
            if length < 1 or length > byte_limit:
                raise MediaBoundaryError("Twilio media exceeds the byte limit")

        chunks: list[bytes] = []
        total = 0
        digest = hashlib.sha256()
        # ``iter_bytes`` also works with fully buffered test transports. Any
        # content encoding was rejected above, so no decoder can inflate data
        # behind the byte limit.
        for chunk in response.iter_bytes():
            if self.clock() - started_at > MEDIA_FETCH_DEADLINE_SECONDS:
                raise MediaBoundaryError("Twilio media fetch exceeded its deadline")
            total += len(chunk)
            if total > byte_limit:
                raise MediaBoundaryError("Twilio media exceeds the byte limit")
            digest.update(chunk)
            chunks.append(chunk)
        data = b"".join(chunks)
        if length is not None and total != length:
            raise MediaBoundaryError("Twilio media length did not match its response")
        if not data or not _magic_matches(data, actual_type):
            raise MediaBoundaryError("Twilio media signature does not match its content type")
        return FetchedMedia(
            data=data,
            content_type=actual_type,
            sha256=digest.hexdigest(),
        )

    def fetch(
        self,
        url: str,
        *,
        message_sid: str,
        expected_content_type: str,
    ) -> FetchedMedia:
        if not _MESSAGE_SID_RE.fullmatch(message_sid):
            raise MediaBoundaryError("message SID is invalid")
        source = _validate_initial_twilio_url(
            url,
            account_sid=self.account_sid,
            message_sid=message_sid,
        )
        if not is_supported_content_type(expected_content_type):
            raise MediaBoundaryError("webhook media content type is unsupported")

        timeout = httpx.Timeout(connect=5.0, read=8.0, write=5.0, pool=5.0)
        headers = {
            "Accept": canonical_content_type(expected_content_type),
            "Accept-Encoding": "identity",
            "User-Agent": "project-lighthouse-media/1",
        }
        started_at = self.clock()
        with httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            transport=self.transport,
            trust_env=False,
        ) as client:
            redirect: str | None = None
            # Twilio documents a short propagation window where a just-created
            # Media resource may answer 404. Retry only that exact status, with
            # a tiny bounded schedule; all other failures fail closed.
            for attempt, delay in enumerate((0.0, 0.25, 0.75)):
                if delay:
                    self.sleeper(delay)
                with client.stream(
                    "GET",
                    source,
                    headers=headers,
                    auth=(self.credential_sid, self.credential_secret),
                ) as response:
                    if response.status_code == 404 and attempt < 2:
                        continue
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise MediaBoundaryError("Twilio media redirect is missing")
                        redirect = _validate_secure_redirect(location)
                        break
                    return self._read_response(
                        response,
                        expected_content_type=expected_content_type,
                        started_at=started_at,
                    )

            if redirect is None:
                raise MediaBoundaryError("Twilio media was not available after bounded retries")
            if self.clock() - started_at > MEDIA_FETCH_DEADLINE_SECONDS:
                raise MediaBoundaryError("Twilio media fetch exceeded its deadline")

            # Intentionally no auth argument here. A regression that forwards
            # the Basic header to the CDN would turn a media redirect into a
            # Twilio credential exfiltration path.
            with client.stream("GET", redirect, headers=headers) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    raise MediaBoundaryError("chained Twilio media redirects are not accepted")
                return self._read_response(
                    response,
                    expected_content_type=expected_content_type,
                    started_at=started_at,
                )


class R2MediaStore:
    """Content-addressed private R2 store using its S3-compatible endpoint."""

    def __init__(
        self,
        *,
        account_id: str,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
        client=None,
    ) -> None:
        if not re.fullmatch(r"^[0-9a-fA-F]{32}$", account_id):
            raise MediaConfigurationError("R2 account id is invalid")
        if not access_key_id or not secret_access_key:
            raise MediaConfigurationError("R2 credentials are missing")
        if not _R2_COMPONENT_RE.fullmatch(bucket):
            raise MediaConfigurationError("R2 bucket name is invalid")
        self.bucket = bucket
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client(
                service_name="s3",
                endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_access_key,
                region_name="auto",
                config=Config(
                    connect_timeout=5,
                    read_timeout=20,
                    retries={"max_attempts": 3, "mode": "standard"},
                    signature_version="s3v4",
                ),
            )
        self.client = client

    @classmethod
    def from_settings(cls, settings) -> "R2MediaStore":
        return cls(
            account_id=str(getattr(settings, "r2_account_id", None) or ""),
            access_key_id=str(getattr(settings, "r2_access_key_id", None) or ""),
            secret_access_key=str(
                getattr(settings, "r2_secret_access_key", None) or ""
            ),
            bucket=str(getattr(settings, "r2_bucket", None) or ""),
        )

    def put(self, media: FetchedMedia) -> StoredMedia:
        digest = hashlib.sha256(media.data).hexdigest()
        if digest != media.sha256:
            raise MediaBoundaryError("media digest changed before storage")
        key = f"intake/sha256/{digest[:2]}/{digest}"
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=media.data,
            ContentLength=len(media.data),
            ContentType=media.content_type,
            ContentDisposition="attachment",
            CacheControl="private, no-store",
            Metadata={"sha256": digest, "schema": "lighthouse-media-v1"},
        )
        head = self.client.head_object(Bucket=self.bucket, Key=key)
        metadata = {str(k).lower(): str(v) for k, v in (head.get("Metadata") or {}).items()}
        if int(head.get("ContentLength", -1)) != len(media.data) or metadata.get(
            "sha256"
        ) != digest:
            raise MediaBoundaryError("private media store did not preserve object integrity")
        return StoredMedia(
            uri=f"r2://{self.bucket}/{key}",
            object_key=key,
            content_type=media.content_type,
            sha256=digest,
            size_bytes=len(media.data),
        )

    def get(
        self,
        object_key: str,
        *,
        expected_sha256: str,
        max_bytes: int = MAX_MEDIA_BYTES,
    ) -> FetchedMedia:
        """Read a previously stored object back, verifying its digest.

        Every reader of stored evidence — not just the writer in ``put`` —
        must not trust bytes that could have changed underneath the row, and
        must not read an unbounded number of them into a worker because the
        object store offered them. ``fetch`` bounds the inbound side; this is
        the same bound on the way back out.
        """
        if max_bytes <= 0 or max_bytes > MAX_MEDIA_BYTES:
            raise MediaBoundaryError("stored media byte limit is invalid")
        obj = self.client.get_object(Bucket=self.bucket, Key=object_key)
        declared = obj.get("ContentLength")
        if declared is not None and int(declared) > max_bytes:
            raise MediaBoundaryError("stored media exceeds the byte limit")
        # One byte past the limit, so an object that under-declares its length
        # is caught by what actually arrived rather than by what it claimed.
        data = obj["Body"].read(max_bytes + 1)
        if len(data) > max_bytes:
            raise MediaBoundaryError("stored media exceeds the byte limit")
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected_sha256:
            raise MediaBoundaryError("stored media digest does not match evidence row")
        return FetchedMedia(
            data=data,
            content_type=str(obj.get("ContentType") or ""),
            sha256=digest,
        )


__all__ = [
    "AUDIO_CONTENT_TYPES",
    "FetchedMedia",
    "IMAGE_CONTENT_TYPES",
    "MAX_AUDIO_BYTES",
    "MAX_MEDIA_BYTES",
    "MediaBoundaryError",
    "MediaConfigurationError",
    "MediaFetcher",
    "MediaStore",
    "R2MediaStore",
    "StoredMedia",
    "SUPPORTED_CONTENT_TYPES",
    "TWILIO_MEDIA_JOB_TYPE",
    "TWILIO_MEDIA_RECONCILE_JOB_TYPE",
    "TwilioMediaFetcher",
    "canonical_content_type",
    "image_perceptual_hash",
    "is_audio_content_type",
    "is_image_content_type",
    "is_supported_content_type",
]
