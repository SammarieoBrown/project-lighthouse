/* Lighthouse read-only offline shell.
 *
 * This caches the console code and deterministic replay after a successful
 * visit. PMTiles Range responses are intentionally never cached here: Cache API
 * matching does not safely distinguish byte ranges, and returning the wrong 206
 * is worse than falling back to the static map while offline.
 */

const CACHE = "lighthouse-console-v5";
const SHELL = ["/eoc", "/icon.svg", "/manifest.webmanifest"];

function isReplayPath(pathname) {
  return pathname === "/replay/index.json" ||
    pathname === "/replay/replay.json" ||
    /^\/replay\/[A-Za-z0-9][A-Za-z0-9._-]*\.json$/.test(pathname);
}

function isCacheablePath(pathname) {
  return (
    pathname.startsWith("/_next/static/") ||
    pathname.startsWith("/maplibre/") ||
    pathname.startsWith("/tiles/") ||
    isReplayPath(pathname) ||
    pathname === "/icon.svg" ||
    pathname === "/manifest.webmanifest"
  );
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then(async (cache) => {
      const responses = await Promise.allSettled(
        SHELL.map(async (url) => {
          const response = await fetch(url, { cache: "reload" });
          if (response.ok) await cache.put(url, response);
        }),
      );
      // Resolve the deployment shape before warming replay data. Fetching the
      // legacy megabyte alongside index.json would recreate the exact
      // double-download the client state machine avoids. A real 404/410 is the
      // only signal that this deployment owns replay.json instead of a library.
      try {
        const indexResponse = await fetch("/replay/index.json", { cache: "reload" });
        if (indexResponse.ok) {
          await cache.put("/replay/index.json", indexResponse.clone());
          const index = await indexResponse.json();
          const selected = Array.isArray(index?.storms)
            ? index.storms.find(
                (storm) =>
                  typeof storm?.id === "string" &&
                  typeof index.default === "string" &&
                  storm.id.toUpperCase() === index.default.toUpperCase(),
              )
            : null;
          if (
            selected &&
            typeof selected.file === "string" &&
            /^[A-Za-z0-9][A-Za-z0-9._-]*\.json$/.test(selected.file)
          ) {
            const replayUrl = `/replay/${selected.file}`;
            const replayResponse = await fetch(replayUrl, { cache: "reload" });
            if (replayResponse.ok) await cache.put(replayUrl, replayResponse);
          }
        } else if (indexResponse.status === 404 || indexResponse.status === 410) {
          const legacyResponse = await fetch("/replay/replay.json", { cache: "reload" });
          if (legacyResponse.ok) await cache.put("/replay/replay.json", legacyResponse);
        }
      } catch {
        // The application performs the authoritative schema/identity check.
        // A malformed or unavailable index must not prevent the shell install,
        // and must not be reinterpreted as permission to cache another storm.
      }
      // A fresh clone may legitimately have no replay yet. Successful shell
      // entries still install; absent optional entries are fetched next visit.
      void responses;
      await self.skipWaiting();
    }),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    Promise.all([
      caches.keys().then((keys) =>
        Promise.all(
          keys
            .filter((key) => key.startsWith("lighthouse-console-") && key !== CACHE)
            .map((key) => caches.delete(key)),
        ),
      ),
      self.clients.claim(),
    ]),
  );
});

async function networkFirst(request, fallback) {
  const cache = await caches.open(CACHE);
  try {
    const response = await fetch(request);
    if (response.ok) {
      await cache.put(request, response.clone());
      return response;
    }

    // A reachable origin can still be unusable during a partial deployment or
    // CDN failure. Preserve the last successfully loaded shell/replay for any
    // non-success response; if this device has never warmed it, retain the real
    // server response so diagnostics are not hidden.
    return (
      (await cache.match(request)) ||
      (fallback ? await cache.match(fallback) : undefined) ||
      response
    );
  } catch {
    const cached = (await cache.match(request)) || (fallback ? await cache.match(fallback) : undefined);
    return (
      cached ||
      new Response("Lighthouse is offline and this view has not been cached yet.", {
        status: 503,
        headers: { "Content-Type": "text/plain; charset=utf-8" },
      })
    );
  }
}

async function cacheFirst(request) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(request);
  if (cached) return cached;
  const response = await fetch(request);
  if (response.ok) await cache.put(request, response.clone());
  return response;
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET" || request.headers.has("range")) return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (request.mode === "navigate") {
    // This worker is served from the site root so it can cache the EOC's
    // root-level Next assets. It must not turn an unrelated offline route into
    // EOC HTML at the wrong URL, however. Only the evidence-complete replay has
    // a static fallback when PMTiles range requests are unavailable.
    if (url.pathname === "/eoc" || url.pathname === "/eoc/") {
      event.respondWith(networkFirst(request, "/eoc"));
    }
    return;
  }

  if (isReplayPath(url.pathname)) {
    event.respondWith(networkFirst(request));
    return;
  }

  // Next build assets are content-addressed. The other paths are stable names
  // whose bytes may change between releases, so they revalidate online and use
  // the cached copy only when the network is unavailable.
  if (url.pathname.startsWith("/_next/static/")) {
    event.respondWith(cacheFirst(request));
  } else if (isCacheablePath(url.pathname)) {
    event.respondWith(networkFirst(request));
  }
});

// The worker is installed after the first page load, so the document reports
// the same-origin resources it already used. Caching those hashed Next.js
// chunks here makes the very next reload usable without a network connection.
self.addEventListener("message", (event) => {
  if (event.data?.type !== "cache-urls" || !Array.isArray(event.data.urls)) return;

  event.waitUntil(
    caches.open(CACHE).then(async (cache) => {
      await Promise.allSettled(
        event.data.urls.map(async (rawUrl) => {
          const url = new URL(rawUrl, self.location.origin);
          if (url.origin !== self.location.origin || !isCacheablePath(url.pathname)) return;

          const response = await fetch(url.href);
          if (response.ok) await cache.put(url.href, response);
        }),
      );
    }),
  );
});
