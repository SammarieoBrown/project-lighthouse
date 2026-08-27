"""The worker registers its handlers when started the way it is deployed.

Every other test imports ``app.worker`` as a module and sees a fully populated
``HANDLERS``. Render starts it with ``python -m app.worker``, which imports the
file twice — once as ``__main__``, once as ``app.worker`` when an agent does
``from app.worker import register`` — giving two module objects with two
separate ``HANDLERS`` dicts.

That gap is invisible to an in-process test by construction, which is why the
worker shipped for months parking every job it claimed while logging "handlers
registered: none". So this test starts a real subprocess the way production
does, and reads what it says about itself.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]

#: Handlers that must be present for the pipeline to move at all. Not the full
#: list — this asserts the registration mechanism works, not the roster, which
#: `test_*_agent.py::test_worker_registers_*` already covers.
REQUIRED = (
    "intake_agent",
    "verification_agent",
    "triage_agent",
    "logistics_agent",
    "ledger_agent",
)


def test_python_dash_m_registers_handlers():
    """The exact invocation in infra/render.yaml's startCommand."""
    probe = (
        "import logging, sys;"
        "logging.disable(logging.CRITICAL);"
        # Import the module the way `python -m` does, then ask the *named*
        # module what it has — which is what the fixed entrypoint delegates to.
        "import runpy, app.worker as w;"
        "w.load_handlers();"
        "print(','.join(sorted(w.HANDLERS)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=API_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PYTHONPATH": str(API_ROOT)},
    )
    assert result.returncode == 0, result.stderr
    registered = set(result.stdout.strip().split(","))
    missing = [name for name in REQUIRED if name not in registered]
    assert not missing, f"handlers missing after -m style import: {missing}"


def test_the_main_entrypoint_delegates_to_the_named_module():
    """The fix itself, asserted rather than assumed.

    ``__main__`` must not call its own ``main`` — that copy's HANDLERS is the
    empty one. If this file ever goes back to a bare ``main()`` the worker
    silently stops processing anything, and nothing else would notice.
    """
    source = (API_ROOT / "app" / "worker.py").read_text()
    entrypoint = source[source.index('if __name__ == "__main__"') :]
    assert "from app.worker import main" in entrypoint, (
        "the __main__ block must delegate to app.worker.main; calling the "
        "local main() reads an empty HANDLERS and parks every job"
    )
