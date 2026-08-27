"""The Patois eval harness (NFR-G-03).

What is tested here is the ruler. The set itself does not exist yet and these
tests must never be the thing that makes it look like it does — so the case
that matters most is the empty one, which has to report nothing rather than a
baseline.
"""

from __future__ import annotations

import json

from app.patois_eval import (
    EvalCase,
    load_cases,
    score,
    word_error_rate,
)


def _case(transcript, damage=None, needs=()):
    return EvalCase(
        utterance_id="test-1",
        audio_path=None,
        reference_transcript=transcript,
        reference_damage_type=damage,
        reference_needs=tuple(needs),
    )


# -- the honest empty answer -------------------------------------------------


def test_an_empty_set_reports_nothing_rather_than_a_baseline():
    """A ruler that always reads "fine" cannot answer the question NFR-G-03
    exists to ask."""
    report = score([], transcribe=lambda case: "")

    assert report.cases == 0
    assert report.word_error_rate is None  # not 0.0, which reads as perfect
    assert report.field_accuracy == {}
    assert any("rather than inventing" in note for note in report.notes)


def test_a_missing_manifest_is_an_empty_set_not_an_error(tmp_path):
    assert load_cases(tmp_path / "absent.jsonl") == []


def test_the_manifest_format_round_trips(tmp_path):
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        "# a comment line is skipped\n"
        + json.dumps(
            {
                "utterance_id": "se-001",
                "audio_path": "audio/se-001.ogg",
                "reference_transcript": "Mi roof gone an mi need tarpaulin",
                "reference_damage_type": "roof_damage",
                "reference_needs": ["tarpaulin"],
            }
        )
        + "\n\n"
    )

    cases = load_cases(manifest)

    assert len(cases) == 1
    assert cases[0].utterance_id == "se-001"
    assert cases[0].reference_needs == ("tarpaulin",)


# -- word error rate ---------------------------------------------------------


def test_a_perfect_transcript_scores_zero():
    assert word_error_rate("mi roof gone", "Mi roof gone") == 0.0


def test_a_dropped_word_costs_one_over_the_reference_length():
    """A transcript that loses "roof" is the failure this measures."""
    assert word_error_rate("mi roof gone", "mi gone") == 1 / 3


def test_an_empty_hypothesis_scores_one():
    assert word_error_rate("mi roof gone", "") == 1.0


def test_punctuation_and_case_do_not_count_as_errors():
    """A Patois transcript spelled differently but read the same is not the
    failure we care about."""
    assert word_error_rate("Mi roof gone!", "mi roof gone") == 0.0


# -- the two halves, reported separately -------------------------------------


def test_transcription_and_extraction_are_scored_apart():
    """Phase 2 makes the fine-tune conditional on which half is the
    bottleneck, and a blended score cannot tell them apart."""
    cases = [_case("mi roof gone an mi need tarpaulin", "roof_damage", ("tarpaulin",))]

    report = score(cases, transcribe=lambda case: case.reference_transcript)

    assert report.cases == 1
    assert report.word_error_rate == 0.0
    assert report.field_accuracy["damage_type"] == 1.0
    assert report.field_accuracy["reported_needs"] == 1.0


def test_bad_audio_is_named_as_a_transcription_problem():
    cases = [_case("mi roof gone an mi need tarpaulin", "roof_damage", ("tarpaulin",))]

    report = score(cases, transcribe=lambda case: "something else entirely here now")

    assert report.word_error_rate > 0.25
    assert any("Transcription looks like the bottleneck" in n for n in report.notes)


def test_good_audio_with_missed_fields_is_named_as_a_prompting_problem():
    """The transcript is right and the fields are not — an afternoon, not a
    week of GPU time."""
    # A real gap in the extractor: "di zinc dem fly weh" is a roof gone, and
    # the rules only match the word "roof". Transcription is perfect and the
    # field is still missed, which is exactly the shape this note is for.
    cases = [_case("di zinc dem fly weh", "roof_damage", ())]

    report = score(cases, transcribe=lambda case: case.reference_transcript)

    assert report.word_error_rate == 0.0
    assert report.field_accuracy["damage_type"] < 1.0
    assert any("Extraction looks like the bottleneck" in n for n in report.notes)


def test_every_case_is_reported_individually():
    cases = [
        _case("mi roof gone", "roof_damage", ()),
        _case("di water inna mi house", "flooding", ()),
    ]

    report = score(cases, transcribe=lambda case: case.reference_transcript)

    assert len(report.per_case) == 2
    assert {row["utterance_id"] for row in report.per_case} == {"test-1"}
    assert all("word_error_rate" in row for row in report.per_case)
