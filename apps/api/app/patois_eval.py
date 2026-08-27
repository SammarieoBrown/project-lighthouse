"""Scoring Patois transcription and extraction against a held-out set (NFR-G-03).

**This ships the ruler, not the measurement.** The eval set itself is roughly a
hundred hand-labelled utterances from Jamaican speakers, and collecting it
depends on other people saying words into a phone. Nothing here generates
audio, and nothing here invents a label. A synthetic eval set would score well
and mean nothing, which is worse than having none — the whole reason NFR-G-03
exists is to find out whether the transcription is good enough to trust, and a
ruler that always reads "fine" cannot answer that.

So: drop recordings and labels into ``data/patois-eval/`` in the format below,
run the scorer, and it reports word error rate against the reference transcript
plus per-field extraction accuracy. Until then it reports an empty set and says
so, which is the honest reading.

**Why WER and field accuracy separately.** Phase 2 makes the LoRA fine-tune
conditional on which half is the bottleneck: if transcription is wrong, that is
GPU time; if extraction is wrong, it is a prompting problem and costs an
afternoon. A single blended score cannot tell those apart, so this does not
produce one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
EVAL_DIR = REPO_ROOT / "data" / "patois-eval"
MANIFEST = EVAL_DIR / "manifest.jsonl"

#: Fields the extractor is scored on. Deliberately the ones it actually
#: persists — scoring it on fields nothing reads would flatter or damn it for
#: no operational reason.
SCORED_FIELDS = ("damage_type", "reported_needs")

_TOKEN = re.compile(r"[a-z0-9']+")


@dataclass(frozen=True, slots=True)
class EvalCase:
    """One held-out utterance and what a human says is in it."""

    utterance_id: str
    audio_path: str | None
    reference_transcript: str
    reference_damage_type: str | None
    reference_needs: tuple[str, ...]


@dataclass
class EvalReport:
    cases: int = 0
    #: Word error rate across the set, 0..1+. None when there is nothing to
    #: score — not 0.0, which would read as perfect.
    word_error_rate: float | None = None
    field_accuracy: dict[str, float] = field(default_factory=dict)
    per_case: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "cases": self.cases,
            "word_error_rate": self.word_error_rate,
            "field_accuracy": self.field_accuracy,
            "per_case": self.per_case,
            "notes": self.notes,
        }


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.casefold())


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Levenshtein distance over words, divided by reference length.

    Word-level rather than character-level because a Patois transcript that
    spells a word differently but reads the same is not the failure we care
    about; a transcript that loses "roof" is.
    """
    ref, hyp = _tokens(reference), _tokens(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    previous = list(range(len(hyp) + 1))
    for i, ref_word in enumerate(ref, start=1):
        current = [i]
        for j, hyp_word in enumerate(hyp, start=1):
            current.append(
                previous[j - 1]
                if ref_word == hyp_word
                else 1 + min(previous[j - 1], previous[j], current[j - 1])
            )
        previous = current
    return previous[-1] / len(ref)


def load_cases(manifest: Path | None = None) -> list[EvalCase]:
    """Read the held-out set. An absent or empty file is a real answer."""
    path = manifest or MANIFEST
    if not path.exists():
        return []
    cases: list[EvalCase] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        row = json.loads(line)
        cases.append(
            EvalCase(
                utterance_id=str(row["utterance_id"]),
                audio_path=row.get("audio_path"),
                reference_transcript=str(row["reference_transcript"]),
                reference_damage_type=row.get("reference_damage_type"),
                reference_needs=tuple(row.get("reference_needs") or ()),
            )
        )
    return cases


def score(
    cases: list[EvalCase],
    transcribe,
    *,
    extract=None,
) -> EvalReport:
    """Score a transcriber and the extractor against the held-out set.

    ``transcribe`` takes an ``EvalCase`` and returns a transcript string, which
    keeps this scorer independent of whether the model runs locally, behind an
    API, or is a stub in a test.
    """
    from app.intake.extraction import extract_claim_fields

    extract = extract or extract_claim_fields
    report = EvalReport()
    if not cases:
        report.notes.append(
            "No eval cases found. Add recordings and hand-written labels to "
            f"{EVAL_DIR.relative_to(REPO_ROOT)}/ — this reports nothing rather "
            "than inventing a baseline."
        )
        return report

    rates: list[float] = []
    hits = {name: 0 for name in SCORED_FIELDS}
    for case in cases:
        hypothesis = transcribe(case)
        rate = word_error_rate(case.reference_transcript, hypothesis)
        rates.append(rate)

        extracted = extract(hypothesis)
        got_damage = extracted.damage_type
        got_needs = tuple(extracted.reported_needs)
        damage_ok = got_damage == case.reference_damage_type
        needs_ok = set(got_needs) == set(case.reference_needs)
        hits["damage_type"] += int(damage_ok)
        hits["reported_needs"] += int(needs_ok)

        report.per_case.append(
            {
                "utterance_id": case.utterance_id,
                "word_error_rate": round(rate, 4),
                "damage_type_correct": damage_ok,
                "reported_needs_correct": needs_ok,
                "expected_damage_type": case.reference_damage_type,
                "got_damage_type": got_damage,
            }
        )

    report.cases = len(cases)
    report.word_error_rate = round(sum(rates) / len(rates), 4)
    report.field_accuracy = {
        name: round(hits[name] / len(cases), 4) for name in SCORED_FIELDS
    }
    # The decision Phase 2 hangs on this, stated rather than left to a reader.
    if report.word_error_rate > 0.25:
        report.notes.append(
            "Transcription looks like the bottleneck: WER above 0.25 means the "
            "extractor is being fed bad text, and fine-tuning is GPU time well "
            "spent."
        )
    elif min(report.field_accuracy.values(), default=1.0) < 0.8:
        report.notes.append(
            "Extraction looks like the bottleneck: the transcript is mostly "
            "right and the fields are not. That is a prompting problem and "
            "costs an afternoon rather than a week."
        )
    return report


__all__ = [
    "EVAL_DIR",
    "MANIFEST",
    "SCORED_FIELDS",
    "EvalCase",
    "EvalReport",
    "load_cases",
    "score",
    "word_error_rate",
]
