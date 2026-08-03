#!/usr/bin/env python3
"""Cache Jamaica's administrative boundaries and parish populations.

Both come from OCHA's Common Operational Datasets on HDX — the boundaries a
humanitarian response is expected to use, keyed by the same p-codes any partner
agency would recognise. That matters more than convenience: a relief platform
that invents its own geography cannot hand a parish list to anyone.

    boundaries  cod-ab-jam   admin0-3 polygons, admin points   CC BY-IGO
    population  cod-ps-jam   parish totals by age and sex      CC BY-IGO

Cached and committed for the same reason as the advisories: the replay makes no
network calls, and the registry seeder has to place the same households in the
same places on every machine.

    python data/replay/fetch_geography.py            # fetch what is missing
    python data/replay/fetch_geography.py --force    # refetch everything
    python data/replay/fetch_geography.py --verify   # check manifest, no network

Standard library only, so a clean clone can rebuild the cache without installing
the API environment first.
"""

from __future__ import annotations

import argparse
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import manifest  # noqa: E402  — sibling module, not an installed package

CACHE = manifest.CACHE
JAMAICA = CACHE / "jamaica"

HDX = "https://data.humdata.org/dataset"

#: Kept as the publisher's own artefacts, unmodified. Trimming the zip to the
#: three layers we use would shrink it, and would also mean the checksum no
#: longer matches anything OCHA published — which is the provenance claim worth
#: more than four megabytes.
SOURCES = {
    "jam_admin_boundaries.shp.zip": (
        f"{HDX}/fee5299d-52f5-4273-a878-56175210da82/resource/"
        "22340d86-48a3-4dbb-bec9-772d0187e25d/download/jam_admin_boundaries.shp.zip"
    ),
    "jam_adm1_pop.csv": (
        f"{HDX}/d6655bd6-4213-4b83-b394-e23a80a9dada/resource/"
        "3d43ebef-07a2-4383-9633-59b681abaa7e/download/jam_adm1_pop_v2.csv"
    ),
}

USER_AGENT = (
    "project-lighthouse/0.1 (disaster relief coordination research; "
    "one-time reference data fetch)"
)


def ssl_context() -> ssl.SSLContext:
    """Prefer certifi where it exists — see fetch_advisories for the why."""
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


_SSL = ssl_context()


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=180, context=_SSL) as response:
            return response.read()
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), ssl.SSLCertVerificationError):
            raise RuntimeError(
                f"TLS verification failed for {url}. Either `pip install certifi`, "
                "or run /Applications/Python\\ 3.13/Install\\ Certificates.command"
            ) from exc
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="refetch files already cached")
    parser.add_argument("--verify", action="store_true", help="check the manifest, fetch nothing")
    args = parser.parse_args()

    if args.verify:
        return manifest.verify()

    JAMAICA.mkdir(parents=True, exist_ok=True)
    fetched = skipped = 0

    for name, url in SOURCES.items():
        target = JAMAICA / name
        if target.exists() and not args.force:
            skipped += 1
            continue
        print(f"  fetching {name}")
        target.write_bytes(fetch(url))
        fetched += 1

    lines = manifest.write()
    size = sum(p.stat().st_size for p in JAMAICA.iterdir())
    print(
        f"\n{fetched} fetched, {skipped} already cached. "
        f"{size / 1_048_576:.1f} MB of reference data. "
        f"manifest.sha256 now pins {lines} files.\n"
        "Source: OCHA Common Operational Datasets via HDX, CC BY-IGO."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
