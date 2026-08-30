"""Agent handlers.

Importing this package registers every handler with the worker. Agents
propose; humans dispose. One handler — ``auto_approval_agent`` — does write
an approval, and only ever under a Director's standing authorization, whose
ceiling, funding source, and validity the database re-checks on every row. No
agent has authority of its own, and none can exceed the authority it was
lent.
"""

from app.agents import (  # noqa: F401  — register handlers
    alert_agent,
    auto_approval_agent,
    damage_assessment_agent,
    forecast_sentinel,
    intake_agent,
    ledger_agent,
    logistics_agent,
    risk_mapper,
    triage_agent,
    verification_agent,
)

__all__ = [
    "alert_agent",
    "auto_approval_agent",
    "damage_assessment_agent",
    "forecast_sentinel",
    "intake_agent",
    "ledger_agent",
    "logistics_agent",
    "risk_mapper",
    "triage_agent",
    "verification_agent",
]
