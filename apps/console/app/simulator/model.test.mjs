import assert from "node:assert/strict";
import test from "node:test";

import {
  calculateImpact,
  defaultScenario,
  destination,
  distanceNm,
  radiusAtThreshold,
  simulationFrames,
  windVectorAt,
} from "./model.ts";

test("authored maximum wind remains authoritative with translation", () => {
  for (const headingDeg of [0, 135, 315]) {
    const control = { maxWindKt: 120, radius34Nm: 150, forwardSpeedKt: 18 };
    const centre = [-77.4, 17.8];
    let maximum = 0;
    for (let bearing = 0; bearing < 360; bearing += 2) {
      for (let radius = 4; radius <= 80; radius += 0.5) {
        maximum = Math.max(
          maximum,
          windVectorAt(destination(centre, bearing, radius), centre, headingDeg, control).speedKt,
        );
      }
    }
    assert.ok(maximum <= control.maxWindKt + 0.05, `${headingDeg}° peak ${maximum}`);
    assert.ok(maximum >= control.maxWindKt - 0.25, `${headingDeg}° peak ${maximum}`);
  }
});

test("size control sets the maximum 34 kt extent", () => {
  const control = { maxWindKt: 105, radius34Nm: 175, forwardSpeedKt: 11 };
  const heading = 315;
  const aligned = (heading + 112.6) % 360;
  assert.ok(Math.abs(radiusAtThreshold(34, aligned, heading, control) - 175) < 0.2);
});

test("a threshold above authoritative maximum is absent", () => {
  const control = { maxWindKt: 60, radius34Nm: 120, forwardSpeedKt: 20 };
  assert.equal(radiusAtThreshold(64, 45, 270, control), 0);
  assert.ok(radiusAtThreshold(50, 45, 270, control) > 0);
});

test("simulation frames preserve both drawn endpoints", () => {
  const scenario = defaultScenario();
  const frames = simulationFrames(scenario);
  assert.deepEqual(frames[0].centre, scenario.track[0]);
  assert.deepEqual(frames.at(-1)?.centre, scenario.track.at(-1));
  assert.equal(frames.at(-1)?.progress, 1);
});

test("long simulations keep their hourly cadence without a final teleport", () => {
  const scenario = {
    ...defaultScenario(),
    track: [[-90, 18], [-60, 18]],
    forwardSpeedKt: 2,
  };
  const frames = simulationFrames(scenario);
  assert.ok(frames.length > 241);
  const largestStep = Math.max(...frames.slice(1).map((frame, index) =>
    distanceNm(frames[index].centre, frame.centre),
  ));
  assert.ok(largestStep <= 2.01, `largest hourly step was ${largestStep} nm`);
  assert.deepEqual(frames.at(-1)?.centre, scenario.track.at(-1));
});

test("community preview preserves the mapped structure denominator", () => {
  const scenario = { maxWindKt: 110, radius34Nm: 150, forwardSpeedKt: 10 };
  const impact = calculateImpact(
    [{ id: 1, parish: "Kingston", district: "Harbour", lon: -76.8, lat: 18, structures: 100 }],
    [
      { parish: "Kingston", community: "Harbour", roof: "zinc" },
      { parish: "Kingston", community: "Harbour", roof: "concrete" },
    ],
    [-76.8, 17.75],
    0,
    scenario,
  );
  assert.equal(
    impact.destroyed + impact.major + impact.minor + impact.none,
    impact.assessedStructures,
  );
  assert.equal(impact.assessedStructures, 100);
});
