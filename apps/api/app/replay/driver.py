"""The replay driver — a cursor over Melissa's advisories.

Everything demos through this. Act 1 is the driver walking the storm in;
Act 2 drops a live voice note into the same pipeline the replayed advisories are
feeding; Act 3 reads the ledger the replay wrote. If it breaks, fixing it
outranks whatever else was in progress.

The driver moves in **storm time**, not wall time. It exposes "apply everything
up to this moment" and lets the caller decide how fast moments arrive — a
console scrubbing a timeline, a rehearsal running at 600×, a test stepping one
advisory at a time. Pacing in here would make the demo untestable and the tests
slow, and would put the one control the presenter needs behind an API call.

Applying an advisory does three things and no more: sets the posture, records
the change, and enqueues the agents that react to it. The driver does not decide
anything itself. It is the clock and the cursor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session, defer

from lighthouse_contracts import AgentName, Event, Posture

from app import queue
from app.models import Advisory, HazardEvent
from app.replay.posture import posture_for

#: Agents woken by a new advisory. Nothing is registered to handle these yet —
#: the jobs queue up and wait, which is the point of enqueueing rather than
#: calling. When Risk Mapper lands, the backlog is already there for it.
ADVISORY_CONSUMERS = (AgentName.RISK_MAPPER,)


@dataclass(frozen=True)
class Applied:
    """What happened when one advisory went through."""

    advisory_number: str
    issued_at: datetime
    posture: Posture
    posture_changed: bool
    previous_posture: Posture
    jobs_enqueued: int


@dataclass(frozen=True)
class ReplayState:
    hazard_event_id: object
    storm_name: str
    posture: Posture
    applied: int
    total: int
    position: datetime | None = None
    next_advisory_at: datetime | None = None
    started_at: datetime | None = None
    ends_at: datetime | None = None
    log: tuple[Applied, ...] = field(default=())

    @property
    def finished(self) -> bool:
        return self.applied >= self.total


class ReplayDriver:
    """Walks one hazard event's advisories forward. Never backwards.

    Rewinding would mean un-emitting events and un-writing ledger entries, and a
    ledger you can rewind is not a ledger. ``reset`` exists for development and
    clears the replay outright rather than pretending to step back.
    """

    def __init__(self, session: Session, *, external_ref: str = "al132025") -> None:
        self.session = session
        self.event = session.scalar(
            select(HazardEvent).where(HazardEvent.external_ref == external_ref)
        )
        if self.event is None:
            raise LookupError(
                f"no hazard event {external_ref!r} — run app.nhc.ingest.ingest_storm first"
            )

    # -- reading -----------------------------------------------------------

    #: Geometry columns. Deferred everywhere in the driver: nothing here reads
    #: them, and loading them turned a 41-step walk into 1,681 full row loads
    #: carrying several megabytes of polygon each. The posture rules read `raw`
    #: and go to PostGIS by id for anything spatial.
    _GEOMETRY = (
        Advisory.cone,
        Advisory.track,
        Advisory.wind_field_34,
        Advisory.wind_field_50,
        Advisory.wind_field_64,
    )

    def _advisory_query(self):
        return (
            select(Advisory)
            .options(*(defer(column) for column in self._GEOMETRY))
            .where(
                Advisory.hazard_event_id == self.event.id,
                Advisory.observed.is_(False),
            )
            .order_by(Advisory.issued_at, Advisory.advisory_number)
        )

    def _advisories(self) -> list[Advisory]:
        """Forecast advisories in issue order. The best track is not a step."""
        return list(self.session.scalars(self._advisory_query()))

    def _dispatched(self) -> set[str]:
        """Advisory ids already pushed through, read off the queue itself.

        Derived rather than stored. A separate cursor column would be a second
        source of truth about how far the replay has got, and the two would
        disagree the first time a run was interrupted — which, during rehearsal
        week, is most runs.
        """
        rows = self.session.execute(
            text(
                """
                SELECT DISTINCT payload->>'advisory_id' AS advisory_id
                FROM agent_job
                WHERE payload ? 'advisory_id'
                  AND payload->>'hazard_event_id' = :event
                """
            ),
            {"event": str(self.event.id)},
        ).scalars()
        return {r for r in rows if r}

    def state(self) -> ReplayState:
        advisories = self._advisories()
        dispatched = self._dispatched()
        done = [a for a in advisories if str(a.id) in dispatched]
        pending = [a for a in advisories if str(a.id) not in dispatched]

        return ReplayState(
            hazard_event_id=self.event.id,
            storm_name=self.event.name,
            posture=self.event.current_posture,
            applied=len(done),
            total=len(advisories),
            position=done[-1].issued_at if done else None,
            next_advisory_at=pending[0].issued_at if pending else None,
            started_at=advisories[0].issued_at if advisories else None,
            ends_at=advisories[-1].issued_at if advisories else None,
        )

    # -- moving ------------------------------------------------------------

    def _apply(self, advisory: Advisory) -> Applied:
        previous = self.event.current_posture
        posture = posture_for(self.session, advisory)
        changed = posture != previous

        if changed:
            self.event.current_posture = posture

        payload = {
            "hazard_event_id": str(self.event.id),
            "advisory_id": str(advisory.id),
            "advisory_number": advisory.advisory_number,
            "issued_at": advisory.issued_at.isoformat(),
            "posture": str(posture),
            "event": str(Event.HAZARD_ADVISORY_INGESTED),
        }

        jobs = 0
        for agent in ADVISORY_CONSUMERS:
            queue.enqueue(self.session, job_type=agent, payload=payload)
            jobs += 1

        if changed:
            # A posture change is its own event because people act on it —
            # alert cascades hang off this, not off the advisory arriving.
            queue.enqueue(
                self.session,
                job_type=AgentName.ALERT_AGENT,
                payload={
                    **payload,
                    "event": str(Event.HAZARD_POSTURE_CHANGED),
                    "previous_posture": str(previous),
                },
            )
            jobs += 1

        self.session.flush()
        return Applied(
            advisory_number=advisory.advisory_number,
            issued_at=advisory.issued_at,
            posture=posture,
            posture_changed=changed,
            previous_posture=previous,
            jobs_enqueued=jobs,
        )

    def step(self) -> Applied | None:
        """Apply the next unapplied advisory. ``None`` when the storm is done."""
        dispatched = self._dispatched()
        for advisory in self._advisories():
            if str(advisory.id) not in dispatched:
                return self._apply(advisory)
        return None

    def advance_to(self, when: datetime) -> list[Applied]:
        """Apply every advisory issued at or before ``when``.

        The scrub bar. Jumping forward four hours replays the four hours rather
        than skipping them, because the ledger has to show what happened in
        between — a demo that arrives at the answer without the working is the
        one thing this product argues against.
        """
        dispatched = self._dispatched()
        applied = []
        for advisory in self._advisories():
            if str(advisory.id) in dispatched or advisory.issued_at > when:
                continue
            applied.append(self._apply(advisory))
        return applied

    def run(self, *, limit: int | None = None) -> list[Applied]:
        """Walk the whole storm. Unattended rehearsal, and the test harness.

        Reads the advisory list and the dispatched set once rather than once per
        step. Re-querying inside the loop is quadratic, and on a storm this size
        that was the difference between seven seconds and eight minutes.
        """
        dispatched = self._dispatched()
        applied = []
        for advisory in self._advisories():
            if str(advisory.id) in dispatched:
                continue
            applied.append(self._apply(advisory))
            if limit and len(applied) >= limit:
                break
        return applied

    def reset(self) -> int:
        """Clear the replay. Development and rehearsal only.

        Deletes the jobs the replay enqueued and returns the posture to QUIET.
        It does not touch the ledger, claims, or the registry: those are the
        record of what happened, and a reset that quietly erased them would make
        every rehearsal run indistinguishable from the first.
        """
        removed = self.session.execute(
            text(
                """
                DELETE FROM agent_job
                WHERE payload ? 'advisory_id'
                  AND payload->>'hazard_event_id' = :event
                """
            ),
            {"event": str(self.event.id)},
        ).rowcount
        self.event.current_posture = Posture.QUIET
        self.session.flush()
        return removed
