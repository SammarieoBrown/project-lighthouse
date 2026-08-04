import assert from "node:assert/strict";
import test from "node:test";

import { defaultScenario } from "./model.ts";
import { stableWeatherSeed, weatherDepiction } from "./weather-field.ts";

test("modelled precipitation depiction is deterministic for the selected simulation hour", () => {
  const scenario = { ...defaultScenario(), name: "Hurricane Gilbert 1988", maxWindKt: 160 };
  const first = weatherDepiction(scenario, 286, 42);
  const second = weatherDepiction({ ...scenario, track: [...scenario.track] }, 286, 42);

  assert.deepEqual(first, second);
  assert.equal(first.seed, stableWeatherSeed("Hurricane Gilbert 1988"));
  assert.notEqual(first.phaseRad, weatherDepiction(scenario, 286, 43).phaseRad);
});

test("hurricanes have a bounded eye while tropical storms do not fabricate one", () => {
  const scenario = defaultScenario();
  const hurricane = weatherDepiction({ ...scenario, maxWindKt: 110 }, 270, 0);
  const tropicalStorm = weatherDepiction({ ...scenario, maxWindKt: 50 }, 270, 0);

  assert.ok(hurricane.eyeRatio >= 0.025 && hurricane.eyeRatio <= 0.105);
  assert.equal(tropicalStorm.eyeRatio, 0);
});

test("weather extent and motion asymmetry remain bounded at authoring extremes", () => {
  const scenario = defaultScenario();
  const compact = weatherDepiction({ ...scenario, radius34Nm: 25, forwardSpeedKt: 2 }, 0, 0);
  const broad = weatherDepiction({ ...scenario, radius34Nm: 320, forwardSpeedKt: 40 }, 180, 0);

  assert.equal(compact.outerRadiusNm, 60);
  assert.ok(Math.abs(broad.outerRadiusNm - 393.6) < 1e-9);
  assert.ok(compact.motionAsymmetry >= 0.09);
  assert.ok(broad.motionAsymmetry <= 0.34);
  assert.notEqual(compact.rightOfMotionRad, broad.rightOfMotionRad);
});
