#!/usr/bin/env node
/**
 * Stage the map's runtime assets into public/ before dev and build.
 *
 * Two kinds of thing land here, and they fail differently on purpose.
 *
 * MapLibre's worker is a hard requirement: without it the map builds, sizes its
 * canvas, draws its controls and never loads a single tile — a blank map that
 * looks like the open sea. It comes from node_modules, so if it is missing the
 * install is broken and this should stop.
 *
 * The basemap archive is 69 MB and fetched rather than committed, so it is
 * legitimately absent on a fresh clone and in CI, where the build only needs to
 * prove the code compiles. Missing tiles are a warning: the console falls back
 * to the SVG map, which is correct and static rather than broken.
 */

import { cpSync, existsSync, mkdirSync, rmSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const console_ = resolve(here, "..");
const repo = resolve(console_, "../..");

const publicTiles = join(console_, "public/tiles");
const publicWorker = join(console_, "public/maplibre");

// Cleared rather than merged: a renamed archive would otherwise leave the old
// one behind and the map would keep loading a file nobody meant to ship.
for (const dir of [publicTiles, publicWorker]) {
  rmSync(dir, { recursive: true, force: true });
  mkdirSync(dir, { recursive: true });
}

// --- MapLibre worker: required ---
const dist = join(console_, "node_modules/maplibre-gl/dist");
for (const file of ["maplibre-gl-worker.mjs", "maplibre-gl-shared.mjs"]) {
  const from = join(dist, file);
  if (!existsSync(from)) {
    console.error(
      `stage-assets: ${file} is missing from maplibre-gl. Run npm install.\n` +
        "Without it every map source silently fails to load.",
    );
    process.exit(1);
  }
  cpSync(from, join(publicWorker, file));
}

// --- basemap + glyphs: optional ---
const cache = join(repo, "data/tiles/cache");
const archive = join(cache, "caribbean-z13.pmtiles");
const assets = join(cache, "assets");

if (existsSync(archive)) {
  cpSync(archive, join(publicTiles, "caribbean-z13.pmtiles"));
  if (existsSync(assets)) cpSync(assets, join(publicTiles, "assets"), { recursive: true });
  console.log("stage-assets: basemap staged");
} else {
  console.warn(
    "stage-assets: no basemap archive — the console will fall back to the SVG map.\n" +
      "  Run: python3 data/tiles/fetch_basemap.py",
  );
}
