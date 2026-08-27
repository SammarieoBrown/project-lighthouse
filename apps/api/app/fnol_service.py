"""First Notice of Loss packets for insured claims (INS-01).

A serializer, not a subsystem. Everything in the packet already exists by the
time a claim is routed to an insurer — this assembles it into the shape a
carrier's intake expects and renders it so a human can read the same thing.

**No dollar figure, anywhere.** INS-05 is explicit: category, severity and
evidence only. That is not a gap to be filled in later — a relief programme
telling an insurer what a loss is worth is doing the adjuster's job and
creating a number the household will be held to. The Damage Assessment Agent's
estimate exists and is deliberately not read here, and the packet says so, so
that nobody reading it later assumes the omission was an oversight.

**Consent is checked at generation, not only at routing.** The routing decision
recorded what was permitted when it was made; this asks again whether the
permission still stands, because a packet is generated at the moment it is
about to leave. A household that withdrew consent yesterday does not get a
packet built today on the strength of last week's decision.
"""

from __future__ import annotations

import io
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from lighthouse_contracts import ClaimStatus, PayerRoute

from app.models import (
    Claim,
    RoutingDecision,
    StormFile,
    Verification,
)
from app.routing_service import active_insurer_consent

#: The one place a carrier name appears in anything a judge can see is a
#: fictional one (PRD closed question 2). Real carriers belong in market
#: sizing, never in demo output that could imply an endorsement.
PACKET_VERSION = "lighthouse-fnol-v1"

_INSURED_ROUTES = (PayerRoute.INSURER, PayerRoute.BOTH)


class FnolServiceError(RuntimeError):
    """Base class for safe, non-PII FNOL failures."""


class ClaimNotFound(FnolServiceError):
    pass


class FnolNotAvailable(FnolServiceError):
    pass


@dataclass(frozen=True, slots=True)
class FnolPacket:
    claim_id: uuid.UUID
    insurer_name: str
    content: dict


def _observed_hazard(session: Session, claim_id: uuid.UUID) -> dict:
    """Peak wind actually experienced at the household's point.

    Read from advisories marked ``observed`` — the post-season best track,
    what happened rather than what was forecast — by asking which wind field
    contains the point. Rainfall is named and reported unavailable rather than
    omitted: an insurer reading a packet with no rainfall key cannot tell
    whether it did not rain or whether we do not measure it, and we do not
    measure it.
    """
    row = session.execute(
        text(
            """
            SELECT max(
                     CASE
                       WHEN a.wind_field_64 IS NOT NULL
                            AND ST_Intersects(a.wind_field_64, loc.point) THEN 64
                       WHEN a.wind_field_50 IS NOT NULL
                            AND ST_Intersects(a.wind_field_50, loc.point) THEN 50
                       WHEN a.wind_field_34 IS NOT NULL
                            AND ST_Intersects(a.wind_field_34, loc.point) THEN 34
                     END
                   ) AS peak_kt,
                   count(a.id) AS observed_advisories
              FROM claim c
              JOIN storm_file sf ON sf.id = c.storm_file_id
              JOIN LATERAL (
                     SELECT COALESCE(c.location, sf.location) AS point
                   ) loc ON true
              JOIN advisory a ON a.hazard_event_id = c.hazard_event_id
             WHERE c.id = :claim_id
               AND a.observed
               AND loc.point IS NOT NULL
            """
        ),
        {"claim_id": claim_id},
    ).one_or_none()
    peak = row.peak_kt if row is not None else None
    return {
        "peak_sustained_wind_kt": peak,
        "peak_wind_basis": (
            "observed best-track wind field containing the property"
            if peak is not None
            else "no observed advisory covers this property"
        ),
        # Named, not omitted. See the docstring.
        "rainfall_mm": None,
        "rainfall_basis": "not measured by this platform",
    }


def _evidence(session: Session, claim_id: uuid.UUID) -> list[dict]:
    """Evidence by reference, never by content.

    The packet names what exists and its digest. It does not embed photographs
    — an insurer receives the household's images through an authenticated
    fetch they are entitled to make, not as a blob attached to a document that
    may be forwarded onward.
    """
    rows = session.execute(
        text(
            """
            SELECT id, kind::text AS kind, sha256, created_at,
                   payload ->> 'content_type' AS content_type
              FROM evidence
             WHERE claim_id = :claim_id
             ORDER BY created_at, id
            """
        ),
        {"claim_id": claim_id},
    ).all()
    return [
        {
            "evidence_id": str(row.id),
            "kind": row.kind,
            "content_type": row.content_type,
            "sha256": row.sha256,
            "captured_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


def build_fnol(session: Session, claim_id: uuid.UUID) -> FnolPacket:
    """Compile the packet for one insured, verified, consented claim."""
    claim = session.get(Claim, claim_id)
    if claim is None:
        raise ClaimNotFound("claim does not exist")
    if claim.status not in {ClaimStatus.VERIFIED, ClaimStatus.SETTLED}:
        raise FnolNotAvailable("claim is not verified")
    storm_file = session.get(StormFile, claim.storm_file_id)
    if storm_file is None:
        raise ClaimNotFound("claim Storm File does not exist")

    decision = session.scalar(
        select(RoutingDecision)
        .where(RoutingDecision.claim_id == claim.id)
        .order_by(RoutingDecision.decided_at.desc(), RoutingDecision.id.desc())
        .limit(1)
    )
    if decision is None or decision.route not in _INSURED_ROUTES:
        raise FnolNotAvailable("claim is not routed to an insurer")
    if not decision.insurer_name:
        raise FnolNotAvailable("routing decision names no insurer")
    if active_insurer_consent(session, storm_file.id) is None:
        # Asked again at the moment the packet is about to leave.
        raise FnolNotAvailable("insurer-sharing consent is not currently active")

    verification = session.scalar(
        select(Verification)
        .where(Verification.claim_id == claim.id)
        .order_by(Verification.created_at.desc(), Verification.id.desc())
        .limit(1)
    )
    if verification is None:
        raise FnolNotAvailable("claim has no verification to attest")

    location = session.execute(
        text(
            "SELECT ST_Y(point::geometry) AS lat, ST_X(point::geometry) AS lon"
            " FROM (SELECT COALESCE(:claim_loc, sf.location) AS point"
            "         FROM storm_file sf WHERE sf.id = :sf_id) p"
        ),
        {"claim_loc": None, "sf_id": storm_file.id},
    ).one_or_none()

    content = {
        "packet_version": PACKET_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "insurer_name": decision.insurer_name,
        "claim": {
            "claim_id": str(claim.id),
            "claim_ref": claim.claim_ref,
            "filed_at": claim.filed_at.isoformat() if claim.filed_at else None,
            "verified_at": claim.verified_at.isoformat() if claim.verified_at else None,
            "damage_category": claim.damage_type,
            "severity": str(claim.severity) if claim.severity else None,
            "reported_needs": list(claim.reported_needs or []),
        },
        "policyholder": {
            "name": storm_file.head_name,
            "contact_phone": storm_file.phone,
            "storm_file_id": str(storm_file.id),
        },
        "property": {
            "parish": storm_file.parish,
            "community": storm_file.community,
            "latitude": location.lat if location else None,
            "longitude": location.lon if location else None,
            "structure": dict(storm_file.structure or {}),
        },
        "event": {
            "hazard_event_id": str(claim.hazard_event_id),
            "observed_hazard": _observed_hazard(session, claim.id),
        },
        "verification": {
            "verification_id": str(verification.id),
            "verdict": str(verification.verdict),
            "confidence": float(verification.confidence),
            "signals": dict(verification.signals or {}),
            "snapshot_hash": verification.snapshot_hash,
        },
        "evidence": _evidence(session, claim.id),
        "consent": dict(decision.consent_snapshot or {}),
        # Stated rather than omitted, so a reader knows the absence is a rule.
        "monetary_estimate": None,
        "monetary_estimate_basis": (
            "withheld by policy (INS-05): this platform emits damage category, "
            "severity and evidence, and does not value a loss"
        ),
    }
    return FnolPacket(
        claim_id=claim.id, insurer_name=decision.insurer_name, content=content
    )


def render_pdf(packet: FnolPacket) -> bytes:
    """The same packet, readable by a person (INS-01).

    Deliberately plain. This is a document an adjuster skims and a household
    may be shown, so it is laid out as labelled facts rather than styled — and
    the withheld-valuation line is printed rather than dropped, for the same
    reason it is in the JSON.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    y = height - 25 * mm

    def line(label: str, value: object = None, *, size: int = 10, gap: float = 6) -> None:
        nonlocal y
        if y < 25 * mm:
            pdf.showPage()
            y = height - 25 * mm
        pdf.setFont("Helvetica-Bold" if value is None else "Helvetica", size)
        text_line = label if value is None else f"{label}: {value}"
        pdf.drawString(20 * mm, y, text_line[:110])
        y -= gap * mm if value is None else (gap - 1.5) * mm

    body = packet.content
    pdf.setFont("Helvetica-Bold", 15)
    pdf.drawString(20 * mm, y, "First Notice of Loss")
    y -= 8 * mm
    pdf.setFont("Helvetica", 9)
    pdf.drawString(20 * mm, y, f"{body['packet_version']} · generated {body['generated_at']}")
    y -= 10 * mm

    line("Insurer")
    line("Carrier", body["insurer_name"])
    line("Claim")
    line("Reference", body["claim"]["claim_ref"])
    line("Damage category", body["claim"]["damage_category"] or "not stated")
    line("Severity", body["claim"]["severity"] or "not triaged")
    line("Filed", body["claim"]["filed_at"])
    line("Policyholder")
    line("Name", body["policyholder"]["name"] or "not recorded")
    line("Contact", body["policyholder"]["contact_phone"] or "not recorded")
    line("Property")
    line("Parish", body["property"]["parish"] or "not recorded")
    line("Community", body["property"]["community"] or "not recorded")
    line("Structure", ", ".join(f"{k}={v}" for k, v in body["property"]["structure"].items()))
    line("Observed hazard")
    hazard = body["event"]["observed_hazard"]
    line("Peak sustained wind", f"{hazard['peak_sustained_wind_kt'] or 'unavailable'} kt")
    line("Basis", hazard["peak_wind_basis"])
    line("Rainfall", hazard["rainfall_basis"])
    line("Verification")
    line("Verdict", body["verification"]["verdict"])
    line("Confidence", f"{body['verification']['confidence']:.2f}")
    line("Snapshot hash", body["verification"]["snapshot_hash"])
    line("Evidence")
    for item in body["evidence"]:
        line(item["kind"], item["sha256"])
    line("Loss valuation")
    line("Amount", "not provided")
    line("Reason", body["monetary_estimate_basis"])

    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


__all__ = [
    "PACKET_VERSION",
    "ClaimNotFound",
    "FnolNotAvailable",
    "FnolPacket",
    "FnolServiceError",
    "build_fnol",
    "render_pdf",
]
