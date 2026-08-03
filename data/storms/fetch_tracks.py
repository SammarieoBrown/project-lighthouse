#!/usr/bin/env python3
"""Fetch the Atlantic storm track archives.

The product replays one hurricane because one hurricane is all it can read.
Melissa arrives as NHC teletype from `data/replay/`, and there is no path to any
other storm. These two files are that path: every Atlantic tropical cyclone
since 1851, with the position, intensity and size the wind field model needs.

    python3 data/storms/fetch_tracks.py            # fetch what is missing
    python3 data/storms/fetch_tracks.py --force    # refetch
    python3 data/storms/fetch_tracks.py --verify   # manifest only, no network

**Two sources, and the second one is not optional.**

`hurdat2` is NHC's reanalysed Atlantic best track — the authority on where a
storm was and how strong it was. It is also missing the two fields that decide
how *big* a storm is: quadrant wind radii appear only from 2004, and radius of
maximum wind only from 2021. Before that they are `-999`.

Which means the storm every Jamaican remembers is unusable on its own. Gilbert
in 1988 reads:

    19880912, 1800,  , HU, 17.7N,  76.5W, 110,  960, -999, -999, ... -999

`ebtrk` is CIRA/Colorado State's Extended Best Track, which exists precisely
because of that gap — operational records digitised back to 1988 and merged.
The same storm, same hour:

    AL081988 GILBERT  091218 1988  17.7  76.5 110  960  22 ... 250200250250 ...

Radius of maximum wind 22 nmi, and the 34/50/64 kt radii by quadrant. That is a
storm we can actually model. **Without this file the catalogue starts in 2004
and Jamaica's defining hurricane is not in it.**

Trust EBTRK's radii and RMW; do not trust its eye, POCI or ROCI columns in the
older records — Gilbert reports a pressure of the outermost closed isobar of
12 hPa, which is not a pressure. The loader ignores them and uses a standard
ambient pressure instead.

Standard library only, so a clean clone can fetch without the API environment.
"""

from __future__ import annotations

import argparse
import re
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
import manifest  # noqa: E402  — one definition of "verified", shared by every cache

HERE = Path(__file__).parent
CACHE = HERE / "cache"

#: NHC republishes HURDAT2 after each season under a dated filename, so the URL
#: is discovered from the directory index rather than pinned. Pinning it means a
#: clone in six months silently fetches nothing.
HURDAT2_INDEX = "https://www.nhc.noaa.gov/data/hurdat/"
HURDAT2_PATTERN = re.compile(r"hurdat2-1851-(\d{4})-(\d{6})\.txt")
HURDAT2 = CACHE / "hurdat2-atlantic.txt"

#: EBTRK is a finished dataset, not a rolling one — it ends at 2021 and the
#: filename has not moved since it was published. A literal URL is honest here.
EBTRK_URL = (
    "https://rammb2.cira.colostate.edu/wp-content/uploads/2020/11/"
    "EBTRK_AL_final_1851-2021_new_format_02-Sep-2022-1.txt"
)
EBTRK = CACHE / "ebtrk-atlantic.txt"

USER_AGENT = (
    "project-lighthouse-storm-tracks/1.0 "
    "(+https://github.com/SammarieoBrown/project-lighthouse)"
)


def ssl_context() -> ssl.SSLContext:
    """Prefer certifi where present — see data/replay/fetch_advisories.py."""
    try:
        import certifi
    except ModuleNotFoundError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


_SSL = ssl_context()


def _get(url: str, *, timeout: int = 180) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_SSL) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        sys.exit(f"{url} returned {exc.code}")
    except OSError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ssl.SSLCertVerificationError):
            sys.exit(
                "TLS verification failed. Either `pip install certifi`, or run "
                '"Install Certificates.command" from your Python install.'
            )
        sys.exit(f"{url} failed: {exc}")


def latest_hurdat2_url() -> str:
    """Newest HURDAT2 in the index, by the season it covers then by its stamp.

    The filenames carry two dates — the last season included and the day it was
    published — and they do not always sort the same way. Read both.
    """
    index = _get(HURDAT2_INDEX).decode("utf-8", "replace")
    found = sorted(
        {(int(m.group(1)), m.group(2), m.group(0)) for m in HURDAT2_PATTERN.finditer(index)}
    )
    if not found:
        sys.exit(f"no hurdat2 file found in {HURDAT2_INDEX} — has the layout changed?")
    return HURDAT2_INDEX + found[-1][2]


def _write(target: Path, payload: bytes, *, label: str) -> None:
    # Through a temporary name so an interrupted write cannot be mistaken for a
    # complete file on the next run.
    partial = target.with_suffix(target.suffix + ".partial")
    partial.write_bytes(payload)
    partial.replace(target)
    print(f"  {label}: {len(payload):,} bytes → {target.name}")


def fetch(force: bool = False) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)

    if HURDAT2.exists() and not force:
        print(f"  hurdat2: cached ({HURDAT2.stat().st_size:,} bytes)")
    else:
        url = latest_hurdat2_url()
        print(f"  hurdat2: {url.rsplit('/', 1)[-1]}")
        _write(HURDAT2, _get(url), label="hurdat2")

    if EBTRK.exists() and not force:
        print(f"  ebtrk:   cached ({EBTRK.stat().st_size:,} bytes)")
    else:
        _write(EBTRK, _get(EBTRK_URL), label="ebtrk")


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
