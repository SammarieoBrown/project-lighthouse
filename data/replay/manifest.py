"""Checksum manifest for everything under ``cache/``.

Shared by the fetch scripts so there is one definition of what the cache is
supposed to contain. Two scripts writing into the same directory with two
opinions about the manifest is how a "verified" cache quietly stops being
verified.

Standard library only, so a clean clone can check the cache without installing
anything.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

CACHE = Path(__file__).parent / "cache"
MANIFEST = CACHE / "manifest.sha256"


def build() -> str:
    """sha256 of every cached file, sorted by path — the sha256sum format."""
    lines = []
    for path in sorted(CACHE.rglob("*")):
        if not path.is_file() or path == MANIFEST:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.relative_to(CACHE).as_posix()}")
    return "\n".join(lines) + "\n"


def write() -> int:
    MANIFEST.write_text(build())
    return len(MANIFEST.read_text().splitlines())


def _parse(text: str) -> dict[str, str]:
    return {
        name: digest
        for digest, name in (line.split("  ", 1) for line in text.splitlines() if line)
    }


def verify() -> int:
    """Exit code, not a boolean — this is what CI runs."""
    if not MANIFEST.exists():
        print("manifest.sha256 is missing — the cache has never been built", file=sys.stderr)
        return 1

    expected = _parse(MANIFEST.read_text())
    actual = _parse(build())

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
            f"\n{len(missing)} missing, {len(extra)} untracked, {len(changed)} changed.\n"
            "Do not regenerate to make this pass. Find out what moved first — a "
            "replay that quietly changed is not a replay.",
            file=sys.stderr,
        )
        return 1

    print(f"cache verified — {len(expected)} files match manifest.sha256")
    return 0
