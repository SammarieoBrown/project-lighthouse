"use client";

import dynamic from "next/dynamic";
import { useCallback, useRef, useState } from "react";
import type { KeyboardEvent } from "react";

import { SynopticMap, type MapFocus, type Snapshot } from "../map";
import styles from "./map-panel.module.css";
import {
  STRUCTURE_FOOTPRINT_ZOOM,
  STRUCTURES_MIN_ZOOM,
  type BaseView,
} from "./layers";

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
  ["map", "Forecast + impact"],
  ["satellite", "Reference imagery"],
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
  focus = null,
}: {
  /** The selected advisory, or null when there is no replay to read. */
  snapshot: Snapshot | null;
  focus?: MapFocus | null;
}) {
  const [base, setBase] = useState<BaseView>("map");
  const [zoom, setZoom] = useState(7.4);
  const [failed, setFailed] = useState<string | null>(null);
  const [structures, setStructures] = useState<{
    status: "idle" | "loading" | "ready" | "unavailable";
    reason?: string;
  }>({ status: "idle" });
  const [imagery, setImagery] = useState<{
    status: "idle" | "loading" | "ready" | "unavailable";
    reason?: string;
  }>({ status: "idle" });
  const bases = useRef<HTMLDivElement>(null);
  const onZoomChange = useCallback((z: number) => setZoom(z), []);
  const onFail = useCallback((reason: string) => setFailed(reason), []);
  const onStructuresStatus = useCallback((
    status: "idle" | "loading" | "ready" | "unavailable",
    reason?: string,
  ) => setStructures({ status, reason }), []);
  const onImageryStatus = useCallback((
    status: "idle" | "loading" | "ready" | "unavailable",
    reason?: string,
  ) => setImagery({ status, reason }), []);

  const structuresTooFar = base === "structures" && zoom < STRUCTURES_MIN_ZOOM;
  const showingFootprints = base === "structures" && zoom >= STRUCTURE_FOOTPRINT_ZOOM;

  /* What the marks mean right now. The distinction at z14 is evidence, not
   * polish: low zoom is a count-weighted grid distribution; high zoom contains
   * the mapped building footprints. */
  const note = base === "structures"
    ? structures.status === "unavailable"
      ? "Structure inventory unavailable · standard buildings remain"
      : structures.status === "loading"
        ? "Loading mapped structure inventory"
        : structuresTooFar
          ? "Mapped structure inventory begins at zoom 9"
          : showingFootprints
            ? "Mapped building footprints · selected forecast above"
            : "Aggregated structure distribution · selected forecast above"
    : base === "satellite"
      ? imagery.status === "ready"
        ? "Reference imagery ready · online context, not post-event damage"
        : imagery.status === "unavailable"
          ? "Reference imagery unavailable · standard basemap remains"
          : "Loading reference imagery · standard basemap remains"
      : "Selected forecast · modelled impact aggregated by parish";

  const spokenStatus = base === "structures"
    ? `${note}. Structures are neutral inventory marks; wind colours come from the selected forecast.`
    : base === "satellite"
      ? imagery.status === "ready"
        ? "Reference imagery ready. This is online context imagery, not post-event damage imagery."
        : imagery.status === "unavailable"
          ? "Reference imagery unavailable. The standard basemap remains visible."
          : "Reference imagery loading. The standard basemap remains visible while it loads."
      : "Forecast and impact selected. Wind is forecast; warm parish fills are modelled expected impact.";

  const onBaseKeyDown = useCallback((event: KeyboardEvent<HTMLDivElement>) => {
    /* Arrow movement starts from the focused radio, not the checked radio.
     * Those normally match, but can legitimately diverge after scripted focus
     * or assistive-technology navigation. Reading `base` here made Right from
     * a focused Forecast wrap back to Forecast whenever Structures was still
     * checked. */
    const focused = event.target instanceof HTMLButtonElement
      ? event.target.dataset.base as BaseView | undefined
      : undefined;
    const current = BASES.findIndex(([value]) => value === focused);
    if (current < 0) return;
    let next = current;
    if (event.key === "ArrowRight" || event.key === "ArrowDown") next = (current + 1) % BASES.length;
    else if (event.key === "ArrowLeft" || event.key === "ArrowUp") next = (current - 1 + BASES.length) % BASES.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = BASES.length - 1;
    else return;
    event.preventDefault();
    const nextBase = BASES[next][0];
    setBase(nextBase);
    bases.current
      ?.querySelector<HTMLButtonElement>(`button[data-base="${nextBase}"]`)
      ?.focus();
  }, []);

  // Static, correct and readable beats interactive and blank. This is what a
  // machine without WebGL2, or a clone that has not fetched the tiles, gets.
  if (failed) {
    return (
      <div className={styles.wrap}>
        <SynopticMap snapshot={snapshot ?? NOTHING} focus={focus} />
        <div className={styles.controls}>
          <span className={styles.scaleNote}>
            Static forecast + impact · {failed}{focus?.parish ? ` · focused on ${focus.parish}` : ""}
          </span>
        </div>
        <MapKey />
        <span className={styles.srOnly} role="status" aria-live="polite">
          Interactive basemap unavailable. Showing the same forecast and parish impact evidence as a static map.
        </span>
      </div>
    );
  }

  return (
    <div className={styles.wrap}>
      <MapView
        snapshot={snapshot}
        base={base}
        focus={focus}
        onZoomChange={onZoomChange}
        onFail={onFail}
        onStructuresStatus={onStructuresStatus}
        onImageryStatus={onImageryStatus}
      />

      <div className={styles.controls}>
        <span className={styles.scaleNote}>{note}</span>
        {/* One control, three states, because "what is under the data" is a
            single question. Radio semantics rather than three toggles: exactly
            one is true at a time and the markup should say so. */}
        <div
          ref={bases}
          className={styles.bases}
          role="radiogroup"
          aria-label="Map view"
          onKeyDown={onBaseKeyDown}
        >
          {BASES.map(([value, label]) => (
            <button
              key={value}
              type="button"
              role="radio"
              className={styles.toggle}
              aria-checked={base === value}
              tabIndex={base === value ? 0 : -1}
              data-base={value}
              onClick={() => setBase(value)}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      <MapKey structures={base === "structures" && structures.status === "ready"} footprints={showingFootprints} />
      <span className={styles.srOnly} role="status" aria-live="polite" aria-atomic="true">
        {spokenStatus}
      </span>

      <noscript>
        <SynopticMap snapshot={snapshot ?? NOTHING} />
      </noscript>
    </div>
  );
}

function MapKey({ structures = false, footprints = false }: { structures?: boolean; footprints?: boolean }) {
  return (
    <aside className={styles.key} aria-label="Map key">
      <span className={styles.keyTitle}>Map key</span>
      <div className={styles.keyGrid}>
        <span className={`${styles.keyMark} ${styles.wind34}`} aria-hidden="true" />
        <span>34 kt forecast</span>
        <span className={`${styles.keyMark} ${styles.wind50}`} aria-hidden="true" />
        <span>50 kt forecast</span>
        <span className={`${styles.keyMark} ${styles.wind64}`} aria-hidden="true" />
        <span>64 kt forecast</span>
        <span className={`${styles.keyMark} ${styles.impactMajor}`} aria-hidden="true" />
        <span>Major+ ≥25% of modelled homes</span>
        <span className={`${styles.keyMark} ${styles.impactDestroyed}`} aria-hidden="true" />
        <span>Destroyed ≥25% of modelled homes</span>
        <span className={`${styles.keyMark} ${styles.track}`} aria-hidden="true" />
        <span>Forecast track</span>
        {structures ? (
          <>
            <span className={`${styles.keyMark} ${styles.structure}`} aria-hidden="true" />
            <span>{footprints ? "Mapped footprint" : "Grouped structures; circle size represents count"}</span>
          </>
        ) : null}
      </div>
      <span className={styles.keyNote}>Parish labels show expected major + destroyed homes.</span>
    </aside>
  );
}
