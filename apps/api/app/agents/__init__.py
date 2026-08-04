"""Agent handlers.

Importing this package registers every handler with the worker. Agents propose;
they never dispose. Nothing in here writes an approval or moves money — those
paths require a human signature and the database enforces it.
"""

from app.agents import (  # noqa: F401  — register handlers
    intake_agent,
    risk_mapper,
    verification_agent,
)

__all__ = ["intake_agent", "risk_mapper", "verification_agent"]
