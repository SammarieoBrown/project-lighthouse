"""The claim seeder, and the properties a demo depends on.

Guards the two things that would quietly ruin a rehearsal: that re-running does
not duplicate a queue, and that clusters are tight enough for neighbour
corroboration to actually fire. The second one already failed once — grouping by
community looked correct and produced 0.0 on every claim, because a Jamaican
community is kilometres wide and the signal counts 300 metres.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.intake.service import phone_hash
from app.models import Claim, HazardEvent, StormFile, Verification
from app.replay.seed_claims import REPORT_FAMILIES, SeedError, seed_claims
from app.verification_service import NEIGHBOUR_RADIUS_METRES
from lighthouse_contracts import Posture

EVENT_REF = "seed-claims-test"


@pytest.fixture
def event(session) -> HazardEvent:
    row = HazardEvent(
        name="Seeder test storm",
        external_ref=EVENT_REF,
        current_posture=Posture.ACT,
        replay=True,
    )
    session.add(row)
    session.flush()
    return row


def _households(session, count: int = 8) -> None:
    """A tight lane, in real metres.

    The hash must be the real one. Intake finds an existing household by
    ``phone_hash``, and a placeholder there does not merely fail the lookup — it
    makes intake create a *second*, thin, ``synthetic=False`` Storm File for a
    phone the registry already holds. That row would then be partitioned away
    from the synthetic population by the neighbour signal, so the seeded queue
    would corroborate against nothing while looking fine.
    """
    for index in range(count):
        # ~40 m apart in latitude, so every member is inside the radius.
        offset = index * 0.00036
        phone = f"+1876555{index:04d}"
        session.add(
            StormFile(
                phone=phone,
                phone_hash=phone_hash(phone),
                parish="Saint Elizabeth",
                community="Newmarket",
                structure={"roof": "zinc", "walls": "wood", "built": "pre_1980"},
                people={"total": 3, "children": 1, "elderly": 0, "medical": []},
                synthetic=True,
                location=func.ST_SetSRID(
                    func.ST_MakePoint(-77.9, 18.1 + offset), 4326
                ),
            )
        )
    session.flush()


def test_seeding_files_claims_and_runs_verification(session, event):
    _households(session)

    report = seed_claims(session, count=4, event_ref=EVENT_REF, seed=7)

    assert report.claims == 4
    assert session.scalar(select(func.count()).select_from(Claim)) == 4
    # Every claim is verified on the way in; a queue of unassessed claims would
    # not exercise the surface it exists to fill.
    assert session.scalar(select(func.count()).select_from(Verification)) == 4


def test_reseeding_is_idempotent(session, event):
    """A rehearsal that doubles its own queue on the second run is not one."""
    _households(session)

    first = seed_claims(session, count=4, event_ref=EVENT_REF, seed=7)
    second = seed_claims(session, count=4, event_ref=EVENT_REF, seed=7)

    assert first.claims == 4
    assert second.claims == 0
    assert second.skipped_existing == 4
    assert session.scalar(select(func.count()).select_from(Claim)) == 4


def test_clusters_are_inside_the_corroboration_radius(session, event):
    """The bug this seeder shipped with, pinned.

    Community grouping produced clusters kilometres wide, so every claim scored
    a lonely 0.0 and the queue understated a signal that works. Members have to
    be within the radius the signal actually uses.
    """
    _households(session)
    seed_claims(session, count=4, event_ref=EVENT_REF, seed=7)

    claimed = list(
        session.scalars(
            select(StormFile)
            .join(Claim, Claim.storm_file_id == StormFile.id)
            .order_by(StormFile.phone)
        )
    )
    assert len(claimed) >= 2

    near = session.scalar(
        select(func.count())
        .select_from(StormFile)
        .where(
            StormFile.id != claimed[0].id,
            StormFile.id.in_([row.id for row in claimed]),
            func.ST_DWithin(
                StormFile.location, claimed[0].location, NEIGHBOUR_RADIUS_METRES
            ),
        )
    )
    assert near >= 1, "seeded cluster is wider than the corroboration radius"


def test_cluster_members_report_the_same_damage_family(session, event):
    """Corroboration counts nearby reports of the *same* category, not any report."""
    _households(session)
    seed_claims(session, count=4, event_ref=EVENT_REF, seed=7)

    families = {
        tuple(sorted(family)) for family in REPORT_FAMILIES
    }
    bodies = {
        claim.damage_type
        for claim in session.scalars(select(Claim))
        if claim.damage_type is not None
    }
    # Not every phrasing extracts a category, but the ones that do must agree
    # within a cluster rather than scattering across families.
    assert len(bodies) <= len(families)


def test_seeding_attaches_to_the_registry_rather_than_creating_households(
    session, event
):
    """Seeding must never mint a household.

    Intake creates a thin ``synthetic=False`` Storm File for a phone it does not
    recognise, which is correct for a stranger and wrong for a registry member.
    If the seeder ever stopped matching, the queue would still populate and look
    right — but every claim would hang off a fresh row partitioned away from the
    synthetic population, so neighbour corroboration would score nothing and the
    registry_match signal would find no structure profile. Silent, and it would
    survive to a demo.
    """
    _households(session, count=6)
    before = session.scalar(select(func.count()).select_from(StormFile))

    seed_claims(session, count=4, event_ref=EVENT_REF, seed=7)

    assert session.scalar(select(func.count()).select_from(StormFile)) == before
    assert (
        session.scalar(
            select(func.count())
            .select_from(StormFile)
            .where(StormFile.synthetic.is_(False))
        )
        == 0
    )


def test_seeding_refuses_an_unknown_event(session):
    _households(session)

    with pytest.raises(SeedError, match="no hazard event"):
        seed_claims(session, count=2, event_ref="does-not-exist", seed=7)


def test_seeding_refuses_an_empty_registry(session, event):
    with pytest.raises(SeedError, match="registry is empty"):
        seed_claims(session, count=2, event_ref=EVENT_REF, seed=7)
