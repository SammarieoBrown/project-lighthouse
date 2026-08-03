# Lighthouse: the map stack

How the EOC console keeps its map useful, truthful and recoverable on an unreliable network.

Read this before changing `apps/console/app/eoc/map/`. Most failures in this stack do not throw a visible exception: the canvas and controls can appear while the evidence is missing or means something different from the legend.

## The evidence contract

The selected replay frame contains three different kinds of information, and the map keeps them visually and verbally separate:

- **Observed:** the advisory's storm position and intensity readings.
- **Forecast:** the cone, track, 34 kt, 50 kt and 64 kt wind fields, plus published location probabilities.
- **Modelled:** expected damage for the synthetic household registry. This is aggregated to real parish geometry; synthetic household and district coordinates are never drawn as observed homes.

The structures archive is neutral reference inventory. A footprint or aggregate says only that mapped structure data from public datasets exists there. The selected forecast polygons are drawn above it; no advisory, damage band or cumulative "first hit" value is baked into a building tile.

The interactive MapLibre view and the SVG fallback both derive parish impact through `parishImpacts()` in `apps/console/app/eoc/map.tsx`. Losing WebGL or a basemap therefore changes navigation and detail, not the evidence being claimed.

## The stack

| Piece | Role | Boundary |
|---|---|---|
| MapLibre GL JS 6 | Interactive renderer | WebGL2; rotation and pitch are disabled |
| PMTiles | Range-addressed vector archives | Never cache partial responses as whole files |
| Protomaps + `@protomaps/basemaps` | Basemap data and token-driven style | Context, not hazard or impact evidence |
| Replay GeoJSON | Forecast, storm position and parish impact | One selected advisory at a time |
| SVG synoptic map | Evidence-equivalent fallback | Static; no basemap or structure-detail claim |

There is no `react-map-gl` wrapper. The map is constructed once, and replay steps call `setData` on the existing GeoJSON sources so the viewport, tile caches and controls survive a scrub.

## Archives and zoom contracts

| Archive | Content | Native zoom |
|---|---|---|
| `caribbean-z11.pmtiles` | Caribbean context | z0–11 |
| `jamaica-z15.pmtiles` | Jamaica detail | z0–15 |
| `structures-z15.pmtiles` | Weighted structure aggregates, then mapped source-footprint features | z9–15 |

The regional and island basemaps hand over at z10.5. The viewport is fenced to their covered region so an operator cannot pan into a blank area that looks like failed data.

The structures archive has two deliberately different layers:

- `structure_points`, z9–13: one deterministic 0.005-degree grid aggregate per occupied cell. Property `w` is the exact number of mapped source footprints represented; the build asserts that `sum(w)` equals the footprint inventory.
- `structures`, z14–15: one vector-tile feature per mapped source footprint, carrying only compact district/community geography properties `d` and `c`. Tile geometry is quantized and simplified for display; the source inventory remains authoritative.

The panel names the active representation. It never says "every building" while grouped aggregates are on screen.

## Local, hosted and offline behavior

With no `NEXT_PUBLIC_TILES_URL`, archives are staged into `apps/console/.tiles` and served by `app/map/[file]/route.ts`. That route allowlists the three filenames, supports GET and HEAD byte ranges, streams through a cancellable Web stream, and requires revalidation because the filenames are stable across rebuilds.

With `NEXT_PUBLIC_TILES_URL`, production reads the same archive and asset tree from the public tile host. The host must support CORS and HTTP 206 range responses. Glyphs and sprites are required for a labelled basemap and live beside the archives in both modes.

The production service worker caches the console shell, deterministic replay and hashed application chunks after a successful visit. It intentionally bypasses every request carrying a `Range` header: the Cache API cannot safely key partial PMTiles responses, and returning the wrong 206 is worse than showing the static map. Consequently:

- a previously visited console and replay can reload offline;
- interactive PMTiles are available only while their local or hosted range source is reachable; and
- a range failure degrades to the replay-backed SVG forecast/parish-impact map.

This is a warm-cache guarantee, not a claim that a device can open the console offline before its first successful visit.

`Reference imagery` is Esri World Imagery used live with attribution. It is online-only, is not cached by Lighthouse, and is explicitly labelled as general context—not storm-dated or post-event damage evidence. Failure of that optional raster leaves the standard basemap in place.

## Build and publication

```bash
brew install pmtiles tippecanoe
python3 data/tiles/fetch_basemap.py
cd apps/api && uv run python -m app.registry.building_tiles
```

Fetched basemap bytes are pinned by `data/tiles/cache/manifest.sha256`. The derived structures archive is pinned separately by `data/tiles/structures.manifest.json`, which records source hashes, the aggregation/tiling recipes, tool versions, intermediate hashes, counts, final byte size and final SHA-256. A basemap fetch must never certify whichever derived archive happens to be present.

Before publication, verify the local artifact and its layer contract:

```bash
pmtiles verify data/tiles/cache/structures-z15.pmtiles
pmtiles show --metadata data/tiles/cache/structures-z15.pmtiles
```

`python3 data/tiles/upload_basemap.py --verify` is a read-only audit of what is
already public; it is expected to fail before a changed local artifact has been
published. The publisher compares full SHA-256 content, not only
`Content-Length`, and separately verifies that each PMTiles object answers a
header range with HTTP 206 and a revalidating cache policy. Run it without
`--verify` only when publishing reviewed local artifacts is intended.

## Failure boundaries

- **Regional or island basemap failure:** render the SVG map with the same selected forecast and parish impact.
- **Structures archive failure:** keep the healthy standard basemap, hide only the optional inventory layers, and report inventory unavailable.
- **Reference imagery failure:** keep the standard basemap and forecast/impact overlays.
- **Missing replay:** keep the console shell, disable replay controls, and state that replay data is unavailable.
- **Invalid replay:** fail closed before drawing plausible-looking bad geography or counts.

## Traps that fail silently

### MapLibre's worker and shared module

Turbopack does not reliably bundle MapLibre's inline worker. `scripts/stage-assets.mjs` copies both `maplibre-gl-worker.mjs` and its sibling `maplibre-gl-shared.mjs`; `setWorkerUrl()` points MapLibre at the staged worker. Missing either file can leave a correctly sized blank map with no useful browser exception.

### Range handling

Do not move PMTiles into `public/` and trust the framework's static handler. A first request has previously returned the entire archive with HTTP 200. Do not mark the route `force-static`: prerendering discards the request Range header and recreates the same bug.

### Stable filenames are not immutable

The archive names stay stable while their bytes can change. `immutable` caching serves old map data after a rebuild. The local route uses validators with `max-age=0, must-revalidate`; the active PMTiles client still caches the ranges it has already read.

### Theme tokens must be read inside the themed subtree

The console applies `data-theme="dark"` to its `<main>`. Read CSS tokens from the map container, not `document.documentElement`, or a browser in light mode can generate a light basemap inside the dark console.

### Optional sources must stay optional

Do not classify every error containing the word `pmtiles` as a basemap failure. Identify the actual source or archive. Otherwise one unavailable structures tile can tear down a healthy regional/island map.

### Archive properties are a contract

Inspect `vector_layers` and `strategies` after every structures build. The wide layer may expose only `w`; footprints may expose only `d` and `c`. Forecast/advisory fields, unexpected zoom ranges, or millions of low-zoom polygon drops mean the archive is wrong even when it renders.

## Debugging checklist

1. Log every MapLibre error; do not filter the event stream down to one expected message.
2. Inspect `isStyleLoaded()`, `isSourceLoaded()` and `queryRenderedFeatures()` through `window.lhMap` in development.
3. Verify the worker and shared module return 200 before blaming tile data.
4. Inspect PMTiles network requests: healthy reads are repeated small 206 responses, not one archive-sized 200.
5. Test Forecast + impact, Reference imagery and Structures independently; an optional-mode failure must not remove another mode.
6. Force the basemap unavailable and compare the SVG fallback's advisory, wind fields, parish bands and counts.
7. Restart a stale development server before treating HMR state as runtime evidence.
