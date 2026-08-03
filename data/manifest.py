"""Checksum manifest for a committed data cache.

Shared by every fetch script so there is one definition of what "verified"
means. Two scripts writing into the same directory with two opinions about the
manifest is how a verified cache quietly stops being verified.

Each cache directory gets its own manifest — the storm advisories and the map
tiles are fetched by different scripts, at different cadences, from different
publishers, and a single combined manifest would mean rebuilding one always
looks like tampering with the other.

Standard library only, so a clean clone can check a cache without installing
anything.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

#: Default for the scripts that predate a second cache.
CACHE = Path(__file__).parent / "replay" / "cache"


def manifest_path(cache: Path) -> Path:
    return cache / "manifest.sha256"


def build(cache: Path = CACHE) -> str:
    """sha256 of every cached file, sorted by path — the sha256sum format."""
    target = manifest_path(cache)
    lines = []
    for path in sorted(cache.rglob("*")):
        if not path.is_file() or path == target:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.relative_to(cache).as_posix()}")
    return "\n".join(lines) + "\n"


def write(cache: Path = CACHE) -> int:
    target = manifest_path(cache)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(build(cache))
    return len(target.read_text().splitlines())


def _parse(text: str) -> dict[str, str]:
    return {
        name: digest
        for digest, name in (line.split("  ", 1) for line in text.splitlines() if line)
    }


def verify(cache: Path = CACHE) -> int:
    """Exit code, not a boolean — this is what CI runs."""
    target = manifest_path(cache)
    if not target.exists():
        print(f"{target} is missing — the cache has never been built", file=sys.stderr)
        return 1

    expected = _parse(target.read_text())
    actual = _parse(build(cache))

    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    changed = sorted(n for n in set(expected) & set(actual) if expected[n] != actual[n])

    for name in missing:
        print(f"MISSING   {name}", file=sys.stderr)
    for name in extra:
        print(f"UNTRACKED {name}", file=sys.stderr)
    for name in changed:
        print(f"CHANGED   {name}", file=sys.stderr)

    if missing or extra or changed:
        print(
            f"\n{len(missing)} missing, {len(extra)} untracked, {len(changed)} changed "
            f"in {cache.name}.\n"
            "Do not regenerate to make this pass. Find out what moved first — a "
            "replay that quietly changed is not a replay.",
            file=sys.stderr,
        )
        return 1

    print(f"{cache.parent.name}/{cache.name} verified — {len(expected)} files match manifest.sha256")
    return 0
