"""Closed, non-identifying taxonomy for facts bound into public receipts.

Raw household text must never cross into the public ledger payload.  Approval
maps the small set of supported operational aliases to canonical values, and
publication accepts canonical values only.  Expanding either vocabulary is an
explicit release decision rather than an accidental consequence of intake.
"""

from __future__ import annotations


PUBLIC_PARISHES = frozenset(
    {
        "Clarendon",
        "Hanover",
        "Kingston",
        "Manchester",
        "Portland",
        "Saint Andrew",
        "Saint Ann",
        "Saint Catherine",
        "Saint Elizabeth",
        "Saint James",
        "Saint Mary",
        "Saint Thomas",
        "Trelawny",
        "UNSPECIFIED",
        "Westmoreland",
    }
)

PUBLIC_NEED_CATEGORIES = frozenset(
    {
        "ACCESS_BLOCKED",
        "CONTENTS_DAMAGE",
        "ESSENTIAL_SERVICES",
        "FLOOD_DAMAGE",
        "OTHER_DAMAGE",
        "ROOF_DAMAGE",
        "STRUCTURAL_DAMAGE",
    }
)

_PARISH_ALIASES = {
    **{name.casefold(): name for name in PUBLIC_PARISHES},
    "st andrew": "Saint Andrew",
    "st ann": "Saint Ann",
    "st catherine": "Saint Catherine",
    "st elizabeth": "Saint Elizabeth",
    "st james": "Saint James",
    "st mary": "Saint Mary",
    "st thomas": "Saint Thomas",
}

_NEED_ALIASES = {
    "access_blocked": "ACCESS_BLOCKED",
    "blocked_access": "ACCESS_BLOCKED",
    "contents_damage": "CONTENTS_DAMAGE",
    "essential_services": "ESSENTIAL_SERVICES",
    "utility_loss": "ESSENTIAL_SERVICES",
    "flood_damage": "FLOOD_DAMAGE",
    "flooding": "FLOOD_DAMAGE",
    "roof_damage": "ROOF_DAMAGE",
    "roof_loss": "ROOF_DAMAGE",
    "structural_damage": "STRUCTURAL_DAMAGE",
    "wall_damage": "STRUCTURAL_DAMAGE",
}


def canonical_public_parish(value: object) -> str:
    if not isinstance(value, str):
        return "UNSPECIFIED"
    canonical = _PARISH_ALIASES.get(value.strip().casefold())
    if canonical is None:
        return "UNSPECIFIED"
    return canonical


def canonical_public_need_category(value: object) -> str:
    if not isinstance(value, str):
        return "OTHER_DAMAGE"
    normalized = value.strip()
    if normalized in PUBLIC_NEED_CATEGORIES:
        return normalized
    canonical = _NEED_ALIASES.get(normalized.casefold())
    if canonical is None:
        return "OTHER_DAMAGE"
    return canonical


def validate_public_taxonomy(*, parish: object, need_category: object) -> None:
    """Accept only canonical values already safe to bind into a receipt."""
    if parish not in PUBLIC_PARISHES:
        raise ValueError("unsupported public parish")
    if need_category not in PUBLIC_NEED_CATEGORIES:
        raise ValueError("unsupported public need category")


__all__ = [
    "PUBLIC_NEED_CATEGORIES",
    "PUBLIC_PARISHES",
    "canonical_public_need_category",
    "canonical_public_parish",
    "validate_public_taxonomy",
]
