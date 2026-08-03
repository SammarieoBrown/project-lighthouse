import assert from "node:assert/strict";
import test from "node:test";

import {
  scenarioFromArchiveTrack,
  validateStormCatalogue,
  validateTrackLibrary,
} from "./historical-catalogue.ts";

const positions = [
  { at: "1988-09-10T00:00:00Z", lat: 15, lon: -70, max_wind_kt: 80, r34_nm: 90 },
  { at: "1988-09-10T06:00:00Z", lat: 16, lon: -72, max_wind_kt: 105, r34_nm: 140 },
  { at: "1988-09-10T12:00:00Z", lat: 17, lon: -74, max_wind_kt: 95, r34_nm: 160 },
];

test("catalogue and track library fail closed on identity and counts", () => {
  const catalogue = validateStormCatalogue({
    schema: "lighthouse.storm-catalogue.v1",
    storm_count: 1,
    storms: [{
      id: "al081988",
      label: "Gilbert 1988",
      name: "Gilbert",
      year: 1988,
      closest_km: 5,
      peak_wind_kt: 160,
      points: 49,
      provenance: "mixed",
    }],
  });
  assert.equal(catalogue.storms[0].id, "al081988");

  const tracks = validateTrackLibrary({
    schema: "lighthouse.storm-track-library.v1",
    storm_count: 1,
    storms: [{ id: "al081988", label: "Gilbert 1988", provenance: "mixed", positions }],
  });
  assert.equal(tracks.storms[0].positions.length, 3);

  assert.throws(
    () => validateTrackLibrary({
      schema: "lighthouse.storm-track-library.v1",
      storm_count: 2,
      storms: [{ id: "al081988", label: "Gilbert 1988", provenance: "mixed", positions }],
    }),
    /declares 2 storms but contains 1/,
  );
});

test("archive fixes become a bounded editable scenario", () => {
  const library = validateTrackLibrary({
    schema: "lighthouse.storm-track-library.v1",
    storm_count: 1,
    storms: [{ id: "al081988", label: "Gilbert 1988", provenance: "mixed", positions }],
  });
  const scenario = scenarioFromArchiveTrack(library.storms[0]);
  assert.equal(scenario.name, "Gilbert 1988 edited scenario");
  assert.equal(scenario.maxWindKt, 105);
  assert.equal(scenario.radius34Nm, 140);
  assert.equal(scenario.startAt, "1988-09-10T00:00:00.000Z");
  assert.deepEqual(scenario.track[0], [-70, 15]);
  assert.deepEqual(scenario.track.at(-1), [-74, 17]);
  assert.ok(scenario.forwardSpeedKt >= 2 && scenario.forwardSpeedKt <= 40);
});
