import { createReadStream, statSync } from "node:fs";
import { join, normalize } from "node:path";
import type { ReadableOptions } from "node:stream";

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

const ALLOWED = /^[a-z0-9-]+\.pmtiles$/;

export async function GET(
  request: Request,
  context: { params: Promise<{ file: string }> },
) {
  const { file } = await context.params;

  // Allowlisted by shape, and the resolved path is checked to still sit inside
  // the tile directory. A filename is user input even when it looks like ours.
  if (!ALLOWED.test(file)) {
    return new Response("not found", { status: 404 });
  }
  const path = normalize(join(TILE_DIR, file));
  if (!path.startsWith(TILE_DIR)) {
    return new Response("not found", { status: 404 });
  }

  let size: number;
  try {
    size = statSync(path).size;
  } catch {
    return new Response(
      `${file} is not staged — run data/tiles/fetch_basemap.py, then npm run assets`,
      { status: 404 },
    );
  }

  const headers: Record<string, string> = {
    "content-type": "application/octet-stream",
    "accept-ranges": "bytes",
    // The archive is immutable for a given build; its checksum is pinned in
    // data/tiles/cache/manifest.sha256.
    "cache-control": "public, max-age=31536000, immutable",
  };

  const range = request.headers.get("range");
  if (!range) {
    return new Response(streamOf(path), {
      status: 200,
      headers: { ...headers, "content-length": String(size) },
    });
  }

  const match = /^bytes=(\d*)-(\d*)$/.exec(range.trim());
  if (!match) {
    return new Response("bad range", { status: 400 });
  }

  // Both open-ended forms are legal: `bytes=100-` to the end, `bytes=-100` for
  // the last hundred. pmtiles uses the first when it reads a directory.
  const [, rawStart, rawEnd] = match;
  let start: number;
  let end: number;
  if (rawStart === "") {
    const suffix = Number(rawEnd);
    if (!suffix) return unsatisfiable(size);
    start = Math.max(0, size - suffix);
    end = size - 1;
  } else {
    start = Number(rawStart);
    end = rawEnd === "" ? size - 1 : Math.min(Number(rawEnd), size - 1);
  }

  if (!Number.isFinite(start) || !Number.isFinite(end) || start > end || start >= size) {
    return unsatisfiable(size);
  }

  return new Response(streamOf(path, { start, end }), {
    status: 206,
    headers: {
      ...headers,
      "content-range": `bytes ${start}-${end}/${size}`,
      "content-length": String(end - start + 1),
    },
  });
}

function unsatisfiable(size: number) {
  return new Response("range not satisfiable", {
    status: 416,
    headers: { "content-range": `bytes */${size}` },
  });
}

function streamOf(path: string, options?: ReadableOptions & { start?: number; end?: number }) {
  return createReadStream(path, options) as unknown as ReadableStream;
}
