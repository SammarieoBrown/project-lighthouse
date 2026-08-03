"use client";

import dynamic from "next/dynamic";
import { useCallback, useState } from "react";

import { SynopticMap, type Snapshot } from "../map";
import styles from "./map-panel.module.css";
import { STRUCTURES_MIN_ZOOM, ZOOM_SWITCH, type BaseView } from "./layers";

/* Client shell for the map.
 *
 * MapLibre touches `window` at import time, so it loads with ssr:false — which
 * Next 15+ only permits inside a client component, hence this file existing at
 * all rather than the page importing MapView directly.
 *
 * The SVG map is kept as the fallback and that is not a formality: MapLibre 6
 * requires WebGL2, and the one machine guaranteed to be unusual is the one the
 * demo runs on. A screen that degrades to a static but correct map beats a
 * screen that degrades to a grey rectangle.
 */

const MapView = dynamic(() => import("./MapView"), {
  ssr: false,
  loading: () => <div className={styles.loading}>Loading map…</div>,
});

const BASES: [BaseView, string][] = [
  ["map", "Map"],
  ["satellite", "Satellite"],
  ["structures", "Structures"],
];

const NOTHING: Snapshot = {
  parishes: [],
  wind34: null,
  wind50: null,
  wind64: null,
  cone: null,
  track: null,
  districts: [],
  households: [],
};

export function MapPanel({
  snapshot,
  maxDistrict,
  advisoryIndex,
}: {
  /** The selected advisory, or null when there is no replay to read. */
  snapshot: Snapshot | null;
  maxDistrict: number;
  advisoryIndex: number;
}) {
  const [base, setBase] = useState<BaseView>("map");
  const [zoom, setZoom] = useState(7.4);
  const [failed, setFailed] = useState<string | null>(null);
  const onZoomChange = useCallback((z: number) => setZoom(z), []);
  const onFail = useCallback((reason: string) => setFailed(reason), []);

  const showingHomes = zoom >= ZOOM_SWITCH;
  const structuresTooFar = base === "structures" && zoom < STRUCTURES_MIN_ZOOM;

  /* What the map is showing, in words, because a map that silently changes what
   * a mark means at some zoom is a map you cannot trust. The structures case is
   * the one that has to be honest about a limit: the footprints are not in the
   * archive below z14, so an empty screen there is missing data and not an
   * absence of buildings. */
  const note = structuresTooFar
    ? "Zoom in for structures"
    : base === "structures"
      ? "Structures · every building"
      : showingHomes
        ? "Individual homes"
        : "Districts · zoom in for homes";

  // Static, correct and readable beats interactive and blank. This is what a
  // machine without WebGL2, or a clone that has not fetched the tiles, gets.
  if (failed) {
    return (
      <div className={styles.wrap}>
        <SynopticMap snapshot={snapshot ?? NOTHING} />
        <div className={styles.controls}>
          <span className={styles.scaleNote}>Static map · {failed}</span>
        </div>
      </div>
    );
  }

  return (
    <div className={styles.wrap}>
      <MapView
        snapshot={snapshot}
        maxDistrict={maxDistrict}
        base={base}
        advisoryIndex={advisoryIndex}
        onZoomChange={onZoomChange}
        onFail={onFail}
      />

      <div className={styles.controls}>
        <span className={styles.scaleNote}>{note}</span>
        {/* One control, three states, because "what is under the data" is a
            single question. Radio semantics rather than three toggles: exactly
            one is true at a time and the markup should say so. */}
        <div className={styles.bases} role="radiogroup" aria-label="Base view">
          {BASES.map(([value, label]) => (
            <button
              key={value}
              type="button"
              role="radio"
              className={styles.toggle}
              aria-checked={base === value}
              onClick={() => setBase(value)}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      <noscript>
        <SynopticMap snapshot={snapshot ?? NOTHING} />
      </noscript>
    </div>
  );
}
