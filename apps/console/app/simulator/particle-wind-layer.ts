import type {
  CustomLayerInterface,
  CustomRenderMethodInput,
  Map as MapLibreMap,
} from "maplibre-gl";

import {
  bearingDeg,
  destination,
  distanceNm,
  type LngLat,
  type WindControl,
  windVectorAt,
} from "./model";

export type ParticleWindState = {
  centre: LngLat;
  headingDeg: number;
  control: WindControl;
  running: boolean;
  reducedMotion: boolean;
};

type Particle = {
  point: LngLat;
  previous: LngLat;
  age: number;
  maxAge: number;
};

const PARTICLES = 1800;
const REAL_SECOND_TO_MODEL_HOURS = 0.022;

/**
 * A deliberately contained MapLibre custom layer. It advances short particle
 * segments through the same client wind field used for impact. The layer owns
 * no status colour and does not imply observation: its caller labels it as
 * synthesised output. A failure disables only this layer, never the map.
 */
export class ParticleWindLayer implements CustomLayerInterface {
  readonly id = "lh-simulated-wind";
  readonly type = "custom" as const;
  readonly renderingMode = "2d" as const;

  private map?: MapLibreMap;
  private program?: WebGLProgram;
  private buffer?: WebGLBuffer;
  private matrix?: WebGLUniformLocation | null;
  private colour?: WebGLUniformLocation | null;
  private particles: Particle[] = [];
  private state: ParticleWindState;
  private lastFrame = 0;
  private disabled = false;
  private failed = false;
  private readonly onFailure?: (reason: string) => void;
  private readonly rgba: [number, number, number, number];
  private seed = 0x45d9f3b;

  constructor(
    initial: ParticleWindState,
    rgba: [number, number, number, number],
    onFailure?: (reason: string) => void,
  ) {
    this.state = initial;
    this.rgba = rgba;
    this.onFailure = onFailure;
  }

  setState(next: ParticleWindState) {
    const moved = distanceNm(this.state.centre, next.centre) > 20;
    const resized = this.state.control.radius34Nm !== next.control.radius34Nm;
    this.state = next;
    if (moved || resized) this.particles = [];
    if (next.running && !next.reducedMotion) this.map?.triggerRepaint();
  }

  onAdd(map: MapLibreMap, gl: WebGL2RenderingContext) {
    this.map = map;
    try {
      const vertex = compileShader(gl, gl.VERTEX_SHADER, `#version 300 es
        uniform mat4 u_matrix;
        in vec2 a_position;
        void main() {
          gl_Position = u_matrix * vec4(a_position, 0.0, 1.0);
        }
      `);
      const fragment = compileShader(gl, gl.FRAGMENT_SHADER, `#version 300 es
        precision mediump float;
        uniform vec4 u_colour;
        out vec4 fragColor;
        void main() {
          fragColor = u_colour;
        }
      `);
      const program = gl.createProgram();
      if (!program) throw new Error("particle shader program could not be created");
      gl.attachShader(program, vertex);
      gl.attachShader(program, fragment);
      gl.bindAttribLocation(program, 0, "a_position");
      gl.linkProgram(program);
      gl.deleteShader(vertex);
      gl.deleteShader(fragment);
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
        throw new Error(gl.getProgramInfoLog(program) || "particle shader did not link");
      }
      const buffer = gl.createBuffer();
      if (!buffer) throw new Error("particle buffer could not be created");
      this.program = program;
      this.buffer = buffer;
      this.matrix = gl.getUniformLocation(program, "u_matrix");
      this.colour = gl.getUniformLocation(program, "u_colour");
      this.particles = this.spawn(PARTICLES);
    } catch (error) {
      this.disable(error);
    }
  }

  render(gl: WebGL2RenderingContext, options: CustomRenderMethodInput) {
    if (this.disabled || !this.program || !this.buffer) return;
    try {
      const now = performance.now();
      const elapsed = this.lastFrame > 0 ? Math.min(0.08, (now - this.lastFrame) / 1000) : 0;
      this.lastFrame = now;
      if (this.particles.length === 0) this.particles = this.spawn(PARTICLES);
      if (this.state.running && !this.state.reducedMotion && elapsed > 0) this.advance(elapsed);

      const vertices = new Float32Array(this.particles.length * 4);
      for (let index = 0; index < this.particles.length; index += 1) {
        const particle = this.particles[index];
        const previous = mercator(particle.previous);
        const point = mercator(particle.point);
        const offset = index * 4;
        vertices[offset] = previous[0];
        vertices[offset + 1] = previous[1];
        vertices[offset + 2] = point[0];
        vertices[offset + 3] = point[1];
      }

      gl.useProgram(this.program);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer);
      gl.bufferData(gl.ARRAY_BUFFER, vertices, gl.DYNAMIC_DRAW);
      gl.enableVertexAttribArray(0);
      gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
      gl.uniformMatrix4fv(this.matrix ?? null, false, options.modelViewProjectionMatrix);
      gl.uniform4fv(this.colour ?? null, this.rgba);
      gl.enable(gl.BLEND);
      gl.blendFuncSeparate(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA, gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      gl.disable(gl.DEPTH_TEST);
      gl.drawArrays(gl.LINES, 0, this.particles.length * 2);
      gl.disableVertexAttribArray(0);
      gl.bindBuffer(gl.ARRAY_BUFFER, null);
      gl.useProgram(null);

      if (this.state.running && !this.state.reducedMotion) this.map?.triggerRepaint();
    } catch (error) {
      this.disable(error);
    }
  }

  onRemove(_map: MapLibreMap, gl: WebGL2RenderingContext) {
    if (this.buffer) gl.deleteBuffer(this.buffer);
    if (this.program) gl.deleteProgram(this.program);
    this.buffer = undefined;
    this.program = undefined;
    this.map = undefined;
    this.particles = [];
  }

  private advance(realSeconds: number) {
    const modelHours = realSeconds * REAL_SECOND_TO_MODEL_HOURS;
    const bound = Math.max(80, this.state.control.radius34Nm * 1.8);
    for (let index = 0; index < this.particles.length; index += 1) {
      const particle = this.particles[index];
      const vector = windVectorAt(
        particle.point,
        this.state.centre,
        this.state.headingDeg,
        this.state.control,
      );
      const distance = vector.speedKt * modelHours;
      const direction = vector.speedKt > 0
        ? normalDegrees(Math.atan2(vector.eastKt, vector.northKt) * 180 / Math.PI)
        : bearingDeg(this.state.centre, particle.point);
      particle.point = destination(particle.point, direction, distance);
      // A physical per-frame displacement is sub-pixel at this map scale.
      // Draw a short direction glyph behind the advected point; its length is
      // proportional to modelled speed, while its motion still comes only from
      // the simulation clock.
      const trailNm = Math.min(7, Math.max(0.8, vector.speedKt / 18));
      particle.previous = destination(particle.point, direction + 180, trailNm);
      particle.age += 1;
      if (
        particle.age >= particle.maxAge
        || distanceNm(this.state.centre, particle.point) > bound
        || vector.speedKt < 8
      ) {
        this.particles[index] = this.spawnOne();
      }
    }
  }

  private spawn(count: number) {
    return Array.from({ length: count }, () => this.spawnOne());
  }

  private spawnOne(): Particle {
    const radius = Math.sqrt(this.random()) * Math.max(90, this.state.control.radius34Nm * 1.55);
    const bearing = this.random() * 360;
    const point = destination(this.state.centre, bearing, radius);
    return {
      point,
      previous: point,
      age: Math.floor(this.random() * 80),
      maxAge: 100 + Math.floor(this.random() * 120),
    };
  }

  private random() {
    this.seed = (1664525 * this.seed + 1013904223) >>> 0;
    return this.seed / 0x100000000;
  }

  private disable(error: unknown) {
    this.disabled = true;
    if (this.failed) return;
    this.failed = true;
    const reason = error instanceof Error ? error.message : "particle wind layer failed";
    this.onFailure?.(reason);
  }
}

function compileShader(gl: WebGL2RenderingContext, type: number, source: string) {
  const shader = gl.createShader(type);
  if (!shader) throw new Error("particle shader could not be created");
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    const reason = gl.getShaderInfoLog(shader) || "particle shader did not compile";
    gl.deleteShader(shader);
    throw new Error(reason);
  }
  return shader;
}

function mercator([lon, lat]: LngLat): LngLat {
  const x = (lon + 180) / 360;
  const clampedLat = Math.max(-85.051129, Math.min(85.051129, lat));
  const y = (1 - Math.log(Math.tan(Math.PI / 4 + clampedLat * Math.PI / 360)) / Math.PI) / 2;
  return [x, y];
}

function normalDegrees(value: number) {
  return ((value % 360) + 360) % 360;
}
