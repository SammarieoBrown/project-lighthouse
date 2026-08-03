"""Registry tests: real geography, invented households, no drift between runs."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text

from app.models import StormFile
from app.registry import load_communities, load_parishes, seed_registry, vulnerability
from app.registry.geography import REPLAY_PARISHES, _parish_population, _parish_shapes

MONTEGO_BAY = (18.4762, -77.8939)


# --------------------------------------------------------------------------
# Geography
# --------------------------------------------------------------------------


def test_the_two_cod_datasets_still_disagree_about_pcodes():
    """A regression guard on a live conflict in the source data.

    OCHA publishes Jamaica's boundaries and its population as two Common
    Operational Datasets, and 11 of 14 parishes carry a different p-code in one
    than in the other. Saint Elizabeth is JM09 in the boundaries and JM11 in the
    population table, and JM09 in the population table is Saint Ann — on the
    other side of the island.

    A p-code join therefore runs clean and produces wrong numbers everywhere.
    This test exists so that if someone "tidies" the join back to p-codes, or if
    OCHA fixes the conflict and the workaround becomes unnecessary, we find out
    from a failing test rather than from a demo.
    """
    import csv

    from app.registry.geography import POPULATION

    shapes = _parish_shapes()
    with POPULATION.open(newline="", encoding="utf-8-sig") as fh:
        pop_pcodes = {r["ADM1_EN"].strip(): r["ADM1_PCODE"].strip() for r in csv.DictReader(fh)}

    assert set(shapes) == set(pop_pcodes), "names are the only usable join key"

    disagreeing = sorted(n for n in shapes if shapes[n]["pcode"] != pop_pcodes[n])
    assert len(disagreeing) == 11, (
        f"the p-code conflict changed shape: {len(disagreeing)} parishes now "
        f"disagree ({disagreeing}). Re-check whether joining by name is still required."
    )

    # The specific collision that would be invisible: our two parishes both
    # resolve to a different, real parish under the other dataset's codes.
    assert shapes["Saint Elizabeth"]["pcode"] == "JM09"
    assert pop_pcodes["Saint Elizabeth"] == "JM11"
    assert pop_pcodes["Saint Ann"] == "JM09"

    assert shapes["Westmoreland"]["pcode"] == "JM14"
    assert pop_pcodes["Westmoreland"] == "JM16"
    assert pop_pcodes["Saint Thomas"] == "JM14"

    # And the populations a p-code join would have silently used instead.
    populations = _parish_population()
    assert populations["Saint Elizabeth"] == 150_205
    assert populations["Saint Ann"] == 172_362


def test_parishes_carry_their_real_populations():
    parishes = load_parishes()
    assert set(parishes) == set(REPLAY_PARISHES)
    assert parishes["Saint Elizabeth"].population == 150_205
    assert parishes["Westmoreland"].population == 144_103
    # Sanity on the geometry, not just the attributes.
    assert 1_000 < parishes["Saint Elizabeth"].area_sqkm < 1_500
    assert parishes["Westmoreland"].geometry["type"] in ("Polygon", "MultiPolygon")


def test_communities_are_real_named_places():
    communities = load_communities()
    assert len(communities) == 137
    by_parish = {p: sum(1 for c in communities if c.parish == p) for p in REPLAY_PARISHES}
    assert by_parish == {"Saint Elizabeth": 61, "Westmoreland": 76}
    assert all(c.name and c.geometry for c in communities)


def test_an_unknown_parish_is_refused():
    with pytest.raises(KeyError, match="no such parish"):
        load_parishes(("Atlantis",))


# --------------------------------------------------------------------------
# Vulnerability — a lookup a human can audit
# --------------------------------------------------------------------------


def test_vulnerability_is_traceable_to_the_fields_it_reads():
    strongest = vulnerability(
        {"roof": "concrete", "walls": "block_steel", "built": "post_2015"},
        {"total": 2, "children": 0, "elderly": 0, "medical": []},
    )
    weakest = vulnerability(
        {"roof": "zinc", "walls": "wood", "built": "pre_1980"},
        {"total": 6, "children": 3, "elderly": 1, "medical": ["dialysis"]},
    )
    assert strongest == 0
    assert weakest == 95  # 30 + 20 + 15 + 10 + 5 + 15
    assert 0 <= strongest <= weakest <= 100


def test_a_zinc_roof_scores_worse_than_concrete_all_else_equal():
    """The single field that most decides whether a roof survives a category 5."""
    people = {"total": 3, "children": 0, "elderly": 0, "medical": []}
    base = {"walls": "block_steel", "built": "2000_2014"}
    assert vulnerability({**base, "roof": "zinc"}, people) > vulnerability(
        {**base, "roof": "concrete"}, people
    )


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def registry(session_module):
    report = seed_registry(session_module, count=500)
    session_module.flush()
    return report


def test_the_registry_is_the_size_asked_for_and_split_by_real_population(
    registry, session_module
):
    assert registry.created == 500
    total = session_module.scalar(select(func.count()).select_from(StormFile))
    assert total == 500

    # 150,205 against 144,103 — close, and St Elizabeth should come out ahead.
    st_e = registry.by_parish["Saint Elizabeth"]
    west = registry.by_parish["Westmoreland"]
    assert st_e + west == 500
    assert st_e > west
    assert st_e == pytest.approx(500 * 150_205 / 294_308, abs=2)


def test_every_household_is_synthetic(registry, session_module):
    """A standing rule, enforced where it is cheapest to check."""
    real = session_module.scalar(
        select(func.count()).select_from(StormFile).where(StormFile.synthetic.is_(False))
    )
    assert real == 0


def test_no_household_is_in_the_sea(registry, session_module):
    """Points are generated inside the community polygon, not its bounding box.

    A bounding box on a coastal parish puts a good fraction of the registry
    offshore, and every one of those would then fail verification for reasons
    that have nothing to do with the claim.
    """
    outside = session_module.execute(
        text(
            """
            SELECT count(*)
            FROM storm_file sf
            WHERE NOT EXISTS (
              SELECT 1 FROM storm_file s2
              WHERE s2.id = sf.id
                AND ST_Y(s2.location::geometry) BETWEEN 17.6 AND 18.6
                AND ST_X(s2.location::geometry) BETWEEN -78.5 AND -77.4
            )
            """
        )
    ).scalar()
    assert outside == 0, "households outside the bounds of western Jamaica"


def test_households_sit_inside_the_parish_they_are_labelled_with(registry, session_module):
    """The label and the location have to agree, or the map lies."""
    mismatched = session_module.execute(
        text(
            """
            SELECT sf.parish, count(*)
            FROM storm_file sf
            GROUP BY sf.parish
            HAVING count(*) = 0
            """
        )
    ).all()
    assert mismatched == []

    parishes = session_module.execute(
        text("SELECT DISTINCT parish FROM storm_file ORDER BY 1")
    ).scalars().all()
    assert parishes == ["Saint Elizabeth", "Westmoreland"]


def test_seeding_twice_does_not_double_the_registry(registry, session_module):
    """Claims and ledger entries reference these rows the moment the replay runs."""
    before = session_module.scalar(select(func.count()).select_from(StormFile))
    again = seed_registry(session_module, count=500)
    session_module.flush()
    after = session_module.scalar(select(func.count()).select_from(StormFile))
    assert again.created == 0
    assert before == after


def test_the_registry_is_reproducible_from_the_seed(registry, session_module):
    """Same seed, same households — otherwise the replay is not a replay."""
    from app.registry.seeder import _household
    import random

    first = [_household(random.Random(20251028)) for _ in range(20)]
    second = [_household(random.Random(20251028)) for _ in range(20)]
    assert first == second

    different = [_household(random.Random(1)) for _ in range(20)]
    assert first != different


def test_the_registry_has_the_spread_the_risk_model_needs(registry, session_module):
    """Not a distribution check — a check that there is anything to distinguish.

    A registry where every household scores the same is useless to triage: it
    cannot rank, so every claim looks equally urgent and the queue is arbitrary.
    """
    row = session_module.execute(
        text(
            """
            SELECT min(vuln_score) AS lo, max(vuln_score) AS hi,
                   count(DISTINCT vuln_score) AS distinct_scores,
                   count(DISTINCT community) AS communities,
                   avg((people->>'total')::int) AS mean_people
            FROM storm_file
            """
        )
    ).one()
    assert row.lo < 20 and row.hi > 70
    assert row.distinct_scores > 15
    assert row.communities > 100
    assert 2.5 < float(row.mean_people) < 4.0
