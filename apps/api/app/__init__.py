"""Lighthouse API — modular monolith, two entrypoints.

``app.web``    FastAPI, run with ``uvicorn app.web:app``     (Render web service)
``app.worker`` job loop, run with ``python -m app.worker``   (Render background worker)

Same codebase, same models, same contracts. The split is deployment, not design.
"""

__version__ = "0.1.0"
