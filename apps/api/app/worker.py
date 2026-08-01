"""Worker entrypoint — ``python -m app.worker``.

Claims jobs with ``FOR UPDATE SKIP LOCKED`` and dispatches them to agent
handlers. Phase 0 ships the loop with no agents registered: an unknown job type
parks the job rather than crashing the worker, so Matthew can land agents one at
a time without the loop needing to change.

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

    try:
        handler = HANDLERS.get(job_type)
        if handler is None:
            raise LookupError(f"no handler registered for {job_type}")

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


def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
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
