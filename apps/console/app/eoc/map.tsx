/* Register I — the synoptic panel.
 *
 * Drawn as SVG from our own committed geometry rather than over a tile
 * basemap. Two reasons, and neither is convenience: the console has to work
 * with no network in a building that has just lost power, and a basemap would
 * put half the pixels on screen outside our control — colours we did not
 * choose, labels we cannot read at distance, and a visual language that is not
 * any of our three registers.
 *
 * Wind fields are contours, not fills in the meaning hues. They are hazard
 * extent, not severity, and borrowing red for "64 kt reaches here" would spend
 * the one colour that means "a human must act now" on a boundary line.
 */

type Ring = [number, number][];

/* All of Jamaica, plus the storm centre to its south.
 *
 * Framed on the two registry parishes alone, the island read as an unplaceable
 * blob — you could not tell what country you were looking at. The rest of
 * Jamaica is drawn as context precisely because it has no registry: the shape
 * of the coverage gap is information an operator needs, and hiding it would
 * make a two-parish pilot look like national coverage. */
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

/* Bounding box of the largest ring, in screen space.
 *
 * Labels go outside it, not on the centroid. A parish name set over a parish
 * packed with five hundred household dots is unreadable however heavy the halo
 * — and the dots are the data, so the label moves rather than them. */
function boxOf(rings: Ring[]): { x0: number; y0: number; x1: number; y1: number; cx: number } | null {
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

/* Topmost point of the largest ring — where a contour label goes on a chart. */
function labelAnchor(rings: Ring[]): [number, number] | null {
  const largest = rings.reduce<Ring | null>(
    (best, r) => (best === null || r.length > best.length ? r : best),
    null,
  );
  if (!largest) return null;
  const points = largest.map((c) => project(c as [number, number]));
  const inFrame = points.filter(([x, y]) => x > 40 && x < W - 40 && y > 20 && y < H - 20);
  const pool = inFrame.length ? inFrame : points;
  return pool.reduce((best, p) => (p[1] < best[1] ? p : best), pool[0]);
}

function linePath(geometry: { type: string; coordinates: number[][] } | null): string {
  if (!geometry) return "";
  const points = geometry.coordinates.map((c) => project(c as [number, number]));
  return `M ${points.map(([x, y]) => `${x.toFixed(1)} ${y.toFixed(1)}`).join(" L ")}`;
}

/* Household marks. Colour carries the predicted band and nothing else — the
 * same four meanings used everywhere in the product. NONE has no hue, which is
 * why an unaffected household reads as a hairline ring rather than a green dot:
 * absence is the state. */
const BAND_FILL: Record<string, string> = {
  DESTROYED: "var(--lh-critical)",
  MAJOR: "var(--lh-elevated)",
  MINOR: "var(--lh-watch)",
  NONE: "none",
};

export type Household = {
  lon: number;
  lat: number;
  band: string;
  parish: string;
  community: string;
  roof: string;
  vuln: number;
};

export type Snapshot = {
  parishes: { name: string; registry: boolean; geometry: { type: string; coordinates: unknown } }[];
  wind34: { type: string; coordinates: unknown } | null;
  wind50: { type: string; coordinates: unknown } | null;
  wind64: { type: string; coordinates: unknown } | null;
  cone: { type: string; coordinates: unknown } | null;
  track: { type: string; coordinates: number[][] } | null;
  households: Household[];
};

export function SynopticMap({ snapshot }: { snapshot: Snapshot }) {
  const centre = snapshot.track?.coordinates?.[0];

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      width="100%"
      height="100%"
      role="img"
      aria-label="Wind field and household risk across St Elizabeth and Westmoreland"
      preserveAspectRatio="xMidYMid meet"
      style={{ display: "block" }}
    >
      {/* Cone — where the centre might go, which is a different and much less
          useful question than who gets hit. Drawn faintest for that reason. */}
      <path d={pathOf(ringsOf(snapshot.cone))} fill="var(--lh-figure)" fillOpacity="0.04" />

      {/* Wind field contours, weakest outermost. Tone, not hue — these are
          hazard extent, and spending a meaning colour on a boundary line would
          take red away from the one thing that means act now. */}
      {([
        [snapshot.wind34, 0.04, 1, "34 kt"],
        [snapshot.wind50, 0.05, 1.5, "50 kt"],
        [snapshot.wind64, 0.07, 2.25, "64 kt"],
      ] as const).map(([field, opacity, stroke, label], i) => (
        <g key={label}>
          <path
            id={`contour-${i}`}
            d={pathOf(ringsOf(field))}
            fill="var(--lh-figure)"
            fillOpacity={opacity}
            stroke="var(--lh-figure)"
            strokeOpacity="0.45"
            strokeWidth={stroke}
          />
          {/* A contour nobody can read the value of is decoration. Set flat at
              the top of the largest ring — text bent around an arbitrary
              polygon edge is legible from no angle. */}
          {labelAnchor(ringsOf(field)) ? (
            <text
              x={labelAnchor(ringsOf(field))![0]}
              y={labelAnchor(ringsOf(field))![1] + 16}
              fontSize="13"
              fill="var(--lh-figure)"
              fillOpacity="0.8"
              fontFamily="var(--lh-font-data)"
              textAnchor="middle"
              letterSpacing="0.5"
            >
              {label}
            </text>
          ) : null}
        </g>
      ))}

      {/* Forecast track. */}
      <path
        d={linePath(snapshot.track)}
        fill="none"
        stroke="var(--lh-figure)"
        strokeWidth="1.5"
        strokeDasharray="6 4"
        strokeOpacity="0.7"
      />

      {/* Parishes. The two we hold a registry for are drawn solid; the other
          twelve are outline only, because a parish we cannot see into is not
          the same as a parish with nobody at risk. */}
      {snapshot.parishes.map((p) => (
        <path
          key={p.name}
          d={pathOf(ringsOf(p.geometry))}
          fill={p.registry ? "var(--lh-panel)" : "var(--lh-ground)"}
          fillOpacity={p.registry ? 0.95 : 0.55}
          stroke="var(--lh-figure)"
          strokeWidth={p.registry ? 1.25 : 0.75}
          strokeOpacity={p.registry ? 0.9 : 0.3}
        />
      ))}

      {/* Name the parishes we cover. A map that does not say where you are
          looking is a shape, and the first question anybody asks of this screen
          is which parishes these are. */}
      {snapshot.parishes
        .filter((p) => p.registry)
        .map((p) => {
          const box = boxOf(ringsOf(p.geometry));
          if (!box) return null;
          // Westmoreland sits above its neighbour, so its name goes above and
          // St Elizabeth's below. Both clear of the dots.
          const above = p.name === "Westmoreland";
          return (
            <text
              key={`label-${p.name}`}
              x={box.cx}
              y={above ? box.y0 - 12 : box.y1 + 24}
              fontSize="15"
              fill="var(--lh-figure)"
              /* Knocked out of what is behind it. A place name sitting in a
                 field of household dots is unreadable, and the halo is what
                 every printed chart has used for the same reason. */
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

      {/* Households. Unaffected first so the ones that matter draw on top. */}
      {["NONE", "MINOR", "MAJOR", "DESTROYED"].map((band) => (
        <g key={band}>
          {snapshot.households
            .filter((h) => h.band === band)
            .map((h, i) => {
              const [x, y] = project([h.lon, h.lat]);
              return (
                <circle
                  key={i}
                  cx={x}
                  cy={y}
                  r={band === "NONE" ? 2 : 3.25}
                  fill={BAND_FILL[band]}
                  stroke={band === "NONE" ? "var(--lh-quiet)" : "none"}
                  strokeWidth="1"
                />
              );
            })}
        </g>
      ))}

      {/* Storm centre. */}
      {centre ? (
        <g transform={`translate(${project(centre as [number, number]).join(",")})`}>
          <circle r="9" fill="none" stroke="var(--lh-figure)" strokeWidth="1.5" />
          <circle r="2" fill="var(--lh-figure)" />
        </g>
      ) : null}
    </svg>
  );
}
