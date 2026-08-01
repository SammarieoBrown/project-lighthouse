"""Project Lighthouse — Phase 0 contract freeze.

Four artifacts, frozen together:

* ``schema.sql``      canonical database schema (this package's sibling file)
* ``transitions.md``  the legal state transitions and per-agent authority
* ``agents``          Pydantic I/O contracts for all eight agents
* ``events``          the event catalogue and follow-on agent wiring

All three apps depend on this package. Changing anything here requires team
agreement, because it is the seam that lets three people build in parallel.
"""

from .agents import AGENT_IO, PROPOSE_ONLY
from .enums import (
    ActorKind,
    AgentName,
    AppRole,
    ClaimStatus,
    DamageBand,
    DisbursementChannel,
    DisbursementStatus,
    EvidenceKind,
    GateKind,
    JobStatus,
    PayerRoute,
    Posture,
    ResourceKind,
    Severity,
    StormFileState,
    Verdict,
)
from .events import DEFAULT_PRIORITY, FOLLOW_ON, SOL_PRIORITY, Envelope, Event

__version__ = "0.1.0"

__all__ = [
    "AGENT_IO",
    "PROPOSE_ONLY",
    "ActorKind",
    "AgentName",
    "AppRole",
    "ClaimStatus",
    "DamageBand",
    "DisbursementChannel",
    "DisbursementStatus",
    "Envelope",
    "Event",
    "EvidenceKind",
    "FOLLOW_ON",
    "GateKind",
    "JobStatus",
    "PayerRoute",
    "Posture",
    "ResourceKind",
    "Severity",
    "StormFileState",
    "Verdict",
    "SOL_PRIORITY",
    "DEFAULT_PRIORITY",
    "__version__",
]
