"""The job queue — Postgres ``SKIP LOCKED`` (PRD 11.6).

Jobs are enqueued in the same transaction as the state transition that caused
them. That is the whole argument for a table over Redis: a Storm File cannot
change state while the job meant to react to it is silently lost.

Workers wake on ``LISTEN/NOTIFY`` rather than polling, which keeps round trips
down and avoids holding Neon awake purely to ask whether anything happened.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from lighthouse_contracts import DEFAULT_PRIORITY, AgentName, JobStatus

from .models import AgentJob

CHANNEL = "lighthouse_jobs"


def enqueue(
    session: Session,
    *,
    job_type: AgentName | str,
    payload: dict | None = None,
    priority: int = DEFAULT_PRIORITY,
    run_after: datetime | None = None,
) -> AgentJob:
    """Add a job. Caller owns the transaction — that is the point."""
    job = AgentJob(
        job_type=str(job_type),
        payload=payload or {},
        priority=priority,
        run_after=run_after or datetime.now(UTC),
    )
    session.add(job)
    session.flush()
    session.execute(text(f"NOTIFY {CHANNEL}"))
    return job


def claim_job(session: Session, *, worker_id: str) -> AgentJob | None:
    """Claim the highest-priority runnable job, or return None.

    ``FOR UPDATE SKIP LOCKED`` is what makes this safe to run from many workers
    at once: each one takes a different row instead of blocking on the same one.
    """
    row = session.execute(
        text(
            """
            SELECT id FROM agent_job
             WHERE status = 'QUEUED' AND run_after <= now()
             ORDER BY priority DESC, run_after
             FOR UPDATE SKIP LOCKED
             LIMIT 1
            """
        )
    ).scalar_one_or_none()

    if row is None:
        return None

    job = session.get(AgentJob, row)
    assert job is not None
    job.status = JobStatus.RUNNING
    job.locked_by = worker_id
    job.locked_at = datetime.now(UTC)
    job.attempts += 1
    session.flush()
    return job


def complete(session: Session, job: AgentJob) -> None:
    job.status = JobStatus.DONE
    job.finished_at = datetime.now(UTC)
    job.locked_by = None
    session.flush()


def fail(session: Session, job: AgentJob, error: str) -> None:
    """Retry until ``max_attempts``, then park as DEAD.

    Nothing is ever dropped silently — a dead job stays queryable, because a
    claim that vanished is exactly the failure this system exists to prevent.
    """
    job.last_error = error[:2000]
    job.locked_by = None
    if job.attempts >= job.max_attempts:
        job.status = JobStatus.DEAD
        job.finished_at = datetime.now(UTC)
    else:
        job.status = JobStatus.QUEUED
    session.flush()


def park(session: Session, job: AgentJob, reason: str, *, retry_after_s: int = 60) -> None:
    """Put a job back without spending a retry.

    For work that is not failing, only unhandled — the replay deliberately
    enqueues jobs for agents that have not been written yet, so the backlog is
    waiting for them the moment they land. Running that through ``fail`` burns
    five attempts against a handler that was never going to answer and marks the
    job DEAD, which is the opposite of waiting.

    ``run_after`` keeps the worker from spinning on the same unhandled job.
    """
    job.last_error = reason[:2000]
    job.locked_by = None
    job.locked_at = None
    job.status = JobStatus.QUEUED
    job.run_after = datetime.now(UTC) + timedelta(seconds=retry_after_s)
    session.flush()


def pending_count(session: Session) -> int:
    """Backlog depth. Surfaced on the console so a queue that is falling behind
    is visible rather than inferred (NFR-P-02: degrade by delay, never by loss)."""
    return session.execute(
        select(func.count()).select_from(AgentJob).where(AgentJob.status == JobStatus.QUEUED)
    ).scalar_one()


__all__ = ["enqueue", "claim_job", "complete", "fail", "park", "pending_count", "CHANNEL"]
