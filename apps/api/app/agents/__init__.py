"""Agent handlers.

Importing this package registers every handler with the worker. Agents propose;
they never dispose. Nothing in here writes an approval or moves money — those
paths require a human signature and the database enforces it.
"""

from app.agents import risk_mapper  # noqa: F401  — import registers the handler

__all__ = ["risk_mapper"]
