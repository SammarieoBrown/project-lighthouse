"""Evidence-backed claim verification and Review Clerk adjudication.

The five VER-01 signals in this module are deliberately boring, deterministic
queries over evidence already persisted in Postgres.  A missing tile, pending
media fetch, thin registry row, or absent location is represented as
``present=False``; none of those conditions is converted into a zero or filled
with a model guess.

Agent decisions and human overrides are append-only ``verification`` rows.
The database supplies their snapshot hash and refuses UPDATE/DELETE.  This
service owns the coupled Claim/StormFile transitions so a confidence number on
its own can never move a household to VERIFIED.
"""

from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from lighthouse_contracts import (
    SOL_PRIORITY,
    ActorKind,
    AgentName,
    AppRole,
    ClaimStatus,
    Event,
    StormFileState,
    Verdict,
)
from lighthouse_contracts.agents import (
    Signal,
    VerificationAgentOutput,
    VerificationSignals,
)

from app import ledger, queue, statemachine
from app.config import get_settings
from app.auto_approval_service import AUTO_APPROVAL_JOB_TYPE
from app.damage_assessment_service import has_readable_photo_evidence
from app.routing_service import route_claim
from app.models import AppUser, Claim, StormFile, Verification


MODEL_VERSION = "verification-rules-2026.08.03"
THRESHOLD_VERSION = "verification-thresholds-2026.08.03"

AUTO_VERIFY_THRESHOLD = 0.85
FLAG_THRESHOLD = 0.50
MISSING_SIGNAL_CAP = 0.80
AUTO_MIN_SIGNAL_SCORE = 0.50
AUTO_MIN_HAZARD_SCORE = 0.70
NEIGHBOUR_RADIUS_METRES = 300

SIGNAL_WEIGHTS: dict[str, float] = {
    "hazard_sufficiency": 0.30,
    "satellite_change": 0.20,
    "neighbour_corroboration": 0.20,
    "registry_match": 0.15,
    "media_integrity": 0.15,
}

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PHASH = re.compile(r"^[0-9a-f]{16,128}$")


class VerificationServiceError(RuntimeError):
    """Base class for safe, non-PII verification failures."""


class ClaimNotFound(VerificationServiceError):
    pass


class VerificationNotRunnable(VerificationServiceError):
    pass


class ReviewDecisionConflict(VerificationServiceError):
    pass


@dataclass(frozen=True, slots=True)
class VerificationRun:
    verification: Verification
    output: VerificationAgentOutput
    created: bool
    transitioned: bool


@dataclass(frozen=True, slots=True)
class _Evidence:
    id: uuid.UUID
    kind: str
    uri: str | None
    payload: dict[str, Any]
    sha256: str | None
    phash: str | None


def _all_evidence(session: Session, claim_id: uuid.UUID) -> list[_Evidence]:
    rows = session.execute(
        text(
            """
            SELECT id, kind::text AS kind, uri, payload, sha256, phash
              FROM evidence
             WHERE claim_id = :claim_id
             ORDER BY created_at, id
            """
        ),
        {"claim_id": claim_id},
    ).all()
    return [
        _Evidence(
            id=row.id,
            kind=row.kind,
            uri=row.uri,
            payload=dict(row.payload or {}),
            sha256=row.sha256,
            phash=row.phash,
        )
        for row in rows
    ]


def _finite_score(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    score = float(value)
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        return None
    return score


def _sha256(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalised = value.casefold()
    return normalised if _SHA256.fullmatch(normalised) else None


def _phash(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalised = value.casefold()
    return normalised if _PHASH.fullmatch(normalised) else None


def _absent(note: str, *, evidence: dict[str, Any] | None = None) -> Signal:
    return Signal(present=False, evidence=evidence or {}, note=note)


def _present(score: float, *, evidence: dict[str, Any], note: str) -> Signal:
    return Signal(present=True, score=round(score, 6), evidence=evidence, note=note)


def _structured_signal(
    rows: list[_Evidence],
    *,
    kind: str,
    score_key: str,
) -> tuple[float, dict[str, Any]] | None:
    """Read only a content-addressed, source-labelled derived evidence row."""
    for row in reversed(rows):
        if row.kind != kind:
            continue
        score = _finite_score(row.payload.get(score_key))
        source = row.payload.get("source")
        digest = _sha256(row.sha256 or row.payload.get("artifact_sha256"))
        if score is None or not isinstance(source, str) or not source.strip() or not digest:
            continue
        return score, {
            "evidence_id": str(row.id),
            "source": source.strip()[:120],
            "artifact_sha256": digest,
        }
    return None


def _hazard_signal(
    session: Session,
    claim: Claim,
    storm_file: StormFile,
    evidence_rows: list[_Evidence],
) -> Signal:
    wind_score: float | None = None
    wind_evidence: dict[str, Any] | None = None

    if claim.location is not None or storm_file.location is not None:
        rows = session.execute(
            text(
                """
                WITH target AS (
                  SELECT COALESCE(c.location, sf.location) AS location
                    FROM claim c
                    JOIN storm_file sf ON sf.id = c.storm_file_id
                   WHERE c.id = :claim_id
                )
                SELECT a.id, a.advisory_number,
                       CASE
                         WHEN a.wind_field_64 IS NOT NULL
                          AND ST_Intersects(a.wind_field_64, target.location) THEN 64
                         WHEN a.wind_field_50 IS NOT NULL
                          AND ST_Intersects(a.wind_field_50, target.location) THEN 50
                         WHEN a.wind_field_34 IS NOT NULL
                          AND ST_Intersects(a.wind_field_34, target.location) THEN 34
                         ELSE 0
                       END AS band
                  FROM advisory a
                  CROSS JOIN target
                 WHERE a.hazard_event_id = :event_id
                   AND a.observed
                   AND target.location IS NOT NULL
                   AND (a.wind_field_34 IS NOT NULL
                        OR a.wind_field_50 IS NOT NULL
                        OR a.wind_field_64 IS NOT NULL)
                 ORDER BY a.issued_at, a.id
                """
            ),
            {"claim_id": claim.id, "event_id": claim.hazard_event_id},
        ).all()
        if rows:
            highest_band = max(int(row.band) for row in rows)
            wind_score = {0: 0.0, 34: 0.72, 50: 0.90, 64: 1.0}[highest_band]
            matched = [str(row.id) for row in rows if int(row.band) == highest_band and row.band]
            wind_evidence = {
                "source": "observed_advisory_wind_field",
                "observed_advisories_evaluated": len(rows),
                "highest_wind_band_kt": highest_band or None,
                "matching_advisory_ids": matched[:20],
            }

    structured = _structured_signal(
        evidence_rows,
        kind="HAZARD",
        score_key="sufficiency_score",
    )
    candidates: list[tuple[float, dict[str, Any]]] = []
    if wind_score is not None and wind_evidence is not None:
        candidates.append((wind_score, wind_evidence))
    if structured is not None:
        candidates.append(structured)
    if not candidates:
        reason = (
            "claim has no usable point location"
            if claim.location is None and storm_file.location is None
            else "no observed wind field or content-addressed hazard observation is available"
        )
        return _absent(reason)

    score, selected = max(candidates, key=lambda candidate: candidate[0])
    return _present(
        score,
        evidence={"selected": selected, "source_count": len(candidates)},
        note="observed hazard evidence was evaluated at the claim location",
    )


def _satellite_signal(evidence_rows: list[_Evidence]) -> Signal:
    structured = _structured_signal(
        evidence_rows,
        kind="SATELLITE",
        score_key="change_score",
    )
    if structured is None:
        return _absent(
            "no content-addressed satellite change result is available"
        )
    score, proof = structured
    row = next(row for row in evidence_rows if str(row.id) == proof["evidence_id"])
    cloud_fraction = _finite_score(row.payload.get("cloud_fraction"))
    if cloud_fraction is not None:
        proof["cloud_fraction"] = cloud_fraction
        if cloud_fraction > 0.40:
            return _absent(
                "satellite observation is cloud-obscured",
                evidence=proof,
            )
    return _present(
        score,
        evidence=proof,
        note="persisted before/after change analysis was evaluated",
    )


def _damage_category(value: str | None) -> str | None:
    damage = (value or "").casefold()
    if "roof" in damage:
        return "roof"
    if "flood" in damage or "water" in damage:
        return "flood"
    if "wall" in damage:
        return "wall"
    if "struct" in damage or "destroy" in damage or "house_gone" in damage:
        return "structure"
    return None


def _neighbour_score(count: int) -> float:
    if count <= 0:
        return 0.0
    if count == 1:
        return 0.45
    if count == 2:
        return 0.70
    if count == 3:
        return 0.85
    return 1.0


def _neighbour_signal(
    session: Session,
    claim: Claim,
    storm_file: StormFile,
) -> Signal:
    if claim.location is None and storm_file.location is None:
        return _absent("claim has no usable point location")

    rows = session.execute(
        text(
            """
            WITH target AS (
              SELECT COALESCE(c.location, sf.location) AS location,
                     sf.synthetic AS synthetic
                FROM claim c
                JOIN storm_file sf ON sf.id = c.storm_file_id
               WHERE c.id = :claim_id
            )
            SELECT DISTINCT ON (other.storm_file_id)
                   other.id, other.storm_file_id, other.damage_type,
                   round(ST_Distance(
                     COALESCE(other.location, other_sf.location),
                     target.location
                   )::numeric, 1) AS distance_metres
              FROM claim other
              JOIN storm_file other_sf ON other_sf.id = other.storm_file_id
              CROSS JOIN target
             WHERE other.id <> :claim_id
               AND other.storm_file_id <> :storm_file_id
               AND other.hazard_event_id = :event_id
               AND other.status::text NOT IN ('REJECTED', 'WITHDRAWN')
               AND other_sf.synthetic = target.synthetic
               AND COALESCE(other.location, other_sf.location) IS NOT NULL
               AND ST_DWithin(
                     COALESCE(other.location, other_sf.location),
                     target.location,
                     :radius_metres
                   )
             ORDER BY other.storm_file_id, distance_metres, other.id
            """
        ),
        {
            "claim_id": claim.id,
            "storm_file_id": storm_file.id,
            "event_id": claim.hazard_event_id,
            "radius_metres": NEIGHBOUR_RADIUS_METRES,
        },
    ).all()

    category = _damage_category(claim.damage_type)
    matching = [row for row in rows if _damage_category(row.damage_type) == category]
    if category is None:
        scored_rows = rows
        score = _neighbour_score(len(rows)) * 0.80
    else:
        scored_rows = matching
        score = _neighbour_score(len(matching))
        if not matching and rows:
            # Nearby impact reports support the event at the point, but not the
            # claimed damage category.  Keep that weaker fact visible.
            score = 0.15

    return _present(
        score,
        evidence={
            "radius_metres": NEIGHBOUR_RADIUS_METRES,
            "independent_households": len(rows),
            "matching_damage_households": len(matching),
            "corroborating_claim_ids": [str(row.id) for row in scored_rows[:20]],
            "synthetic_provenance": storm_file.synthetic,
        },
        note="independent same-event household reports were counted spatially",
    )


def _registry_signal(claim: Claim, storm_file: StormFile) -> Signal:
    if storm_file.thin:
        return _absent("thin Storm File has no authoritative structure profile")
    structure = storm_file.structure if isinstance(storm_file.structure, dict) else {}
    category = _damage_category(claim.damage_type)
    if category is None:
        return _absent("reported damage type cannot be compared to the registry")

    component: str | None = None
    score: float | None = None
    if category == "roof" and structure.get("roof"):
        component, score = "roof", 0.90
    elif category == "wall" and structure.get("walls"):
        component, score = "walls", 0.90
    elif category == "structure" and (structure.get("walls") or structure.get("roof")):
        component, score = "structure", 0.85
    elif category == "flood" and structure.get("floors") is not None:
        component, score = "floors", 0.80

    if component is None or score is None:
        return _absent(
            "registry profile lacks the component needed for this comparison",
            evidence={"damage_category": category},
        )

    return _present(
        score,
        evidence={
            "source": "storm_file_structure_profile",
            "damage_category": category,
            "matched_component": component,
            "registered_value": structure[component]
            if component in structure
            else {
                key: structure[key]
                for key in ("roof", "walls")
                if key in structure
            },
        },
        note="reported damage was compared with the registered structure profile",
    )


def _media_signal(
    session: Session,
    claim: Claim,
    evidence_rows: list[_Evidence],
) -> Signal:
    media = [row for row in evidence_rows if row.kind in {"AUDIO", "PHOTO"}]
    if not media:
        return _absent("claim has no audio or photo evidence")

    ready: list[tuple[_Evidence, str, str | None]] = []
    pending_reasons: list[str] = []
    for row in media:
        digest = _sha256(row.sha256)
        perceptual = _phash(row.phash)
        state = row.payload.get("media_state")
        if state != "STORED" or not (row.uri or "").startswith("r2://") or digest is None:
            pending_reasons.append(f"{row.kind.lower()}_not_content_addressed")
            continue
        if row.kind == "PHOTO" and perceptual is None:
            pending_reasons.append("photo_missing_perceptual_hash")
            continue
        ready.append((row, digest, perceptual))

    if pending_reasons or len(ready) != len(media):
        return _absent(
            "one or more media objects have not completed integrity processing",
            evidence={
                "media_count": len(media),
                "ready_count": len(ready),
                "pending_reasons": sorted(set(pending_reasons)),
            },
        )

    digests = [digest for _, digest, _ in ready]
    phashes = [perceptual for _, _, perceptual in ready if perceptual is not None]
    collisions = session.execute(
        text(
            """
            SELECT id, claim_id, sha256, phash
              FROM evidence
             WHERE claim_id <> :claim_id
               AND kind::text IN ('AUDIO', 'PHOTO')
               AND (
                 sha256 = ANY(CAST(:digests AS text[]))
                 OR phash = ANY(CAST(:phashes AS text[]))
               )
             ORDER BY claim_id, id
            """
        ),
        {
            "claim_id": claim.id,
            "digests": digests,
            "phashes": phashes,
        },
    ).all()
    exact_claims = sorted(
        {str(row.claim_id) for row in collisions if _sha256(row.sha256) in digests}
    )
    perceptual_claims = sorted(
        {str(row.claim_id) for row in collisions if _phash(row.phash) in phashes}
    )

    if exact_claims:
        score = 0.0
        note = "exact media content appears on another claim"
    elif perceptual_claims:
        score = 0.10
        note = "photo perceptual hash appears on another claim"
    else:
        # Exact hashing is meaningful for audio; a decoded perceptual hash adds
        # the reuse check photos require.  This is not presented as a forensic
        # manipulation detector, so the deterministic v1 score stays below 1.
        score = 0.85 if any(row.kind == "PHOTO" for row, _, _ in ready) else 0.80
        note = "all media is private, content-addressed, and unique in the evidence store"

    integrity_scores: list[float] = []
    manipulation_detected = False
    for row, _, _ in ready:
        integrity = row.payload.get("integrity")
        if not isinstance(integrity, dict):
            continue
        candidate = _finite_score(integrity.get("score"))
        processor = integrity.get("processor")
        if candidate is not None and isinstance(processor, str) and processor.strip():
            integrity_scores.append(candidate)
        if integrity.get("manipulation_detected") is True:
            manipulation_detected = True
    if manipulation_detected:
        score, note = 0.0, "a persisted integrity processor flagged possible manipulation"
    elif integrity_scores:
        score = min(score, min(integrity_scores))

    return _present(
        score,
        evidence={
            "media_evidence_ids": [str(row.id) for row, _, _ in ready],
            "exact_duplicate_claim_ids": exact_claims[:20],
            "perceptual_duplicate_claim_ids": perceptual_claims[:20],
            "content_hash_count": len(digests),
            "perceptual_hash_count": len(phashes),
            "forensic_processor_count": len(integrity_scores),
        },
        note=note,
    )


def build_verification_output(
    session: Session,
    claim: Claim,
    storm_file: StormFile,
) -> VerificationAgentOutput:
    """Compute the frozen five-signal bundle without writing any state."""
    evidence_rows = _all_evidence(session, claim.id)
    signals = VerificationSignals(
        hazard_sufficiency=_hazard_signal(session, claim, storm_file, evidence_rows),
        satellite_change=_satellite_signal(evidence_rows),
        neighbour_corroboration=_neighbour_signal(session, claim, storm_file),
        registry_match=_registry_signal(claim, storm_file),
        media_integrity=_media_signal(session, claim, evidence_rows),
    )
    signal_map = signals.model_dump(mode="json")
    missing = [name for name in SIGNAL_WEIGHTS if not signal_map[name]["present"]]
    present_weight = sum(
        SIGNAL_WEIGHTS[name] for name in SIGNAL_WEIGHTS if signal_map[name]["present"]
    )
    raw_confidence = (
        sum(
            SIGNAL_WEIGHTS[name] * float(signal_map[name]["score"])
            for name in SIGNAL_WEIGHTS
            if signal_map[name]["present"]
        )
        / present_weight
        if present_weight
        else 0.0
    )
    capped = bool(missing)
    confidence = min(raw_confidence, MISSING_SIGNAL_CAP) if capped else raw_confidence
    confidence = round(confidence, 6)

    scores = [float(signal_map[name]["score"]) for name in SIGNAL_WEIGHTS if not missing]
    hazard_score = signal_map["hazard_sufficiency"].get("score")
    if missing:
        verdict = Verdict.REVIEW
        rationale = (
            f"{len(missing)} of 5 signals are absent; confidence is capped at "
            f"{MISSING_SIGNAL_CAP:.2f} and the claim requires Review Clerk adjudication."
        )
    elif (
        confidence >= AUTO_VERIFY_THRESHOLD
        and float(hazard_score) >= AUTO_MIN_HAZARD_SCORE
        and min(scores) >= AUTO_MIN_SIGNAL_SCORE
    ):
        verdict = Verdict.AUTO_VERIFIED
        rationale = (
            "All five persisted signals are present and meet the automatic "
            f"verification policy at confidence {confidence:.3f}."
        )
    elif confidence < FLAG_THRESHOLD:
        verdict = Verdict.FLAGGED
        rationale = (
            "All five signals ran, but their combined confidence is below the "
            f"{FLAG_THRESHOLD:.2f} flag threshold."
        )
    else:
        verdict = Verdict.REVIEW
        rationale = (
            "All five signals ran, but the automatic verification policy was "
            "not satisfied; Review Clerk adjudication is required."
        )

    return VerificationAgentOutput(
        signals=signals,
        confidence=confidence,
        verdict=verdict,
        capped=capped,
        missing_signals=missing,
        model_version=MODEL_VERSION,
        threshold_version=THRESHOLD_VERSION,
        rationale=rationale,
    )


def _output_from_row(row: Verification) -> VerificationAgentOutput:
    signals = VerificationSignals.model_validate(row.signals)
    missing = [
        name
        for name, value in signals.model_dump(mode="json").items()
        if not value["present"]
    ]
    return VerificationAgentOutput(
        signals=signals,
        confidence=float(row.confidence),
        verdict=row.verdict,
        capped=row.capped,
        missing_signals=missing,
        model_version=row.model_version or "unknown",
        threshold_version=row.threshold_version or "unknown",
        rationale=row.rationale or "No rationale recorded.",
    )


def _same_agent_decision(
    row: Verification,
    output: VerificationAgentOutput,
) -> bool:
    return (
        row.actor_kind is ActorKind.AGENT
        and row.actor_id is None
        and row.agent_name == str(AgentName.VERIFICATION_AGENT)
        and row.signals == output.signals.model_dump(mode="json")
        and math.isclose(float(row.confidence), output.confidence, abs_tol=1e-6)
        and row.verdict is output.verdict
        and row.capped is output.capped
        and row.model_version == output.model_version
        and row.threshold_version == output.threshold_version
        and row.rationale == output.rationale
    )


def _append_completed(
    session: Session,
    verification: Verification,
    output: VerificationAgentOutput,
    *,
    actor_kind: ActorKind,
    actor_id: uuid.UUID | None = None,
) -> None:
    ledger.append(
        session,
        action=str(Event.VERIFICATION_COMPLETED),
        subject_type="verification",
        subject_id=verification.id,
        payload={
            "claim_id": str(verification.claim_id),
            "verification_id": str(verification.id),
            "snapshot_hash": verification.snapshot_hash,
            "confidence": output.confidence,
            "verdict": str(output.verdict),
            "capped": output.capped,
            "missing_signals": output.missing_signals,
            "model_version": output.model_version,
            "threshold_version": output.threshold_version,
        },
        actor_kind=actor_kind,
        actor_id=actor_id,
        agent=(
            AgentName.VERIFICATION_AGENT
            if actor_kind is ActorKind.AGENT
            else None
        ),
    )


def _enqueue_triage(
    session: Session,
    claim: Claim,
    storm_file: StormFile,
    verification: Verification,
) -> None:
    queue.enqueue(
        session,
        job_type=AgentName.TRIAGE_AGENT,
        priority=SOL_PRIORITY if claim.sol else 0,
        payload={
            "claim_id": str(claim.id),
            "storm_file_id": str(storm_file.id),
            "verification_id": str(verification.id),
            "verification_confidence": float(verification.confidence),
            "sol": claim.sol,
        },
    )


def _enqueue_damage_assessment(session: Session, claim: Claim, storm_file: StormFile) -> None:
    """Only when a paid vision call could actually succeed.

    Two gates, and both of them are quiet rather than loud. A disabled
    provider and a claim with no decodable photo are ordinary states, not
    incidents — but a job enqueued in either one spends its whole retry budget
    failing and then reports itself as an ANOMALY_FLAGGED, which reaches an
    operator as a claim demanding attention rather than as a feature that is
    switched off. The photo gate is ``has_readable_photo_evidence`` rather
    than a second, looser query, so the enqueue side cannot drift from what
    the handler will actually accept.
    """
    assessable = (
        get_settings().damage_assessment_provider != "disabled"
        and has_readable_photo_evidence(session, claim.id)
    )
    if assessable:
        queue.enqueue(
            session,
            job_type=AgentName.DAMAGE_ASSESSMENT_AGENT,
            priority=SOL_PRIORITY if claim.sol else 0,
            payload={
                "claim_id": str(claim.id),
                "storm_file_id": str(storm_file.id),
            },
        )
        return
    # No estimate is coming for this claim. Hand it to the standing
    # authorization now rather than leaving it to wait on a job that will
    # never be enqueued; it will defer to a human, and say why.
    queue.enqueue(
        session,
        job_type=AUTO_APPROVAL_JOB_TYPE,
        priority=SOL_PRIORITY if claim.sol else 0,
        payload={"claim_id": str(claim.id), "trigger": "verification"},
    )


def _transition_approved(
    session: Session,
    claim: Claim,
    storm_file: StormFile,
    verification: Verification,
    *,
    actor_kind: ActorKind,
    actor_id: uuid.UUID | None = None,
) -> None:
    statemachine.transition_claim(
        session,
        claim,
        ClaimStatus.VERIFIED,
        actor_kind=actor_kind,
        actor_id=actor_id,
        agent=(
            AgentName.VERIFICATION_AGENT
            if actor_kind is ActorKind.AGENT
            else None
        ),
        payload={
            "verification_id": str(verification.id),
            "verification_snapshot_hash": verification.snapshot_hash,
            "confidence": float(verification.confidence),
            "verdict": str(verification.verdict),
        },
    )
    if storm_file.state is StormFileState.AFFECTED:
        statemachine.transition(
            session,
            storm_file,
            StormFileState.VERIFIED,
            actor_kind=actor_kind,
            actor_id=actor_id,
            agent=(
                AgentName.VERIFICATION_AGENT
                if actor_kind is ActorKind.AGENT
                else None
            ),
            enqueue_follow_on=False,
            payload={
                "claim_id": str(claim.id),
                "verification_id": str(verification.id),
                "verification_snapshot_hash": verification.snapshot_hash,
            },
        )
    _enqueue_triage(session, claim, storm_file, verification)
    _enqueue_damage_assessment(session, claim, storm_file)
    # RTE-01: "after verification". Decided inline rather than queued because
    # the answer is a read of consent already on file — there is no external
    # call to defer, and a claim that is verified but not yet routed is a claim
    # nothing downstream can decide who pays for.
    route_claim(session, claim.id)


def run_verification(session: Session, claim_id: uuid.UUID) -> VerificationRun:
    """Evaluate one FILED claim and append exactly one decision per snapshot.

    The Claim row lock serialises duplicate jobs.  An exact replay reuses the
    immutable row; changed persisted evidence creates a new row instead of
    mutating history.
    """
    claim = session.scalar(
        select(Claim).where(Claim.id == claim_id).with_for_update()
    )
    if claim is None:
        raise ClaimNotFound("claim does not exist")
    storm_file = session.get(StormFile, claim.storm_file_id)
    if storm_file is None:
        raise ClaimNotFound("claim Storm File does not exist")

    latest = session.scalar(
        select(Verification)
        .where(Verification.claim_id == claim.id)
        .order_by(Verification.created_at.desc(), Verification.id.desc())
        .limit(1)
    )
    if claim.status is not ClaimStatus.FILED:
        if latest is not None and claim.status in {
            ClaimStatus.VERIFIED,
            ClaimStatus.SETTLED,
            ClaimStatus.REJECTED,
        }:
            return VerificationRun(latest, _output_from_row(latest), False, False)
        raise VerificationNotRunnable("claim is not in a verifiable state")
    if latest is not None and latest.actor_kind is ActorKind.HUMAN:
        raise VerificationNotRunnable("a human adjudication is already latest")

    output = build_verification_output(session, claim, storm_file)
    if latest is not None and _same_agent_decision(latest, output):
        return VerificationRun(latest, output, False, False)

    verification = Verification(
        claim_id=claim.id,
        signals=output.signals.model_dump(mode="json"),
        confidence=output.confidence,
        verdict=output.verdict,
        actor_kind=ActorKind.AGENT,
        actor_id=None,
        agent_name=str(AgentName.VERIFICATION_AGENT),
        model_version=output.model_version,
        threshold_version=output.threshold_version,
        rationale=output.rationale,
        capped=output.capped,
        # PostgreSQL now() is transaction-stable. Explicit wall time preserves
        # append order when a clerk adjudicates in the same transaction in
        # tests/replay, instead of using random UUID order as a false clock.
        created_at=datetime.now(UTC),
    )
    session.add(verification)
    session.flush()
    session.refresh(verification)
    _append_completed(
        session,
        verification,
        output,
        actor_kind=ActorKind.AGENT,
    )

    transitioned = False
    if output.verdict is Verdict.AUTO_VERIFIED:
        if storm_file.state not in {
            StormFileState.AFFECTED,
            StormFileState.VERIFIED,
            StormFileState.SETTLED,
        }:
            raise VerificationNotRunnable(
                "claim Storm File is not in a verifiable lifecycle state"
            )
        _transition_approved(
            session,
            claim,
            storm_file,
            verification,
            actor_kind=ActorKind.AGENT,
        )
        transitioned = True
    else:
        ledger.append(
            session,
            action=str(Event.VERIFICATION_QUEUED_FOR_REVIEW),
            subject_type="claim",
            subject_id=claim.id,
            payload={
                "verification_id": str(verification.id),
                "verdict": str(output.verdict),
                "confidence": output.confidence,
                "missing_signals": output.missing_signals,
            },
            actor_kind=ActorKind.AGENT,
            agent=AgentName.VERIFICATION_AGENT,
        )

    return VerificationRun(verification, output, True, transitioned)


def record_review_decision(
    session: Session,
    *,
    claim_id: uuid.UUID,
    verification_id: uuid.UUID,
    clerk_id: uuid.UUID,
    verdict: Verdict,
    rationale: str,
) -> VerificationRun:
    """Append an APPROVED/REJECTED Review Clerk override.

    ``verification_id`` is an optimistic-concurrency/idempotency hook: it must
    still be the latest agent decision.  An exact retry returns the existing
    override; a conflicting second decision is refused. The authenticated
    Review Clerk route calls this service after checking its short-lived role
    credential; this function still revalidates the persisted clerk authority.
    """
    if verdict not in {Verdict.APPROVED, Verdict.REJECTED}:
        raise ReviewDecisionConflict("review verdict must be APPROVED or REJECTED")
    rationale = rationale.strip()
    if not rationale:
        raise ReviewDecisionConflict("review rationale is required")

    claim = session.scalar(
        select(Claim).where(Claim.id == claim_id).with_for_update()
    )
    if claim is None:
        raise ClaimNotFound("claim does not exist")
    storm_file = session.get(StormFile, claim.storm_file_id)
    clerk = session.get(AppUser, clerk_id)
    if (
        storm_file is None
        or clerk is None
        or not clerk.active
        or clerk.role not in {AppRole.REVIEW_CLERK, AppRole.DIRECTOR}
    ):
        raise ReviewDecisionConflict(
            "active Review Clerk or Director authority is required"
        )

    existing = session.scalars(
        select(Verification)
        .where(Verification.overrides_id == verification_id)
        .order_by(Verification.created_at, Verification.id)
    ).all()
    if existing:
        if len(existing) == 1:
            row = existing[0]
            if (
                row.actor_kind is ActorKind.HUMAN
                and row.actor_id == clerk_id
                and row.verdict is verdict
                and row.rationale == rationale
            ):
                return VerificationRun(row, _output_from_row(row), False, False)
        raise ReviewDecisionConflict("verification already has a different override")

    latest = session.scalar(
        select(Verification)
        .where(Verification.claim_id == claim.id)
        .order_by(Verification.created_at.desc(), Verification.id.desc())
        .limit(1)
    )
    if latest is None or latest.id != verification_id:
        raise ReviewDecisionConflict("review decision is stale")
    if (
        latest.actor_kind is not ActorKind.AGENT
        or latest.verdict not in {Verdict.REVIEW, Verdict.FLAGGED}
        or claim.status is not ClaimStatus.FILED
    ):
        raise ReviewDecisionConflict("verification is not awaiting human review")

    output = _output_from_row(latest).model_copy(
        update={"verdict": verdict, "rationale": rationale}
    )
    override = Verification(
        claim_id=claim.id,
        signals=latest.signals,
        confidence=float(latest.confidence),
        verdict=verdict,
        actor_kind=ActorKind.HUMAN,
        actor_id=clerk.id,
        agent_name=None,
        model_version=latest.model_version,
        threshold_version=latest.threshold_version,
        rationale=rationale,
        capped=latest.capped,
        overrides_id=latest.id,
        created_at=datetime.now(UTC),
    )
    session.add(override)
    session.flush()
    session.refresh(override)
    _append_completed(
        session,
        override,
        output,
        actor_kind=ActorKind.HUMAN,
        actor_id=clerk.id,
    )

    if verdict is Verdict.APPROVED:
        _transition_approved(
            session,
            claim,
            storm_file,
            override,
            actor_kind=ActorKind.HUMAN,
            actor_id=clerk.id,
        )
    else:
        claim.closed_reason = rationale
        statemachine.transition_claim(
            session,
            claim,
            ClaimStatus.REJECTED,
            actor_kind=ActorKind.HUMAN,
            actor_id=clerk.id,
            payload={
                "verification_id": str(override.id),
                "overrides_id": str(latest.id),
                "reason_recorded": True,
            },
        )

    return VerificationRun(override, output, True, True)


__all__ = [
    "AUTO_VERIFY_THRESHOLD",
    "ClaimNotFound",
    "MISSING_SIGNAL_CAP",
    "MODEL_VERSION",
    "ReviewDecisionConflict",
    "THRESHOLD_VERSION",
    "VerificationNotRunnable",
    "VerificationRun",
    "build_verification_output",
    "record_review_decision",
    "run_verification",
]
