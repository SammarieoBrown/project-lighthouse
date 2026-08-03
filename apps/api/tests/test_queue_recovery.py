"""Crash recovery for the Postgres-backed worker queue."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from lighthouse_contracts import AgentName, JobStatus

from app import queue
from app.models import AgentJob


def _running_job(
    *,
    locked_at: datetime | None,
    attempts: int = 1,
    max_attempts: int = 5,
) -> AgentJob:
    return AgentJob(
        job_type=str(AgentName.INTAKE_AGENT),
        payload={},
        status=JobStatus.RUNNING,
        attempts=attempts,
        max_attempts=max_attempts,
        locked_by="retired-worker:1",
        locked_at=locked_at,
    )


def test_expired_worker_lease_is_requeued_and_visible(session):
    now = datetime.now(UTC)
    stale = _running_job(locked_at=now - queue.JOB_LEASE_TIMEOUT - timedelta(seconds=1))
    fresh = _running_job(locked_at=now)
    session.add_all([stale, fresh])
    session.flush()

    assert queue.pending_count(session) == 1
    assert queue.recover_expired_leases(session, now=now) == 1
    session.refresh(stale)
    session.refresh(fresh)

    assert stale.status is JobStatus.QUEUED
    assert stale.locked_by is None
    assert stale.locked_at is None
    assert stale.last_error == "worker_lease_expired"
    assert stale.attempts == 1
    assert fresh.status is JobStatus.RUNNING


def test_expired_worker_lease_exhausts_retry_budget(session):
    now = datetime.now(UTC)
    exhausted = _running_job(
        locked_at=None,
        attempts=5,
        max_attempts=5,
    )
    session.add(exhausted)
    session.flush()

    assert queue.recover_expired_leases(session, now=now) == 1
    session.refresh(exhausted)

    assert exhausted.status is JobStatus.DEAD
    assert exhausted.finished_at == now
    assert exhausted.locked_by is None
    assert exhausted.locked_at is None
    assert exhausted.last_error == "worker_lease_expired"
