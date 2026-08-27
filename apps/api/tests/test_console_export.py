"""Replay export tests.

The console reads one file and has no way to check it. Nothing downstream will
notice if the frames are in string order, if the best track has been folded into
the timeline as though it were a forecast, or if the household bands are one
short and every home after the gap is labelled with its neighbour's damage.
All three render. So the checks live here and in the exporter itself, and the
exporter fails loudly rather than writing a file that looks fine.

The fixture walks the real storm against a real registry rather than a stub:
the invariants under test are about 41 advisories and thousands of positional
joins, and two advisories and four households would prove none of them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text

from app.agents.risk_mapper import assess
from app.console.export import (
    BAND_CHAR,
    ExportError,
    _building_data,
    _exposure_advisory_rows,
    _frame,
    _lock_building_snapshot,
    advisory_key,
    build_replay,
    serialise,
    write_replay,
)
from app.models import Advisory, HazardEvent
from app.nhc.ingest import ingest_storm
from app.registry import seed_registry
from app.registry.buildings import (
    INVENTORY_RECIPE_VERSION,
    advisory_fingerprint as exposure_advisory_fingerprint,
    exposure_rows_sha256,
    inventory_fingerprint,
    stored_event_exposure_rows,
    stored_structure_rows,
    structure_rows_sha256,
)
from app.forecast_sentinel_service import SIMPLIFY_DEG

#: Small enough that 41 advisories of assessments stay quick, large enough that
#: households land in a couple of dozen districts and the positional joins are
#: doing real work.
HOUSEHOLDS = 120

PINNED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


@pytest.fixture(scope="module")
def exported(session_module):
    """The whole storm, assessed against a registry, exported once."""
    ingest_storm(session_module)
    seed_registry(session_module, count=HOUSEHOLDS)
    session_module.flush()

    advisories = session_module.scalars(
        select(Advisory).where(Advisory.observed.is_(False))
    ).all()
    for advisory in advisories:
        assess(session_module, advisory)
    session_module.flush()

    return build_replay(session_module, generated_at=PINNED)


# --------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------


def test_advisory_numbers_sort_as_numbers_not_as_text():
    """The bug that renders a plausible-looking storm in the wrong order."""
    assert advisory_key("9") < advisory_key("10")
    assert sorted(["10", "9", "1", "2"], key=advisory_key) == ["1", "2", "9", "10"]

    # NHC issues intermediate advisories as 15A. It follows 15 and precedes 16.
    assert advisory_key("15") < advisory_key("15A") < advisory_key("16")

    with pytest.raises(ExportError, match="does not start with a number"):
        advisory_key("best_track")


def test_there_is_one_frame_per_forecast_advisory_in_ascending_order(exported):
    numbers = [frame["n"] for frame in exported["frames"]]

    assert len(numbers) == 41
    assert exported["event"]["advisory_count"] == 41
    assert numbers == [str(n) for n in range(1, 42)]
    assert numbers == sorted(numbers, key=int)

    issued = [frame["at"] for frame in exported["frames"]]
    assert issued == sorted(issued), "frames are out of time order"


def test_the_best_track_is_not_a_frame(exported, session_module):
    """It is observed truth, not forecast. Mixing it in claims foresight.

    Asserted against the database rather than against a hardcoded string, so
    this keeps working if the best track is ever numbered differently.
    """
    observed = session_module.scalars(
        select(Advisory.advisory_number).where(Advisory.observed.is_(True))
    ).all()
    assert observed, "the fixture has no best track, so this proves nothing"

    numbers = {frame["n"] for frame in exported["frames"]}
    assert numbers.isdisjoint(set(observed))


# --------------------------------------------------------------------------
# The positional joins
# --------------------------------------------------------------------------


def test_every_frame_carries_one_count_per_district_and_one_band_per_household(exported):
    """Ordering is the join, so length is the whole of its integrity."""
    districts = len(exported["districts"])
    households = len(exported["households"])
    assert districts > 1 and households == HOUSEHOLDS

    for frame in exported["frames"]:
        assert len(frame["district_counts"]) == districts, frame["n"]
        assert len(frame["household_bands"]) == households, frame["n"]
        assert all(len(counts) == 4 for counts in frame["district_counts"]), frame["n"]


def test_the_exporter_refuses_to_build_a_frame_whose_arrays_disagree():
    """The assertion has to live in the exporter, not only in this file.

    A test that checks the output of a run that already happened cannot stop a
    bad file being written. This one drives the exporter into the mismatch
    directly and requires it to raise.
    """
    advisory = Advisory(advisory_number="7", issued_at=PINNED, raw={})

    with pytest.raises(ExportError, match="mislabels synthetic household records"):
        _frame(
            advisory,
            codes=set(),
            arrival={},
            geometry={},
            bands="dmno",
            district_index=[0, 0, 0],  # one short
            district_count=1,
        )


def test_nothing_is_written_when_the_payload_cannot_be_built(session, tmp_path):
    """Build first, write second. A half-written replay is worse than none."""
    destination = tmp_path / "nested" / "replay.json"

    with pytest.raises(ExportError, match="no hazard event"):
        write_replay(session, destination, external_ref="al999999")

    assert not destination.exists()


def test_district_counts_and_totals_are_the_same_households_counted_twice(exported):
    """Two independent paths to the same four numbers, per frame."""
    order = ("destroyed", "major", "minor", "none")

    for frame in exported["frames"]:
        totals = [frame["totals"][name] for name in order]
        by_district = [sum(column) for column in zip(*frame["district_counts"], strict=True)]
        by_household = [frame["household_bands"].count(char) for char in "dmno"]

        assert by_district == totals, frame["n"]
        assert by_household == totals, frame["n"]
        assert sum(totals) == len(exported["households"]), frame["n"]


def test_districts_and_households_are_emitted_in_a_stable_documented_order(exported):
    districts = exported["districts"]
    keys = [(d["parish"], d["district"]) for d in districts]
    assert keys == sorted(keys), "districts are not ordered by (parish, district)"
    assert [d["id"] for d in districts] == list(range(len(districts)))
    assert [h["id"] for h in exported["households"]] == list(
        range(len(exported["households"]))
    )
    assert sum(d["n"] for d in districts) == len(exported["households"])


# --------------------------------------------------------------------------
# Values
# --------------------------------------------------------------------------


def test_a_household_band_is_one_of_four_characters(exported):
    """d, m, n, o — and nothing else, ever.

    A stray character is not a rendering bug downstream, it is a household the
    console cannot place in any band at all.
    """
    allowed = set(BAND_CHAR.values())
    assert allowed == {"d", "m", "n", "o"}

    seen: set[str] = set()
    for frame in exported["frames"]:
        seen |= set(frame["household_bands"])
    assert seen <= allowed, f"unknown band characters: {sorted(seen - allowed)}"
    assert seen, "no bands at all"


def test_every_timestamp_is_utc_and_says_so(exported):
    """The console formats fixed to en-JM. A naive timestamp is a hydration bug."""
    stamps = [exported["generated_at"]] + [f["at"] for f in exported["frames"]]
    for stamp in stamps:
        assert stamp.endswith("Z"), stamp
        assert "+" not in stamp, stamp
        # Parses back as the instant it claims to be.
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        assert parsed.tzinfo is UTC or parsed.utcoffset().total_seconds() == 0


def test_posture_and_warnings_are_the_ones_for_here(exported):
    """Same four rules the driver uses, not a second opinion.

    Melissa gave five days of warning and the whole argument for anticipatory
    action is that they are usable, so the curve has to climb rather than jump.
    The codes are the segments that cover the replay area: a watch/warning
    bundle covers everywhere the storm threatens, and at advisory 1 the
    hurricane watch in the file is for Hispaniola.
    """
    postures = [frame["posture"] for frame in exported["frames"]]
    assert set(postures) == {"QUIET", "WATCH", "READY", "ACT"}
    assert postures[0] == "QUIET"
    assert postures[-1] != "ACT", "the country never stood down"

    first = exported["frames"][0]
    assert "HWA" not in first["watch_codes"], "picked up Hispaniola's hurricane watch"

    acting = next(f for f in exported["frames"] if f["posture"] == "ACT")
    assert "HWR" in acting["watch_codes"]


def test_absent_data_is_an_absent_key_rather_than_a_zero(exported):
    """A zero states that the chance is nil, which is a different claim.

    Early advisories have no hurricane-force wind field and no 64 kt
    probability for anywhere near Jamaica. Both are real states, and neither is
    a zero or a null.
    """
    payload = json.dumps(exported)
    assert ":null" not in payload, "a null reached the payload"

    without_wind64 = [f["n"] for f in exported["frames"] if "wind64" not in f]
    assert without_wind64, "expected at least one advisory with no 64 kt field"

    # Locations drop out as the storm passes them; the last frame knows about
    # fewer places than the peak did.
    counts = [len(f["probabilities"]) for f in exported["frames"]]
    assert max(counts) > counts[-1]


def test_unbuilt_structure_exposure_is_unavailable_not_zero(exported):
    assert all("structures" not in district for district in exported["districts"])
    assert all("district_exposed" not in frame for frame in exported["frames"])


def _insert_inventory_marker(
    session,
    *,
    structures: int,
    places: int = 1,
    recipe_version: str = INVENTORY_RECIPE_VERSION,
) -> str:
    source = "1" * 64
    boundaries = "2" * 64
    fingerprint = inventory_fingerprint(
        source_sha256=source,
        boundaries_sha256=boundaries,
        recipe_version=recipe_version,
    )
    session.execute(
        text(
            "INSERT INTO place_structure_build "
            "(inventory_fingerprint, source_sha256, boundaries_sha256, "
            " recipe_version, structure_count, place_count, structure_rows_sha256) "
            "VALUES (:fingerprint, :source, :boundaries, :recipe, :structures, "
            " :places, :rows_sha256)"
        ),
        {
            "fingerprint": fingerprint,
            "source": source,
            "boundaries": boundaries,
            "recipe": recipe_version,
            "structures": structures,
            "places": places,
            "rows_sha256": structure_rows_sha256(stored_structure_rows(session)),
        },
    )
    return fingerprint


def _insert_exposure_marker(
    session,
    event: HazardEvent,
    advisories: list[Advisory],
    *,
    inventory_fingerprint: str,
    exposure_row_count: int = 0,
    exposed_structure_count: int = 0,
) -> None:
    rows = _exposure_advisory_rows(session, event)
    session.execute(
        text(
            "INSERT INTO place_exposure_build "
            "(hazard_event_id, inventory_fingerprint, structure_rows_sha256, "
            " advisory_fingerprint, "
            " advisory_count, exposure_row_count, exposed_structure_count, "
            " exposure_rows_sha256) "
            "VALUES (:event, :inventory, :structure_rows, :advisories, :advisory_count, "
            " :row_count, :structure_count, :rows_sha256)"
        ),
        {
            "event": event.id,
            "inventory": inventory_fingerprint,
            "structure_rows": structure_rows_sha256(stored_structure_rows(session)),
            "advisories": exposure_advisory_fingerprint(rows),
            "advisory_count": len(advisories),
            "row_count": exposure_row_count,
            "structure_count": exposed_structure_count,
            "rows_sha256": exposure_rows_sha256(
                event.id,
                stored_event_exposure_rows(session, event.id),
            ),
        },
    )


def _insert_complete_district_inventory(
    session, districts: list[dict], *, structures_each: int = 17
) -> tuple[int, int]:
    session.execute(
        text(
            "INSERT INTO place_structures "
            "(parish, district, community, structures, built_m2) "
            "VALUES (:parish, :district, :community, :structures, 125.0)"
        ),
        [
            {
                "parish": district["parish"],
                "district": district["district"],
                "community": f"mapped-test-place-{index}",
                "structures": structures_each,
            }
            for index, district in enumerate(districts)
        ],
    )
    return len(districts) * structures_each, len(districts)


def _insert_legacy_inventory(session, districts: list[dict]) -> None:
    session.execute(
        text(
            "INSERT INTO place_structures "
            "(parish, district, community, structures, built_m2) "
            "VALUES (:parish, :district, :community, :structures, 125.0)"
        ),
        [
            {
                "parish": districts[index % len(districts)]["parish"],
                "district": districts[index % len(districts)]["district"],
                "community": f"legacy-mapped-place-{index}",
                "structures": 1 if index else 1_841_391,
            }
            for index in range(775)
        ],
    )


def test_completed_all_zero_exposure_emits_zero_arrays(
    exported, session_module
):
    """A marker makes no sparse rows mean zero, not unavailable."""
    savepoint = session_module.begin_nested()
    try:
        event = session_module.scalar(
            select(HazardEvent).where(HazardEvent.external_ref == "al132025")
        )
        advisories = session_module.scalars(
            select(Advisory)
            .where(Advisory.hazard_event_id == event.id, Advisory.observed.is_(False))
        ).all()
        district = exported["districts"][0]
        structure_count, place_count = _insert_complete_district_inventory(
            session_module, exported["districts"]
        )
        fingerprint = _insert_inventory_marker(
            session_module,
            structures=structure_count,
            places=place_count,
        )
        _insert_exposure_marker(
            session_module,
            event,
            advisories,
            inventory_fingerprint=fingerprint,
        )

        payload = build_replay(session_module, generated_at=PINNED)
        selected = next(
            item
            for item in payload["districts"]
            if item["parish"] == district["parish"]
            and item["district"] == district["district"]
        )
        assert selected["structures"] == 17
        assert all("district_exposed" in frame for frame in payload["frames"])
        assert all(
            all(bands == [0, 0, 0] for bands in frame["district_exposed"])
            for frame in payload["frames"]
        )
    finally:
        savepoint.rollback()


def test_stale_exposure_inventory_fails_closed(exported, session_module):
    savepoint = session_module.begin_nested()
    try:
        event = session_module.scalar(
            select(HazardEvent).where(HazardEvent.external_ref == "al132025")
        )
        advisories = session_module.scalars(
            select(Advisory)
            .where(Advisory.hazard_event_id == event.id, Advisory.observed.is_(False))
        ).all()
        structure_count, place_count = _insert_complete_district_inventory(
            session_module, exported["districts"]
        )
        _insert_inventory_marker(
            session_module,
            structures=structure_count,
            places=place_count,
        )
        _insert_exposure_marker(
            session_module,
            event,
            advisories,
            inventory_fingerprint="b" * 64,
        )

        payload = build_replay(session_module, generated_at=PINNED)
        assert any("structures" in item for item in payload["districts"])
        assert all("district_exposed" not in frame for frame in payload["frames"])
    finally:
        savepoint.rollback()


def test_exposure_marker_must_bind_the_exact_inventory_rows(
    exported, session_module
):
    savepoint = session_module.begin_nested()
    try:
        event = session_module.scalar(
            select(HazardEvent).where(HazardEvent.external_ref == "al132025")
        )
        advisories = session_module.scalars(
            select(Advisory)
            .where(Advisory.hazard_event_id == event.id, Advisory.observed.is_(False))
        ).all()
        structure_count, place_count = _insert_complete_district_inventory(
            session_module,
            exported["districts"],
        )
        fingerprint = _insert_inventory_marker(
            session_module,
            structures=structure_count,
            places=place_count,
        )
        _insert_exposure_marker(
            session_module,
            event,
            advisories,
            inventory_fingerprint=fingerprint,
        )
        session_module.execute(
            text(
                "UPDATE place_exposure_build SET structure_rows_sha256 = :stale "
                "WHERE hazard_event_id = :event"
            ),
            {"stale": "f" * 64, "event": event.id},
        )

        structures, exposure = _building_data(session_module, event, advisories)
        assert structures is not None
        assert exposure is None
    finally:
        savepoint.rollback()


def test_wrong_inventory_counts_fail_closed(exported, session_module):
    savepoint = session_module.begin_nested()
    try:
        event = session_module.scalar(
            select(HazardEvent).where(HazardEvent.external_ref == "al132025")
        )
        advisories = session_module.scalars(
            select(Advisory)
            .where(Advisory.hazard_event_id == event.id, Advisory.observed.is_(False))
        ).all()
        district = exported["districts"][0]
        session_module.execute(
            text(
                "INSERT INTO place_structures "
                "(parish, district, community, structures, built_m2) "
                "VALUES (:parish, :district, 'mapped-test-place', 17, 125.0)"
            ),
            {"parish": district["parish"], "district": district["district"]},
        )
        _insert_inventory_marker(
            session_module,
            structures=18,  # does not match the actual aggregate
        )

        assert _building_data(session_module, event, advisories) == (None, None)
    finally:
        savepoint.rollback()


def test_stale_inventory_recipe_fails_closed(exported, session_module):
    savepoint = session_module.begin_nested()
    try:
        event = session_module.scalar(
            select(HazardEvent).where(HazardEvent.external_ref == "al132025")
        )
        advisories = session_module.scalars(
            select(Advisory)
            .where(Advisory.hazard_event_id == event.id, Advisory.observed.is_(False))
        ).all()
        district = exported["districts"][0]
        session_module.execute(
            text(
                "INSERT INTO place_structures "
                "(parish, district, community, structures, built_m2) "
                "VALUES (:parish, :district, 'mapped-test-place', 17, 125.0)"
            ),
            {"parish": district["parish"], "district": district["district"]},
        )
        _insert_inventory_marker(
            session_module,
            structures=17,
            recipe_version="place-structures-v1",
        )

        assert _building_data(session_module, event, advisories) == (None, None)
    finally:
        savepoint.rollback()


def test_same_count_and_total_inventory_redistribution_fails_row_digest(
    exported, session_module
):
    savepoint = session_module.begin_nested()
    try:
        event = session_module.scalar(
            select(HazardEvent).where(HazardEvent.external_ref == "al132025")
        )
        advisories = session_module.scalars(
            select(Advisory)
            .where(Advisory.hazard_event_id == event.id, Advisory.observed.is_(False))
        ).all()
        structure_count, place_count = _insert_complete_district_inventory(
            session_module, exported["districts"]
        )
        _insert_inventory_marker(
            session_module,
            structures=structure_count,
            places=place_count,
        )

        session_module.execute(
            text(
                "UPDATE place_structures SET structures = CASE community "
                "WHEN 'mapped-test-place-0' THEN 16 "
                "WHEN 'mapped-test-place-1' THEN 18 ELSE structures END "
                "WHERE community IN ('mapped-test-place-0', 'mapped-test-place-1')"
            )
        )

        assert _building_data(session_module, event, advisories) == (None, None)
    finally:
        savepoint.rollback()


def test_nonfinite_inventory_area_fails_closed_without_aborting_replay(
    exported, session_module
):
    savepoint = session_module.begin_nested()
    try:
        event = session_module.scalar(
            select(HazardEvent).where(HazardEvent.external_ref == "al132025")
        )
        advisories = session_module.scalars(
            select(Advisory)
            .where(Advisory.hazard_event_id == event.id, Advisory.observed.is_(False))
        ).all()
        district = exported["districts"][0]
        session_module.execute(
            text(
                "INSERT INTO place_structures "
                "(parish, district, community, structures, built_m2) "
                "VALUES (:parish, :district, 'mapped-test-place', 17, 125.0)"
            ),
            {"parish": district["parish"], "district": district["district"]},
        )
        _insert_inventory_marker(session_module, structures=17)
        session_module.execute(
            text(
                "UPDATE place_structures SET built_m2 = 'NaN'::double precision "
                "WHERE community = 'mapped-test-place'"
            )
        )

        assert _building_data(session_module, event, advisories) == (None, None)
    finally:
        savepoint.rollback()


def test_same_total_exposure_redistribution_above_inventory_fails_closed(
    exported, session_module
):
    savepoint = session_module.begin_nested()
    try:
        event = session_module.scalar(
            select(HazardEvent).where(HazardEvent.external_ref == "al132025")
        )
        advisories = session_module.scalars(
            select(Advisory)
            .where(Advisory.hazard_event_id == event.id, Advisory.observed.is_(False))
            .order_by(Advisory.issued_at)
        ).all()
        structure_count, place_count = _insert_complete_district_inventory(
            session_module,
            exported["districts"],
            structures_each=10,
        )
        fingerprint = _insert_inventory_marker(
            session_module,
            structures=structure_count,
            places=place_count,
        )
        first, second = exported["districts"][:2]
        session_module.execute(
            text(
                "INSERT INTO place_exposure "
                "(advisory_id, parish, district, community, band, structures) "
                "VALUES (:advisory, :parish, :district, :community, 34, :structures)"
            ),
            [
                {
                    "advisory": advisories[0].id,
                    "parish": first["parish"],
                    "district": first["district"],
                    "community": "redistributed-a",
                    "structures": 4,
                },
                {
                    "advisory": advisories[0].id,
                    "parish": second["parish"],
                    "district": second["district"],
                    "community": "redistributed-b",
                    "structures": 11,
                },
            ],
        )
        _insert_exposure_marker(
            session_module,
            event,
            advisories,
            inventory_fingerprint=fingerprint,
            exposure_row_count=2,
            exposed_structure_count=15,
        )

        structures, exposure = _building_data(session_module, event, advisories)
        assert structures is not None
        assert exposure is None
    finally:
        savepoint.rollback()


def test_same_count_and_total_exposure_redistribution_within_inventory_fails_digest(
    exported, session_module
):
    savepoint = session_module.begin_nested()
    try:
        event = session_module.scalar(
            select(HazardEvent).where(HazardEvent.external_ref == "al132025")
        )
        advisories = session_module.scalars(
            select(Advisory)
            .where(Advisory.hazard_event_id == event.id, Advisory.observed.is_(False))
            .order_by(Advisory.issued_at)
        ).all()
        structure_count, place_count = _insert_complete_district_inventory(
            session_module,
            exported["districts"],
            structures_each=10,
        )
        fingerprint = _insert_inventory_marker(
            session_module,
            structures=structure_count,
            places=place_count,
        )
        first, second = exported["districts"][:2]
        session_module.execute(
            text(
                "INSERT INTO place_exposure "
                "(advisory_id, parish, district, community, band, structures) "
                "VALUES (:advisory, :parish, :district, :community, 34, :structures)"
            ),
            [
                {
                    "advisory": advisories[0].id,
                    "parish": first["parish"],
                    "district": first["district"],
                    "community": "digest-a",
                    "structures": 4,
                },
                {
                    "advisory": advisories[0].id,
                    "parish": second["parish"],
                    "district": second["district"],
                    "community": "digest-b",
                    "structures": 6,
                },
            ],
        )
        _insert_exposure_marker(
            session_module,
            event,
            advisories,
            inventory_fingerprint=fingerprint,
            exposure_row_count=2,
            exposed_structure_count=10,
        )
        session_module.execute(
            text(
                "UPDATE place_exposure SET structures = 5 "
                "WHERE advisory_id = :advisory "
                "  AND community IN ('digest-a', 'digest-b')"
            ),
            {"advisory": advisories[0].id},
        )

        structures, exposure = _building_data(session_module, event, advisories)
        assert structures is not None
        assert exposure is None
    finally:
        savepoint.rollback()


def test_marker_backed_exposure_rejects_observed_advisory_rows(
    exported, session_module
):
    savepoint = session_module.begin_nested()
    try:
        event = session_module.scalar(
            select(HazardEvent).where(HazardEvent.external_ref == "al132025")
        )
        advisories = session_module.scalars(
            select(Advisory)
            .where(Advisory.hazard_event_id == event.id, Advisory.observed.is_(False))
        ).all()
        observed = session_module.scalar(
            select(Advisory).where(
                Advisory.hazard_event_id == event.id,
                Advisory.observed.is_(True),
            )
        )
        structure_count, place_count = _insert_complete_district_inventory(
            session_module,
            exported["districts"],
        )
        fingerprint = _insert_inventory_marker(
            session_module,
            structures=structure_count,
            places=place_count,
        )
        district = exported["districts"][0]
        session_module.execute(
            text(
                "INSERT INTO place_exposure "
                "(advisory_id, parish, district, community, band, structures) "
                "VALUES (:advisory, :parish, :district, 'observed-row', 34, 3)"
            ),
            {
                "advisory": observed.id,
                "parish": district["parish"],
                "district": district["district"],
            },
        )
        _insert_exposure_marker(
            session_module,
            event,
            advisories,
            inventory_fingerprint=fingerprint,
            exposure_row_count=1,
            exposed_structure_count=3,
        )

        structures, exposure = _building_data(session_module, event, advisories)
        assert structures is not None
        assert exposure is None
    finally:
        savepoint.rollback()


def test_partial_district_inventory_omits_inventory_and_exposure(
    exported, session_module
):
    savepoint = session_module.begin_nested()
    try:
        event = session_module.scalar(
            select(HazardEvent).where(HazardEvent.external_ref == "al132025")
        )
        advisories = session_module.scalars(
            select(Advisory)
            .where(Advisory.hazard_event_id == event.id, Advisory.observed.is_(False))
        ).all()
        district = exported["districts"][0]
        session_module.execute(
            text(
                "INSERT INTO place_structures "
                "(parish, district, community, structures, built_m2) "
                "VALUES (:parish, :district, 'mapped-test-place', 17, 125.0)"
            ),
            {"parish": district["parish"], "district": district["district"]},
        )
        fingerprint = _insert_inventory_marker(session_module, structures=17)
        _insert_exposure_marker(
            session_module,
            event,
            advisories,
            inventory_fingerprint=fingerprint,
        )

        payload = build_replay(session_module, generated_at=PINNED)
        assert all("structures" not in item for item in payload["districts"])
        assert all("district_exposed" not in frame for frame in payload["frames"])
    finally:
        savepoint.rollback()


def test_building_snapshot_holds_share_locks_on_all_present_tables(session_module):
    # Use the module transaction that built the replay fixture: its own writes
    # hold RowExclusive locks, and PostgreSQL correctly blocks another
    # connection's SHARE request until that fixture ends.
    assert _lock_building_snapshot(session_module) == (True, True, True)
    locks = set(
        session_module.execute(
            text(
                "SELECT c.relname, l.mode "
                "FROM pg_locks l "
                "JOIN pg_class c ON c.oid = l.relation "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE l.pid = pg_backend_pid() "
                "  AND n.nspname = current_schema()"
            )
        ).all()
    )
    for table in (
        "advisory",
        "place_exposure",
        "place_exposure_build",
        "place_structures",
        "place_structure_build",
    ):
        assert (table, "ShareLock") in locks


def test_predigest_marker_schema_fails_closed_instead_of_selecting_missing_columns(
    exported, session_module
):
    savepoint = session_module.begin_nested()
    try:
        session_module.execute(
            text("ALTER TABLE place_exposure_build DROP COLUMN exposure_rows_sha256")
        )
        assert _lock_building_snapshot(session_module) == (True, True, False)

        payload = build_replay(session_module, generated_at=PINNED)
        assert all("structures" not in item for item in payload["districts"])
        assert all("district_exposed" not in frame for frame in payload["frames"])
    finally:
        savepoint.rollback()


@pytest.mark.parametrize(
    "marker_tables_exist",
    [False, True],
    ids=["pre-migration", "upgraded-empty-markers"],
)
def test_legacy_complete_melissa_build_infers_sparse_zero_frames(
    exported, session_module, marker_tables_exist
):
    """The pre-marker builder replaced the full 41-advisory build atomically."""
    savepoint = session_module.begin_nested()
    try:
        event = session_module.scalar(
            select(HazardEvent).where(HazardEvent.external_ref == "al132025")
        )
        advisories = session_module.scalars(
            select(Advisory)
            .where(Advisory.hazard_event_id == event.id, Advisory.observed.is_(False))
            .order_by(Advisory.issued_at)
        ).all()
        district = exported["districts"][0]
        _insert_legacy_inventory(session_module, exported["districts"])
        session_module.execute(
            text(
                "INSERT INTO place_exposure "
                "(advisory_id, parish, district, community, band, structures) "
                "VALUES (:advisory, :parish, :district, 'mapped-test-place', 34, 3)"
            ),
            [
                {
                    "advisory": advisory.id,
                    "parish": district["parish"],
                    "district": district["district"],
                }
                for advisory in advisories[:31]
            ],
        )
        if not marker_tables_exist:
            session_module.execute(text("DROP TABLE place_exposure_build"))
            session_module.execute(text("DROP TABLE place_structure_build"))

        payload = build_replay(session_module, generated_at=PINNED)
        assert all("district_exposed" in frame for frame in payload["frames"])
        assert all(
            all(bands == [0, 0, 0] for bands in frame["district_exposed"])
            for frame in payload["frames"][31:]
        )
        selected_index = next(
            index
            for index, item in enumerate(payload["districts"])
            if item["parish"] == district["parish"]
            and item["district"] == district["district"]
        )
        assert payload["frames"][30]["district_exposed"][selected_index] == [0, 0, 3]
    finally:
        savepoint.rollback()


def test_observed_advisory_cannot_substitute_for_missing_legacy_forecast(
    exported, session_module
):
    savepoint = session_module.begin_nested()
    try:
        event = session_module.scalar(
            select(HazardEvent).where(HazardEvent.external_ref == "al132025")
        )
        advisories = session_module.scalars(
            select(Advisory)
            .where(Advisory.hazard_event_id == event.id, Advisory.observed.is_(False))
            .order_by(Advisory.issued_at)
        ).all()
        _insert_legacy_inventory(session_module, exported["districts"])
        district = exported["districts"][0]
        observed_id = session_module.scalar(
            text(
                "INSERT INTO advisory "
                "(hazard_event_id, advisory_number, issued_at, observed) "
                "VALUES (:event, '31', :issued_at, true) RETURNING id"
            ),
            {"event": event.id, "issued_at": PINNED},
        )
        exposure_ids = [advisory.id for advisory in advisories[:30]] + [observed_id]
        session_module.execute(
            text(
                "INSERT INTO place_exposure "
                "(advisory_id, parish, district, community, band, structures) "
                "VALUES (:advisory, :parish, :district, 'observed-collision', 34, 3)"
            ),
            [
                {
                    "advisory": advisory_id,
                    "parish": district["parish"],
                    "district": district["district"],
                }
                for advisory_id in exposure_ids
            ],
        )

        assert _building_data(session_module, event, advisories) == (None, None)
    finally:
        savepoint.rollback()


def test_geometry_is_geojson_in_lon_lat_order_rounded_on_write(exported):
    """[lon, lat], WGS84, six decimals — about 10 cm."""
    frame = next(f for f in exported["frames"] if "wind64" in f)

    assert frame["track"]["type"] == "LineString"
    assert frame["cone"]["type"] == "Polygon"
    assert frame["wind64"]["type"] in {"Polygon", "MultiPolygon"}

    lon, lat = frame["track"]["coordinates"][0]
    assert -90 < lon < -60 and 5 < lat < 45, f"looks like [lat, lon]: {(lon, lat)}"

    for value in (lon, lat, frame["position"]["lon"], frame["position"]["lat"]):
        assert round(value, 6) == value, value

    for household in exported["households"][:50]:
        assert round(household["lon"], 6) == household["lon"]
        assert round(household["lat"], 6) == household["lat"]


def test_static_geometry_is_emitted_once_and_not_per_frame(exported):
    """The whole reason the file fits in a browser."""
    assert len(exported["parishes"]) == 14
    assert all("geometry" in p for p in exported["parishes"])

    for frame in exported["frames"]:
        assert "parishes" not in frame
        assert "households" not in frame
        assert "districts" not in frame

    for district in exported["districts"]:
        assert "geometry" not in district


def test_the_registry_flag_follows_the_households_not_a_constant(exported):
    """It has to mean "we can help someone here", or it should not be there."""
    held = {h["parish"] for h in exported["households"]}
    flagged = {p["name"] for p in exported["parishes"] if p["registry"]}

    assert flagged == held
    assert flagged, "no parish is flagged as holding a registry"
    assert len(flagged) < 14, "the fixture seeds two parishes, not all fourteen"


def test_the_map_is_drawn_at_the_resolution_the_posture_was_decided_at(exported):
    """One tolerance, imported rather than restated.

    A parish outline simplified here at a different tolerance from the one the
    wind field was tested against would put a household inside the warning on
    screen and outside it in the decision.
    """
    from app.console import export

    assert export.SIMPLIFY_DEG is SIMPLIFY_DEG


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


def test_two_runs_produce_byte_identical_json(exported, session_module):
    """What makes a committed generated file safe.

    The output is checked in, because the Vercel build has no Python and no
    database. That is only defensible if a stale file shows up as a diff, which
    requires the bytes to depend on the database and on nothing else.
    """
    again = serialise(build_replay(session_module, generated_at=PINNED))

    assert again == serialise(exported)
    assert again.endswith(b"\n")


def test_only_generated_at_moves_between_runs(exported, session_module):
    """The one value allowed to differ, and the proof that it is the only one."""
    later = build_replay(
        session_module, generated_at=datetime(2027, 6, 7, 8, 9, 10, tzinfo=UTC)
    )

    assert exported["generated_at"] == "2026-01-02T03:04:05Z"
    assert later["generated_at"] == "2027-06-07T08:09:10Z"

    # Copies, so the module-scoped payload every other test reads is untouched.
    earlier_rest = {k: v for k, v in exported.items() if k != "generated_at"}
    later_rest = {k: v for k, v in later.items() if k != "generated_at"}
    assert serialise(earlier_rest) == serialise(later_rest)


def test_write_replay_puts_a_readable_file_where_it_says_it_did(
    exported, session_module, tmp_path
):
    destination = tmp_path / "public" / "replay" / "replay.json"
    written = write_replay(session_module, destination, generated_at=PINNED)

    assert written == destination
    assert written.exists()

    reloaded = json.loads(written.read_bytes())
    assert reloaded == exported
    assert written.read_bytes() == serialise(exported)


def test_the_export_reads_the_database_and_writes_nothing_to_it(session_module):
    """It is a report, not a step of the replay."""
    counts = text(
        "SELECT (SELECT count(*) FROM risk_assessment) AS ra, "
        "(SELECT count(*) FROM agent_job) AS jobs, "
        "(SELECT count(*) FROM ledger_entry) AS ledger"
    )
    before = session_module.execute(counts).one()
    build_replay(session_module, generated_at=PINNED)
    after = session_module.execute(counts).one()

    assert before == after
