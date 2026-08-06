/* Register I — the synoptic panel.
 *
 * Drawn as SVG from our own committed geometry rather than over a tile
 * basemap. The console has to work with no network in a building that has just
 * lost power, and a basemap would also put half the pixels outside our
 * control — colours we did not choose, labels unreadable at distance, and a
 * visual language that is none of our three registers.
 *
 * Two layers share this canvas: hazard (where the selected forecast says wind
 * may reach) and expected impact (the model output for that same advisory).
 * They get separate colour scales — cool for hazard, warm for impact — so the
 * eye can hold both at once. Impact is aggregated onto the real parish
 * boundaries. The model's synthetic household coordinates are useful for the
 * simulation, but they are not evidence that a home stands at a point on the
 * ground and therefore never become map marks.
 */

type Ring = [number, number][];

/* All of Jamaica, plus the storm centre to its south. Framed on two parishes
 * the island read as an unplaceable blob. */
const VIEW = { minLon: -78.9, maxLon: -75.9, minLat: 16.2, maxLat: 18.9 };
const W = 1000;
const H = (W * (VIEW.maxLat - VIEW.minLat)) / (VIEW.maxLon - VIEW.minLon);

function project([lon, lat]: [number, number]): [number, number] {
  const x = ((lon - VIEW.minLon) / (VIEW.maxLon - VIEW.minLon)) * W;
  const y = H - ((lat - VIEW.minLat) / (VIEW.maxLat - VIEW.minLat)) * H;
  return [x, y];
}

function ringsOf(geometry: { type: string; coordinates: unknown } | null): Ring[] {
  if (!geometry) return [];
  const { type, coordinates } = geometry as { type: string; coordinates: number[][][][] };
  if (type === "Polygon") return coordinates as unknown as Ring[];
  if (type === "MultiPolygon") return (coordinates as number[][][][]).flatMap((p) => p as unknown as Ring[]);
  return [];
}

function pathOf(rings: Ring[]): string {
  return rings
    .map((ring) => {
      const points = ring.map((c) => project(c as [number, number]));
      return `M ${points.map(([x, y]) => `${x.toFixed(1)} ${y.toFixed(1)}`).join(" L ")} Z`;
    })
    .join(" ");
}

function linePath(geometry: { type: string; coordinates: number[][] } | null): string {
  if (!geometry) return "";
  const points = geometry.coordinates.map((c) => project(c as [number, number]));
  return `M ${points.map(([x, y]) => `${x.toFixed(1)} ${y.toFixed(1)}`).join(" L ")}`;
}

function boxOf(rings: Ring[]) {
  const largest = rings.reduce<Ring | null>(
    (best, r) => (best === null || r.length > best.length ? r : best),
    null,
  );
  if (!largest) return null;
  const points = largest.map((c) => project(c as [number, number]));
  const xs = points.map((p) => p[0]);
  const ys = points.map((p) => p[1]);
  const x0 = Math.min(...xs);
  const x1 = Math.max(...xs);
  const y0 = Math.min(...ys);
  const y1 = Math.max(...ys);
  return { x0, y0, x1, y1, cx: (x0 + x1) / 2, cy: (y0 + y1) / 2 };
}

export type District = {
  parish: string;
  district: string;
  n: number;
  lon?: number;
  lat?: number;
  destroyed: number;
  major: number;
  minor: number;
  none: number;
};

export type Household = {
  lon?: number;
  lat?: number;
  band: string;
  parish: string;
  community: string;
  roof?: string;
};

export type Snapshot = {
  parishes: { name: string; registry: boolean; geometry: { type: string; coordinates: unknown } }[];
  wind34: { type: string; coordinates: unknown } | null;
  wind50: { type: string; coordinates: unknown } | null;
  wind64: { type: string; coordinates: unknown } | null;
  cone: { type: string; coordinates: unknown } | null;
  track: { type: string; coordinates: number[][] } | null;
  /** Storm centre for this advisory, `[lon, lat]`. The first track vertex is
   *  the same point in practice, but the position is what the advisory states
   *  and the track is a forecast drawn from it. */
  centre?: [number, number] | null;
  /** Intensity and motion for this advisory, for the modelled flow marks. The
   *  polygons say how far the wind reaches; these say which way it turns, and
   *  nothing here is a second measurement — see map/advisory-wind.ts. */
  motion?: { maxWindKt: number; headingDeg: number; forwardSpeedKt: number } | null;
  districts: District[];
  /** Only rendered past the zoom switch; absent in the SVG fallback. */
  households?: Household[];
};

export type MapFocus = {
  key: string;
  lon: number;
  lat: number;
  parish?: string;
};

export const IMPACT_SHARE_THRESHOLD = 0.25;

export type ImpactBand = "destroyed" | "major" | "below-threshold";

export type ParishImpact = {
  name: string;
  label: string;
  geometry: Snapshot["parishes"][number]["geometry"];
  homes: number;
  destroyed: number;
  major: number;
  minor: number;
  none: number;
  majorPlus: number;
  band: ImpactBand;
};

function parishKey(name: string): string {
  return name.trim().toLowerCase().replace(/^st\.?\s+/, "saint ");
}

function parishLabel(name: string): string {
  return name.replace(/^Saint /, "St ");
}

/** Join selected-advisory model output to mapped parish geometry.
 *
 * District coordinates in the replay belong to a synthetic model registry, so
 * they must not be plotted. Their counts are valid model output, however, and
 * can be summed to the administrative unit whose geometry is real. Both the
 * WebGL map and the SVG fallback call this function so a rendering failure can
 * never change the evidence being shown. */
export function parishImpacts(snapshot: Snapshot): ParishImpact[] {
  const totals = new Map<string, Omit<ParishImpact, "name" | "label" | "geometry" | "majorPlus" | "band">>();

  for (const district of snapshot.districts) {
    const key = parishKey(district.parish);
    const current = totals.get(key) ?? {
      homes: 0,
      destroyed: 0,
      major: 0,
      minor: 0,
      none: 0,
    };
    current.homes += district.n;
    current.destroyed += district.destroyed;
    current.major += district.major;
    current.minor += district.minor;
    current.none += district.none;
    totals.set(key, current);
  }

  return snapshot.parishes.map((parish) => {
    const counts = totals.get(parishKey(parish.name)) ?? {
      homes: 0,
      destroyed: 0,
      major: 0,
      minor: 0,
      none: 0,
    };
    const destroyedShare = counts.homes > 0 ? counts.destroyed / counts.homes : 0;
    const majorPlus = counts.destroyed + counts.major;
    const majorPlusShare = counts.homes > 0 ? majorPlus / counts.homes : 0;
    const band: ImpactBand = destroyedShare >= IMPACT_SHARE_THRESHOLD
      ? "destroyed"
      : majorPlusShare >= IMPACT_SHARE_THRESHOLD
        ? "major"
        : "below-threshold";

    return {
      name: parish.name,
      label: parishLabel(parish.name),
      geometry: parish.geometry,
      ...counts,
      majorPlus,
      band,
    };
  });
}

export function SynopticMap({ snapshot, focus = null }: { snapshot: Snapshot; focus?: MapFocus | null }) {
  const centre = snapshot.centre ?? snapshot.track?.coordinates?.[0];
  const impacts = parishImpacts(snapshot);
  const focusedParish = focus?.parish ? parishKey(focus.parish) : "";
  const focusPoint = focus && Number.isFinite(focus.lon) && Number.isFinite(focus.lat)
    ? project([focus.lon, focus.lat])
    : null;
  const focusWidth = W * 0.48;
  const focusHeight = H * 0.48;
  const viewBox = focusPoint
    ? [
        Math.min(Math.max(focusPoint[0] - focusWidth / 2, 0), W - focusWidth),
        Math.min(Math.max(focusPoint[1] - focusHeight / 2, 0), H - focusHeight),
        focusWidth,
        focusHeight,
      ].map((value) => value.toFixed(1)).join(" ")
    : `0 0 ${W} ${H}`;
  const labelled = new Set(
    [...impacts]
      .filter((impact) => impact.majorPlus > 0)
      .sort((a, b) => b.majorPlus - a.majorPlus)
      .slice(0, 5)
      .map((impact) => impact.name),
  );

  return (
    <svg
      viewBox={viewBox}
      width="100%"
      height="100%"
      role="img"
      aria-label={`Selected forecast wind field and expected model impact by parish across Jamaica${focus?.parish ? `, focused on ${focus.parish}` : ""}`}
      preserveAspectRatio="xMidYMid meet"
      style={{ display: "block" }}
    >
      {/* The island and its expected impact. Each mark is a real parish
          polygon; the fill is derived from counts aggregated for this advisory. */}
      {impacts.map((impact) => (
        <path
          key={impact.name}
          d={pathOf(ringsOf(impact.geometry))}
          fill={impact.band === "destroyed"
            ? "var(--lh-critical)"
            : impact.band === "major"
              ? "var(--lh-elevated)"
              : "var(--lh-ground)"}
          fillOpacity={impact.band === "below-threshold" ? 0.72 : 0.38}
          stroke="none"
          aria-label={`${impact.name}: ${impact.destroyed} expected destroyed, ${impact.major} expected major damage among ${impact.homes} modelled homes`}
        />
      ))}

      {/* Cone — where the centre might go, which is a different and much less
          useful question than who gets hit. Faintest for that reason. */}
      <path d={pathOf(ringsOf(snapshot.cone))} fill="var(--lh-figure)" fillOpacity="0.03" />

      {/* Wind field, weakest outermost. Cool scale, so the warm marks on top
          stay readable through it. */}
      {([
        [snapshot.wind34, "var(--lh-hazard-34)", 0.1, 1, "34 kt"],
        [snapshot.wind50, "var(--lh-hazard-50)", 0.12, 1.25, "50 kt"],
        [snapshot.wind64, "var(--lh-hazard-64)", 0.15, 1.75, "64 kt"],
      ] as const).map(([field, colour, opacity, stroke, label]) => {
        const box = boxOf(ringsOf(field));
        return (
          <g key={label}>
            <path
              d={pathOf(ringsOf(field))}
              fill={colour}
              fillOpacity={opacity}
              stroke={colour}
              strokeOpacity="0.7"
              strokeWidth={stroke}
            />
            {box ? (
              <text
                x={Math.min(Math.max(box.cx, 60), W - 60)}
                y={Math.max(box.y0 + 20, 22)}
                fontSize="13"
                fill={colour}
                fontFamily="var(--lh-font-data)"
                textAnchor="middle"
                letterSpacing="0.5"
              >
                {label}
              </text>
            ) : null}
          </g>
        );
      })}

      <path
        d={linePath(snapshot.track)}
        fill="none"
        stroke="var(--lh-figure)"
        strokeWidth="1.25"
        strokeDasharray="7 5"
        strokeOpacity="0.5"
      />

      {/* Parish boundaries remain legible through both data layers. */}
      {impacts.map((impact) => (
        <path
          key={`outline-${impact.name}`}
          d={pathOf(ringsOf(impact.geometry))}
          fill="none"
          stroke="var(--lh-figure)"
          strokeWidth={parishKey(impact.name) === focusedParish ? 2.5 : 0.75}
          strokeOpacity={parishKey(impact.name) === focusedParish ? 0.95 : 0.4}
        />
      ))}

      {/* Label only the five largest major-or-worse model counts. The full
          parish scale is decoded in the map key; fourteen labels would obscure
          the hazard field at this size. */}
      {impacts
        .filter((impact) => labelled.has(impact.name))
        .map((impact) => {
          const box = boxOf(ringsOf(impact.geometry));
          if (!box) return null;
          return (
            <text
              key={`label-${impact.name}`}
              x={box.cx}
              y={box.cy}
              fontSize="12"
              fill="var(--lh-figure)"
              fillOpacity="0.9"
              stroke="var(--lh-ground)"
              strokeWidth="4"
              strokeLinejoin="round"
              paintOrder="stroke"
              fontFamily="var(--lh-font-data)"
              textAnchor="middle"
            >
              <tspan x={box.cx}>{impact.label}</tspan>
              <tspan x={box.cx} dy="14">{impact.majorPlus} major+</tspan>
            </text>
          );
        })}

      {centre ? (
        <g transform={`translate(${project(centre as [number, number]).join(",")})`}>
          <circle r="10" fill="none" stroke="var(--lh-figure)" strokeWidth="1.25" strokeOpacity="0.8" />
          <circle r="2.5" fill="var(--lh-figure)" />
        </g>
      ) : null}
    </svg>
  );
}
