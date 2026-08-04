import type { AuthoredScenario } from "./model";

export type WeatherDepiction = {
  seed: number;
  phaseRad: number;
  intensity: number;
  eyeRatio: number;
  outerRadiusNm: number;
  motionAsymmetry: number;
  rightOfMotionRad: number;
};

const DEG = Math.PI / 180;

/**
 * Parameters for the modelled precipitation field. They are deterministic and come
 * only from the authored scenario and selected simulation hour. The result is
 * a visual explanation of the model wind field, not satellite or radar data.
 */
export function weatherDepiction(
  scenario: AuthoredScenario,
  headingDeg: number,
  elapsedHours: number,
): WeatherDepiction {
  const intensity = clamp((scenario.maxWindKt - 34) / (180 - 34), 0, 1);
  const rmwNm = clamp(52 - 0.32 * scenario.maxWindKt, 8, 42);
  const outerRadiusNm = clamp(scenario.radius34Nm * 1.18 + 16, 60, 410);
  const eyeRatio = scenario.maxWindKt >= 64
    ? clamp((rmwNm * (0.48 + intensity * 0.12)) / outerRadiusNm, 0.025, 0.105)
    : 0;
  const seed = stableWeatherSeed(scenario.name);

  return {
    seed,
    // The field advances only when the selected simulation hour advances. It
    // does not run an independent decorative animation.
    phaseRad: ((seed % 4096) / 4096) * Math.PI * 2 + elapsedHours * 0.035,
    intensity,
    eyeRatio,
    outerRadiusNm,
    motionAsymmetry: clamp(0.08 + scenario.forwardSpeedKt / 115, 0.09, 0.34),
    rightOfMotionRad: (headingDeg + 90) * DEG,
  };
}

export function stableWeatherSeed(value: string): number {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

function clamp(value: number, low: number, high: number) {
  return Math.max(low, Math.min(high, value));
}
