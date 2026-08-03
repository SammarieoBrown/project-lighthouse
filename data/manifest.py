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
from typing import Iterable

#: Default for the scripts that predate a second cache.
CACHE = Path(__file__).parent / "replay" / "cache"

#: Optional newline-delimited glob rules, relative to a cache directory. Some
#: caches contain both fetched source material and large derived build outputs.
#: The source manifest must not silently adopt those outputs merely because a
#: build happened to run before ``manifest.write``.
IGNORE_FILE = ".manifestignore"


def manifest_path(cache: Path) -> Path:
    return cache / "manifest.sha256"


def sha256_file(path: Path, *, chunk_size: int = 1 << 20) -> str:
    """Hash a file without reading a multi-hundred-megabyte asset into RAM."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse(text: str) -> dict[str, str]:
    """Parse the two-space ``sha256sum`` format and reject unsafe names."""
    entries: dict[str, str] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line:
            continue
        try:
            digest, name = line.split("  ", 1)
        except ValueError as exc:
            raise ValueError(f"invalid checksum line {line_number}: {line!r}") from exc
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError(f"invalid sha256 on line {line_number}: {digest!r}")
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or not name:
            raise ValueError(f"unsafe manifest path on line {line_number}: {name!r}")
        if name in entries:
            raise ValueError(f"duplicate manifest path on line {line_number}: {name!r}")
        entries[name] = digest
    return entries


def read(path: Path) -> dict[str, str]:
    """Read a checksum manifest. Kept public for publishers and build tools."""
    return parse(path.read_text())


def pinned_digest(cache: Path, path: Path) -> str | None:
    """Return the committed digest for one cache file, if a manifest exists."""
    try:
        relative = path.relative_to(cache).as_posix()
    except ValueError as exc:
        raise ValueError(f"{path} is outside cache {cache}") from exc
    target = manifest_path(cache)
    if not target.exists():
        return None
    return read(target).get(relative)


def _ignore_patterns(cache: Path) -> tuple[str, ...]:
    path = cache / IGNORE_FILE
    if not path.exists():
        return ()
    return tuple(
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def _ignored(relative: Path, patterns: Iterable[str]) -> bool:
    return any(relative.match(pattern) for pattern in patterns)


def build(cache: Path = CACHE) -> str:
    """sha256 of every cached file, sorted by path — the sha256sum format."""
    target = manifest_path(cache)
    partial = target.with_name(f"{target.name}.partial")
    patterns = _ignore_patterns(cache)
    lines = []
    for path in sorted(cache.rglob("*")):
        if not path.is_file() or path in (target, partial):
            continue
        relative = path.relative_to(cache)
        if _ignored(relative, patterns):
            continue
        lines.append(f"{sha256_file(path)}  {relative.as_posix()}")
    return "\n".join(lines) + "\n"


def write(cache: Path = CACHE) -> int:
    target = manifest_path(cache)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f"{target.name}.partial")
    partial.unlink(missing_ok=True)
    content = build(cache)
    partial.write_text(content)
    partial.replace(target)
    return len(content.splitlines())


def verify(cache: Path = CACHE) -> int:
    """Exit code, not a boolean — this is what CI runs."""
    target = manifest_path(cache)
    if not target.exists():
        print(f"{target} is missing — the cache has never been built", file=sys.stderr)
        return 1

    try:
        expected = read(target)
        actual = parse(build(cache))
    except ValueError as exc:
        print(f"INVALID   {exc}", file=sys.stderr)
        return 1

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
