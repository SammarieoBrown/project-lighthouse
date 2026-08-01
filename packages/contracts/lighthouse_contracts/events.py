"""Event catalogue — the Phase 0 freeze.

Every state transition emits exactly one event, in the same transaction as the
state change and the ledger entry. Events are how agents chain: the orchestrator
maps an emitted event to the follow-on agent job and enqueues it in that same
transaction (see ``FOLLOW_ON``).

That transactional coupling is the whole reason the job queue is Postgres and
not Redis (PRD 11.6). A Storm File cannot change state while the job that was
supposed to react to it is silently lost.

Event names are stable strings. They appear in ``ledger_entry.action`` and are
read by auditors, so renaming one is a breaking change to the audit record, not
a refactor.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .enums import AgentName, GateKind, Posture, Severity, StormFileState, Verdict


class Event(StrEnum):
    # Hazard
    HAZARD_POSTURE_CHANGED = "hazard.posture_changed"
    HAZARD_ADVISORY_INGESTED = "hazard.advisory_ingested"
    HAZARD_RISK_ASSESSED = "hazard.risk_assessed"

    # Household lifecycle
    HOUSEHOLD_REGISTERED = "household.registered"
    HOUSEHOLD_AT_RISK = "household.at_risk"
    HOUSEHOLD_STOOD_DOWN = "household.stood_down"
    HOUSEHOLD_SETTLED = "household.settled"
    HOUSEHOLD_CONSENT_REVOKED = "household.consent_revoked"

    # Claims
    CLAIM_CREATED = "claim.created"
    CLAIM_VERIFIED = "claim.verified"
    CLAIM_REJECTED = "claim.rejected"
    CLAIM_WITHDRAWN = "claim.withdrawn"
    CLAIM_APPEALED = "claim.appealed"
    CLAIM_TRIAGED = "claim.triaged"
    CLAIM_ROUTED = "claim.routed"
    CLAIM_SOL_RAISED = "claim.sol_raised"

    # Verification
    VERIFICATION_COMPLETED = "verification.completed"
    VERIFICATION_QUEUED_FOR_REVIEW = "verification.queued_for_review"
    THRESHOLD_CHANGED = "verification.threshold_changed"

    # Gates
    ALERT_CASCADE_PROPOSED = "alert.cascade_proposed"
    ALERT_CASCADE_APPROVED = "alert.cascade_approved"
    ALERT_SENT = "alert.sent"
    ALLOCATION_PLAN_PROPOSED = "allocation.plan_proposed"
    ALLOCATION_APPROVED = "allocation.approved"
    DISBURSEMENT_BATCH_SIGNED = "disbursement.batch_signed"

    # Money
    DONATION_RECEIVED = "donation.received"
    DISBURSEMENT_EXECUTED = "disbursement.executed"
    DISBURSEMENT_CONFIRMED = "disbursement.confirmed"
    DISBURSEMENT_FAILED = "disbursement.failed"
    DELIVERY_CONFIRMED = "delivery.confirmed"

    # Insurance
    FNOL_GENERATED = "insurance.fnol_generated"

    # Audit
    ANOMALY_FLAGGED = "ledger.anomaly_flagged"


class Envelope(BaseModel):
    """What every event carries. ``payload`` is event-specific and free-form;
    everything an auditor needs to reconstruct *who did what to what* is here.
    """

    model_config = ConfigDict(extra="forbid")

    event: Event
    subject_type: str
    subject_id: UUID | None = None
    actor_agent: AgentName | None = None
    actor_user_id: UUID | None = None
    payload: dict = Field(default_factory=dict)


# --- Typed payloads for the events where shape matters ---------------------


class PostureChanged(BaseModel):
    model_config = ConfigDict(extra="forbid")
    previous: Posture
    current: Posture
    advisory_number: str | None = None


class StateChanged(BaseModel):
    model_config = ConfigDict(extra="forbid")
    storm_file_id: UUID
    previous: StormFileState | None
    current: StormFileState
    transition: str  # "T4" — the row in transitions.md that authorised this


class ClaimCreated(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_id: UUID
    claim_ref: str
    storm_file_id: UUID
    channel: str
    safety_of_life: bool = False
    partial: bool = False


class VerificationCompleted(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_id: UUID
    verification_id: UUID
    confidence: float
    verdict: Verdict
    capped: bool = False


class ClaimTriaged(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_id: UUID
    severity: Severity
    rank: int


class GateSigned(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gate: GateKind
    approval_id: UUID
    subject_type: str
    subject_id: UUID
    signed_by: UUID


# ---------------------------------------------------------------------------
# Follow-on wiring
# ---------------------------------------------------------------------------

#: Which agent job an event enqueues. This map *is* the orchestration — it is
#: the only place the agent chain is described, so read it top to bottom to see
#: how a voice note becomes a payment.
#:
#: Events that are absent are terminal on purpose. ``ALERT_CASCADE_APPROVED``
#: and ``ALLOCATION_APPROVED`` are deliberately not here: a human gate hands
#: control to an executor, not to another agent.
FOLLOW_ON: dict[Event, AgentName] = {
    Event.HAZARD_ADVISORY_INGESTED: AgentName.RISK_MAPPER,
    Event.HAZARD_RISK_ASSESSED: AgentName.ALERT_AGENT,
    Event.CLAIM_CREATED: AgentName.VERIFICATION_AGENT,
    Event.VERIFICATION_COMPLETED: AgentName.TRIAGE_AGENT,
    Event.CLAIM_TRIAGED: AgentName.LOGISTICS_AGENT,
    Event.DISBURSEMENT_CONFIRMED: AgentName.LEDGER_AGENT,
    Event.DELIVERY_CONFIRMED: AgentName.LEDGER_AGENT,
}

#: SOL claims ride at this priority through the whole queue (INT-04).
SOL_PRIORITY = 100
DEFAULT_PRIORITY = 0
