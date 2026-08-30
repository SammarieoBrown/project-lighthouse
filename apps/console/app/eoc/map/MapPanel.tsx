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
import type { StormEntry } from "../replay";

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

const BASES: BaseView[] = ["map", "satellite", "structures"];
type EvidenceKind = "advisory" | "hindcast" | "live" | "unknown";

function evidenceLabel(kind: EvidenceKind): string {
  if (kind === "hindcast") return "historical hindcast";
  if (kind === "advisory") return "advisory forecast";
  if (kind === "live") return "live NHC storm position";
  return "legacy replay with unavailable evidence provenance";
}

function baseViewLabel(base: BaseView, kind: EvidenceKind): string {
  if (base === "satellite") return "Reference imagery";
  if (base === "structures") return "Structures";
  if (kind === "hindcast") return "Hindcast + impact";
  if (kind === "advisory") return "Forecast + impact";
  if (kind === "live") return "Live positions";
  return "Replay + impact";
}

function windQualifierFor(kind: EvidenceKind, source: StormEntry["sizeSource"]): string {
  if (kind === "advisory") return "advisory forecast";
  if (kind === "live") return "live position · no wind field is published with it";
  if (kind === "unknown") return "replay wind field with unavailable provenance";
  if (source === "modelled") return "modelled hindcast";
  if (source === "measured") return "measured-radii hindcast";
  if (source === "mixed") return "mixed-source hindcast";
  return "hindcast with unavailable extent provenance";
}

function hindcastSourceDescription(source: StormEntry["sizeSource"]): string {
  if (source === "modelled") return "modelled";
  if (source === "measured") return "reconstructed from measured radii";
  if (source === "mixed") return "reconstructed from mixed measured and modelled radii";
  return "shown without source provenance";
}

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
  evidenceKind = "unknown",
  sizeSource = "unavailable",
}: {
  /** The selected replay frame, or null when there is no replay to read. */
  snapshot: Snapshot | null;
  focus?: MapFocus | null;
  evidenceKind?: EvidenceKind;
  sizeSource?: StormEntry["sizeSource"];
}) {
  // The live board is about the basin; everything else is about Jamaica.
  const view = evidenceKind === "live" ? ("basin" as const) : ("island" as const);
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
  const windQualifier = windQualifierFor(evidenceKind, sizeSource);

  /* What the marks mean right now, and only when that is not what the selected
   * view already says. The distinction at z14 is evidence, not polish: low zoom
   * is a count-weighted grid distribution; high zoom contains the mapped
   * building footprints — so those speak. So does every loading, unavailable
   * and zoom-gated state.
   *
   * The default view does not. "Selected advisory forecast · synthetic impact
   * aggregated by parish", sitting directly beneath a selector reading
   * FORECAST + IMPACT and directly above a key decoding both layers, was a
   * caption for a caption. Null renders nothing at all rather than an empty
   * chip, which is the difference between a quiet map and a map with a hole in
   * it. */
  const note: string | null = base === "structures"
    ? structures.status === "unavailable"
      ? "Structure inventory unavailable · standard buildings remain"
      : structures.status === "loading"
        ? "Loading mapped structure inventory"
        : structuresTooFar
          ? "Mapped structure inventory begins at zoom 9"
          : showingFootprints
            ? `Mapped building footprints · selected ${windQualifier} above`
            : `Aggregated structure distribution · selected ${windQualifier} above`
    : base === "satellite"
      ? imagery.status === "ready"
        ? "Reference imagery ready · online context, not post-event damage"
        : imagery.status === "unavailable"
          ? "Reference imagery unavailable · standard basemap remains"
          : "Loading reference imagery · standard basemap remains"
      : evidenceKind === "unknown"
        ? "Selected legacy replay · evidence provenance unavailable"
        : null;

  const spokenStatus = base === "structures"
    ? `${note}. Structures are neutral inventory marks; wind colours come from the selected ${evidenceLabel(evidenceKind)}.`
    : base === "satellite"
      ? imagery.status === "ready"
        ? "Reference imagery ready. This is online context imagery, not post-event damage imagery."
        : imagery.status === "unavailable"
          ? "Reference imagery unavailable. The standard basemap remains visible."
          : "Reference imagery loading. The standard basemap remains visible while it loads."
      : evidenceKind === "hindcast"
        ? `Historical hindcast and impact selected. Wind extent is ${hindcastSourceDescription(sizeSource)}; warm parish fills are synthetic modelled impact.`
        : evidenceKind === "advisory"
          ? "Advisory forecast and impact selected. Wind is forecast; warm parish fills are synthetic modelled impact."
          : "Legacy replay and impact selected. Wind evidence provenance is unavailable; warm parish fills are synthetic modelled impact.";

  const onBaseKeyDown = useCallback((event: KeyboardEvent<HTMLDivElement>) => {
    /* Arrow movement starts from the focused radio, not the checked radio.
     * Those normally match, but can legitimately diverge after scripted focus
     * or assistive-technology navigation. Reading `base` here made Right from
     * a focused Forecast wrap back to Forecast whenever Structures was still
     * checked. */
    const focused = event.target instanceof HTMLButtonElement
      ? event.target.dataset.base as BaseView | undefined
      : undefined;
    const current = BASES.findIndex((value) => value === focused);
    if (current < 0) return;
    let next = current;
    if (event.key === "ArrowRight" || event.key === "ArrowDown") next = (current + 1) % BASES.length;
    else if (event.key === "ArrowLeft" || event.key === "ArrowUp") next = (current - 1 + BASES.length) % BASES.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = BASES.length - 1;
    else return;
    event.preventDefault();
    const nextBase = BASES[next];
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
            Static {evidenceLabel(evidenceKind)} + impact · {failed}{focus?.parish ? ` · focused on ${focus.parish}` : ""}
          </span>
        </div>
        <MapKey evidenceKind={evidenceKind} />
        <span className={styles.srOnly} role="status" aria-live="polite">
          Interactive basemap unavailable. Showing the same {evidenceLabel(evidenceKind)} and parish impact evidence as a static map.
        </span>
      </div>
    );
  }

  return (
    <div className={styles.wrap}>
      <MapView
        snapshot={snapshot}
        base={base}
        view={view}
        ariaLabel={
          evidenceKind === "live"
            ? "Interactive map of live NHC storm positions and outlook areas across the Atlantic basin"
            : `Interactive map of the selected ${evidenceLabel(evidenceKind)} and synthetic modelled impact across Jamaica`
        }
        focus={focus}
        onZoomChange={onZoomChange}
        onFail={onFail}
        onStructuresStatus={onStructuresStatus}
        onImageryStatus={onImageryStatus}
      />

      <div className={styles.controls}>
        {note ? <span className={styles.scaleNote}>{note}</span> : null}
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
          {BASES.map((value) => (
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
              {baseViewLabel(value, evidenceKind)}
            </button>
          ))}
        </div>
      </div>

      <MapKey
        structures={base === "structures" && structures.status === "ready"}
        footprints={showingFootprints}
        evidenceKind={evidenceKind}
      />
      <span className={styles.srOnly} role="status" aria-live="polite" aria-atomic="true">
        {spokenStatus}
      </span>

      <noscript>
        <SynopticMap snapshot={snapshot ?? NOTHING} />
      </noscript>
    </div>
  );
}

function MapKey({
  structures = false,
  footprints = false,
  evidenceKind = "unknown",
}: {
  structures?: boolean;
  footprints?: boolean;
  evidenceKind?: EvidenceKind;
}) {
  /* The qualifier belongs to the layer, not to each threshold in it. Repeated
   * per row it read "34 kt forecast / 50 kt forecast / 64 kt forecast" — three
   * statements of one provenance fact, and a key tall enough to cover a
   * quarter of the coastline saying them. Grouped, each row carries only what
   * distinguishes it from its neighbours. */
  const wind = evidenceKind === "hindcast"
    ? "Wind extent · hindcast"
    : evidenceKind === "advisory"
      ? "Wind extent · forecast"
      : "Wind extent · provenance unavailable";

  /* Open by default, because a key nobody opens is a map nobody can read — but
   * it sits on top of the coastline it decodes, and an operator who has read it
   * once wants the water back. Held in state rather than left to the `open`
   * attribute: React reconciles that attribute, and a native toggle it did not
   * initiate is exactly the divergence that snaps back on the next replay
   * frame. */
  const [keyOpen, setKeyOpen] = useState(true);

  /* The live board draws one thing: the storm centre. A key listing wind
   * bands and parish fills above an empty layer would decode marks that are
   * not there, so live gets a one-line key. After the hook, so the key keeps
   * an identical hook order whichever evidence kind is selected. */
  if (evidenceKind === "live") {
    return (
      <details
        className={styles.key}
        open={keyOpen}
        onToggle={(event) => setKeyOpen(event.currentTarget.open)}
      >
        <summary className={styles.keyTitle}>Map key</summary>
        <div className={styles.keyBody}>
          <span className={styles.keyGroup}>Live · NHC products</span>
          <div className={styles.keyGrid}>
            <span className={styles.centreGlyph} aria-hidden="true">◌</span>
            <span>Storm centre · name and sustained wind</span>
            <span className={`${styles.keyMark} ${styles.outlookArea}`} aria-hidden="true" />
            <span>Formation potential area · dashed · labelled with NHC&apos;s 7-day chance</span>
            <span className={`${styles.keyMark} ${styles.outlookMove}`} aria-hidden="true" />
            <span>Disturbance movement · NHC graphical outlook</span>
          </div>
          <span className={styles.keyNote}>
            Wind extents, track and modelled impact appear when an advisory is
            ingested.
          </span>
        </div>
      </details>
    );
  }

  return (
    <details
      className={styles.key}
      open={keyOpen}
      onToggle={(event) => setKeyOpen(event.currentTarget.open)}
    >
      <summary className={styles.keyTitle}>Map key</summary>

      <div className={styles.keyBody}>
        <span className={styles.keyGroup}>{wind}</span>
        <div className={styles.keyGrid}>
          <span className={`${styles.keyMark} ${styles.wind34}`} aria-hidden="true" />
          <span>34 kt</span>
          <span className={`${styles.keyMark} ${styles.wind50}`} aria-hidden="true" />
          <span>50 kt</span>
          <span className={`${styles.keyMark} ${styles.wind64}`} aria-hidden="true" />
          <span>64 kt</span>
          <span className={`${styles.keyMark} ${styles.track}`} aria-hidden="true" />
          <span>
            {evidenceKind === "hindcast"
              ? "Historical best track"
              : evidenceKind === "advisory"
                ? "Forecast track"
                : "Replay track"}
          </span>
          {/* Named as modelled on the same line as the mark, because it is the
              one thing in this key that is not published. The bands above it
              are the advisory's own radii; this is a reading of them. */}
          <span className={`${styles.keyMark} ${styles.flow}`} aria-hidden="true" />
          <span>Circulation · modelled from these radii</span>
        </div>

        <span className={styles.keyGroup}>Parish fill · ≥25% of modelled homes</span>
        <div className={styles.keyGrid}>
          <span className={`${styles.keyMark} ${styles.impactMajor}`} aria-hidden="true" />
          <span>Major or worse</span>
          <span className={`${styles.keyMark} ${styles.impactDestroyed}`} aria-hidden="true" />
          <span>Destroyed</span>
        </div>

        {structures ? (
          <>
            <span className={styles.keyGroup}>Mapped inventory</span>
            <div className={styles.keyGrid}>
              <span className={`${styles.keyMark} ${styles.structure}`} aria-hidden="true" />
              <span>{footprints ? "Building footprint" : "Grouped structures; circle size is count"}</span>
            </div>
          </>
        ) : null}

        <span className={styles.keyNote}>Parish labels show expected major + destroyed homes.</span>
      </div>
    </details>
  );
}
