"""Walk synthetic households through intake and verification.

The registry seeder produces households. Nothing produced *claims*, so every
Act 2 and Act 3 surface — the intake queue, the review queue, the approval gate,
the settlement workbench, the public ledger — rendered an honest but useless
empty state, and the whole second half of the platform had never run against a
database. The release-flow test proved the seams fit together; it proved it in a
throwaway schema that is dropped when the test ends.

This drives the same seams against a real database so there is something to
develop and demonstrate against.

**What it does not skip.** Claims are created through ``enqueue_twilio_inbound``
and ``process_intake_job``, the same two calls the signed webhook makes, with a
payload of the same shape. Verification runs the real five-signal service. The
only thing bypassed is Twilio's HMAC on the HTTP edge, which is a property of
the edge rather than of the loop, and is covered by its own tests.

**Everything it writes is synthetic**, because everything it reads is: the
households come from the registry seeder and carry ``synthetic = true``. This
never invents a real person and never contacts a provider.

Deterministic by default. The same seed produces the same queue on every
machine, because a demo whose queue reshuffles between rehearsals is not a
rehearsal.
"""

from __future__ import annotations

import argparse
import random
import sys
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lighthouse_contracts import ClaimStatus, Posture, Verdict

from ..config import get_settings
from ..db import session_scope
from ..intake.service import enqueue_twilio_inbound, process_intake_job
from ..intake.twilio import TwilioInbound
from ..models import AgentJob, Claim, HazardEvent, StormFile
from ..verification_service import NEIGHBOUR_RADIUS_METRES, run_verification

#: Reports in the register a household actually writes in. Patois and English
#: mixed, because that is what arrives — the extractor is expected to cope with
#: both and these exist partly to keep it honest.
#:
#: Two carry safety-of-life phrases. That is deliberate and it is a small
#: number: the bypass exists to make a rare thing impossible to miss, and a
#: seeded queue where half the rows page a human would train an operator to
#: ignore the one that matters.
#: Grouped by what the storm did to that lane, because that is how damage
#: actually arrives — a gust takes the roofs off one row of houses, a gully
#: floods another. Neighbour corroboration only counts nearby reports of the
#: *same* damage category, so a cluster whose members each reported something
#: different would score no better than a cluster of strangers, and the seeded
#: queue would understate a signal that works.
REPORT_FAMILIES: tuple[tuple[str, ...], ...] = (
    (
        "Di roof gone, whole a it. We need tarpaulin an water fi di pickney dem.",
        "Roof damage bad, zinc fly off. Need tarpaulin.",
        "Half di roof gone. Rain a come inna di bedroom.",
        "No roof left at all. Five people, two pickney.",
        "Zinc peel back off di roof. Need tarpaulin before di next rain.",
    ),
    (
        "Water inna di house up to mi knee. Two elderly people here.",
        "Flood water rising fast. Need tarpaulin and drinking water.",
        "Di gully burst, whole yard under water. Nowhere fi sleep.",
        "Everything wash weh. Water still inna di house.",
        "Flood reach di bed. Two pickney and one elderly here.",
    ),
    (
        "Wall crack right through. Fraid it fall down pon we.",
        "Di house shift off di foundation. Big crack inna di back wall.",
        "Tree fall pon di back a di house. Wall mash up.",
        "Structure damage bad, di whole side lean over.",
        "Mi house mash up, wall crack, no water no light.",
    ),
)

#: Safety-of-life phrases, injected into a small number of reports.
#:
#: Deliberately rare. The bypass exists to make one thing impossible to miss,
#: and a seeded queue where a third of the rows page a human would train an
#: operator to ignore the row that mattered — which is the exact failure the
#: bypass was built to prevent.
SAFETY_OF_LIFE: tuple[str, ...] = (
    "Mi granny trapped inna di back room, di door block.",
    "Someone injured, cut up bad from di zinc. Need help now.",
)


@dataclass(frozen=True, slots=True)
class SeedReport:
    event_ref: str
    claims: int
    verified: int
    review: int
    rejected: int
    skipped_existing: int

    def render(self) -> str:
        return (
            f"Hazard event  {self.event_ref}\n"
            f"Claims filed  {self.claims}"
            f"{f' (skipped {self.skipped_existing} already present)' if self.skipped_existing else ''}\n"
            f"  auto-verified  {self.verified}\n"
            f"  needs review   {self.review}\n"
            f"  rejected       {self.rejected}"
        )


def _event(session: Session, external_ref: str) -> HazardEvent:
    """The hazard these claims belong to.

    Intake binds a claim to one event explicitly rather than choosing the
    latest, because production commonly carries several open rows and guessing
    is how a claim lands on the wrong storm.
    """
    event = session.scalar(
        select(HazardEvent).where(HazardEvent.external_ref == external_ref)
    )
    if event is None:
        raise SeedError(
            f"no hazard event with external_ref {external_ref!r}. Ingest a storm "
            "first, or pass --event-ref for one that exists."
        )
    return event


class SeedError(Exception):
    """Seeding was refused."""


def _households(
    session: Session, count: int, rng: random.Random
) -> list[tuple[StormFile, int, int]]:
    """Households clustered within actual walking distance of each other.

    Neighbour corroboration counts other claims inside 300 m. Grouping by
    community is not nearly tight enough — a Jamaican community routinely spans
    several kilometres, so four households drawn from one are almost never
    within 300 m and every claim scores a lonely 0.0. Corroboration has to be
    found the way the signal finds it: with PostGIS, at the same radius.

    So each cluster starts from one household and takes its real neighbours.
    That reproduces what a storm does — several reports from one lane — and it
    is what lets a seeded queue show a spread of confidence instead of one
    repeated verdict.

    Cluster members are returned together and in order, because the first claim
    in a lane legitimately has nobody to corroborate it yet and the fourth has
    three. That gradient is the point.
    """
    seeds = list(
        session.scalars(
            select(StormFile)
            .where(
                StormFile.synthetic.is_(True),
                StormFile.location.is_not(None),
                StormFile.phone.is_not(None),
            )
            .order_by(StormFile.id)
        )
    )
    if not seeds:
        raise SeedError(
            "the synthetic registry is empty or unplaced. Run the registry "
            "seeder before seeding claims."
        )
    rng.shuffle(seeds)

    chosen: list[tuple[StormFile, int, int]] = []
    used: set[uuid.UUID] = set()
    for seed in seeds:
        if len(chosen) >= count:
            break
        if seed.id in used:
            continue
        neighbours = list(
            session.scalars(
                select(StormFile)
                .where(
                    StormFile.id != seed.id,
                    StormFile.synthetic.is_(True),
                    StormFile.phone.is_not(None),
                    StormFile.location.is_not(None),
                    func.ST_DWithin(
                        StormFile.location, seed.location, NEIGHBOUR_RADIUS_METRES
                    ),
                )
                .order_by(StormFile.id)
                .limit(4)
            )
        )
        cluster = [seed, *(n for n in neighbours if n.id not in used)]
        if len(cluster) < 2:
            # A household with no neighbour inside the radius can still file,
            # but it cannot demonstrate corroboration. Keep a few for realism
            # rather than pretending every report has witnesses.
            if rng.random() > 0.2:
                continue
            cluster = [seed]
        family = len(chosen) % len(REPORT_FAMILIES)
        for position, member in enumerate(cluster[: count - len(chosen)]):
            used.add(member.id)
            chosen.append((member, family, position))
    return chosen[:count]


def seed_claims(
    session: Session,
    *,
    count: int,
    event_ref: str,
    seed: int,
) -> SeedReport:
    rng = random.Random(seed)
    event = _event(session, event_ref)

    # Claims are only meaningful once the event is one somebody would act on.
    if event.current_posture is Posture.QUIET:
        event.current_posture = Posture.ACT
        session.flush()

    households = _households(session, count, rng)
    verified = review = rejected = skipped = 0
    filed = 0

    for index, (household, family, position) in enumerate(households):
        if household.phone is None:
            skipped += 1
            continue

        phrasings = REPORT_FAMILIES[family]
        body = phrasings[position % len(phrasings)]
        # Two of the whole seeded set, and never the first report in a lane —
        # the point is that it stands out, not that it is common.
        if index in (3, 9) and position > 0:
            body = f"{body} {SAFETY_OF_LIFE[index // 9]}"
        # Deterministic and unique: re-running with the same seed re-enqueues
        # the same SIDs, and intake's own duplicate guard makes that a no-op
        # rather than a second claim.
        sid = "SM" + uuid.uuid5(uuid.UUID(int=seed), f"{household.id}/{index}").hex

        enqueued = enqueue_twilio_inbound(
            session,
            TwilioInbound(
                message_sid=sid,
                from_phone=household.phone,
                body=body,
                media=(),
            ),
            hazard_external_ref=event_ref,
        )
        if not enqueued.created:
            skipped += 1
            continue

        job = session.scalar(select(AgentJob).where(AgentJob.id == enqueued.job_id))
        if job is None:
            skipped += 1
            continue

        intake = process_intake_job(session, dict(job.payload))
        if intake.duplicate:
            skipped += 1
            continue
        filed += 1

        outcome = run_verification(session, intake.claim_id)
        if outcome.output.verdict is Verdict.APPROVED:
            verified += 1
        elif outcome.output.verdict is Verdict.REJECTED:
            rejected += 1
        else:
            review += 1

    return SeedReport(
        event_ref=event_ref,
        claims=filed,
        verified=verified,
        review=review,
        rejected=rejected,
        skipped_existing=skipped,
    )


#: The pools the demo stands up. "St Elizabeth pool" is named in the
#: buildathon acceptance test (step 6) and the parishes are the replay area's.
#: Seeded here because the replay seeder is the shared heartbeat — a demo that
#: has claims but nowhere for a donor to give is only half of Act 3.
#:
#: The pool *name* is the PRD's literal string; the scope is
#: ``REPLAY_PARISHES``'s. Those differ on purpose — "St Elizabeth pool" is what
#: step 6 types, while "Saint Elizabeth" is the platform's canonical parish
#: everywhere else, and parish name is the join key precisely because the COD
#: p-codes disagree. A pool scoped to a parish string nothing else uses would
#: read correctly on the portal and match nothing the day scope starts being
#: enforced.
DEMO_POOLS: tuple[tuple[str, str, str | None], ...] = (
    ("St Elizabeth pool", "PARISH", "Saint Elizabeth"),
    ("Westmoreland pool", "PARISH", "Westmoreland"),
    ("Melissa response fund", "EVENT", None),
)


def seed_pools(session: Session) -> int:
    """Create the demo donation pools that do not already exist, and pull the
    scope of any that do back to ``DEMO_POOLS``. Idempotent by name, because a
    re-run of the seeder must not mint duplicate pots — but a pool seeded by an
    earlier run with a parish spelled differently from every other row is a
    demo fixture that has drifted, and the heartbeat is what corrects it.

    Returns the number of pools created; a repaired scope is not a new pot.
    """
    from app.donations_service import create_pool
    from app.models import DonationPool

    created = 0
    for name, scope_kind, scope_value in DEMO_POOLS:
        existing = session.scalar(
            select(DonationPool).where(DonationPool.name == name)
        )
        if existing is not None:
            if (existing.scope_kind, existing.scope_value) != (scope_kind, scope_value):
                existing.scope_kind = scope_kind
                existing.scope_value = scope_value
            continue
        create_pool(session, name=name, scope_kind=scope_kind, scope_value=scope_value)
        created += 1
    return created


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Walk synthetic households through intake and verification so the "
            "Act 2 and Act 3 surfaces have something real to render."
        )
    )
    parser.add_argument("--count", type=int, default=12)
    parser.add_argument(
        "--event-ref",
        default=None,
        help="hazard external_ref to file against (default: INTAKE_HAZARD_EXTERNAL_REF)",
    )
    parser.add_argument("--seed", type=int, default=None, help="default: REPLAY_SEED")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = get_settings()
    event_ref = args.event_ref or settings.intake_hazard_external_ref
    if not event_ref:
        print(
            "Refused: no hazard event named. Set INTAKE_HAZARD_EXTERNAL_REF or "
            "pass --event-ref.",
            file=sys.stderr,
        )
        return 2

    try:
        with session_scope() as session:
            report = seed_claims(
                session,
                count=max(1, args.count),
                event_ref=event_ref,
                seed=args.seed if args.seed is not None else settings.replay_seed,
            )
            pools = seed_pools(session)
    except SeedError as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 2

    print(report.render())
    if pools:
        print(f"donation pools created: {pools}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
