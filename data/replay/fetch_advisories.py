#!/usr/bin/env python3
"""Rebuild the Hurricane Melissa advisory cache from the NHC archive.

Melissa is AL132025 — the thirteenth Atlantic storm of 2025 — and she made
landfall on Jamaica on 28 October 2025. The replay walks her real advisory
history through the system, so this script fetches that history once and the
demo never touches the network again (PRD RPL-01).

Run it when the cache needs regenerating. Do not run it to "fix" a failing
manifest: the manifest failing means something changed upstream, and finding
out what changed is the entire point of having one.

    python data/replay/fetch_advisories.py            # fetch what is missing
    python data/replay/fetch_advisories.py --force    # refetch everything
    python data/replay/fetch_advisories.py --verify   # check manifest, no network

Standard library only, on purpose. This has to run from a clean clone without
installing the API's environment first.
"""

from __future__ import annotations

import argparse
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

STORM = "al132025"
YEAR = 2025
INDEX_URL = f"https://www.nhc.noaa.gov/archive/{YEAR}/MELISSA.shtml"
TEXT_BASE = f"https://www.nhc.noaa.gov/archive/{YEAR}/al13"
GIS_BASE = "https://www.nhc.noaa.gov/gis/forecast/archive"
BEST_TRACK_URL = f"https://www.nhc.noaa.gov/gis/best_track/{STORM}_best_track.zip"

sys.path.insert(0, str(Path(__file__).parent))
import manifest  # noqa: E402  — sibling module, not an installed package

CACHE = manifest.CACHE

# The five products the replay actually consumes, plus the two that make the
# demo legible. Each maps to a directory under cache/<storm>/text/.
#
#   fstadv    Forecast/Advisory — centre position, max wind, wind radii, and
#             the forecast track. The machine-readable backbone.
#   wndprb    Wind speed probabilities — the 34/50/64 kt product. This is what
#             says a parish has a 38% chance of hurricane-force wind, and it is
#             the one that drives risk. The cone describes where the storm
#             centre might go, which is a different and less useful question.
#   public    Public advisory — watches and warnings in prose, by parish.
#   public_a  Intermediate public advisories, issued between the six-hourly
#             ones as landfall approaches.
#   discus    Forecast discussion — the forecaster's reasoning. Not parsed;
#             kept because it is the best narration available for the demo.
#   update    Tropical cyclone updates — irregular, issued when something
#             changes abruptly. Numbered by timestamp rather than sequence.
TEXT_PRODUCTS = ("fstadv", "wndprb", "public", "public_a", "discus", "update")

# GIS bundles, per advisory. The 5day archive carries the cone polygon, the
# track line, the forecast points and the watch/warning segments in one zip;
# there is no standalone watches/warnings archive, which is worth knowing
# before you go looking for one.
GIS_PRODUCTS = ("5day", "fcst")

USER_AGENT = (
    "project-lighthouse/0.1 (disaster relief coordination research; "
    "one-time archive fetch)"
)


def ssl_context() -> ssl.SSLContext:
    """A context that trusts NOAA on a stock macOS python.org install.

    Python from python.org ships its own OpenSSL and does not read the system
    keychain, so urllib fails to verify certificates that curl handles fine
    until someone runs Install Certificates.command. Rather than make the cache
    depend on a machine being set up correctly, prefer certifi's bundle when it
    is importable and fall back to the default. Never disable verification —
    silently trusting anything is not a fix, it is a different bug.
    """
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


_SSL = ssl_context()


def fetch(url: str, *, retries: int = 3) -> bytes:
    """GET with retries. The archive is a public good — go gently."""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=60, context=_SSL) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            # A 404 is an answer, not a network problem — some advisories have
            # no GIS bundle. Retrying it just wastes the archive's time.
            raise RuntimeError(f"HTTP {exc.code}: {url}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
            if isinstance(getattr(exc, "reason", None), ssl.SSLCertVerificationError):
                raise RuntimeError(
                    f"TLS verification failed for {url}.\n"
                    "This machine's Python cannot verify certificates. Either "
                    "`pip install certifi`, or run:\n"
                    '  /Applications/Python\\ 3.13/Install\\ Certificates.command'
                ) from exc
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"failed after {retries} attempts: {url}") from last


def extract_product(html: bytes) -> str:
    """Pull the advisory text out of NHC's HTML wrapper.

    We store the <pre> block rather than the page. The advisory is the product;
    the surrounding navigation is presentation, and it changes for reasons that
    have nothing to do with the storm. Caching the whole page would mean the
    manifest breaks the next time NOAA reworks its header, which trains you to
    ignore exactly the alarm you built it to hear.
    """
    text = html.decode("utf-8", errors="replace")
    match = re.search(r"<pre>(.*?)</pre>", text, re.S | re.I)
    if not match:
        raise ValueError("no <pre> block — the archive page layout has changed")
    body = re.sub(r"<[^>]+>", "", match.group(1))
    return body.strip("\n") + "\n"


def discover_advisories(index_html: bytes) -> dict[str, list[str]]:
    """Read the advisory numbers off the storm index rather than assuming them.

    Ranges are not always 1..N: NHC issues corrected advisories, and the update
    product is numbered by timestamp. A hardcoded range would silently skip
    whatever does not fit, and a cache that is quietly incomplete is worse than
    one that is obviously missing.
    """
    text = index_html.decode("utf-8", errors="replace")
    found: dict[str, list[str]] = {product: [] for product in TEXT_PRODUCTS}
    pattern = re.compile(rf"{STORM}\.([a-z_]+)\.(\d+[A-Za-z]?)\.shtml")
    for product, number in pattern.findall(text):
        if product in found and number not in found[product]:
            found[product].append(number)
    for numbers in found.values():
        numbers.sort()
    return found


def write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="refetch files already cached")
    parser.add_argument("--verify", action="store_true", help="check the manifest, fetch nothing")
    parser.add_argument("--delay", type=float, default=0.25, help="seconds between requests")
    args = parser.parse_args()

    if args.verify:
        return manifest.verify()

    root = CACHE / STORM
    fetched = skipped = 0

    print(f"reading advisory index: {INDEX_URL}")
    advisories = discover_advisories(fetch(INDEX_URL))
    for product, numbers in advisories.items():
        print(f"  {product:9s} {len(numbers):3d} advisories")

    # --- text products -----------------------------------------------------
    for product, numbers in advisories.items():
        for number in numbers:
            target = root / "text" / product / f"{STORM}.{product}.{number}.txt"
            if target.exists() and not args.force:
                skipped += 1
                continue
            url = f"{TEXT_BASE}/{STORM}.{product}.{number}.shtml"
            write(target, extract_product(fetch(url)).encode("utf-8"))
            fetched += 1
            print(f"  + {target.relative_to(CACHE)}")
            time.sleep(args.delay)

    # --- GIS bundles, keyed off the forecast advisory numbers --------------
    for product in GIS_PRODUCTS:
        for number in advisories["fstadv"]:
            name = f"{STORM}_{product}_{number}.zip"
            target = root / "gis" / product / name
            if target.exists() and not args.force:
                skipped += 1
                continue
            try:
                write(target, fetch(f"{GIS_BASE}/{name}"))
            except RuntimeError:
                # Not every advisory has every GIS bundle — early advisories
                # predate some products. Say so rather than failing the run.
                print(f"  ! no {product} bundle for advisory {number}")
                continue
            fetched += 1
            print(f"  + {target.relative_to(CACHE)}")
            time.sleep(args.delay)

    # --- best track: what actually happened, not what was forecast ---------
    # Verification in Phase 2 compares a claim against the observed wind field,
    # so the replay needs the post-storm truth as well as the forecasts.
    best_track = root / "gis" / "best_track" / f"{STORM}_best_track.zip"
    if not best_track.exists() or args.force:
        write(best_track, fetch(BEST_TRACK_URL))
        fetched += 1
        print(f"  + {best_track.relative_to(CACHE)}")
    else:
        skipped += 1

    manifest.write()
    total = sum(1 for p in CACHE.rglob("*") if p.is_file() and p != manifest.MANIFEST)
    size = sum(p.stat().st_size for p in CACHE.rglob("*") if p.is_file())
    print(
        f"\n{fetched} fetched, {skipped} already cached. "
        f"{total} files, {size / 1_048_576:.1f} MB. manifest.sha256 written."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
