"""Agent handlers.

Importing this package registers every handler with the worker. Agents propose;
they never dispose. Nothing in here writes an approval or moves money — those
paths require a human signature and the database enforces it.
"""

from app.agents import (  # noqa: F401  — register handlers
    damage_assessment_agent,
    forecast_sentinel,
    intake_agent,
    logistics_agent,
    risk_mapper,
    triage_agent,
    verification_agent,
)

__all__ = [
    "damage_assessment_agent",
    "forecast_sentinel",
    "intake_agent",
    "logistics_agent",
    "risk_mapper",
    "triage_agent",
    "verification_agent",
]
