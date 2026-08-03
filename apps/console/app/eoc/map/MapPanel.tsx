"use client";

import dynamic from "next/dynamic";
import { useCallback, useState } from "react";

import { SynopticMap, type Snapshot } from "../map";
import styles from "./map-panel.module.css";
import { ZOOM_SWITCH } from "./layers";

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

export function MapPanel({ snapshot }: { snapshot: Snapshot }) {
  const [satellite, setSatellite] = useState(false);
  const [zoom, setZoom] = useState(7.4);
  const [failed, setFailed] = useState<string | null>(null);
  const onZoomChange = useCallback((z: number) => setZoom(z), []);
  const onFail = useCallback((reason: string) => setFailed(reason), []);

  const showingHomes = zoom >= ZOOM_SWITCH;

  // Static, correct and readable beats interactive and blank. This is what a
  // machine without WebGL2, or a clone that has not fetched the tiles, gets.
  if (failed) {
    return (
      <div className={styles.wrap}>
        <SynopticMap snapshot={snapshot} />
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
        satellite={satellite}
        onZoomChange={onZoomChange}
        onFail={onFail}
      />

      <div className={styles.controls}>
        {/* What the map is showing right now, in words. A map that silently
            swaps what a mark means at some zoom is a map you cannot trust. */}
        <span className={styles.scaleNote}>
          {showingHomes ? "Individual homes" : "Districts · zoom in for homes"}
        </span>
        <button
          type="button"
          className={styles.toggle}
          aria-pressed={satellite}
          onClick={() => setSatellite((s) => !s)}
        >
          Satellite
        </button>
      </div>

      <noscript>
        <SynopticMap snapshot={snapshot} />
      </noscript>
    </div>
  );
}
