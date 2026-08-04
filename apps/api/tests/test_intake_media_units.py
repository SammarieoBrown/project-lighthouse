"""Network-free security and provider-contract tests for intake media."""

from __future__ import annotations

import base64
import hashlib
import io
import json

import httpx
import pytest
from PIL import Image

from app.intake.extraction import extract_claim_fields
from app.intake.media import (
    FetchedMedia,
    MAX_AUDIO_BYTES,
    MediaBoundaryError,
    MediaConfigurationError,
    R2MediaStore,
    TwilioMediaFetcher,
    image_perceptual_hash,
)
from app.intake.transcription import (
    CloudflareWhisperTranscriber,
    DeterministicTranscriber,
    TranscriptionError,
    TranscriptionResult,
)


ACCOUNT_SID = "AC" + "a" * 32
API_KEY_SID = "SK" + "b" * 32
MESSAGE_SID = "SM" + "c" * 32
MEDIA_SID = "ME" + "d" * 32
MEDIA_URL = (
    f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}/"
    f"Messages/{MESSAGE_SID}/Media/{MEDIA_SID}"
)
OGG = b"OggS" + b"\x00" * 128 + b"OpusHead" + b"\x00" * 32


def _fetcher(transport, **kw) -> TwilioMediaFetcher:
    return TwilioMediaFetcher(
        account_sid=ACCOUNT_SID,
        credential_sid=API_KEY_SID,
        credential_secret="private-test-secret",
        environment="production",
        transport=transport,
        sleeper=kw.pop("sleeper", lambda _: None),
        **kw,
    )


def test_twilio_fetch_retries_transient_404_and_strips_auth_on_redirect():
    requests: list[httpx.Request] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "api.twilio.com" and len(requests) == 1:
            return httpx.Response(404)
        if request.url.host == "api.twilio.com":
            return httpx.Response(
                302,
                headers={
                    "Location": "https://mms.twiliocdn.com/secure/object?token=opaque"
                },
            )
        assert request.url.host == "mms.twiliocdn.com"
        return httpx.Response(
            200,
            headers={"Content-Type": "audio/ogg", "Content-Length": str(len(OGG))},
            content=OGG,
        )

    result = _fetcher(
        httpx.MockTransport(handler), sleeper=sleeps.append
    ).fetch(
        MEDIA_URL,
        message_sid=MESSAGE_SID,
        expected_content_type="application/ogg",
    )
    assert result.data == OGG
    assert result.sha256 == hashlib.sha256(OGG).hexdigest()
    assert sleeps == [0.25]
    assert requests[0].headers["authorization"].startswith("Basic ")
    assert requests[1].headers["authorization"].startswith("Basic ")
    assert "authorization" not in requests[2].headers


@pytest.mark.parametrize(
    "url",
    [
        MEDIA_URL.replace("api.twilio.com", "api.twilio.com.attacker.example"),
        MEDIA_URL.replace("https://", "http://"),
        MEDIA_URL.replace(ACCOUNT_SID, "AC" + "f" * 32),
        MEDIA_URL.replace(MESSAGE_SID, "SM" + "f" * 32),
        MEDIA_URL + "?redirect=https://169.254.169.254",
        MEDIA_URL.replace("api.twilio.com", "user:pass@api.twilio.com"),
        MEDIA_URL + "/descendant",
    ],
)
def test_twilio_fetch_rejects_noncanonical_or_unbound_sources_without_network(url):
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=OGG)

    with pytest.raises(MediaBoundaryError):
        _fetcher(httpx.MockTransport(handler)).fetch(
            url,
            message_sid=MESSAGE_SID,
            expected_content_type="audio/ogg",
        )
    assert calls == 0


def test_twilio_fetch_rejects_unallowlisted_redirect_content_mismatch_and_oversize():
    cases = [
        httpx.Response(302, headers={"Location": "https://example.com/private"}),
        httpx.Response(200, headers={"Content-Type": "image/png"}, content=OGG),
        httpx.Response(
            200,
            headers={"Content-Type": "audio/ogg", "Content-Length": "999"},
            content=OGG,
        ),
    ]
    for response in cases:
        transport = httpx.MockTransport(lambda _: response)
        with pytest.raises(MediaBoundaryError):
            _fetcher(transport, max_bytes=256).fetch(
                MEDIA_URL,
                message_sid=MESSAGE_SID,
                expected_content_type="audio/ogg",
            )


def test_voice_note_has_a_stricter_paid_inference_byte_cap():
    response = httpx.Response(
        200,
        headers={
            "Content-Type": "audio/ogg",
            "Content-Length": str(MAX_AUDIO_BYTES + 1),
        },
        content=OGG,
    )
    with pytest.raises(MediaBoundaryError, match="byte limit"):
        _fetcher(httpx.MockTransport(lambda _: response)).fetch(
            MEDIA_URL,
            message_sid=MESSAGE_SID,
            expected_content_type="audio/ogg",
        )


def test_production_media_fetch_requires_restricted_api_key():
    with pytest.raises(MediaConfigurationError, match="API key"):
        TwilioMediaFetcher(
            account_sid=ACCOUNT_SID,
            credential_sid=ACCOUNT_SID,
            credential_secret="auth-token",
            environment="production",
        )
    # Local sandbox compatibility is explicit, not an accidental prod fallback.
    TwilioMediaFetcher(
        account_sid=ACCOUNT_SID,
        credential_sid=ACCOUNT_SID,
        credential_secret="auth-token",
        environment="local",
    )


class _FakeR2:
    def __init__(self):
        self.put: dict | None = None

    def put_object(self, **kwargs):
        self.put = kwargs

    def head_object(self, **_):
        assert self.put is not None
        return {
            "ContentLength": self.put["ContentLength"],
            "Metadata": self.put["Metadata"],
        }


def test_r2_store_is_content_addressed_private_and_integrity_checked():
    digest = hashlib.sha256(OGG).hexdigest()
    client = _FakeR2()
    store = R2MediaStore(
        account_id="e" * 32,
        access_key_id="key",
        secret_access_key="secret",
        bucket="lighthouse-media",
        client=client,
    )
    result = store.put(FetchedMedia(OGG, "audio/ogg", digest))
    assert result.uri == f"r2://lighthouse-media/intake/sha256/{digest[:2]}/{digest}"
    assert client.put is not None
    assert client.put["CacheControl"] == "private, no-store"
    assert client.put["Metadata"]["sha256"] == digest
    assert MESSAGE_SID not in result.uri


def test_real_image_perceptual_hash_is_deterministic():
    image = Image.new("RGB", (12, 12))
    for x in range(12):
        for y in range(12):
            image.putpixel((x, y), (x * 20, y * 20, 100))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    data = buffer.getvalue()
    media = FetchedMedia(data, "image/png", hashlib.sha256(data).hexdigest())
    first = image_perceptual_hash(media)
    assert first is not None and len(first) == 16
    assert image_perceptual_hash(media) == first


def test_cloudflare_whisper_adapter_preserves_patois_context_without_network():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        assert base64.b64decode(body["audio"]) == OGG
        assert body["task"] == "transcribe"
        assert body["language"] == "en"
        assert "Jamaican Patois" in body["initial_prompt"]
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json={
                "success": True,
                "result": {
                    "text": "Mi roof gone and mi need wata.",
                    "transcription_info": {"language": "en"},
                },
            },
        )

    result = CloudflareWhisperTranscriber(
        account_id="e" * 32,
        api_token="worker-ai-token",
        transport=httpx.MockTransport(handler),
    ).transcribe(
        FetchedMedia(OGG, "audio/ogg", hashlib.sha256(OGG).hexdigest())
    )
    assert result.text == "Mi roof gone and mi need wata."
    assert result.lang == "en"
    assert seen[0].url.host == "api.cloudflare.com"
    assert seen[0].headers["authorization"] == "Bearer worker-ai-token"


def test_cloudflare_whisper_rechecks_audio_cap_before_network():
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    data = OGG + b"\x00" * (MAX_AUDIO_BYTES + 1 - len(OGG))
    transcriber = CloudflareWhisperTranscriber(
        account_id="e" * 32,
        api_token="worker-ai-token",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(TranscriptionError, match="byte boundary"):
        transcriber.transcribe(
            FetchedMedia(data, "audio/ogg", hashlib.sha256(data).hexdigest())
        )
    assert calls == 0


def test_deterministic_transcriber_and_patois_extraction_are_truthful():
    result = TranscriptionResult(
        text="Di roof blow off and wi need wata fi drink and insulin.",
        lang="jam",
        provider="test",
        model="fixed-v1",
    )
    transcriber = DeterministicTranscriber(result)
    media = FetchedMedia(OGG, "audio/ogg", hashlib.sha256(OGG).hexdigest())
    assert transcriber.transcribe(media) == result
    assert transcriber.calls == [media.sha256]
    extracted = extract_claim_fields(result.text)
    assert extracted.damage_type == "roof_damage"
    assert extracted.reported_needs == ("water", "insulin")
    assert extracted.is_complete(transcript=result.text, lang=result.lang)


def test_flood_description_does_not_invent_a_drinking_water_need():
    extracted = extract_claim_fields("Water come inna di house and flood di yard")
    assert extracted.damage_type == "flooding"
    assert extracted.reported_needs == ()
