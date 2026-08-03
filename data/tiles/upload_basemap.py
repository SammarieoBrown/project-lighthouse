#!/usr/bin/env python3
"""Publish checksummed map content to the public R2 bucket.

The source basemap is pinned by ``cache/manifest.sha256``. The derived
structures archive is pinned separately by ``structures.manifest.json``, which
also records the source and build fingerprints that produced it. Keeping those
manifests separate prevents a basemap fetch from certifying an arbitrary local
derived archive merely because it happens to exist.

    python3 data/tiles/upload_basemap.py            # publish changed content
    python3 data/tiles/upload_basemap.py --force    # republish local content
    python3 data/tiles/upload_basemap.py --verify   # hash public content only

Remote equality is SHA-256 equality. Matching ``Content-Length`` is only a
diagnostic; same-sized stale PMTiles archives are not accepted as published.
Verification streams every public object, so it transfers the full archive
bytes. PMTiles range support is checked separately after the content hash.

Requires Cloudflare credentials in ``.env`` and npx on PATH. Standard library
only, like its sibling.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).parent
CACHE = HERE / "cache"
REPO = HERE.parents[1]
STRUCTURE_MANIFEST = HERE / "structures.manifest.json"

sys.path.insert(0, str(HERE.parent))
import manifest as checksum_manifest  # noqa: E402

BASE_ARCHIVES = ("caribbean-z11.pmtiles", "jamaica-z15.pmtiles")
STRUCTURE_ARCHIVE = "structures-z15.pmtiles"

CONTENT_TYPES = {
    ".pmtiles": "application/octet-stream",
    ".pbf": "application/x-protobuf",
    ".json": "application/json",
    ".png": "image/png",
}

USER_AGENT = (
    "project-lighthouse-map-publisher/2.0 "
    "(+https://github.com/SammarieoBrown/project-lighthouse)"
)
CACHE_CONTROL = "public, max-age=0, must-revalidate"
EXPECTED_CACHE_DIRECTIVES = frozenset({"public", "max-age=0", "must-revalidate"})


@dataclass(frozen=True)
class Artifact:
    key: str
    path: Path
    sha256: str
    bytes: int | None = None


@dataclass(frozen=True)
class RemoteDigest:
    status: int
    sha256: str | None
    bytes: int
    error: str | None = None


@dataclass(frozen=True)
class RemoteHead:
    status: int
    bytes: int | None
    cache_control: str | None


def load_env() -> dict[str, str]:
    """Read .env without sourcing shell-sensitive credential values."""
    env: dict[str, str] = {}
    path = REPO / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    return {**env, **os.environ}


def public_base(env: dict[str, str]) -> str:
    url = env.get("NEXT_PUBLIC_TILES_URL", "").rstrip("/")
    if not url:
        sys.exit("NEXT_PUBLIC_TILES_URL is not set — see .env.example")
    return url


def _valid_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _structure_artifact() -> Artifact | None:
    archive = CACHE / STRUCTURE_ARCHIVE
    if not STRUCTURE_MANIFEST.exists():
        if archive.exists():
            sys.exit(
                f"{archive} exists without {STRUCTURE_MANIFEST}. Rebuild it with "
                "`cd apps/api && uv run python -m app.registry.building_tiles`; "
                "an unproven derived archive will not be published."
            )
        return None

    try:
        document = json.loads(STRUCTURE_MANIFEST.read_text())
        if document["schema"] != "lighthouse.structure-archive.v1":
            raise ValueError(f"unsupported schema {document.get('schema')!r}")
        record = document["artifact"]
        path = (HERE / record["path"]).resolve()
        if path != archive.resolve():
            raise ValueError(f"artifact path must resolve to cache/{STRUCTURE_ARCHIVE}")
        digest = record["sha256"]
        size = int(record["bytes"])
        if not _valid_digest(digest) or size <= 0:
            raise ValueError("artifact digest or byte count is invalid")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        sys.exit(f"invalid {STRUCTURE_MANIFEST}: {exc}")
    return Artifact(STRUCTURE_ARCHIVE, archive, digest, size)


def files() -> list[Artifact]:
    """Published objects and authoritative digests, in upload order."""
    base_manifest = checksum_manifest.manifest_path(CACHE)
    if not base_manifest.exists():
        sys.exit(f"{base_manifest} is missing — run data/tiles/fetch_basemap.py")
    try:
        expected = checksum_manifest.read(base_manifest)
    except ValueError as exc:
        sys.exit(f"invalid {base_manifest}: {exc}")

    keys = sorted(name for name in expected if name.startswith("assets/"))
    keys.extend(BASE_ARCHIVES)
    missing_entries = [name for name in BASE_ARCHIVES if name not in expected]
    if missing_entries:
        sys.exit(f"basemap manifest does not pin: {', '.join(missing_entries)}")

    artifacts = [Artifact(key, CACHE / key, expected[key]) for key in keys]
    structures = _structure_artifact()
    if structures is not None:
        artifacts.append(structures)
    return artifacts


def url_for(base: str, key: str) -> str:
    """Percent-encode font directories such as ``Noto Sans Regular``."""
    return f"{base}/{urllib.parse.quote(key)}"


def ssl_context() -> ssl.SSLContext:
    try:
        import certifi
    except ModuleNotFoundError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


_SSL = ssl_context()


def _network_error(url: str, exc: OSError) -> str:
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return (
            f"TLS verification failed for {url}. Either `pip install certifi`, or run "
            '"Install Certificates.command" from your Python install.'
        )
    return str(reason)


def head(url: str) -> RemoteHead:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30, context=_SSL) as response:
            length = response.headers.get("content-length")
            return RemoteHead(
                response.status,
                int(length) if length else None,
                response.headers.get("cache-control"),
            )
    except urllib.error.HTTPError as exc:
        return RemoteHead(exc.code, None, None)
    except OSError:
        return RemoteHead(0, None, None)


def _cache_directives(value: str | None) -> frozenset[str]:
    """Normalize Cache-Control directives without depending on order or case."""
    if not value:
        return frozenset()
    normalized: set[str] = set()
    for directive in value.split(","):
        directive = directive.strip()
        if not directive:
            continue
        if "=" in directive:
            name, setting = directive.split("=", 1)
            normalized.add(f"{name.strip().lower()}={setting.strip().lower()}")
        else:
            normalized.add(directive.lower())
    return frozenset(normalized)


def _cache_control_failure(value: str | None) -> str | None:
    actual = _cache_directives(value)
    if actual == EXPECTED_CACHE_DIRECTIVES:
        return None
    if not value:
        return f"Cache-Control is missing (expected {CACHE_CONTROL})"
    return f"Cache-Control {value!r} != {CACHE_CONTROL!r}"


def remote_sha256(url: str) -> RemoteDigest:
    """Stream and hash the public object; never buffer an archive in memory."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=300, context=_SSL) as response:
            while chunk := response.read(1 << 20):
                digest.update(chunk)
                total += len(chunk)
            return RemoteDigest(response.status, digest.hexdigest(), total)
    except urllib.error.HTTPError as exc:
        return RemoteDigest(exc.code, None, total, str(exc))
    except OSError as exc:
        return RemoteDigest(0, None, total, _network_error(url, exc))


def _range_check(url: str) -> str | None:
    request = urllib.request.Request(
        url, headers={"Range": "bytes=0-16383", "User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=30, context=_SSL) as response:
            body = response.read()
            if response.status != 206:
                return f"range gave HTTP {response.status}, not 206"
            if body[:7] != b"PMTiles":
                return "range body is not a PMTiles header"
            return None
    except OSError as exc:
        return f"range failed: {_network_error(url, exc)}"


def _local_failure(artifact: Artifact) -> str | None:
    if not artifact.path.exists():
        return "local file is missing"
    actual = checksum_manifest.sha256_file(artifact.path)
    if actual != artifact.sha256:
        return f"local sha256 {actual} != manifest {artifact.sha256}"
    if artifact.bytes is not None and artifact.path.stat().st_size != artifact.bytes:
        return (
            f"local size {artifact.path.stat().st_size} != manifest {artifact.bytes}"
        )
    return None


def verify(base: str, artifacts: list[Artifact] | None = None) -> int:
    """Verify full public bytes and PMTiles Range behaviour."""
    bad = 0
    for artifact in artifacts or files():
        url = url_for(base, artifact.key)
        remote_head = head(url)
        remote = remote_sha256(url)
        failures: list[str] = []
        if remote_head.status != 200:
            failures.append(f"HEAD HTTP {remote_head.status}")
        else:
            cache_failure = _cache_control_failure(remote_head.cache_control)
            if cache_failure:
                failures.append(cache_failure)
        if remote.status != 200:
            failures.append(f"GET HTTP {remote.status}: {remote.error or 'unknown error'}")
        elif remote.sha256 != artifact.sha256:
            failures.append(
                f"sha256 {remote.sha256} != manifest {artifact.sha256}"
            )
        if (
            remote_head.bytes is not None
            and remote.status == 200
            and remote_head.bytes != remote.bytes
        ):
            failures.append(
                f"Content-Length {remote_head.bytes} != GET bytes {remote.bytes}"
            )
        if artifact.bytes is not None and remote.status == 200 and remote.bytes != artifact.bytes:
            failures.append(f"bytes {remote.bytes} != manifest {artifact.bytes}")
        if not failures and artifact.key.endswith(".pmtiles"):
            range_failure = _range_check(url)
            if range_failure:
                failures.append(range_failure)

        if failures:
            bad += 1
            print(f"  FAIL  {artifact.key:<44} {'; '.join(failures)}")
        else:
            print(
                f"  ok    {artifact.key:<44} sha256 {artifact.sha256[:12]} · "
                f"{remote.bytes:,} B"
            )
    return bad


def upload(
    base: str, env: dict[str, str], force: bool, artifacts: list[Artifact] | None = None
) -> int:
    cf_env = {
        **os.environ,
        "CLOUDFLARE_ACCOUNT_ID": env.get("CLOUDFLARE_ACCOUNT_ID", ""),
        "CLOUDFLARE_API_TOKEN": env.get("CLOUDFLARE_API_TOKEN", ""),
    }
    if not cf_env["CLOUDFLARE_API_TOKEN"]:
        sys.exit("CLOUDFLARE_API_TOKEN is not set — see .env.example")
    bucket = env.get("R2_TILES_BUCKET", "lighthouse-tiles")

    failed = 0
    for artifact in artifacts or files():
        if not artifact.path.exists():
            print(f"  omit  {artifact.key:<44} not present locally")
            continue
        local_failure = _local_failure(artifact)
        if local_failure:
            print(f"  FAIL  {artifact.key:<44} {local_failure}")
            failed += 1
            continue

        if not force:
            remote_head = head(url_for(base, artifact.key))
            cache_current = _cache_control_failure(remote_head.cache_control) is None
            if (
                remote_head.status == 200
                and remote_head.bytes == artifact.path.stat().st_size
                and cache_current
            ):
                remote = remote_sha256(url_for(base, artifact.key))
                if remote.status == 200 and remote.sha256 == artifact.sha256:
                    print(
                        f"  skip  {artifact.key:<44} sha256 {artifact.sha256[:12]} verified"
                    )
                    continue

        size = artifact.path.stat().st_size
        print(f"  put   {artifact.key:<44} {size:>11,} B ... ", end="", flush=True)
        content_type = CONTENT_TYPES.get(artifact.path.suffix, "application/octet-stream")
        proc = subprocess.run(
            [
                "npx",
                "--yes",
                "wrangler@4",
                "r2",
                "object",
                "put",
                f"{bucket}/{artifact.key}",
                f"--file={artifact.path}",
                f"--content-type={content_type}",
                f"--cache-control={CACHE_CONTROL}",
                "--remote",
            ],
            env=cf_env,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            print("ok")
        else:
            print("FAILED")
            print(proc.stderr.strip()[:400])
            failed += 1
    return failed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="republish local content")
    parser.add_argument(
        "--verify", action="store_true", help="stream and hash public content, upload nothing"
    )
    args = parser.parse_args()

    env = load_env()
    base = public_base(env)
    artifacts = files()

    if args.verify:
        print(f"verifying {base} by SHA-256")
        sys.exit(1 if verify(base, artifacts) else 0)

    if not any(artifact.path.exists() for artifact in artifacts):
        sys.exit("no local map content — run data/tiles/fetch_basemap.py first")

    print(f"publishing to {base}")
    if upload(base, env, args.force, artifacts):
        sys.exit("upload failed")
    print("\nverifying full public object checksums and Range responses")
    sys.exit(1 if verify(base, artifacts) else 0)


if __name__ == "__main__":
    main()
