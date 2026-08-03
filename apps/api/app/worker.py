"""Worker entrypoint — ``python -m app.worker``.

Claims jobs with ``FOR UPDATE SKIP LOCKED`` and dispatches them to agent
handlers. An unknown job type parks the job rather than crashing the worker, so
agents land one at a time without the loop needing to change — and so the
replay can enqueue work for an agent that does not exist yet and have it waiting
when it does.

Each job runs in its own transaction. A handler that raises leaves the job
retryable and the database untouched.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import time
from collections.abc import Callable

from sqlalchemy.orm import Session

from lighthouse_contracts import AgentName

from . import queue
from .config import get_settings
from .db import session_scope
from .models import AgentJob

log = logging.getLogger("lighthouse.worker")

#: Agent handlers register here. Empty in Phase 0 — the contracts exist, the
#: implementations land in weeks 1 and 2.
HANDLERS: dict[str, Callable[[Session, dict], None]] = {}


def register(agent: AgentName):
    def deco(fn: Callable[[Session, dict], None]):
        HANDLERS[str(agent)] = fn
        return fn

    return deco


_running = True


def _stop(signum, _frame) -> None:
    global _running
    log.info("signal %s received, finishing current job then stopping", signum)
    _running = False


def run_once(worker_id: str) -> bool:
    """Claim and run one job. Returns False when the queue is empty.

    The claim commits before the work starts, in its own transaction. If the
    handler then rolls back, the job stays claimed rather than becoming
    invisible — a job that vanishes on failure is exactly the loss this queue
    exists to prevent.
    """
    with session_scope() as session:
        job = queue.claim_job(session, worker_id=worker_id)
        if job is None:
            return False
        job_id, job_type, payload = job.id, job.job_type, dict(job.payload)

    handler = HANDLERS.get(job_type)
    if handler is None:
        # Not a failure — unhandled. The replay enqueues work for agents that
        # have not been written yet so the backlog is waiting when they land,
        # and running that through the retry path would burn five attempts
        # against a handler that was never going to answer, then mark the job
        # DEAD. Park it instead and say so once.
        log.info("no handler for %s yet — parking job %s", job_type, job_id)
        with session_scope() as session:
            unhandled = session.get(AgentJob, job_id)
            if unhandled is not None:
                queue.park(session, unhandled, f"no handler registered for {job_type}")
        return True

    try:
        with session_scope() as session:
            handler(session, payload)
            done = session.get(AgentJob, job_id)
            if done is not None:
                queue.complete(session, done)

    except Exception as exc:  # noqa: BLE001 — the queue is where failures live
        log.exception("job %s (%s) failed", job_id, job_type)
        with session_scope() as session:
            failed = session.get(AgentJob, job_id)
            if failed is not None:
                queue.fail(session, failed, str(exc))

    return True


def load_handlers() -> dict[str, Callable[[Session, dict], None]]:
    """Import the agents so their decorators run.

    Imported here rather than at module scope because every handler imports
    ``register`` from this module, and doing it at the top would be a circular
    import. The cost of getting this wrong is quiet: the worker starts, claims
    jobs, finds no handler, and parks every one of them.
    """
    from app import agents  # noqa: F401  — importing registers the handlers

    return HANDLERS


def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    load_handlers()
    log.info("handlers registered: %s", ", ".join(sorted(HANDLERS)) or "none")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    log.info("worker %s started (env=%s)", worker_id, settings.environment)

    idle_sleep = 1.0
    while _running:
        try:
            did_work = run_once(worker_id)
        except Exception:
            log.exception("worker loop error")
            time.sleep(idle_sleep)
            continue
        if not did_work:
            time.sleep(idle_sleep)

    log.info("worker %s stopped", worker_id)


if __name__ == "__main__":
    main()
