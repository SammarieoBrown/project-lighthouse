/* Register I — the synoptic panel.
 *
 * Drawn as SVG from our own committed geometry rather than over a tile
 * basemap. The console has to work with no network in a building that has just
 * lost power, and a basemap would also put half the pixels outside our
 * control — colours we did not choose, labels unreadable at distance, and a
 * visual language that is none of our three registers.
 *
 * Two layers share this canvas: hazard (where the wind reaches) and impact
 * (which homes it hits). They get separate colour scales — cool for hazard,
 * warm for impact — so the eye can hold both at once. See the hazard ramp note
 * in tokens.css for why that is a stated exception rather than drift.
 *
 * Homes are aggregated to district, not drawn one by one. Two thousand
 * individual dots over an island this size is a texture, not a map: you cannot
 * count them, you cannot tell which places are worst, and they bury everything
 * underneath. One mark per district, area proportional to homes, is the same
 * information at a density a person can actually read.
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
  return { x0, y0: Math.min(...ys), x1, y1: Math.max(...ys), cx: (x0 + x1) / 2 };
}

export type District = {
  parish: string;
  district: string;
  n: number;
  lon: number;
  lat: number;
  destroyed: number;
  major: number;
  minor: number;
  none: number;
};

export type Household = {
  lon: number;
  lat: number;
  band: string;
  parish: string;
  community: string;
  roof: string;
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
  districts: District[];
  /** Only rendered past the zoom switch; absent in the SVG fallback. */
  households?: Household[];
};

/* A district is coloured by the worst band covering a quarter of its homes.
 *
 * Only two hues appear, and that is the point. Minor damage is not what an
 * operations room is deciding about in Act 1 — reserving colour for major and
 * destroyed keeps the map answering the question it is actually asked, and
 * leaves the cool end of the palette free for the hazard layer. */
function severityOf(d: District): { fill: string; ring: string } {
  const quarter = d.n * 0.25;
  if (d.destroyed >= quarter) return { fill: "var(--lh-critical)", ring: "var(--lh-critical)" };
  if (d.destroyed + d.major >= quarter) return { fill: "var(--lh-elevated)", ring: "var(--lh-elevated)" };
  return { fill: "none", ring: "var(--lh-quiet)" };
}

/* Area proportional to count, so a district with four times the homes looks
 * four times the size — which is what a reader assumes a circle means. Scaling
 * the radius instead is the oldest lie in data graphics. */
function radiusOf(n: number, max: number): number {
  return 4 + 17 * Math.sqrt(n / max);
}

export function SynopticMap({ snapshot }: { snapshot: Snapshot }) {
  const centre = snapshot.centre ?? snapshot.track?.coordinates?.[0];
  const biggest = Math.max(...snapshot.districts.map((d) => d.n), 1);

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      width="100%"
      height="100%"
      role="img"
      aria-label="Forecast wind field and expected damage by district across Jamaica"
      preserveAspectRatio="xMidYMid meet"
      style={{ display: "block" }}
    >
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

      {/* Jamaica. Every parish now carries a registry, so they are drawn alike. */}
      {snapshot.parishes.map((p) => (
        <path
          key={p.name}
          d={pathOf(ringsOf(p.geometry))}
          fill="var(--lh-ground)"
          fillOpacity="0.6"
          stroke="var(--lh-figure)"
          strokeWidth="0.75"
          strokeOpacity="0.4"
        />
      ))}

      {/* Districts, largest first so a small severe one is never buried. */}
      {[...snapshot.districts]
        .sort((a, b) => b.n - a.n)
        .map((d) => {
          const [x, y] = project([d.lon, d.lat]);
          const { fill, ring } = severityOf(d);
          return (
            <circle
              key={`${d.parish}-${d.district}`}
              cx={x}
              cy={y}
              r={radiusOf(d.n, biggest)}
              fill={fill}
              fillOpacity={fill === "none" ? 0 : 0.7}
              stroke={ring}
              strokeWidth="1.25"
              strokeOpacity="0.85"
              /* aria-label rather than an SVG <title> child. React 19 treats
                 <title> as hoistable document metadata, and the server and
                 client disagree about SVG scope — a hydration mismatch that
                 regenerates the whole tree on load. */
              aria-label={`${d.district}, ${d.parish}: ${d.n} homes, ${d.destroyed} destroyed, ${d.major} major`}
            />
          );
        })}

      {/* Name the parishes the storm is aimed at, outside their outline and
          knocked out of what is behind. A name set over data is unreadable at
          any halo weight, and the data does not move. */}
      {snapshot.parishes
        .filter((p) => p.name === "Westmoreland" || p.name === "Saint Elizabeth")
        .map((p) => {
          const box = boxOf(ringsOf(p.geometry));
          if (!box) return null;
          const above = p.name === "Westmoreland";
          return (
            <text
              key={`label-${p.name}`}
              x={box.cx}
              y={above ? box.y0 - 14 : box.y1 + 26}
              fontSize="14"
              fill="var(--lh-figure)"
              fillOpacity="0.85"
              stroke="var(--lh-ground)"
              strokeWidth="5"
              strokeLinejoin="round"
              paintOrder="stroke"
              fontFamily="var(--lh-font-display)"
              fontWeight="700"
              textAnchor="middle"
              letterSpacing="0.6"
              style={{ textTransform: "uppercase" }}
            >
              {p.name.replace("Saint ", "St ")}
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
