import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  validateLibrary,
  validateReplay,
  validateReplayForEntry,
} from "./replay.ts";

function fixture(name) {
  return JSON.parse(
    readFileSync(new URL(`../../public/replay/${name}`, import.meta.url), "utf8"),
  );
}

test("the published index and artifacts agree on identity and provenance", () => {
  const library = validateLibrary(fixture("index.json"));
  assert.equal(library.storms.length, 2);

  for (const entry of library.storms) {
    const replay = validateReplay(fixture(entry.file));
    assert.equal(validateReplayForEntry(replay, entry), replay);
  }
});

test("unknown evidence provenance fails closed", () => {
  const raw = fixture("index.json");
  raw.storms[0].kind = "prediction";
  assert.throws(() => validateLibrary(raw), /kind must be advisory or hindcast/);

  const rawSize = fixture("index.json");
  rawSize.storms[0].size_source = "probably-measured";
  assert.throws(
    () => validateLibrary(rawSize),
    /size_source must be measured, modelled, mixed or unavailable/,
  );
});

test("mixed and unavailable wind provenance remain explicit", () => {
  for (const source of ["mixed", "unavailable"]) {
    const raw = fixture("index.json");
    raw.storms[0].size_source = source;
    assert.equal(validateLibrary(raw).storms[0].sizeSource, source);
  }
});

test("index replay paths cannot escape the replay library", () => {
  const raw = fixture("index.json");
  raw.storms[0].file = "../another-storm.json";
  assert.throws(() => validateLibrary(raw), /replay JSON basename/);
});

test("an index cannot relabel a different replay artifact", () => {
  const library = validateLibrary(fixture("index.json"));
  const melissa = validateReplay(fixture("al132025.json"));
  const gilbertEntry = library.storms.find((storm) => storm.id === "al081988");
  assert.ok(gilbertEntry);
  assert.throws(
    () => validateReplayForEntry(melissa, gilbertEntry),
    /Replay identity mismatch/,
  );
});
