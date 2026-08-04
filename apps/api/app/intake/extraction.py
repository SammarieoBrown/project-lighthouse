"""Conservative structured extraction for English and Jamaican Patois intake.

These rules fill only fields that the household actually said.  They are not a
verification model and never infer entitlement, severity, identity, or truth.
Unknown remains ``None``/empty and keeps the claim visibly partial.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


_DAMAGE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "structural_damage",
        re.compile(
            r"\b(?:house|home|place)\s+(?:is\s+|completely\s+)?"
            r"(?:destroy(?:ed)?|gone|collapse[ds]?|mash(?:ed)?\s+up)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "roof_damage",
        re.compile(
            r"\broof\s+(?:is\s+|did\s+)?"
            r"(?:gone|off|damage[ds]?|blow(?:n|\s+off)?|leak(?:ing|s)?|collapse[ds]?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "wall_damage",
        re.compile(
            r"\bwall(?:s)?\s+(?:is\s+|did\s+)?"
            r"(?:crack(?:ed|ing|s)?|fall(?:en|\s+down)?|gone|damage[ds]?|collapse[ds]?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "flooding",
        re.compile(
            r"\b(?:flood(?:ed|ing)?|water|wata|waata)\b.{0,28}"
            r"\b(?:come|coming|a\s+come|inside|inna|house|yard|rise|rising)\b|"
            r"\b(?:house|home|yard)\b.{0,24}\b(?:full|flood)\b.{0,12}"
            r"\b(?:water|wata|waata)\b",
            re.IGNORECASE,
        ),
    ),
)

_NEED_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "tarpaulin",
        re.compile(r"\b(?:tarpaulin|tarp|blue\s+sheet)\b", re.IGNORECASE),
    ),
    (
        "water",
        re.compile(
            r"\b(?:need|needs|needing|no|without|out\s+of|want|waan)\b.{0,18}"
            r"\b(?:drinking\s+)?(?:water|wata|waata)\b|"
            r"\b(?:water|wata|waata)\s+(?:fi|to)\s+drink\b",
            re.IGNORECASE,
        ),
    ),
    (
        "food",
        re.compile(
            r"\b(?:need|needs|needing|no|without|out\s+of|want|waan)\b.{0,18}"
            r"\b(?:food|meal|mealies)\b|\b(?:hungry|starving)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "shelter",
        re.compile(
            r"\b(?:need|needs|needing|no|without|want|waan)\b.{0,18}"
            r"\b(?:shelter|place\s+(?:to|fi)\s+(?:stay|sleep))\b|"
            r"\bnowhere\s+(?:to|fi)\s+(?:stay|sleep)\b",
            re.IGNORECASE,
        ),
    ),
    ("insulin", re.compile(r"\binsulin\b", re.IGNORECASE)),
    (
        "medicine",
        re.compile(
            r"\b(?:need|needs|needing|no|without|out\s+of)\b.{0,18}"
            r"\b(?:medicine|medication|tablets?|pills?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "medical_support",
        re.compile(
            r"\b(?:need|needs|needing|want|waan)\b.{0,18}"
            r"\b(?:doctor|nurse|ambulance|medical\s+(?:care|help|support))\b|"
            r"\b(?:injured|bleeding|cannot\s+breathe|can't\s+breathe)\b",
            re.IGNORECASE,
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class ExtractedClaimFields:
    damage_type: str | None
    reported_needs: tuple[str, ...]

    def is_complete(self, *, transcript: str | None, lang: str | None) -> bool:
        """Completeness means the three persisted intake fields are known.

        This is intentionally narrower than verification. A complete extraction
        says only that the report named damage and a need in transcribed speech;
        it does not say either claim is true.
        """
        return bool(
            transcript
            and transcript.strip()
            and lang
            and self.damage_type
            and self.reported_needs
        )


def extract_claim_fields(text: str) -> ExtractedClaimFields:
    if not isinstance(text, str):
        raise TypeError("claim extraction input must be text")
    damage = next(
        (canonical for canonical, pattern in _DAMAGE_RULES if pattern.search(text)),
        None,
    )
    needs = tuple(
        canonical for canonical, pattern in _NEED_RULES if pattern.search(text)
    )
    return ExtractedClaimFields(damage_type=damage, reported_needs=needs)


__all__ = ["ExtractedClaimFields", "extract_claim_fields"]
