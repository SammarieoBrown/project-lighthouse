"""Agent I/O contracts — the Phase 0 freeze.

Every agent is a pure function: job in, structured result out. The orchestrator
writes the ledger entry and performs the state transition; the agent never does.
Agents never write money rows.

These models do double duty. They are the Python interface between the
orchestrator and each agent, **and** the JSON schema handed to the model for
structured output. That is deliberate: the contract and the prompt cannot drift
apart if they are the same object.

Two conventions worth knowing before you add anything here:

1. Output models carry ``rationale``. Every agent explains itself, because
   VER-07 says every verdict is stored raw including the ones humans override,
   and an unexplained verdict is useless as training data next season.
2. Nothing here has a default that hides a failure. If an agent could not
   determine a value, the field is ``None`` and a sibling field says why.
   Silent zeros are how a missing satellite tile becomes a confident verdict.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .enums import (
    AgentName,
    DamageBand,
    DisbursementChannel,
    EvidenceKind,
    PayerRoute,
    Posture,
    ResourceKind,
    Severity,
    Verdict,
)


class Contract(BaseModel):
    """Base for every agent contract. Extra fields are an error, not a shrug."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Point(Contract):
    lon: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)


# ---------------------------------------------------------------------------
# 1. Forecast Sentinel — fully autonomous. Watches feeds, sets posture.
# ---------------------------------------------------------------------------


class ForecastSentinelInput(Contract):
    hazard_event_id: UUID | None = None
    feed_payloads: list[dict] = Field(default_factory=list)
    current_posture: Posture


class ForecastSentinelOutput(Contract):
    """HAZ-03. Posture changes are ledger events and notify the Director."""

    posture: Posture
    posture_changed: bool
    affected_parishes: list[str] = Field(default_factory=list)
    advisory_number: str | None = None
    escalate_to_human: bool = False
    rationale: str


# ---------------------------------------------------------------------------
# 2. Risk Mapper — autonomous. Hazard x exposure -> per-household assessment.
# ---------------------------------------------------------------------------


class RiskAssessmentOut(Contract):
    storm_file_id: UUID
    p34: float | None = Field(default=None, ge=0, le=1)
    p50: float | None = Field(default=None, ge=0, le=1)
    p64: float | None = Field(default=None, ge=0, le=1)
    predicted_band: DamageBand
    confidence: float = Field(ge=0, le=1)


class RiskMapperInput(Contract):
    advisory_id: UUID
    hazard_event_id: UUID
    storm_file_ids: list[UUID]


class RiskMapperOutput(Contract):
    """IMP-01. v1 is a transparent parametric lookup, and ``method`` says so.

    ``method`` and ``model_version`` are stored on every assessment so a
    prediction can always be explained and the learning loop (IMP-04) has
    provenance. Do not let this become a black box without changing these.
    """

    assessments: list[RiskAssessmentOut]
    method: str = "parametric_lookup_v1"
    model_version: str
    at_risk_storm_file_ids: list[UUID] = Field(default_factory=list)
    rationale: str


# ---------------------------------------------------------------------------
# 3. Alert Agent — PROPOSE ONLY. Nothing sends without gate G1.
# ---------------------------------------------------------------------------


class AlertDraft(Contract):
    parish: str
    community: str | None = None
    recipient_count: int = Field(ge=0)
    text_en: str
    text_patois: str
    voice_script_patois: str | None = None
    nearest_shelter: str | None = None
    preparation_steps: list[str] = Field(default_factory=list)


class AlertAgentInput(Contract):
    hazard_event_id: UUID
    posture: Posture
    assessments: list[RiskAssessmentOut]


class AlertAgentOutput(Contract):
    """ALT-01. ``requires_approval`` is always True and is not negotiable.

    It is stated as a field rather than assumed so that any code path which
    ignores it is visibly wrong in review.
    """

    drafts: list[AlertDraft]
    requires_approval: bool = True
    rationale: str


# ---------------------------------------------------------------------------
# 4. Intake Agent — autonomous, tool-using, multi-turn.
# ---------------------------------------------------------------------------


class InboundMessage(Contract):
    from_phone: str
    channel: str  # whatsapp | sms | web | kiosk
    text: str | None = None
    media_urls: list[str] = Field(default_factory=list)
    location: Point | None = None
    received_at: datetime


class ExtractedFields(Contract):
    """INT-02. Every field is optional — that is the point.

    A trembling voice note at 3am is missing most of this, and an agent that
    invents values to fill the shape is worse than one that admits the gap.
    Missing fields drive the adaptive follow-ups (INT-03).
    """

    damage_type: str | None = None
    reported_needs: list[str] = Field(default_factory=list)
    household_size: int | None = Field(default=None, ge=0)
    injuries: bool | None = None
    medical_needs: list[str] = Field(default_factory=list)
    location: Point | None = None
    location_description: str | None = None


class EvidenceOut(Contract):
    kind: EvidenceKind
    uri: str | None = None
    payload: dict = Field(default_factory=dict)
    sha256: str | None = None
    phash: str | None = None


class IntakeAgentInput(Contract):
    message: InboundMessage
    storm_file_id: UUID | None = None
    open_claim_id: UUID | None = None
    hazard_event_id: UUID
    follow_ups_so_far: int = Field(default=0, ge=0)


class IntakeAgentOutput(Contract):
    """INT-02/03/04.

    ``safety_of_life`` short-circuits everything: it pages the on-duty human and
    rides the queue at priority 100. It changes ordering, never authority — an
    SOL claim still gets verified and still needs a signature before money moves.
    """

    transcript: str | None = None
    transcript_alt: str | None = None
    lang: str | None = None
    extracted: ExtractedFields
    evidence: list[EvidenceOut] = Field(default_factory=list)

    safety_of_life: bool = False
    sol_keywords: list[str] = Field(default_factory=list)

    file_claim: bool
    partial: bool = False
    follow_up_question: str | None = None
    reply_to_household: str
    rationale: str


# ---------------------------------------------------------------------------
# 5. Verification Agent — confidence gated.
# ---------------------------------------------------------------------------


class Signal(Contract):
    """One of the five independent signals (VER-01).

    ``present=False`` means the signal could not be computed — no satellite tile,
    a thin SMS claim with no media. That is different from a signal that ran and
    scored zero, and VER-04 depends on the difference: absent signals redistribute
    weight and cap the maximum confidence at 0.80, forcing human review rather
    than letting thin evidence auto-verify.
    """

    present: bool
    score: float | None = Field(default=None, ge=0, le=1)
    evidence: dict = Field(default_factory=dict)
    note: str | None = None


class VerificationSignals(Contract):
    hazard_sufficiency: Signal
    satellite_change: Signal
    neighbour_corroboration: Signal
    registry_match: Signal
    media_integrity: Signal


class VerificationAgentInput(Contract):
    claim_id: UUID
    storm_file_id: UUID
    hazard_event_id: UUID


class VerificationAgentOutput(Contract):
    """VER-01/02/04.

    Thresholds: >= 0.85 auto-verify, 0.50-0.85 human review, < 0.50 flagged.
    They live in config, not here, and every change is a ledger entry — which is
    why ``threshold_version`` is stamped on the output.
    """

    signals: VerificationSignals
    confidence: float = Field(ge=0, le=1)
    verdict: Verdict
    capped: bool = False
    missing_signals: list[str] = Field(default_factory=list)
    model_version: str
    threshold_version: str
    rationale: str


# ---------------------------------------------------------------------------
# 6. Triage Agent — autonomous, holds no transition authority.
# ---------------------------------------------------------------------------


class TriageAgentInput(Contract):
    claim_id: UUID
    verification_confidence: float = Field(ge=0, le=1)
    extracted: ExtractedFields
    vuln_score: int | None = Field(default=None, ge=0, le=100)
    safety_of_life: bool = False


class TriageAgentOutput(Contract):
    """TRI-01. Medical urgency first, then habitability, then property;
    vulnerability score breaks ties. SOL pins to the top regardless."""

    severity: Severity
    rank: int = Field(ge=0)
    drivers: list[str] = Field(default_factory=list)
    rationale: str


# ---------------------------------------------------------------------------
# 7. Logistics Agent — PROPOSE ONLY. Nothing moves without gate G2.
# ---------------------------------------------------------------------------


class AllocationDraft(Contract):
    claim_id: UUID
    resource: ResourceKind
    sku: str | None = None
    quantity: int | None = Field(default=None, ge=0)
    amount: float | None = Field(default=None, ge=0)
    payer_route: PayerRoute
    pool_id: UUID | None = None
    warehouse_id: UUID | None = None


class RunSheetStop(Contract):
    claim_id: UUID
    community: str | None = None
    items: list[str] = Field(default_factory=list)


class RunSheet(Contract):
    label: str
    warehouse_id: UUID | None = None
    stops: list[RunSheetStop] = Field(default_factory=list)


class LogisticsAgentInput(Contract):
    hazard_event_id: UUID
    verified_claim_ids: list[UUID]


class LogisticsAgentOutput(Contract):
    """LGX-02. Plans execute only after Director approval."""

    allocations: list[AllocationDraft]
    run_sheets: list[RunSheet] = Field(default_factory=list)
    unmet_needs: list[str] = Field(default_factory=list)
    requires_approval: bool = True
    rationale: str


# ---------------------------------------------------------------------------
# 8. Damage Assessment Agent — PROPOSE ONLY. Never auto-transitions anything.
#
# A dollar figure is money-adjacent by nature, so unlike Verification there is
# no confidence-gated fast path: every proposal waits for a Director, always.
# ---------------------------------------------------------------------------


class DamagePhotoFinding(Contract):
    evidence_id: UUID
    observed_damage: str
    band: DamageBand
    confidence: float = Field(ge=0, le=1)


class DamageAssessmentInput(Contract):
    claim_id: UUID
    storm_file_id: UUID
    evidence_ids: list[UUID]


class DamageAssessmentOutput(Contract):
    """A range, not a single figure — a photo-based estimate is inherently
    uncertain, and a range gives the Director something honest to review
    rather than false precision."""

    band: DamageBand
    estimate_low: float = Field(ge=0)
    estimate_high: float = Field(ge=0)
    currency: str = "JMD"
    confidence: float = Field(ge=0, le=1)
    findings: list[DamagePhotoFinding]
    location_source: Literal["claim", "storm_file"]
    model_version: str
    rationale: str


# ---------------------------------------------------------------------------
# 9. Ledger Agent — autonomous audit. Records and reconciles; flags to humans.
# ---------------------------------------------------------------------------


class AnomalyFlag(Contract):
    kind: str  # DUPLICATE | ORPHAN | AMOUNT_MISMATCH | UNCONFIRMED
    subject_type: str
    subject_id: UUID
    detail: str


class LedgerAgentInput(Contract):
    hazard_event_id: UUID
    since: datetime | None = None
    channel: DisbursementChannel | None = None


class LedgerAgentOutput(Contract):
    """LGR-04. Flags go to humans; the agent never resolves its own findings."""

    reconciled_count: int = Field(ge=0)
    anomalies: list[AnomalyFlag] = Field(default_factory=list)
    chain_valid: bool
    rationale: str


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

AGENT_IO: dict[AgentName, tuple[type[Contract], type[Contract]]] = {
    AgentName.FORECAST_SENTINEL: (ForecastSentinelInput, ForecastSentinelOutput),
    AgentName.RISK_MAPPER: (RiskMapperInput, RiskMapperOutput),
    AgentName.ALERT_AGENT: (AlertAgentInput, AlertAgentOutput),
    AgentName.INTAKE_AGENT: (IntakeAgentInput, IntakeAgentOutput),
    AgentName.VERIFICATION_AGENT: (VerificationAgentInput, VerificationAgentOutput),
    AgentName.TRIAGE_AGENT: (TriageAgentInput, TriageAgentOutput),
    AgentName.LOGISTICS_AGENT: (LogisticsAgentInput, LogisticsAgentOutput),
    AgentName.DAMAGE_ASSESSMENT_AGENT: (
        DamageAssessmentInput,
        DamageAssessmentOutput,
    ),
    AgentName.LEDGER_AGENT: (LedgerAgentInput, LedgerAgentOutput),
}

#: Agents that may only propose. Their outputs carry ``requires_approval`` and
#: the orchestrator refuses to act on them without a matching approval row.
PROPOSE_ONLY: frozenset[AgentName] = frozenset(
    {
        AgentName.ALERT_AGENT,
        AgentName.LOGISTICS_AGENT,
        AgentName.DAMAGE_ASSESSMENT_AGENT,
    }
)
