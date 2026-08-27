"""Photo-based damage estimation and Director adjudication.

A dollar figure is money-adjacent by nature (transitions.md), so unlike
verification there is no confidence-gated auto path: every proposal from the
Damage Assessment Agent waits for a Director, always. This module is the
first real use of the "contract doubles as JSON schema" pattern described in
``lighthouse_contracts.agents`` — ``DamageAssessmentOutput`` is both the
Python return type and the schema handed to Claude for structured output.

Location is resolved the same way ``verification_service`` already resolves
it: the claim's own point if set, falling back to the household's registered
Storm File address. WhatsApp photos carry no EXIF/GPS — Meta strips it on
send — so there is no geotag to read, and inventing one would be exactly the
kind of silent zero the agent contracts are written to forbid.
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal, Protocol

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from lighthouse_contracts import (
    ActorKind,
    AgentName,
    AppRole,
    ClaimStatus,
    DamageAssessmentVerdict,
    Event,
)
from lighthouse_contracts.agents import DamageAssessmentOutput, DamagePhotoFinding

from app import ledger
from app.config import get_settings
from app.intake.media import FetchedMedia, MediaBoundaryError, R2MediaStore
from app.models import AppUser, Claim, DamageAssessment, StormFile

MODEL_NAME = "claude-opus-5"
MODEL_VERSION = f"anthropic:{MODEL_NAME}"

#: The programme disburses in Jamaican dollars and nothing else. The model is
#: asked for JMD, but a unit is not a thing we take a model's word for: a
#: silent "USD" here is a 150x error that the immutability trigger would then
#: lock into the record, because an override must copy currency unchanged.
CURRENCY = "JMD"

#: Media types the vision call accepts. HEIC/HEIF is excluded for the same
#: reason the perceptual hash skips it (media.py) — Pillow, and the vision
#: API, both need a directly decodable format.
_SUPPORTED_IMAGE_MEDIA_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp"}
)

_ASSESSMENT_SYSTEM_PROMPT = (
    "You are assessing hurricane damage from household-submitted photos for a "
    "Jamaican disaster relief program. Estimate a realistic repair cost range "
    "in Jamaican dollars (JMD) based only on what is visible in the photos. "
    "Do not invent damage that is not shown, and do not assume damage types "
    "the household did not report. Every entry in `findings` must reference "
    "the exact `evidence_id` given for that photo. If the photos are "
    "inconclusive or show only minor, ambiguous damage, say so in the "
    "rationale and keep confidence low rather than guessing a precise figure."
)


class DamageAssessmentServiceError(RuntimeError):
    """Base class for safe, non-PII damage assessment failures."""


class ClaimNotFound(DamageAssessmentServiceError):
    pass


class DamageAssessmentNotRunnable(DamageAssessmentServiceError):
    pass


class DamageAssessmentProviderDisabled(DamageAssessmentServiceError):
    pass


class ReviewDecisionConflict(DamageAssessmentServiceError):
    pass


@dataclass(frozen=True, slots=True)
class DamageAssessmentRun:
    assessment: DamageAssessment
    output: DamageAssessmentOutput
    created: bool


@dataclass(frozen=True, slots=True)
class _PhotoEvidence:
    id: uuid.UUID
    uri: str
    sha256: str
    content_type: str


def _object_key(uri: str) -> str:
    """``r2://<bucket>/<key>`` -> ``<key>``. The key itself may contain ``/``."""
    _, _, rest = uri.partition("r2://")
    _, _, key = rest.partition("/")
    return key


def _readable_photo_evidence(session: Session, claim_id: uuid.UUID) -> list[_PhotoEvidence]:
    """Only content-addressed, privately-stored PHOTO rows in a decodable format.

    Mirrors the same fields ``verification_service._media_signal`` already
    trusts (``media_state == STORED``, an ``r2://`` URI, a real sha256) rather
    than inventing a second notion of "ready" evidence.
    """
    rows = session.execute(
        text(
            """
            SELECT id, uri, sha256, payload
              FROM evidence
             WHERE claim_id = :claim_id
               AND kind = 'PHOTO'
             ORDER BY created_at, id
            """
        ),
        {"claim_id": claim_id},
    ).all()
    photos: list[_PhotoEvidence] = []
    for row in rows:
        payload = dict(row.payload or {})
        content_type = str(payload.get("content_type") or "")
        uri = str(row.uri or "")
        if (
            payload.get("media_state") != "STORED"
            or not uri.startswith("r2://")
            or not row.sha256
            or content_type not in _SUPPORTED_IMAGE_MEDIA_TYPES
        ):
            continue
        photos.append(
            _PhotoEvidence(id=row.id, uri=uri, sha256=row.sha256, content_type=content_type)
        )
    return photos


def has_readable_photo_evidence(session: Session, claim_id: uuid.UUID) -> bool:
    """The one definition of "there is something here worth a vision call".

    The enqueue side and the handler have to agree on this. When they do not,
    a claim whose only photo is HEIC — or has no digest — passes the looser
    gate, fails every retry against the stricter one, and surfaces to an
    operator as an ANOMALY_FLAGGED naming a claim that needs nothing done.
    """
    return bool(_readable_photo_evidence(session, claim_id))


def _resolve_location_source(
    claim: Claim, storm_file: StormFile
) -> Literal["claim", "storm_file"] | None:
    if claim.location is not None:
        return "claim"
    if storm_file.location is not None:
        return "storm_file"
    return None


def _quantize(value: float) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class PhotoStore(Protocol):
    def get(self, object_key: str, *, expected_sha256: str) -> FetchedMedia: ...


@dataclass
class DeterministicPhotoStore:
    """Test double for ``R2MediaStore.get`` — no network call, no bucket."""

    media_by_key: dict[str, FetchedMedia] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def get(self, object_key: str, *, expected_sha256: str) -> FetchedMedia:
        self.calls.append(object_key)
        media = self.media_by_key.get(object_key)
        if media is None:
            raise MediaBoundaryError(f"no test double media registered for {object_key}")
        return media


class DamageAssessor(Protocol):
    def assess(
        self,
        *,
        claim: Claim,
        photos: list[_PhotoEvidence],
        media: list[FetchedMedia],
    ) -> DamageAssessmentOutput: ...


class ClaudeDamageAssessor:
    """The real provider — a paid vision call, gated fail-closed by config."""

    def __init__(self, *, api_key: str) -> None:
        self.api_key = api_key

    @classmethod
    def from_settings(cls, settings) -> "ClaudeDamageAssessor":
        provider = str(getattr(settings, "damage_assessment_provider", "disabled"))
        api_key = str(getattr(settings, "anthropic_api_key", None) or "")
        if provider != "anthropic" or not api_key:
            raise DamageAssessmentProviderDisabled(
                "damage assessment provider is not configured"
            )
        return cls(api_key=api_key)

    def assess(
        self,
        *,
        claim: Claim,
        photos: list[_PhotoEvidence],
        media: list[FetchedMedia],
    ) -> DamageAssessmentOutput:
        import anthropic

        client = anthropic.Anthropic(api_key=self.api_key)

        content: list[dict] = []
        for photo, fetched in zip(photos, media, strict=True):
            content.append({"type": "text", "text": f"Photo evidence_id: {photo.id}"})
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        # The evidence row's type, not the one R2 echoes back:
                        # ``_readable_photo_evidence`` checked *this* value
                        # against ``_SUPPORTED_IMAGE_MEDIA_TYPES``, and an
                        # object stored without ContentType reads back as "".
                        "media_type": photo.content_type,
                        "data": base64.standard_b64encode(fetched.data).decode("ascii"),
                    },
                }
            )
        content.append(
            {
                "type": "text",
                "text": (
                    f"Reported damage type: {claim.damage_type or 'not stated'}\n"
                    f"Reported needs: {', '.join(claim.reported_needs) or 'none stated'}\n"
                    f"Photo count: {len(media)}"
                ),
            }
        )

        response = client.messages.parse(
            model=MODEL_NAME,
            max_tokens=4096,
            system=_ASSESSMENT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
            output_format=DamageAssessmentOutput,
        )
        parsed = response.parsed_output
        if parsed is None:
            raise DamageAssessmentNotRunnable("model did not return a valid assessment")
        return parsed


@dataclass
class DeterministicDamageAssessor:
    """Test double — same shape as ``DeterministicTranscriber``
    (app/intake/transcription.py): no network call, records what it was asked
    to assess so a test can inspect exactly which photos were sent."""

    output: DamageAssessmentOutput
    calls: list[tuple[uuid.UUID, list[uuid.UUID]]] = field(default_factory=list)

    def assess(
        self,
        *,
        claim: Claim,
        photos: list[_PhotoEvidence],
        media: list[FetchedMedia],
    ) -> DamageAssessmentOutput:
        self.calls.append((claim.id, [photo.id for photo in photos]))
        return self.output


def _validate_findings(
    output: DamageAssessmentOutput, photos: list[_PhotoEvidence]
) -> None:
    """Every finding must ground itself in a photo we actually sent — a model
    (real or fake) that references evidence outside the claim is a bug, not
    something to silently store."""
    known_ids = {photo.id for photo in photos}
    if not output.findings:
        raise DamageAssessmentNotRunnable("assessment returned no photo findings")
    for finding in output.findings:
        if finding.evidence_id not in known_ids:
            raise DamageAssessmentNotRunnable(
                "assessment referenced evidence outside this claim"
            )


def _output_from_row(row: DamageAssessment) -> DamageAssessmentOutput:
    findings = [DamagePhotoFinding.model_validate(item) for item in (row.findings or [])]
    return DamageAssessmentOutput(
        band=row.band,
        estimate_low=float(row.estimate_low),
        estimate_high=float(row.estimate_high),
        currency=row.currency,
        confidence=float(row.confidence),
        findings=findings,
        location_source=row.location_source,  # type: ignore[arg-type]
        model_version=row.model_version or "unknown",
        rationale=row.rationale or "No rationale recorded.",
    )


def _append_ledger(
    session: Session,
    assessment: DamageAssessment,
    output: DamageAssessmentOutput,
    *,
    action: Event,
    actor_kind: ActorKind,
    actor_id: uuid.UUID | None = None,
) -> None:
    ledger.append(
        session,
        action=str(action),
        subject_type="damage_assessment",
        subject_id=assessment.id,
        payload={
            "claim_id": str(assessment.claim_id),
            "damage_assessment_id": str(assessment.id),
            # Without these two a `decided` entry carries a dollar range and no
            # way to tell an approval from a rejection, which is precisely the
            # disposition the ledger exists to remember.
            "verdict": str(assessment.verdict),
            "snapshot_hash": assessment.snapshot_hash,
            "overrides_id": (
                str(assessment.overrides_id) if assessment.overrides_id else None
            ),
            "band": str(output.band),
            "estimate_low": float(output.estimate_low),
            "estimate_high": float(output.estimate_high),
            "currency": output.currency,
            "confidence": output.confidence,
            "location_source": output.location_source,
            "model_version": output.model_version,
        },
        actor_kind=actor_kind,
        actor_id=actor_id,
        agent=(
            AgentName.DAMAGE_ASSESSMENT_AGENT if actor_kind is ActorKind.AGENT else None
        ),
    )


def run_damage_assessment(
    session: Session,
    claim_id: uuid.UUID,
    *,
    assessor: DamageAssessor | None = None,
    store: PhotoStore | None = None,
) -> DamageAssessmentRun:
    """Assess one verified claim's photo evidence and append one proposal.

    Never transitions claim or Storm File state, and never touches allocation
    or disbursement — a dollar figure always waits for a Director. An exact
    replay against the same evidence set reuses the existing row rather than
    spending a second paid vision call. ``assessor``/``store`` are test seams
    (``DeterministicDamageAssessor``/``DeterministicPhotoStore``) — the worker
    path always leaves them unset, which defers provider gating until a real
    call is actually about to happen.
    """
    claim = session.scalar(select(Claim).where(Claim.id == claim_id).with_for_update())
    if claim is None:
        raise ClaimNotFound("claim does not exist")
    storm_file = session.get(StormFile, claim.storm_file_id)
    if storm_file is None:
        raise ClaimNotFound("claim Storm File does not exist")
    if claim.status not in {ClaimStatus.VERIFIED, ClaimStatus.SETTLED}:
        raise DamageAssessmentNotRunnable("claim is not in an assessable state")

    latest = session.scalar(
        select(DamageAssessment)
        .where(DamageAssessment.claim_id == claim.id)
        .order_by(DamageAssessment.created_at.desc(), DamageAssessment.id.desc())
        .limit(1)
    )
    if latest is not None and latest.actor_kind is ActorKind.HUMAN:
        raise DamageAssessmentNotRunnable("a Director decision is already latest")

    photos = _readable_photo_evidence(session, claim.id)
    if not photos:
        raise DamageAssessmentNotRunnable("claim has no readable photo evidence")

    if latest is not None and latest.actor_kind is ActorKind.AGENT:
        latest_evidence_ids = {
            str(finding.get("evidence_id"))
            for finding in (latest.findings or [])
            if isinstance(finding, dict)
        }
        if latest_evidence_ids == {str(photo.id) for photo in photos}:
            return DamageAssessmentRun(latest, _output_from_row(latest), False)

    location_source = _resolve_location_source(claim, storm_file)
    if location_source is None:
        raise DamageAssessmentNotRunnable("claim has no usable point location")

    settings = None
    if assessor is None or store is None:
        settings = get_settings()
    assessor = assessor or ClaudeDamageAssessor.from_settings(settings)
    store = store or R2MediaStore.from_settings(settings)

    try:
        media = [
            store.get(_object_key(photo.uri), expected_sha256=photo.sha256)
            for photo in photos
        ]
    except MediaBoundaryError as exc:
        raise DamageAssessmentNotRunnable(str(exc)) from exc

    parsed = assessor.assess(claim=claim, photos=photos, media=media)
    _validate_findings(parsed, photos)
    output = parsed.model_copy(
        update={
            "location_source": location_source,
            "model_version": MODEL_VERSION,
            "currency": CURRENCY,
        }
    )

    assessment = DamageAssessment(
        claim_id=claim.id,
        storm_file_id=storm_file.id,
        band=output.band,
        estimate_low=_quantize(output.estimate_low),
        estimate_high=_quantize(max(output.estimate_high, output.estimate_low)),
        currency=output.currency,
        confidence=output.confidence,
        findings=[finding.model_dump(mode="json") for finding in output.findings],
        location_source=output.location_source,
        verdict=DamageAssessmentVerdict.PROPOSED,
        actor_kind=ActorKind.AGENT,
        actor_id=None,
        agent_name=str(AgentName.DAMAGE_ASSESSMENT_AGENT),
        model_version=output.model_version,
        rationale=output.rationale,
        created_at=datetime.now(UTC),
    )
    session.add(assessment)
    session.flush()
    session.refresh(assessment)
    _append_ledger(
        session,
        assessment,
        output,
        action=Event.DAMAGE_ASSESSMENT_PROPOSED,
        actor_kind=ActorKind.AGENT,
    )

    return DamageAssessmentRun(assessment, output, True)


def record_damage_assessment_decision(
    session: Session,
    *,
    claim_id: uuid.UUID,
    assessment_id: uuid.UUID,
    director_id: uuid.UUID,
    verdict: Literal["APPROVED", "REJECTED"],
    rationale: str,
    confirmed_low: float | None = None,
    confirmed_high: float | None = None,
) -> DamageAssessmentRun:
    """Append an APPROVED/REJECTED Director override.

    ``assessment_id`` is an optimistic-concurrency/idempotency hook, same
    pattern as ``verification_service.record_review_decision``: it must still
    be the latest agent proposal. The Director may narrow or widen the dollar
    range when approving; every other observed fact about the photos is
    copied unchanged, and the database refuses an override that tries to
    change them (schema.sql, ``damage_assessment_snapshot_guard``).
    """
    if verdict not in {"APPROVED", "REJECTED"}:
        raise ReviewDecisionConflict("decision verdict must be APPROVED or REJECTED")
    rationale = rationale.strip()
    if not rationale:
        raise ReviewDecisionConflict("decision rationale is required")

    claim = session.scalar(select(Claim).where(Claim.id == claim_id).with_for_update())
    if claim is None:
        raise ClaimNotFound("claim does not exist")
    director = session.get(AppUser, director_id)
    if director is None or not director.active or director.role is not AppRole.DIRECTOR:
        raise ReviewDecisionConflict("active Director authority is required")

    existing = session.scalars(
        select(DamageAssessment)
        .where(DamageAssessment.overrides_id == assessment_id)
        .order_by(DamageAssessment.created_at, DamageAssessment.id)
    ).all()
    if existing:
        if len(existing) == 1:
            row = existing[0]
            if (
                row.actor_kind is ActorKind.HUMAN
                and row.actor_id == director_id
                and row.verdict == verdict
                and row.rationale == rationale
            ):
                return DamageAssessmentRun(row, _output_from_row(row), False)
        raise ReviewDecisionConflict("assessment already has a different override")

    latest = session.scalar(
        select(DamageAssessment)
        .where(DamageAssessment.claim_id == claim.id)
        .order_by(DamageAssessment.created_at.desc(), DamageAssessment.id.desc())
        .limit(1)
    )
    if latest is None or latest.id != assessment_id:
        raise ReviewDecisionConflict("decision is stale")
    if (
        latest.actor_kind is not ActorKind.AGENT
        or latest.verdict is not DamageAssessmentVerdict.PROPOSED
    ):
        raise ReviewDecisionConflict("assessment is not awaiting Director decision")

    low = _quantize(confirmed_low) if confirmed_low is not None else latest.estimate_low
    high = _quantize(confirmed_high) if confirmed_high is not None else latest.estimate_high
    if high < low:
        raise ReviewDecisionConflict("confirmed range cannot have high below low")

    override = DamageAssessment(
        claim_id=claim.id,
        storm_file_id=latest.storm_file_id,
        band=latest.band,
        estimate_low=low,
        estimate_high=high,
        currency=latest.currency,
        confidence=float(latest.confidence),
        findings=latest.findings,
        location_source=latest.location_source,
        verdict=DamageAssessmentVerdict(verdict),
        actor_kind=ActorKind.HUMAN,
        actor_id=director.id,
        agent_name=None,
        model_version=latest.model_version,
        rationale=rationale,
        overrides_id=latest.id,
        created_at=datetime.now(UTC),
    )
    session.add(override)
    session.flush()
    session.refresh(override)
    output = _output_from_row(override)
    _append_ledger(
        session,
        override,
        output,
        action=Event.DAMAGE_ASSESSMENT_DECIDED,
        actor_kind=ActorKind.HUMAN,
        actor_id=director.id,
    )

    return DamageAssessmentRun(override, output, True)


__all__ = [
    "CURRENCY",
    "ClaimNotFound",
    "ClaudeDamageAssessor",
    "DamageAssessmentNotRunnable",
    "DamageAssessmentProviderDisabled",
    "DamageAssessmentRun",
    "DamageAssessor",
    "DeterministicDamageAssessor",
    "DeterministicPhotoStore",
    "MODEL_VERSION",
    "PhotoStore",
    "ReviewDecisionConflict",
    "has_readable_photo_evidence",
    "record_damage_assessment_decision",
    "run_damage_assessment",
]
