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
 * The checksummed basemap and structure archives are fetched or built rather
 * than committed, so they are legitimately absent on a fresh clone and in CI,
 * where the build only needs to prove the code compiles. Missing core tiles are
 * a warning: the console falls back to the SVG map, which presents the same
 * forecast and parish-impact evidence in a static form. A missing structures
 * archive affects only that optional view.
 */

import { createHash } from "node:crypto";
import {
  cpSync,
  createReadStream,
  existsSync,
  mkdirSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const console_ = resolve(here, "..");
const repo = resolve(console_, "../..");

const publicTiles = join(console_, "public/tiles");
// Archives sit outside public/ and are served by app/map/[file]/route.ts,
// which implements Range correctly. Next's static handler does not.
const archiveDir = join(console_, ".tiles");
const publicWorker = join(console_, "public/maplibre");

function readChecksums(path) {
  if (!existsSync(path)) return null;
  const checksums = new Map();
  for (const [index, line] of readFileSync(path, "utf8").split(/\r?\n/).entries()) {
    if (!line) continue;
    const match = /^([0-9a-f]{64})  (.+)$/.exec(line);
    const name = match?.[2] ?? "";
    if (
      !match ||
      name.startsWith("/") ||
      name.split("/").includes("..") ||
      checksums.has(name)
    ) {
      throw new Error(`invalid checksum manifest line ${index + 1}`);
    }
    checksums.set(name, match[1]);
  }
  return checksums;
}

function sha256File(path) {
  return new Promise((resolveHash, rejectHash) => {
    const hash = createHash("sha256");
    const stream = createReadStream(path);
    stream.on("data", (chunk) => hash.update(chunk));
    stream.on("error", rejectHash);
    stream.on("end", () => resolveHash(hash.digest("hex")));
  });
}

async function verified(path, expected) {
  return Boolean(expected) && existsSync(path) && (await sha256File(path)) === expected;
}

// Cleared rather than merged: a renamed archive would otherwise leave the old
// one behind and the map would keep loading a file nobody meant to ship.
for (const dir of [publicTiles, publicWorker, archiveDir]) {
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
const archives = ["caribbean-z11.pmtiles", "jamaica-z15.pmtiles"];
// Ours, and separately optional. A clone that has not built it should still
// get a working basemap — only the structures view goes missing, and the
// panel says so. Bundling it with the two above would mean one absent file
// silently downgrades the whole map to the SVG fallback.
const extras = ["structures-z15.pmtiles"];
const requiredAssetKeys = [
  ...["Noto Sans Regular", "Noto Sans Medium", "Noto Sans Italic"].flatMap((stack) =>
    ["0-255", "256-511"].map((range) => `assets/fonts/${stack}/${range}.pbf`),
  ),
  ...["black.json", "black.png", "black@2x.json", "black@2x.png"].map(
    (name) => `assets/sprites/${name}`,
  ),
];
const basemapManifestPath = join(cache, "manifest.sha256");
let basemapManifest = null;
try {
  basemapManifest = readChecksums(basemapManifestPath);
} catch (error) {
  console.warn(`stage-assets: basemap manifest invalid — ${error instanceof Error ? error.message : error}`);
}
const runtimeKeys = [...archives, ...requiredAssetKeys];
const runtimeChecks = await Promise.all(
  runtimeKeys.map(async (name) => [
    name,
    await verified(join(cache, name), basemapManifest?.get(name)),
  ]),
);
const invalidRuntime = runtimeChecks.filter(([, valid]) => !valid).map(([name]) => name);
const archiveValidators = {};

if (basemapManifest && invalidRuntime.length === 0) {
  for (const name of archives) {
    const source = join(cache, name);
    cpSync(source, join(archiveDir, name));
    archiveValidators[name] = {
      bytes: statSync(source).size,
      sha256: basemapManifest.get(name),
    };
  }
  for (const name of requiredAssetKeys) {
    const destination = join(publicTiles, name);
    mkdirSync(dirname(destination), { recursive: true });
    cpSync(join(cache, name), destination);
  }

  for (const name of extras) {
    const source = join(cache, name);
    const provenancePath = join(cache, "..", "structures.manifest.json");
    let record = null;
    try {
      const document = JSON.parse(readFileSync(provenancePath, "utf8"));
      if (
        document.schema === "lighthouse.structure-archive.v1" &&
        document.artifact?.path === `cache/${name}` &&
        Number.isInteger(document.artifact?.bytes) &&
        document.artifact.bytes > 0 &&
        /^[0-9a-f]{64}$/.test(document.artifact?.sha256 ?? "")
      ) {
        record = document.artifact;
      }
    } catch {
      // Reported below as unavailable. A malformed manifest must never stage
      // plausible but unproven structure bytes.
    }

    const validStructure = record
      && existsSync(source)
      && (await verified(source, record.sha256))
      && Number(record.bytes) === statSync(source).size;
    if (validStructure) {
      cpSync(source, join(archiveDir, name));
      archiveValidators[name] = {
        bytes: Number(record.bytes),
        sha256: record.sha256,
      };
    } else {
      console.warn(
        `stage-assets: ${name} absent or unproven — the structures view will be unavailable.`,
      );
    }
  }
  console.log(`stage-assets: basemap staged (${archives.join(", ")})`);
} else {
  console.warn(
    `stage-assets: basemap absent or unverified${invalidRuntime.length ? ` (${invalidRuntime.join(", ")})` : ""} — ` +
      "the console will fall back to the SVG map.\n" +
      "  Run: python3 data/tiles/fetch_basemap.py",
  );
}

/* The route needs a strong validator before it can safely combine browser
 * caches with byte ranges. Re-hashing up to 180 MB on each serverless cold
 * start would defeat PMTiles' small-request model, so carry forward the hashes
 * we already verified while staging. The file is not public or routeable. */
writeFileSync(
  join(archiveDir, "validators.json"),
  `${JSON.stringify({ schema: "lighthouse.tile-validators.v1", archives: archiveValidators }, null, 2)}\n`,
);
