# Lighthouse: the map stack

How the console draws a map with no internet, and the four traps that cost most of a day.

Read the traps before touching `apps/console/app/eoc/map/`. Every one of them fails **silently** — the map builds, the canvas sizes, the controls draw, and the thing is simply wrong in a way that survives all the way to a demo.

---

## The stack

| Piece | What | Why |
|---|---|---|
| **MapLibre GL JS 6** | Renderer | Named in the build spec. WebGL2 only. |
| **PMTiles** | Tile format | One file, read over HTTP range requests. Opening a 98 MB archive costs kilobytes. |
| **Protomaps** | Tile source + style | Daily planet build; `pmtiles extract` pulls our region without downloading 137 GB. |
| **`@protomaps/basemaps`** | Style generator | A Flavor is a plain colour object, so the basemap wears our tokens. |

No `react-map-gl`. One map, a static snapshot, and a design system that wants every layer — a hundred lines of init beats a dependency whose main offer is JSX sugar.

### Two archives, switched by zoom

One archive cannot be both seamless and detailed at a sane size.

| Archive | Extent | Zoom | Size |
|---|---|---|---|
| `caribbean-z11.pmtiles` | Trinidad → Yucatán → Bahamas | z0–11 | 98 MB |
| `jamaica-z15.pmtiles` | Jamaica | z0–15 | 40 MB |

They hand over at **z10.5**, where Jamaica fills the frame and nobody sees the seam. The basin at working zoom would be 442 MB, almost all of it street geometry for countries we hold no registry in.

The viewport is **fenced** to the covered region (`maxBounds`, `minZoom`). A map that ends mid-pan reads as broken, and an operator who can scroll into blank ocean has been given a control that only does the wrong thing. Constrain the viewport; do not chase the bounding box outward forever.

### Rebuilding

```bash
brew install pmtiles
python3 data/tiles/fetch_basemap.py
```

Archives are **fetched, not committed** — 138 MB, rebuildable in under a minute from a pinned upstream build. Their checksums live in `data/tiles/cache/manifest.sha256`, which *is* committed: the point of the manifest is that your copy matches, not that git carries it.

Glyphs and sprites **are** committed. They are about a megabyte, and without them the map renders and silently loses every place name — see trap 4.

---

## The four traps

### 1. Turbopack drops MapLibre's worker

**Symptom.** The map builds. The canvas sizes correctly. Navigation controls draw. The scale bar reports a sensible distance. `map.getStyle().sources` lists everything. And not one tile ever loads — every source stays `isSourceLoaded() === false`, `queryRenderedFeatures()` returns nothing, and the screen renders as a map of the open sea. No errors.

**Cause.** MapLibre parses every source in a web worker. Turbopack does not bundle its inline worker, so the worker never starts and every source stalls forever.

**Fix.** Serve the worker yourself and call `setWorkerUrl()` once per document:

```js
setWorkerUrl(`${window.location.origin}/maplibre/maplibre-gl-worker.mjs`);
```

**The part that bites twice:** `maplibre-gl-worker.mjs` imports `maplibre-gl-shared.mjs` from *its own directory*. Ship the worker without its sibling and it fails on its first import — the same silent blank map, one step further along. `scripts/stage-assets.mjs` copies both, and treats a missing worker as fatal because a broken install should stop the build.

### 2. Byte-range serving cannot be assumed

**Symptom.** The map works, but feels slow and occasionally throws
`Server returned no content-length header or content-length exceeding request`.

**Cause.** Next's static handler answered the **first** request for a large file in `public/` with a **200 and the entire body**, ignoring the `Range` header it was sent and omitting `Content-Range`. Every *subsequent* request was a correct 206. The pmtiles client notices, complains, retries, and recovers — so the map works, having first downloaded **98 MB to read 16 KB of header**.

This is the worst kind of bug: it is not a failure, it is a success with a hidden cost, and it only hurts on the network you cannot control.

**Fix.** Archives live outside `public/` and are served by `app/map/[file]/route.ts`, which implements Range properly — both open-ended forms (`bytes=100-`, `bytes=-100`), 416 where required, filename allowlisted and the resolved path re-checked.

**Measured:** 11 requests and 199 KB to draw the map, against 98 MB before.

### 3. `force-static` reintroduces trap 2

The fix for trap 2 shipped with `export const dynamic = "force-static"`, which lets Next prerender the route and cache the response — **discarding the `Range` header** and serving the whole archive with a 200.

The fix failed the same way as the thing it fixed. A range handler must be `force-dynamic`, and there is no version of this that is cacheable at the route level.

### 4. Tokens inherit from the wrong element

`readTokens()` read from `document.documentElement`, but the console sets `data-theme="dark"` on its `<main>`. The basemap resolved the **light** palette and painted near-white on a machine set to light mode.

The same bug bit earlier at the CSS level: `color` inherits as a *computed* value, so scoping `data-theme` to an element redefines the tokens for its subtree while text colour resolved higher up keeps inheriting down. Every reading and the whole counts table came out near-black on near-black.

**Rule.** Read tokens from an element **inside** the themed subtree, and restate `color` wherever you restate ground.

---

## Debugging a blank map

A blank map is the default failure of this stack, so start by making it talk.

1. **Log every map error.** `map.on("error", …)` is where MapLibre puts style failures, and a handler that filters for one message swallows the rest. That is how trap 1 hid behind a sprite-URL error for an hour.
2. **Check `isStyleLoaded()` and `isSourceLoaded()`.** Style loaded but no source caches means the worker (trap 1).
3. **Check whether GeoJSON sources load.** They need no protocol and no network. If *those* stall, it is the worker, not the tiles.
4. **Count the bytes.** `performance.getEntriesByType("resource")` filtered to `.pmtiles`. Healthy is ~11 requests and ~200 KB. Anything near the archive size is trap 2.
5. **A dev handle helps.** `window.lhMap` is set outside production; a map that renders nothing is very hard to debug from the outside.

And restart the dev server after changing map code. Half an hour went into an error that was a stale server holding old module state.
