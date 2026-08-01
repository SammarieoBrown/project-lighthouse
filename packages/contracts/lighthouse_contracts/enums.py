"""Shared enums.

These mirror the PostgreSQL enum types in ``schema.sql`` exactly. If you change
one, change both — the database is the referee, and a mismatch surfaces as an
InvalidTextRepresentation error at the worst possible moment.
"""

from enum import StrEnum


class StormFileState(StrEnum):
    REGISTERED = "REGISTERED"
    AT_RISK = "AT_RISK"
    AFFECTED = "AFFECTED"
    VERIFIED = "VERIFIED"
    SETTLED = "SETTLED"


class ClaimStatus(StrEnum):
    FILED = "FILED"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"
    SETTLED = "SETTLED"


class Posture(StrEnum):
    """National readiness level (HAZ-03)."""

    QUIET = "QUIET"
    WATCH = "WATCH"
    READY = "READY"
    ACT = "ACT"


class DamageBand(StrEnum):
    NONE = "NONE"
    MINOR = "MINOR"
    MAJOR = "MAJOR"
    DESTROYED = "DESTROYED"


class Severity(StrEnum):
    URGENT = "URGENT"
    HIGH = "HIGH"
    MED = "MED"


class EvidenceKind(StrEnum):
    AUDIO = "AUDIO"
    PHOTO = "PHOTO"
    TRANSCRIPT = "TRANSCRIPT"
    SATELLITE = "SATELLITE"
    NEIGHBOUR = "NEIGHBOUR"
    REGISTRY = "REGISTRY"
    HAZARD = "HAZARD"


class Verdict(StrEnum):
    AUTO_VERIFIED = "AUTO_VERIFIED"
    REVIEW = "REVIEW"
    FLAGGED = "FLAGGED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ActorKind(StrEnum):
    AGENT = "AGENT"
    HUMAN = "HUMAN"
    SYSTEM = "SYSTEM"


class PayerRoute(StrEnum):
    """RTE-01 routing outcomes."""

    GOV_RELIEF = "GOV_RELIEF"
    INSURER = "INSURER"
    BOTH = "BOTH"
    DONOR_POOL = "DONOR_POOL"


class ResourceKind(StrEnum):
    CASH = "CASH"
    ITEM = "ITEM"


class DisbursementChannel(StrEnum):
    BANK = "BANK"
    MOBILE_MONEY = "MOBILE_MONEY"
    VOUCHER = "VOUCHER"
    GOODS = "GOODS"


class DisbursementStatus(StrEnum):
    PENDING = "PENDING"
    EXECUTING = "EXECUTING"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"


class GateKind(StrEnum):
    """The three human gates. Nothing else gates."""

    ALERT_CASCADE = "ALERT_CASCADE"
    ALLOCATION_PLAN = "ALLOCATION_PLAN"
    DISBURSEMENT_BATCH = "DISBURSEMENT_BATCH"


class AppRole(StrEnum):
    DIRECTOR = "DIRECTOR"
    REVIEW_CLERK = "REVIEW_CLERK"
    FINANCE_OFFICER = "FINANCE_OFFICER"
    AUDITOR = "AUDITOR"
    ADMIN = "ADMIN"
    PARISH_COORDINATOR = "PARISH_COORDINATOR"
    SHELTER_MANAGER = "SHELTER_MANAGER"
    FIELD_TEAM = "FIELD_TEAM"
    INSURER_USER = "INSURER_USER"


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    DEAD = "DEAD"


class AgentName(StrEnum):
    """The eight agents. Authority per agent is fixed in transitions.md."""

    FORECAST_SENTINEL = "forecast_sentinel"
    RISK_MAPPER = "risk_mapper"
    ALERT_AGENT = "alert_agent"
    INTAKE_AGENT = "intake_agent"
    VERIFICATION_AGENT = "verification_agent"
    TRIAGE_AGENT = "triage_agent"
    LOGISTICS_AGENT = "logistics_agent"
    LEDGER_AGENT = "ledger_agent"
