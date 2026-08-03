import assert from "node:assert/strict";
import test from "node:test";

import { nearestImagery, validateImageryManifest } from "./imagery.ts";

const manifest = validateImageryManifest({
  storms: [{
    id: "al132025",
    source: "NOAA GOES-19",
    frames: [{
      at: "2025-10-27T15:00:20Z",
      tiles: "pmtiles://https://tiles.example/storm-imagery/al132025/frame.pmtiles/{z}/{x}/{y}",
    }],
  }],
});

test("GOES imagery is shown only near its actual observation time", () => {
  assert.equal(
    nearestImagery(manifest, "AL132025", "2025-10-27T16:00:00Z")?.at,
    "2025-10-27T15:00:20Z",
  );
  assert.equal(nearestImagery(manifest, "al132025", "2025-10-28T15:00:00Z"), null);
});

test("imagery manifests fail closed on non-production tile templates", () => {
  assert.throws(
    () => validateImageryManifest({
      storms: [{
        id: "al132025",
        source: "NOAA GOES-19",
        frames: [{ at: "2025-10-27T15:00:20Z", tiles: "http://example.test/{z}/{x}/{y}" }],
      }],
    }),
    /HTTPS PMTiles template/,
  );
});
