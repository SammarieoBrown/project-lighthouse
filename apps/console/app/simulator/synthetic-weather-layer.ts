import {
  destination,
  type AuthoredScenario,
  type LngLat,
  type SimulationFrame,
} from "./model";
import { weatherDepiction, type WeatherDepiction } from "./weather-field";

export type SyntheticWeatherState = {
  scenario: AuthoredScenario;
  frame: SimulationFrame | null;
  animationPhaseRad?: number;
};

export type SyntheticWeatherPalette = {
  rain: string;
  cold: string;
  deep: string;
  severe: string;
  core: string;
  extreme: string;
};

export type SyntheticWeatherImage = {
  image: HTMLCanvasElement;
  coordinates: [LngLat, LngLat, LngLat, LngLat];
};

const IMAGE_SIZE = 384;
const TAU = Math.PI * 2;
let cachedKey = "";
let cachedImage: SyntheticWeatherImage | null = null;

/**
 * Builds a deterministic modelled-radar raster for one simulation hour. It is
 * derived from the scenario's wind extent, intensity, heading and translation
 * asymmetry. It is not calibrated rainfall and never represents observation.
 */
export function createSyntheticWeatherImage(
  state: SyntheticWeatherState,
  palette: SyntheticWeatherPalette,
): SyntheticWeatherImage | null {
  if (!state.frame) return null;
  const key = JSON.stringify([
    state.scenario.name,
    state.scenario.maxWindKt,
    state.scenario.radius34Nm,
    state.scenario.forwardSpeedKt,
    state.frame.centre,
    state.frame.headingDeg,
    state.frame.elapsedHours,
    roundPhase(state.animationPhaseRad ?? 0),
    palette,
  ]);
  if (key === cachedKey && cachedImage) return cachedImage;
  const baseDepiction = weatherDepiction(
    state.scenario,
    state.frame.headingDeg,
    state.frame.elapsedHours,
  );
  const depiction = {
    ...baseDepiction,
    phaseRad: normalAngle(baseDepiction.phaseRad + (state.animationPhaseRad ?? 0)),
  };
  const image = document.createElement("canvas");
  image.width = IMAGE_SIZE;
  image.height = IMAGE_SIZE;
  const context = image.getContext("2d");
  if (!context) throw new Error("modelled radar canvas is unavailable");
  const pixels = context.createImageData(IMAGE_SIZE, IMAGE_SIZE);
  renderModelledRadar(pixels, depiction, palette, state.animationPhaseRad ?? 0);
  context.putImageData(pixels, 0, 0);
  cachedKey = key;
  cachedImage = {
    image,
    coordinates: weatherCoordinates(state.frame.centre, depiction.outerRadiusNm),
  };
  return cachedImage;
}

function renderModelledRadar(
  image: ImageData,
  depiction: WeatherDepiction,
  palette: SyntheticWeatherPalette,
  animationPhaseRad: number,
) {
  const colours = [
    parseHex(palette.rain),
    parseHex(palette.cold),
    parseHex(palette.deep),
    parseHex(palette.severe),
    parseHex(palette.core),
    parseHex(palette.extreme),
  ];
  const half = image.width / 2;
  const radiusPixels = image.width * 0.46;
  // Slow advection of the noise domain makes cells grow, shear and decay as
  // the circulation rotates. This is a qualitative model animation, not a
  // sequence of observed radar frames.
  const evolutionX = Math.cos(depiction.phaseRad * 0.73) * 0.16;
  const evolutionY = Math.sin(depiction.phaseRad * 0.61) * 0.16;
  const seedX = ((depiction.seed >>> 8) & 0xffff) / 8192 + evolutionX;
  const seedY = ((depiction.seed >>> 16) & 0xffff) / 8192 + evolutionY;
  const eye = depiction.eyeRatio;
  const eyewall = eye > 0 ? Math.max(eye * 1.72, 0.058) : 0.07;
  // Preserve the irregular generated radar field and rotate it as one evolving
  // mass. This adds circulation without winding the texture into perfect rings.
  const rotation = animationPhaseRad * 0.9;
  const rotationCos = Math.cos(rotation);
  const rotationSin = Math.sin(rotation);

  for (let y = 0; y < image.height; y += 1) {
    const north = (half - y) / radiusPixels;
    for (let x = 0; x < image.width; x += 1) {
      const east = (x - half) / radiusPixels;
      const rawRadius = Math.hypot(east, north);
      if (rawRadius > 1.18) continue;
      const angle = normalAngle(Math.atan2(east, north));
      const sampleEast = east * rotationCos + north * rotationSin;
      const sampleNorth = north * rotationCos - east * rotationSin;
      const sampleAngle = normalAngle(angle + rotation);
      const right = Math.cos(angle - depiction.rightOfMotionRad);
      const forwardRight = Math.cos(angle - depiction.rightOfMotionRad + 0.58);
      const shape = 1 + depiction.motionAsymmetry * (right * 0.72 + forwardRight * 0.28);
      const radius = rawRadius / Math.max(0.62, shape);
      if (radius > 1.12) continue;

      const broad = fbm(sampleEast * 4.2 + seedX, sampleNorth * 4.2 + seedY, depiction.seed, 3);
      const fine = fbm(sampleEast * 13.5 - seedY, sampleNorth * 13.5 + seedX, depiction.seed ^ 0x9e3779b9, 2);
      const breakup = fbm(sampleEast * 7.1 + seedY, sampleNorth * 7.1 - seedX, depiction.seed ^ 0x85ebca6b, 2);
      const arcNoise = fbm(sampleEast * 2.7 - seedX, sampleNorth * 2.7 + seedY, depiction.seed ^ 0xc2b2ae35, 2);
      const cellNoise = fbm(sampleEast * 23.5 + seedY, sampleNorth * 23.5 - seedX, depiction.seed ^ 0x27d4eb2d, 1);
      const dryNoise = fbm(
        Math.cos(sampleAngle) * 3.2 + radius * 4.4 + seedX,
        Math.sin(sampleAngle) * 3.2 - radius * 3.7 + seedY,
        depiction.seed ^ 0x165667b1,
        2,
      );

      // Domain-warped, narrow band cores. Independent coarse, angular and
      // high-frequency gates break them into finite rainbands and dry slots.
      const warpedAngle = sampleAngle + (arcNoise - 0.5) * 0.42 + (fine - 0.5) * 0.2;
      const warpedRadius = radius + (broad - 0.5) * 0.075 + (fine - 0.5) * 0.025;
      const spiralPhase = warpedAngle * 3.15 - warpedRadius * 22.5 + depiction.phaseRad * 1.6;
      const spiral = 0.5 + 0.5 * Math.sin(spiralPhase);
      const finiteArcGate = smoothstep(
        0.36,
        0.67,
        arcNoise + 0.16 * Math.sin(sampleAngle * 4.2 + depiction.phaseRad - radius * 3.5),
      );
      const drySlotGate = smoothstep(0.37, 0.64, dryNoise + breakup * 0.14);
      const cellGate = smoothstep(0.42, 0.7, cellNoise + fine * 0.18);
      const band = smoothstep(0.7, 0.93, spiral)
        * smoothstep(0.34, 0.72, breakup + broad * 0.18)
        * finiteArcGate
        * drySlotGate
        * (0.38 + cellGate * 0.62);
      const outerFade = 1 - smoothstep(0.72, 1.08, radius);
      const innerBandGate = smoothstep(eyewall * 1.15, 0.24, radius);
      let reflectivity = band * outerFade * innerBandGate
        * (0.3 + depiction.intensity * 0.42)
        * (0.62 + 0.38 * fine);

      // A finer scattered-cell and stratiform component fills the asymmetric
      // shield between band cores, without painting an opaque storm disk.
      const scatteredGate = smoothstep(0.49, 0.7, cellNoise * 0.62 + broad * 0.3 + breakup * 0.2);
      const scatteredEnvelope = (1 - smoothstep(0.48, 1.06, radius))
        * innerBandGate
        * clamp(0.82 + right * depiction.motionAsymmetry * 0.62, 0.58, 1.2);
      const scattered = scatteredEnvelope
        * scatteredGate
        * drySlotGate
        * (0.18 + depiction.intensity * 0.24)
        * (0.7 + fine * 0.3);
      reflectivity = Math.max(reflectivity, scattered);

      // The central dense precipitation shield is lopsided and porous, not a
      // circular fill. It strengthens with authored maximum wind.
      const shieldOffset = 0.055 + depiction.motionAsymmetry * 0.08;
      const shiftedEast = east - Math.sin(depiction.rightOfMotionRad) * shieldOffset;
      const shiftedNorth = north - Math.cos(depiction.rightOfMotionRad) * shieldOffset;
      const along = shiftedEast * Math.sin(depiction.rightOfMotionRad)
        + shiftedNorth * Math.cos(depiction.rightOfMotionRad);
      const across = shiftedEast * Math.cos(depiction.rightOfMotionRad)
        - shiftedNorth * Math.sin(depiction.rightOfMotionRad);
      const shieldRadius = Math.hypot(along / 0.48, across / 0.35);
      const centralEnvelope = 1 - smoothstep(0.46, 1.04, shieldRadius);
      const centralTexture = smoothstep(0.34, 0.76, broad * 0.67 + fine * 0.43);
      const motionBoost = clamp(0.88 + right * depiction.motionAsymmetry * 0.75, 0.58, 1.3);
      reflectivity = Math.max(
        reflectivity,
        centralEnvelope * centralTexture * motionBoost * (0.42 + depiction.intensity * 0.42),
      );

      // A broken, high-reflectivity eyewall. Its radius follows the model RMW
      // ratio, and the right-of-motion side is intentionally stronger.
      const wallWidth = 0.017 + eyewall * 0.34;
      const wallDistance = (radius - eyewall) / wallWidth;
      const wallBreaks = smoothstep(0.25, 0.67, breakup + 0.18 * fine);
      const wallValue = Math.exp(-wallDistance * wallDistance)
        * wallBreaks
        * clamp(0.82 + right * depiction.motionAsymmetry, 0.55, 1.24)
        * (0.78 + depiction.intensity * 0.24);
      reflectivity = Math.max(reflectivity, wallValue);

      // Small convective maxima inside otherwise green/yellow bands.
      if (fine > 0.7 && band > 0.35 && radius < 0.78) {
        reflectivity = Math.max(
          reflectivity,
          (fine - 0.63) * 1.65 + depiction.intensity * (1 - radius) * 0.24,
        );
      }

      if (eye > 0) {
        const eyeNoise = 0.9 + (fine - 0.5) * 0.16 + 0.045 * Math.sin(sampleAngle * 5 + depiction.phaseRad);
        if (radius < eye * eyeNoise) reflectivity = 0;
      }

      reflectivity = clamp(reflectivity, 0, 1);
      if (reflectivity < 0.115) continue;
      const bandIndex = reflectivityBand(reflectivity);
      const colour = colours[bandIndex];
      const alphaByBand = [0.5, 0.62, 0.72, 0.78, 0.86, 0.9];
      const alpha = Math.round(255 * alphaByBand[bandIndex]);
      const offset = (y * image.width + x) * 4;
      image.data[offset] = colour[0];
      image.data[offset + 1] = colour[1];
      image.data[offset + 2] = colour[2];
      image.data[offset + 3] = alpha;
    }
  }
}

function reflectivityBand(value: number) {
  if (value >= 0.93) return 5;
  if (value >= 0.76) return 4;
  if (value >= 0.59) return 3;
  if (value >= 0.43) return 2;
  if (value >= 0.26) return 1;
  return 0;
}

function fbm(x: number, y: number, seed: number, octaves: number) {
  let value = 0;
  let amplitude = 0.56;
  let total = 0;
  for (let octave = 0; octave < octaves; octave += 1) {
    value += valueNoise(x, y, seed + octave * 1013) * amplitude;
    total += amplitude;
    x = x * 2.03 + 19.1;
    y = y * 2.01 - 7.7;
    amplitude *= 0.5;
  }
  return total > 0 ? value / total : 0;
}

function valueNoise(x: number, y: number, seed: number) {
  const x0 = Math.floor(x);
  const y0 = Math.floor(y);
  const tx = smoothFraction(x - x0);
  const ty = smoothFraction(y - y0);
  const a = hash2(x0, y0, seed);
  const b = hash2(x0 + 1, y0, seed);
  const c = hash2(x0, y0 + 1, seed);
  const d = hash2(x0 + 1, y0 + 1, seed);
  return mix(mix(a, b, tx), mix(c, d, tx), ty);
}

function hash2(x: number, y: number, seed: number) {
  let hash = Math.imul(x, 0x27d4eb2d) ^ Math.imul(y, 0x165667b1) ^ seed;
  hash = Math.imul(hash ^ (hash >>> 15), 0x85ebca6b);
  hash ^= hash >>> 13;
  return (hash >>> 0) / 0x100000000;
}

function smoothFraction(value: number) {
  return value * value * (3 - 2 * value);
}

function smoothstep(low: number, high: number, value: number) {
  if (low === high) return value < low ? 0 : 1;
  const amount = clamp((value - low) / (high - low), 0, 1);
  return amount * amount * (3 - 2 * amount);
}

function mix(a: number, b: number, amount: number) {
  return a + (b - a) * amount;
}

function parseHex(colour: string): [number, number, number] {
  const value = colour.startsWith("#") ? colour.slice(1) : "2fa463";
  const full = value.length === 3 ? value.split("").map((part) => part + part).join("") : value;
  return [
    Number.parseInt(full.slice(0, 2), 16),
    Number.parseInt(full.slice(2, 4), 16),
    Number.parseInt(full.slice(4, 6), 16),
  ];
}

function weatherCoordinates(
  centre: LngLat,
  radiusNm: number,
): [LngLat, LngLat, LngLat, LngLat] {
  const cornerDistance = radiusNm * Math.SQRT2;
  return [
    destination(centre, 315, cornerDistance),
    destination(centre, 45, cornerDistance),
    destination(centre, 135, cornerDistance),
    destination(centre, 225, cornerDistance),
  ];
}

function normalAngle(value: number) {
  return ((value % TAU) + TAU) % TAU;
}

function roundPhase(value: number) {
  return Math.round(value * 1000) / 1000;
}

function clamp(value: number, low: number, high: number) {
  return Math.max(low, Math.min(high, value));
}
