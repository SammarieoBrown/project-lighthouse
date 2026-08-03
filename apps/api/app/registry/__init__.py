"""The synthetic household registry and the geography it sits in.

Synthetic only, for the whole buildathon. Every Storm File this creates carries
``synthetic = true``, and no real household data enters the system until a data
sharing agreement exists.
"""

from app.registry.geography import (
    REPLAY_PARISHES,
    Community,
    Parish,
    load_communities,
    load_parishes,
)
from app.registry.seeder import SeedReport, seed_registry, vulnerability

__all__ = [
    "REPLAY_PARISHES",
    "Community",
    "Parish",
    "SeedReport",
    "load_communities",
    "load_parishes",
    "seed_registry",
    "vulnerability",
]
