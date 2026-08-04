"""Pluggable, bounded speech-to-text adapters for intake voice notes."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from .media import MAX_AUDIO_BYTES, FetchedMedia, is_audio_content_type


MAX_TRANSCRIPT_CHARS = 100_000
MAX_PROVIDER_RESPONSE_BYTES = 1024 * 1024
TRANSCRIPTION_DEADLINE_SECONDS = 45.0
DEFAULT_CLOUDFLARE_MODEL = "@cf/openai/whisper-large-v3-turbo"

_ACCOUNT_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_LANG_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


class TranscriptionError(RuntimeError):
    """The configured ASR did not return a valid, bounded transcription."""


class TranscriptionConfigurationError(RuntimeError):
    """ASR is disabled or missing its worker-only credential."""


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    text: str
    lang: str | None
    provider: str
    model: str


class Transcriber(Protocol):
    def transcribe(self, media: FetchedMedia) -> TranscriptionResult: ...


@dataclass(slots=True)
class DeterministicTranscriber:
    """Explicit network-free test double with an inspectable call record."""

    result: TranscriptionResult
    calls: list[str] = field(default_factory=list)

    def transcribe(self, media: FetchedMedia) -> TranscriptionResult:
        self.calls.append(media.sha256)
        return self.result


class CloudflareWhisperTranscriber:
    """Cloudflare Workers AI Whisper adapter tuned for Jamaican voice notes.

    Whisper's API language code remains ``en``.  The context prompt asks the
    model to preserve Patois rather than translate it into standard English;
    the result's own detected language is still stored, never invented here.
    """

    _INITIAL_PROMPT = (
        "Emergency report from Jamaica. Preserve Jamaican Patois wording, "
        "Jamaican place names, names, numbers, and English-Patois code-switching. "
        "Transcribe what is spoken; do not translate, correct, or invent words."
    )

    def __init__(
        self,
        *,
        account_id: str,
        api_token: str,
        model: str = DEFAULT_CLOUDFLARE_MODEL,
        transport: httpx.BaseTransport | None = None,
        clock=time.monotonic,
    ) -> None:
        if not _ACCOUNT_ID_RE.fullmatch(account_id):
            raise TranscriptionConfigurationError("Cloudflare account id is invalid")
        if not api_token:
            raise TranscriptionConfigurationError("Cloudflare AI token is missing")
        if not re.fullmatch(r"@[A-Za-z0-9._/-]{3,160}", model):
            raise TranscriptionConfigurationError("Cloudflare transcription model is invalid")
        self.account_id = account_id
        self.api_token = api_token
        self.model = model
        self.transport = transport
        self.clock = clock

    @classmethod
    def from_settings(cls, settings) -> "CloudflareWhisperTranscriber":
        provider = str(
            getattr(settings, "intake_transcription_provider", None) or "disabled"
        ).casefold()
        if provider != "cloudflare_workers_ai":
            raise TranscriptionConfigurationError(
                "intake transcription provider is not enabled"
            )
        return cls(
            account_id=str(getattr(settings, "r2_account_id", None) or ""),
            api_token=str(
                getattr(settings, "cloudflare_ai_api_token", None) or ""
            ),
            model=str(
                getattr(settings, "intake_transcription_model", None)
                or DEFAULT_CLOUDFLARE_MODEL
            ),
        )

    def transcribe(self, media: FetchedMedia) -> TranscriptionResult:
        if not is_audio_content_type(media.content_type):
            raise TranscriptionError("only validated audio can be transcribed")
        # Re-check the stricter audio limit at the paid-provider boundary.  A
        # test double or future fetch adapter must not be able to bypass the
        # fetcher's content-aware cap and submit an oversized inference call.
        if not media.data or len(media.data) > MAX_AUDIO_BYTES:
            raise TranscriptionError("audio exceeds the transcription byte boundary")
        if hashlib.sha256(media.data).hexdigest() != media.sha256:
            raise TranscriptionError("audio identity changed before transcription")
        payload = {
            "audio": base64.b64encode(media.data).decode("ascii"),
            "task": "transcribe",
            # Whisper's supported language parameter is ISO-639-1. Jamaican
            # Patois is preserved through context, not mislabeled as a model
            # locale the endpoint does not advertise.
            "language": "en",
            "vad_filter": True,
            "condition_on_previous_text": False,
            "initial_prompt": self._INITIAL_PROMPT,
        }
        url = (
            "https://api.cloudflare.com/client/v4/accounts/"
            f"{self.account_id}/ai/run/{self.model}"
        )
        started_at = self.clock()
        chunks: list[bytes] = []
        total = 0
        with httpx.Client(
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=15.0, pool=5.0),
            follow_redirects=False,
            transport=self.transport,
            trust_env=False,
        ) as client:
            with client.stream(
                "POST",
                url,
                headers={
                    "Authorization": f"Bearer {self.api_token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "User-Agent": "project-lighthouse-transcription/1",
                },
                json=payload,
            ) as response:
                if response.status_code != 200:
                    raise TranscriptionError("transcription provider request failed")
                if response.headers.get("content-encoding", "identity").casefold() not in {
                    "",
                    "identity",
                }:
                    raise TranscriptionError("encoded transcription responses are rejected")
                if (
                    response.headers.get("content-type", "")
                    .split(";", 1)[0]
                    .strip()
                    .casefold()
                    != "application/json"
                ):
                    raise TranscriptionError("transcription provider returned a non-JSON body")
                for chunk in response.iter_bytes():
                    if self.clock() - started_at > TRANSCRIPTION_DEADLINE_SECONDS:
                        raise TranscriptionError("transcription provider exceeded its deadline")
                    total += len(chunk)
                    if total > MAX_PROVIDER_RESPONSE_BYTES:
                        raise TranscriptionError("transcription provider response is too large")
                    chunks.append(chunk)

        try:
            envelope = json.loads(b"".join(chunks))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TranscriptionError("transcription provider JSON is invalid") from exc
        if not isinstance(envelope, dict) or envelope.get("success") is not True:
            raise TranscriptionError("transcription provider reported failure")
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise TranscriptionError("transcription provider result is missing")
        text = result.get("text")
        if not isinstance(text, str) or not text.strip():
            raise TranscriptionError("transcription provider returned no speech")
        text = text.strip()
        if len(text) > MAX_TRANSCRIPT_CHARS:
            raise TranscriptionError("transcription is too large")

        lang: str | None = None
        info = result.get("transcription_info")
        if isinstance(info, dict):
            candidate = info.get("language")
            if isinstance(candidate, str) and _LANG_RE.fullmatch(candidate.strip()):
                lang = candidate.strip().lower()
        return TranscriptionResult(
            text=text,
            lang=lang,
            provider="cloudflare_workers_ai",
            model=self.model,
        )


__all__ = [
    "CloudflareWhisperTranscriber",
    "DEFAULT_CLOUDFLARE_MODEL",
    "DeterministicTranscriber",
    "Transcriber",
    "TranscriptionConfigurationError",
    "TranscriptionError",
    "TranscriptionResult",
]
