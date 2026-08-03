#!/usr/bin/env python3
"""Rebuild the offline Jamaica basemap: vector tiles, glyphs and sprites.

The console has to draw a map in a building that has just lost power and its
internet with it. That rules out a tile server, and it also rules out the three
quieter dependencies people forget: glyphs, sprites, and the style itself. A
basemap that renders but shows no place names, because the font ranges are
fetched from a CDN at draw time, is not an offline basemap.

So everything lands here and is pinned by the same manifest as the storm cache.

    python3 data/tiles/fetch_basemap.py            # fetch what is missing
    python3 data/tiles/fetch_basemap.py --force    # refetch everything
    python3 data/tiles/fetch_basemap.py --verify   # check manifest, no network

Vector tiles come from the Protomaps daily planet build — 137 GB of it — but
``pmtiles extract`` reads the archive over HTTP range requests and pulls only
the tiles inside our bounding box. The last run took 28 requests and 13 seconds
for 38 MB. Downloading the planet is neither necessary nor kind.

Requires the pmtiles CLI: ``brew install pmtiles``.
Standard library only otherwise, so a clean clone can rebuild without the API
environment.
"""

from __future__ import annotations

import argparse
import json
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
import manifest  # noqa: E402  — shared by every cache, one definition of "verified"

HERE = Path(__file__).parent
CACHE = HERE / "cache"
ASSETS = CACHE / "assets"

#: Two archives, because one cannot be both seamless and detailed at a sane size.
#:
#: A single box tight around Jamaica ends in a hard black edge the moment
#: anybody pans. A single box around the whole basin at operational zoom is
#: 442 MB, almost all of it street geometry for Florida and Central America
#: nobody will ever look at. So: the region for context, the island for work.
#:
#: CONTEXT covers every island the Caribbean has — Trinidad and Barbados at the
#: south-east corner through the Bahamas and Yucatán — because a platform that
#: claims the region cannot end at the edge of one storm's track.
#:
#: DETAIL is Jamaica, where the registry actually is. z15 puts a household on a
#: named street; the region has no registry, so paying for its streets buys
#: nothing.
ARCHIVES = {
    "caribbean-z11.pmtiles": {"bbox": "-92.0,7.0,-57.0,28.0", "maxzoom": 11},
    "jamaica-z15.pmtiles": {"bbox": "-78.6,17.6,-75.9,18.7", "maxzoom": 15},
}

BUILD_INDEX = "https://build-metadata.protomaps.dev/builds.json"
BUILD_BASE = "https://build.protomaps.com"
ASSET_BASE = "https://protomaps.github.io/basemaps-assets"

#: The three Latin stacks the Protomaps v4 style asks for, and only the ranges
#: that carry Latin and Latin Extended. All 256 ranges per stack would be about
#: 60 MB of Devanagari, Cyrillic and CJK to render English place names in
#: Jamaica.
FONTSTACKS = ("Noto Sans Regular", "Noto Sans Medium", "Noto Sans Italic")
GLYPH_RANGES = ("0-255", "256-511")

#: Matches the flavor the console uses. Retina too — the demo runs on a laptop.
SPRITES = ("black.json", "black.png", "black@2x.json", "black@2x.png")

USER_AGENT = "project-lighthouse/0.1 (disaster relief coordination research)"


def ssl_context() -> ssl.SSLContext:
    """Prefer certifi where present — see data/replay/fetch_advisories.py."""
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


_SSL = ssl_context()


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=120, context=_SSL) as response:
            return response.read()
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), ssl.SSLCertVerificationError):
            raise RuntimeError(
                f"TLS verification failed for {url}. Either `pip install certifi`, "
                "or run /Applications/Python\\ 3.13/Install\\ Certificates.command"
            ) from exc
        raise


def latest_build() -> str:
    """Newest daily planet build.

    Read from the index rather than pinned to a date. The extract is pinned by
    its own checksum in the manifest, so what matters is that a rebuild is
    reproducible and *visible* — if OSM has moved under us, the manifest fails
    and somebody decides whether to accept the new world.
    """
    builds = json.loads(fetch(BUILD_INDEX))
    return builds[-1]["key"]


def extract_tiles(*, force: bool) -> list[Path]:
    build = None
    out = []

    for name, spec in ARCHIVES.items():
        target = CACHE / name
        out.append(target)
        if target.exists() and not force:
            print(f"  {name} already cached ({target.stat().st_size / 1e6:.0f} MB)")
            continue

        if shutil.which("pmtiles") is None:
            raise RuntimeError("pmtiles CLI not found — `brew install pmtiles`")

        build = build or latest_build()
        print(f"  extracting {name} — {spec['bbox']} to z{spec['maxzoom']} from {build}")
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "pmtiles", "extract", f"{BUILD_BASE}/{build}", str(target),
                f"--bbox={spec['bbox']}", f"--maxzoom={spec['maxzoom']}",
            ],
            check=True,
        )
    return out


def fetch_assets(*, force: bool) -> int:
    """Glyphs and sprites. The quiet half of offline."""
    fetched = 0

    for stack in FONTSTACKS:
        for rng in GLYPH_RANGES:
            target = ASSETS / "fonts" / stack / f"{rng}.pbf"
            if target.exists() and not force:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            quoted = stack.replace(" ", "%20")
            target.write_bytes(fetch(f"{ASSET_BASE}/fonts/{quoted}/{rng}.pbf"))
            fetched += 1

    for name in SPRITES:
        target = ASSETS / "sprites" / name
        if target.exists() and not force:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(fetch(f"{ASSET_BASE}/sprites/v4/{name.replace('@', '%40')}"))
        fetched += 1

    return fetched


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="refetch everything")
    parser.add_argument("--verify", action="store_true", help="check the manifest, no network")
    args = parser.parse_args()

    if args.verify:
        return manifest.verify(CACHE)

    print("basemap")
    archives = extract_tiles(force=args.force)
    print("assets")
    fetched = fetch_assets(force=args.force)
    print(f"  {fetched} glyph and sprite files fetched")

    lines = manifest.write(CACHE)
    size = sum(p.stat().st_size for p in CACHE.rglob("*") if p.is_file())
    print(
        "\n" + " · ".join(f"{a.name} {a.stat().st_size / 1e6:.0f} MB" for a in archives)
        + f"\n{size / 1e6:.0f} MB of tiles and assets. "
        f"manifest.sha256 now pins {lines} files.\n"
        "Source: OpenStreetMap via Protomaps, ODbL. Attribution is required on screen."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
