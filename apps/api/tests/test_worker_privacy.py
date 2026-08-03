"""Worker failures retain retry state without retaining household secrets."""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

from lighthouse_contracts import JobStatus

from app import db, queue, worker
from app.models import AgentJob


class _FakeSession:
    def __init__(self, job: AgentJob) -> None:
        self.job = job
        self.flushes = 0

    def get(self, model, identity):
        assert model is AgentJob
        assert identity == self.job.id
        return self.job

    def flush(self) -> None:
        self.flushes += 1


def test_engine_hides_bound_parameters(monkeypatch):
    captured: dict = {}
    sentinel = object()

    def fake_create_engine(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(
        db,
        "get_settings",
        lambda: SimpleNamespace(sqlalchemy_url="postgresql+psycopg://example.test/db"),
    )
    monkeypatch.setattr(db, "create_engine", fake_create_engine)

    assert db.get_engine() is sentinel
    assert captured["hide_parameters"] is True


def test_worker_failure_never_logs_or_persists_exception_message(
    monkeypatch, caplog
):
    secret = "whatsapp:+18760001111 said roof gone at Secret Lane"
    job = AgentJob(
        id=uuid.uuid4(),
        job_type="privacy_test",
        payload={"body": secret},
        status=JobStatus.RUNNING,
        attempts=1,
        max_attempts=5,
        locked_by="worker-before-failure",
    )
    fake_session = _FakeSession(job)

    @contextmanager
    def fake_session_scope():
        yield fake_session

    def leaking_handler(_session, _payload):
        raise RuntimeError(secret)

    monkeypatch.setattr(worker, "session_scope", fake_session_scope)
    monkeypatch.setattr(queue, "claim_job", lambda _session, worker_id: job)
    monkeypatch.setitem(worker.HANDLERS, job.job_type, leaking_handler)

    with caplog.at_level(logging.ERROR, logger="lighthouse.worker"):
        assert worker.run_once("privacy-worker") is True

    assert secret not in caplog.text
    assert "RuntimeError" in caplog.text
    assert job.last_error == "handler_error:RuntimeError"
    assert secret not in job.last_error
    # Retry semantics are unchanged: below max attempts the same job returns to
    # QUEUED and is unlocked for another worker.
    assert job.status is JobStatus.QUEUED
    assert job.locked_by is None
    assert job.finished_at is None
    assert fake_session.flushes == 1
