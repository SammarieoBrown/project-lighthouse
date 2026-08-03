import { createReadStream } from "node:fs";
import { readFile, stat } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { Readable } from "node:stream";

/* Byte-range serving for the map archives.
 *
 * PMTiles is a single file read by the browser through HTTP range requests —
 * that is the whole design, and it is what lets a 98 MB archive cost a few
 * kilobytes to open. It only works if the server honours Range.
 *
 * Next's static handler does not, reliably. It answers the *first* request for
 * a large file in public/ with a 200 and the entire body, ignoring the Range
 * header it was sent and omitting Content-Range, then serves every subsequent
 * request as a correct 206. The pmtiles client notices, complains, retries, and
 * recovers — so the map works, having first downloaded ninety-eight megabytes
 * to read sixteen kilobytes of header. On a venue network that is the
 * difference between a map and a stalled demo.
 *
 * Rather than depend on framework behaviour for something this load-bearing,
 * the archives are served from here. Forty lines, and correct on every host,
 * including a laptop with no internet.
 */

/* Dynamic, and it has to be. `force-static` lets Next prerender and cache the
 * response, which discards the Range header entirely and serves the whole
 * archive with a 200 — reintroducing the exact bug this handler exists to fix,
 * from the other direction. */
export const dynamic = "force-dynamic";

/** Archives live outside public/ so nothing can route around this handler. */
const TILE_DIR = join(process.cwd(), ".tiles");
const VALIDATORS = join(TILE_DIR, "validators.json");

const ALLOWED = new Set([
  "caribbean-z11.pmtiles",
  "jamaica-z15.pmtiles",
  "structures-z15.pmtiles",
]);

type RouteContext = { params: Promise<{ file: string }> };

export async function GET(
  request: Request,
  context: RouteContext,
) {
  return serve(request, context, false);
}

export async function HEAD(
  request: Request,
  context: RouteContext,
) {
  return serve(request, context, true);
}

async function serve(request: Request, context: RouteContext, head: boolean) {
  const { file } = await context.params;

  // The three build artifacts are the whole public surface. A filename is user
  // input even when it looks like one of ours, so both membership and resolved
  // parent are checked before the filesystem is touched.
  if (!ALLOWED.has(file)) {
    return new Response("not found", { status: 404 });
  }
  const path = resolve(TILE_DIR, file);
  if (dirname(path) !== resolve(TILE_DIR)) {
    return new Response("not found", { status: 404 });
  }

  let metadata: Awaited<ReturnType<typeof stat>>;
  try {
    metadata = await stat(path);
  } catch {
    return new Response(
      `${file} is not staged — run data/tiles/fetch_basemap.py, then npm run assets`,
      { status: 404 },
    );
  }
  if (!metadata.isFile()) return new Response("not found", { status: 404 });

  const size = metadata.size;
  const etag = await strongEtag(file, size);
  if (!etag) {
    return new Response("archive validator unavailable — run npm run assets", {
      status: 503,
      headers: { "cache-control": "no-store" },
    });
  }

  const headers: Record<string, string> = {
    "content-type": "application/octet-stream",
    "accept-ranges": "bytes",
    /* These filenames are stable across builds, so neither `immutable` nor a
     * stale window is safe after a replay/data refresh. PMTiles caches active
     * session ranges; a later session must revalidate them against this ETag. */
    "cache-control": "public, max-age=0, must-revalidate",
    "etag": etag,
    "last-modified": metadata.mtime.toUTCString(),
    "vary": "Range",
    "x-content-type-options": "nosniff",
  };

  /* RFC preconditions are evaluated before Range. In particular, a matching
   * If-None-Match still means 304 when a Range header accompanies it; serving a
   * 206 instead would let a cache splice two versions of a stable-name archive. */
  const ifMatch = request.headers.get("if-match");
  if (ifMatch && !etagMatches(ifMatch, etag, false)) {
    return preconditionFailed(headers, head);
  }

  const ifUnmodifiedSince = request.headers.get("if-unmodified-since");
  if (!ifMatch && ifUnmodifiedSince) {
    const modified = modifiedAfter(metadata.mtimeMs, ifUnmodifiedSince);
    if (modified === true) return preconditionFailed(headers, head);
  }

  const ifNoneMatch = request.headers.get("if-none-match");
  if (ifNoneMatch && etagMatches(ifNoneMatch, etag, true)) {
    return new Response(null, { status: 304, headers });
  }

  const ifModifiedSince = request.headers.get("if-modified-since");
  if (!ifNoneMatch && ifModifiedSince) {
    const modified = modifiedAfter(metadata.mtimeMs, ifModifiedSince);
    if (modified === false) return new Response(null, { status: 304, headers });
  }

  const fullResponse = () => new Response(head ? null : streamOf(path), {
    status: 200,
    headers: { ...headers, "content-length": String(size) },
  });

  const range = request.headers.get("range");
  if (!range) return fullResponse();

  /* If-Range deliberately accepts only this representation's strong tag, or a
   * date not older than Last-Modified. A weak/mismatched/invalid validator makes
   * us ignore Range and send a complete 200, as required; returning a 206 would
   * allow callers to append new bytes to an old cached PMTiles file. */
  const ifRange = request.headers.get("if-range");
  if (ifRange && !ifRangeMatches(ifRange, etag, metadata.mtimeMs)) {
    return fullResponse();
  }

  const parsed = parseRange(range, size);
  if (!parsed) return unsatisfiable(size, headers, head);
  const { start, end } = parsed;

  return new Response(head ? null : streamOf(path, start, end), {
    status: 206,
    headers: {
      ...headers,
      "content-range": `bytes ${start}-${end}/${size}`,
      "content-length": String(end - start + 1),
    },
  });
}

type ValidatorDocument = {
  schema?: unknown;
  archives?: Record<string, { bytes?: unknown; sha256?: unknown }>;
};

/** Resolve the already-verified build hash rather than hashing up to 180 MB on
 * every serverless cold start. stage-assets writes this sidecar only after the
 * source archive's SHA-256 and byte count have both passed verification. */
async function strongEtag(file: string, size: number): Promise<string | null> {
  try {
    const document = JSON.parse(await readFile(VALIDATORS, "utf8")) as ValidatorDocument;
    const record = document.archives?.[file];
    if (
      document.schema !== "lighthouse.tile-validators.v1"
      || record?.bytes !== size
      || typeof record.sha256 !== "string"
      || !/^[0-9a-f]{64}$/.test(record.sha256)
    ) {
      return null;
    }
    return `"sha256-${record.sha256}"`;
  } catch {
    return null;
  }
}

function etagMatches(value: string, current: string, weak: boolean): boolean {
  if (value.trim() === "*") return true;
  const candidates = value.match(/(?:W\/)?"[^"]*"/g) ?? [];
  return candidates.some((candidate) => {
    if (!weak) return !candidate.startsWith("W/") && candidate === current;
    return candidate.replace(/^W\//, "") === current.replace(/^W\//, "");
  });
}

function modifiedAfter(mtimeMs: number, rawDate: string): boolean | null {
  const date = Date.parse(rawDate);
  if (!Number.isFinite(date)) return null;
  // HTTP dates have one-second precision; compare the same representation that
  // appears in Last-Modified rather than treating its discarded milliseconds as
  // a modification immediately after itself.
  return Math.floor(mtimeMs / 1000) * 1000 > date;
}

function ifRangeMatches(value: string, etag: string, mtimeMs: number): boolean {
  const candidate = value.trim();
  if (candidate.startsWith('"')) return candidate === etag;
  if (candidate.startsWith("W/")) return false;
  const date = Date.parse(candidate);
  return Number.isFinite(date) && Math.floor(mtimeMs / 1000) * 1000 <= date;
}

function parseRange(value: string, size: number): { start: number; end: number } | null {
  const match = /^bytes=(\d*)-(\d*)$/.exec(value.trim());
  if (!match || size <= 0) return null;

  const [, rawStart, rawEnd] = match;
  const number = (raw: string): number | null => {
    if (raw === "") return null;
    const parsed = Number(raw);
    return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : null;
  };

  if (rawStart === "") {
    const suffix = number(rawEnd);
    if (suffix === null || suffix === 0) return null;
    return { start: Math.max(0, size - suffix), end: size - 1 };
  }

  const start = number(rawStart);
  const requestedEnd = rawEnd === "" ? size - 1 : number(rawEnd);
  if (start === null || requestedEnd === null || start >= size || start > requestedEnd) return null;
  return { start, end: Math.min(requestedEnd, size - 1) };
}

function preconditionFailed(headers: Record<string, string>, head: boolean) {
  return new Response(head ? null : "precondition failed", {
    status: 412,
    headers: {
      ...headers,
      "content-length": "19",
      "content-type": "text/plain; charset=utf-8",
    },
  });
}

function unsatisfiable(size: number, headers: Record<string, string>, head: boolean) {
  return new Response(head ? null : "range not satisfiable", {
    status: 416,
    headers: {
      ...headers,
      "content-length": "21",
      "content-range": `bytes */${size}`,
      "content-type": "text/plain; charset=utf-8",
    },
  });
}

function streamOf(path: string, start?: number, end?: number): ReadableStream<Uint8Array> {
  const stream = createReadStream(path, start === undefined ? undefined : { start, end });
  // Response expects a Web stream. The explicit bridge propagates cancellation
  // back to the file descriptor instead of relying on an unsafe structural cast
  // from a Node stream.
  return Readable.toWeb(stream) as ReadableStream<Uint8Array>;
}
