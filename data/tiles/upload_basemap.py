#!/usr/bin/env python3
"""Publish the basemap archives, glyphs and sprites to the public R2 bucket.

`fetch_basemap.py` builds a map that works with no internet at all. This script
solves the opposite problem: making it work for everyone else.

The archives are 138 MB. They are deliberately not committed, and Vercel builds
the console with `npm run build` — no Python, no pmtiles CLI, no way to produce
them. So production had no basemap at all: both archives and every sprite
returned 404, and the console fell back to the static SVG map without saying so.
That fallback is correct behaviour and a bad thing to ship.

R2 fixes it for one specific reason: it serves HTTP Range natively. That is the
entire premise of PMTiles — a 98 MB archive costs 16 KB to open — and it is the
thing `apps/console/app/map/[file]/route.ts` had to be hand-written to do,
because Next's static handler would not.

    python3 data/tiles/upload_basemap.py            # publish what has changed
    python3 data/tiles/upload_basemap.py --force    # publish everything
    python3 data/tiles/upload_basemap.py --verify   # check the public URL only

Requires the Cloudflare credentials in `.env` and npx on PATH. Standard library
only, like its sibling.
"""

from __future__ import annotations

import argparse
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
CACHE = HERE / "cache"
ASSETS = CACHE / "assets"
REPO = HERE.parents[1]

ARCHIVES = ("caribbean-z11.pmtiles", "jamaica-z15.pmtiles")

CONTENT_TYPES = {
    ".pmtiles": "application/octet-stream",
    ".pbf": "application/x-protobuf",
    ".json": "application/json",
    ".png": "image/png",
}

#: The managed r2.dev domain sits behind Cloudflare's bot protection, and it
#: answers `Python-urllib/3.x` with a flat 403 — measured: 403 for that agent,
#: 200 for curl's default and for anything browser-shaped, same URL, same
#: second. Nothing is wrong with the bucket when that happens, so say who we are.
#:
#: Browsers are unaffected, which is why the console works while a script does
#: not. It is also a reminder that r2.dev is rate-limited and explicitly not
#: Cloudflare's recommendation for production — a custom domain is, once there
#: is a domain on the account to attach.
USER_AGENT = "project-lighthouse-basemap-publisher/1.0 (+https://github.com/SammarieoBrown/project-lighthouse)"


def load_env() -> dict[str, str]:
    """Read .env without a dependency. Never source it in a shell — one of the
    Twilio values contains characters zsh treats as a command."""
    env: dict[str, str] = {}
    path = REPO / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return {**env, **os.environ}


def public_base(env: dict[str, str]) -> str:
    url = env.get("NEXT_PUBLIC_TILES_URL", "").rstrip("/")
    if not url:
        sys.exit("NEXT_PUBLIC_TILES_URL is not set — see .env.example")
    return url


def files() -> list[tuple[str, Path]]:
    """(key, path) pairs, in upload order: small things first so a failure is
    cheap and obvious before 138 MB goes over the wire."""
    out: list[tuple[str, Path]] = []
    if ASSETS.exists():
        for p in sorted(ASSETS.rglob("*")):
            if p.is_file():
                out.append((str(p.relative_to(CACHE)), p))
    for name in ARCHIVES:
        p = CACHE / name
        if p.exists():
            out.append((name, p))
    return out


def url_for(base: str, key: str) -> str:
    """Font stacks are directories with spaces in them — "Noto Sans Regular" —
    so the key is stored with a literal space and has to be percent-encoded on
    the way out. MapLibre does this for us in the browser; urllib does not, and
    refuses the request outright rather than encoding it."""
    return f"{base}/{urllib.parse.quote(key)}"


def ssl_context() -> ssl.SSLContext:
    """Prefer certifi where present — see data/replay/fetch_advisories.py."""
    try:
        import certifi
    except ModuleNotFoundError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


_SSL = ssl_context()


def head(url: str) -> tuple[int, int | None]:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30, context=_SSL) as r:
            length = r.headers.get("content-length")
            return r.status, int(length) if length else None
    except urllib.error.HTTPError as e:
        return e.code, None
    except OSError as e:
        # Do not swallow the reason. A bare "HTTP 0" on every row sent me looking
        # at the bucket when the problem was this machine's certificate store.
        reason = getattr(e, "reason", e)
        if isinstance(reason, ssl.SSLCertVerificationError):
            sys.exit(
                f"TLS verification failed for {url}\n"
                "This machine's Python cannot verify certificates. "
                "Either `pip install certifi`, or run "
                '"Install Certificates.command" from your Python install.'
            )
        return 0, None


def verify(base: str) -> int:
    """A HEAD is not enough. The failure that matters is a server that answers
    200 with the whole body when asked for a range — which is exactly how the
    Next static handler broke this, and it looks like success until you measure
    the bytes."""
    bad = 0
    for key, path in files():
        url = url_for(base, key)
        status, length = head(url)
        local = path.stat().st_size
        note = ""
        if status != 200:
            note, bad = f"HTTP {status}", bad + 1
        elif length != local:
            note, bad = f"size {length} != local {local}", bad + 1
        elif key.endswith(".pmtiles"):
            req = urllib.request.Request(
                url, headers={"Range": "bytes=0-16383", "User-Agent": USER_AGENT}
            )
            try:
                with urllib.request.urlopen(req, timeout=30, context=_SSL) as r:
                    body = r.read()
                if r.status != 206:
                    note, bad = f"range gave {r.status}, not 206", bad + 1
                elif body[:7] != b"PMTiles":
                    note, bad = "range body is not a PMTiles header", bad + 1
                else:
                    note = f"206, {len(body)} B of {local:,}, spec v{body[7]}"
            except OSError as e:
                note, bad = f"range failed: {e}", bad + 1
        print(f"  {'FAIL' if note and bad else 'ok  '}  {key:<44} {note}")
    return bad


def upload(base: str, env: dict[str, str], force: bool) -> int:
    cf_env = {
        **os.environ,
        "CLOUDFLARE_ACCOUNT_ID": env.get("CLOUDFLARE_ACCOUNT_ID", ""),
        "CLOUDFLARE_API_TOKEN": env.get("CLOUDFLARE_API_TOKEN", ""),
    }
    if not cf_env["CLOUDFLARE_API_TOKEN"]:
        sys.exit("CLOUDFLARE_API_TOKEN is not set — see .env.example")
    bucket = env.get("R2_TILES_BUCKET", "lighthouse-tiles")

    failed = 0
    for key, path in files():
        size = path.stat().st_size
        if not force:
            status, length = head(url_for(base, key))
            if status == 200 and length == size:
                print(f"  skip  {key:<44} already published")
                continue
        print(f"  put   {key:<44} {size:>11,} B ... ", end="", flush=True)
        ctype = CONTENT_TYPES.get(path.suffix, "application/octet-stream")
        proc = subprocess.run(
            ["npx", "--yes", "wrangler@4", "r2", "object", "put",
             f"{bucket}/{key}", f"--file={path}", f"--content-type={ctype}", "--remote"],
            env=cf_env, capture_output=True, text=True,
        )
        if proc.returncode == 0:
            print("ok")
        else:
            print("FAILED")
            print(proc.stderr.strip()[:400])
            failed += 1
    return failed


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="republish even if present and the right size")
    ap.add_argument("--verify", action="store_true", help="check the public URL, upload nothing")
    args = ap.parse_args()

    env = load_env()
    base = public_base(env)

    if not CACHE.exists() or not any((CACHE / a).exists() for a in ARCHIVES):
        sys.exit("no archives in data/tiles/cache — run data/tiles/fetch_basemap.py first")

    if args.verify:
        print(f"verifying {base}")
        sys.exit(1 if verify(base) else 0)

    print(f"publishing to {base}")
    if upload(base, env, args.force):
        sys.exit("upload failed")
    print("\nverifying what landed")
    sys.exit(1 if verify(base) else 0)


if __name__ == "__main__":
    main()
