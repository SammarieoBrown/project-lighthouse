#!/usr/bin/env python3
"""Fetch Jamaica's building footprints.

The registry is 2,000 synthetic households scattered inside community polygons
by ``ST_GeneratePoints``. Every one sits where nothing necessarily stands. That
is honest for a seeded demo and useless as an exposure denominator: "413 of our
500 synthetic homes" is a sentence about our seed, not about Jamaica.

These footprints are the denominator. **1,844,379 buildings**, which is 1.8× the
1,022,977 OSM has — the difference is rural coverage OSM has never mapped, and
that is precisely the population the registry claims to serve. Using OSM alone
would have undercounted the countryside by roughly half and looked fine doing it.

    python3 data/buildings/fetch_footprints.py            # fetch if missing
    python3 data/buildings/fetch_footprints.py --force    # refetch
    python3 data/buildings/fetch_footprints.py --verify   # manifest only, no network

Source is VIDA's combined build: Google Open Buildings, Microsoft GlobalML and
OSM, already deduplicated against each other, published per country as one
GeoParquet. Google supplies 87.9%, OSM 7.7%, Microsoft 4.5%. ODbL, inherited
from OSM.

**Fetch-only, never committed.** 232 MB is far past what belongs in git, and
unlike the storm advisories this is not something a replay has to reproduce
byte-for-byte on a demo laptop — it is loaded into Postgres once and queried
from there.

Standard library only, so a clean clone can fetch without the API environment.
"""

from __future__ import annotations

import argparse
import shutil
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
import manifest  # noqa: E402  — one definition of "verified", shared by every cache

HERE = Path(__file__).parent
CACHE = HERE / "cache"

SOURCE = (
    "https://data.source.coop/vida/google-microsoft-osm-open-buildings"
    "/geoparquet/by_country/country_iso=JAM/JAM.parquet"
)
TARGET = CACHE / "jamaica-buildings.parquet"

#: Measured 2026-08-03. A size that has moved is a republished build, not a
#: corrupt download — check what changed upstream before refetching over it.
EXPECTED_BYTES = 231_863_933

#: source.coop sits behind a CDN that answers some default agents with a 403.
USER_AGENT = "project-lighthouse-footprints/1.0 (+https://github.com/SammarieoBrown/project-lighthouse)"


def ssl_context() -> ssl.SSLContext:
    """Prefer certifi where present — see data/replay/fetch_advisories.py."""
    try:
        import certifi
    except ModuleNotFoundError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


_SSL = ssl_context()


def fetch(force: bool = False) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)

    if TARGET.exists() and not force:
        size = TARGET.stat().st_size
        if size == EXPECTED_BYTES:
            print(f"{TARGET.name} already cached ({size:,} bytes)")
            return TARGET
        print(f"{TARGET.name} is {size:,} bytes, expected {EXPECTED_BYTES:,} — refetching")

    request = urllib.request.Request(SOURCE, headers={"User-Agent": USER_AGENT})
    # Straight to a temp name so an interrupted download cannot be mistaken for
    # a complete one on the next run.
    partial = TARGET.with_suffix(".partial")
    try:
        with urllib.request.urlopen(request, timeout=300, context=_SSL) as response:
            total = int(response.headers.get("content-length") or 0)
            print(f"fetching {total / 1e6:.0f} MB from source.coop …")
            with partial.open("wb") as fh:
                shutil.copyfileobj(response, fh, length=1 << 20)
    except urllib.error.HTTPError as exc:
        sys.exit(f"source.coop returned {exc.code} for {SOURCE}")
    except OSError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ssl.SSLCertVerificationError):
            sys.exit(
                "TLS verification failed. Either `pip install certifi`, or run "
                '"Install Certificates.command" from your Python install.'
            )
        sys.exit(f"download failed: {exc}")

    size = partial.stat().st_size
    if size != EXPECTED_BYTES:
        print(
            f"warning: got {size:,} bytes, expected {EXPECTED_BYTES:,}. "
            "Upstream may have republished — check before trusting the counts.",
            file=sys.stderr,
        )
    partial.replace(TARGET)
    print(f"wrote {TARGET.relative_to(Path.cwd()) if TARGET.is_relative_to(Path.cwd()) else TARGET} ({size:,} bytes)")
    return TARGET


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="refetch even if cached")
    ap.add_argument("--verify", action="store_true", help="check the manifest, no network")
    args = ap.parse_args()

    if args.verify:
        sys.exit(manifest.verify(CACHE))

    fetch(force=args.force)
    n = manifest.write(CACHE)
    print(f"manifest.sha256 written — {n} file(s)")


if __name__ == "__main__":
    main()
