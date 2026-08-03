"""Inbound claim intake.

The HTTP edge, provider validation, persistence service, and redacted read API
live together here so an inbound report has one auditable path from provider to
claim.  Importing this package does not register worker handlers; the worker
does that through :mod:`app.agents.intake_agent`.
"""

from .router import router

__all__ = ["router"]
