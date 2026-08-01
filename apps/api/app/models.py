"""SQLAlchemy models.

These mirror ``packages/contracts/schema.sql``. The SQL file is canonical: it is
what the migration applies and what the database enforces. These classes exist
so application code has types, not so they can define the schema — never add a
column here without adding it there first.

Enum columns use ``native_enum`` types that already exist in the database, so
they are declared with ``create_type=False``: the migration made them, not us.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from geoalchemy2 import Geography
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from lighthouse_contracts import (
    ActorKind,
    AppRole,
    ClaimStatus,
    DisbursementChannel,
    DisbursementStatus,
    GateKind,
    JobStatus,
    PayerRoute,
    Posture,
    ResourceKind,
    Severity,
    StormFileState,
    Verdict,
)


class Base(DeclarativeBase):
    pass


def _enum(py_enum, name: str) -> SAEnum:
    """Bind to a Postgres enum the migration already created."""
    return SAEnum(
        py_enum,
        name=name,
        create_type=False,
        native_enum=True,
        values_callable=lambda e: [m.value for m in e],
    )


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


TS = DateTime(timezone=True)


class AppUser(Base):
    __tablename__ = "app_user"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(Text, unique=True)
    display_name: Mapped[str] = mapped_column(Text)
    role: Mapped[AppRole] = mapped_column(_enum(AppRole, "app_role"))
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    webauthn_cred: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class StormFile(Base):
    """One record per household. The atomic object of the whole system."""

    __tablename__ = "storm_file"

    id: Mapped[uuid.UUID] = _uuid_pk()
    phone: Mapped[str | None] = mapped_column(Text, unique=True, nullable=True)
    phone_hash: Mapped[str] = mapped_column(Text, unique=True)
    head_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    location: Mapped[object | None] = mapped_column(
        Geography(geometry_type="POINT", srid=4326), nullable=True
    )
    parish: Mapped[str | None] = mapped_column(Text, nullable=True)
    community: Mapped[str | None] = mapped_column(Text, nullable=True)
    structure: Mapped[dict] = mapped_column(JSONB, default=dict)
    people: Mapped[dict] = mapped_column(JSONB, default=dict)
    vuln_score: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    state: Mapped[StormFileState] = mapped_column(
        _enum(StormFileState, "storm_file_state"), default=StormFileState.REGISTERED
    )
    thin: Mapped[bool] = mapped_column(Boolean, default=False)
    assisted_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("app_user.id"), nullable=True
    )
    synthetic: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())

    claims: Mapped[list[Claim]] = relationship(back_populates="storm_file")


class HazardEvent(Base):
    __tablename__ = "hazard_event"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text)
    external_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_posture: Mapped[Posture] = mapped_column(
        _enum(Posture, "posture"), default=Posture.QUIET
    )
    started_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    replay: Mapped[bool] = mapped_column(Boolean, default=False)


class Claim(Base):
    __tablename__ = "claim"

    id: Mapped[uuid.UUID] = _uuid_pk()
    claim_ref: Mapped[str] = mapped_column(Text, unique=True)
    storm_file_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("storm_file.id", ondelete="CASCADE")
    )
    hazard_event_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("hazard_event.id")
    )
    status: Mapped[ClaimStatus] = mapped_column(
        _enum(ClaimStatus, "claim_status"), default=ClaimStatus.FILED
    )
    damage_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    reported_needs: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    location: Mapped[object | None] = mapped_column(
        Geography(geometry_type="POINT", srid=4326), nullable=True
    )
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript_alt: Mapped[str | None] = mapped_column(Text, nullable=True)
    lang: Mapped[str | None] = mapped_column(Text, nullable=True)
    channel: Mapped[str] = mapped_column(Text)
    sol: Mapped[bool] = mapped_column(Boolean, default=False)
    partial: Mapped[bool] = mapped_column(Boolean, default=False)
    severity: Mapped[Severity | None] = mapped_column(
        _enum(Severity, "severity"), nullable=True
    )
    triage_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: T2R starts here.
    filed_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    verified_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    #: T2R stops here.
    settled_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    closed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    storm_file: Mapped[StormFile] = relationship(back_populates="claims")


class Verification(Base):
    """Never updated in place. A human override is a new row (VER-07)."""

    __tablename__ = "verification"

    id: Mapped[uuid.UUID] = _uuid_pk()
    claim_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("claim.id", ondelete="CASCADE")
    )
    signals: Mapped[dict] = mapped_column(JSONB)
    confidence: Mapped[float] = mapped_column(Numeric(asdecimal=False))
    verdict: Mapped[Verdict] = mapped_column(_enum(Verdict, "verdict"))
    actor_kind: Mapped[ActorKind] = mapped_column(_enum(ActorKind, "actor_kind"))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("app_user.id"), nullable=True
    )
    agent_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    threshold_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    capped: Mapped[bool] = mapped_column(Boolean, default=False)
    overrides_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("verification.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class Approval(Base):
    """Gates G1/G2/G3. ``reauth_at`` is required — ADM-02."""

    __tablename__ = "approval"

    id: Mapped[uuid.UUID] = _uuid_pk()
    gate: Mapped[GateKind] = mapped_column(_enum(GateKind, "gate_kind"))
    subject_type: Mapped[str] = mapped_column(Text)
    subject_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True))
    approved_by: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("app_user.id")
    )
    role_at_time: Mapped[AppRole] = mapped_column(_enum(AppRole, "app_role"))
    reauth_at: Mapped[datetime] = mapped_column(TS)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class AllocationPlan(Base):
    __tablename__ = "allocation_plan"

    id: Mapped[uuid.UUID] = _uuid_pk()
    hazard_event_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("hazard_event.id")
    )
    proposed_by: Mapped[str] = mapped_column(Text)
    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("approval.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class Allocation(Base):
    __tablename__ = "allocation"

    id: Mapped[uuid.UUID] = _uuid_pk()
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("allocation_plan.id"), nullable=True
    )
    claim_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("claim.id", ondelete="CASCADE")
    )
    resource: Mapped[ResourceKind] = mapped_column(_enum(ResourceKind, "resource_kind"))
    sku: Mapped[str | None] = mapped_column(Text, nullable=True)
    quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String, default="JMD")
    payer_route: Mapped[PayerRoute] = mapped_column(_enum(PayerRoute, "payer_route"))
    pool_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    warehouse_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class DisbursementBatch(Base):
    __tablename__ = "disbursement_batch"

    id: Mapped[uuid.UUID] = _uuid_pk()
    channel: Mapped[DisbursementChannel] = mapped_column(
        _enum(DisbursementChannel, "disbursement_channel")
    )
    total: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("approval.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class Disbursement(Base):
    """``approval_id`` is NOT NULL by design.

    This row cannot exist before a Finance Officer has signed. That is the
    "no code path moves money without a signature" rule, made structural.
    """

    __tablename__ = "disbursement"

    id: Mapped[uuid.UUID] = _uuid_pk()
    allocation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("allocation.id", ondelete="CASCADE")
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("disbursement_batch.id")
    )
    approval_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("approval.id")
    )
    channel: Mapped[DisbursementChannel] = mapped_column(
        _enum(DisbursementChannel, "disbursement_channel")
    )
    status: Mapped[DisbursementStatus] = mapped_column(
        _enum(DisbursementStatus, "disbursement_status"),
        default=DisbursementStatus.PENDING,
    )
    simulated: Mapped[bool] = mapped_column(Boolean, default=True)
    external_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class LedgerEntry(Base):
    """Append-only and hash-chained. The database refuses UPDATE and DELETE."""

    __tablename__ = "ledger_entry"

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), unique=True, default=uuid.uuid4
    )
    prev_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    hash: Mapped[str] = mapped_column(Text, unique=True)
    actor_kind: Mapped[ActorKind] = mapped_column(_enum(ActorKind, "actor_kind"))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("app_user.id"), nullable=True
    )
    agent_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    action: Mapped[str] = mapped_column(Text)
    subject_type: Mapped[str] = mapped_column(Text)
    subject_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    payload_hash: Mapped[str] = mapped_column(Text)
    ts: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class AgentJob(Base):
    """The queue. Claimed with ``FOR UPDATE SKIP LOCKED``."""

    __tablename__ = "agent_job"

    id: Mapped[uuid.UUID] = _uuid_pk()
    job_type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[JobStatus] = mapped_column(
        _enum(JobStatus, "job_status"), default=JobStatus.QUEUED
    )
    priority: Mapped[int] = mapped_column(SmallInteger, default=0)
    attempts: Mapped[int] = mapped_column(SmallInteger, default=0)
    max_attempts: Mapped[int] = mapped_column(SmallInteger, default=5)
    run_after: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    locked_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)


__all__ = [
    "Base",
    "AppUser",
    "StormFile",
    "HazardEvent",
    "Claim",
    "Verification",
    "Approval",
    "AllocationPlan",
    "Allocation",
    "DisbursementBatch",
    "Disbursement",
    "LedgerEntry",
    "AgentJob",
]
