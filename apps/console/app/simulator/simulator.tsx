"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  LIBRARY_URL,
  REPLAY_URL,
  nf,
  type Library,
  type Replay,
  type StormEntry,
  validateLibrary,
  validateReplay,
  validateReplayForEntry,
} from "../eoc/replay";
import styles from "./simulator.module.css";
import {
  calculateImpact,
  clampScenario,
  defaultScenario,
  distanceNm,
  simulationFrames,
  type AuthoredScenario,
  type CommunityImpact,
  type ImpactSummary,
  type LngLat,
} from "./model";
import {
  scenarioFromArchiveTrack,
  validateStormCatalogue,
  validateTrackLibrary,
  type CatalogueEntry,
  type StormCatalogue,
  type TrackLibrary,
} from "./historical-catalogue";
import {
  nearestImagery,
  validateImageryManifest,
  type ImageryManifest,
} from "./imagery";

const SimulatorMap = dynamic(() => import("./SimulatorMap"), {
  ssr: false,
  loading: () => <div className={styles.mapLoading}>Loading simulation map…</div>,
});

type InventoryState =
  | { status: "loading" }
  | { status: "ready"; replay: Replay }
  | { status: "error"; reason: string };

type HistoricalSource = {
  id: string;
  name: string;
  kind: "hindcast" | "advisory" | "archive";
  sizeSource: "measured" | "modelled" | "mixed" | "unavailable";
} | null;

type SourceOption = CatalogueEntry & { replayReady: boolean };

const EMPTY_IMPACT: ImpactSummary = {
  assessedStructures: 0,
  unavailableStructures: 0,
  exposed34: 0,
  exposed50: 0,
  exposed64: 0,
  destroyed: 0,
  major: 0,
  minor: 0,
  none: 0,
  communities: [],
};

export function StormSimulator() {
  const [scenario, setScenario] = useState<AuthoredScenario>(() => defaultScenario());
  const [inventory, setInventory] = useState<InventoryState>({ status: "loading" });
  const [library, setLibrary] = useState<Library | null>(null);
  const [catalogue, setCatalogue] = useState<StormCatalogue | null>(null);
  const [trackLibrary, setTrackLibrary] = useState<TrackLibrary | null>(null);
  const [libraryReason, setLibraryReason] = useState<string | null>(null);
  const [catalogueSearch, setCatalogueSearch] = useState("");
  const [historical, setHistorical] = useState<HistoricalSource>(null);
  const [loadingHistorical, setLoadingHistorical] = useState(false);
  const [frameIndex, setFrameIndex] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [drawing, setDrawing] = useState(false);
  const [reducedMotion, setReducedMotion] = useState(false);
  const [mapFailure, setMapFailure] = useState<string | null>(null);
  const [particleState, setParticleState] = useState<{ status: "ready" | "unavailable"; reason?: string }>(
    { status: "ready" },
  );
  const [imageryManifest, setImageryManifest] = useState<ImageryManifest | null>(null);
  const [imageryStatus, setImageryStatus] = useState<"idle" | "loading" | "ready" | "unavailable">("idle");
  const [saveState, setSaveState] = useState<"saved" | "unsaved">("saved");
  const playback = useRef<ReturnType<typeof setInterval> | null>(null);

  const frames = useMemo(() => simulationFrames(scenario), [scenario]);
  const frame = frames[Math.min(frameIndex, Math.max(0, frames.length - 1))] ?? null;
  const impact = useMemo(() => {
    if (inventory.status !== "ready" || !frame) return EMPTY_IMPACT;
    return calculateImpact(
      inventory.replay.districts,
      inventory.replay.households,
      frame.centre,
      frame.headingDeg,
      scenario,
    );
  }, [inventory, frame, scenario]);
  const parishImpact = useMemo(() => aggregateParishes(impact.communities), [impact.communities]);
  const eventAt = frame
    ? new Date(Date.parse(scenario.startAt) + frame.elapsedHours * 3_600_000).toISOString()
    : scenario.startAt;

  const imagery = useMemo(() => {
    if (!historical || !imageryManifest) return null;
    return nearestImagery(imageryManifest, historical.id, eventAt);
  }, [eventAt, historical, imageryManifest]);

  useEffect(() => {
    const controller = new AbortController();
    fetch(LIBRARY_URL, { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error(`Storm replay library returned ${response.status}`);
        return validateLibrary(await response.json());
      })
      .then(async (stormLibrary) => {
        if (controller.signal.aborted) return;
        setLibrary(stormLibrary);
        const selected = stormLibrary.storms.find(
          (entry) => entry.id.toLowerCase() === stormLibrary.default?.toLowerCase(),
        ) ?? stormLibrary.storms[0];
        const url = selected ? `/replay/${selected.file}` : REPLAY_URL;
        const response = await fetch(url, { signal: controller.signal });
        if (!response.ok) throw new Error(`Building inventory returned ${response.status}`);
        const replay = validateReplay(await response.json());
        return selected ? validateReplayForEntry(replay, selected) : replay;
      })
      .then((replay) => {
        if (replay && !controller.signal.aborted) setInventory({ status: "ready", replay });
      })
      .catch(async (error) => {
        if (controller.signal.aborted) return;
        const reason = error instanceof Error ? error.message : "storm replay library is unavailable";
        setLibraryReason(reason);
        try {
          const response = await fetch(REPLAY_URL, { signal: controller.signal });
          if (!response.ok) throw new Error(`Legacy building inventory returned ${response.status}`);
          const replay = validateReplay(await response.json());
          if (!controller.signal.aborted) setInventory({ status: "ready", replay });
        } catch (fallbackError) {
          if (controller.signal.aborted) return;
          setInventory({
            status: "error",
            reason: fallbackError instanceof Error ? fallbackError.message : "mapped inventory is unavailable",
          });
        }
      });

    fetch("/replay/catalogue.json", { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error(`Atlantic storm catalogue returned ${response.status}`);
        return validateStormCatalogue(await response.json());
      })
      .then((stormCatalogue) => {
        if (!controller.signal.aborted) setCatalogue(stormCatalogue);
      })
      .catch((error) => {
        if (!controller.signal.aborted) {
          setLibraryReason(error instanceof Error ? error.message : "Atlantic storm catalogue is unavailable");
        }
      });

    fetch("/storm-imagery/index.json", { signal: controller.signal })
      .then(async (response) => {
        if (response.status === 404) return null;
        if (!response.ok) throw new Error(`GOES manifest returned ${response.status}`);
        return validateImageryManifest(await response.json());
      })
      .then((manifest) => {
        if (!controller.signal.aborted) setImageryManifest(manifest);
      })
      .catch(() => {
        if (!controller.signal.aborted) setImageryStatus("unavailable");
      });

    const stored = window.localStorage.getItem("lighthouse:storm-scenario:v1");
    if (stored) {
      try {
        setScenario(clampScenario(JSON.parse(stored) as AuthoredScenario));
      } catch {
        window.localStorage.removeItem("lighthouse:storm-scenario:v1");
      }
    }
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const updateMotion = () => setReducedMotion(media.matches);
    updateMotion();
    media.addEventListener("change", updateMotion);
    return () => {
      controller.abort();
      media.removeEventListener("change", updateMotion);
    };
  }, []);

  useEffect(() => {
    if (!playing || frames.length < 2) return;
    playback.current = setInterval(() => {
      setFrameIndex((current) => {
        if (current >= frames.length - 1) {
          setPlaying(false);
          return current;
        }
        return current + 1;
      });
    }, reducedMotion ? 1000 : 320);
    return () => {
      if (playback.current) clearInterval(playback.current);
      playback.current = null;
    };
  }, [playing, frames.length, reducedMotion]);

  const updateScenario = useCallback((patch: Partial<AuthoredScenario>) => {
    setScenario((current) => clampScenario({ ...current, ...patch }));
    setFrameIndex(0);
    setPlaying(false);
    setSaveState("unsaved");
  }, []);

  const loadHistorical = useCallback(async (id: string) => {
    if (!id) {
      setHistorical(null);
      updateScenario(defaultScenario());
      return;
    }
    const replayEntry = library?.storms.find((storm) => storm.id.toLowerCase() === id.toLowerCase());
    const catalogueEntry = catalogue?.storms.find((storm) => storm.id === id.toLowerCase());
    if (!replayEntry && !catalogueEntry) return;
    setLoadingHistorical(true);
    setPlaying(false);
    setLibraryReason(null);
    try {
      if (replayEntry) {
        const response = await fetch(`/replay/${replayEntry.file}`);
        if (!response.ok) throw new Error(`${replayEntry.name} replay returned ${response.status}`);
        const replay = validateReplayForEntry(validateReplay(await response.json()), replayEntry);
        const replayFrames = relevantReplayFrames(replay);
        const track = historicalTrack(replayFrames);
        if (track.length < 2) throw new Error(`${replayEntry.name} has no usable track geometry`);
        const maxWindKt = Math.max(...replayFrames.map((item) => item.position.max_wind_kt ?? 0));
        setScenario(clampScenario({
          name: `${replayEntry.name} edited scenario`,
          track,
          maxWindKt: Math.max(34, maxWindKt),
          radius34Nm: estimateRadius34(replayFrames),
          forwardSpeedKt: estimateTrackSpeed(replayFrames),
          startAt: replayFrames[0].at,
        }));
        setHistorical({
          id: replayEntry.id,
          name: replayEntry.name,
          kind: replayEntry.kind,
          sizeSource: replayEntry.sizeSource,
        });
      } else if (catalogueEntry) {
        let availableTracks = trackLibrary;
        if (!availableTracks) {
          const response = await fetch("/replay/catalogue-tracks.json");
          if (!response.ok) throw new Error(`Atlantic source tracks returned ${response.status}`);
          availableTracks = validateTrackLibrary(await response.json());
          setTrackLibrary(availableTracks);
        }
        const sourceTrack = availableTracks.storms.find((storm) => storm.id === catalogueEntry.id);
        if (!sourceTrack) throw new Error(`${catalogueEntry.label} is missing from the source-track library`);
        setScenario(scenarioFromArchiveTrack(sourceTrack));
        setHistorical({
          id: sourceTrack.id,
          name: sourceTrack.label,
          kind: "archive",
          sizeSource: sourceTrack.provenance,
        });
      }
      setFrameIndex(0);
      setSaveState("unsaved");
    } catch (error) {
      const label = replayEntry?.name ?? catalogueEntry?.label ?? id;
      setLibraryReason(error instanceof Error ? error.message : `Could not load ${label}`);
    } finally {
      setLoadingHistorical(false);
    }
  }, [catalogue, library, trackLibrary, updateScenario]);

  const save = useCallback(() => {
    window.localStorage.setItem("lighthouse:storm-scenario:v1", JSON.stringify(scenario));
    setSaveState("saved");
  }, [historical, scenario]);

  const exportScenario = useCallback(() => {
    const body = JSON.stringify({
      schema: "lighthouse.authored-storm.v1",
      generated_at: new Date().toISOString(),
      evidence: {
        kind: "synthetic",
        wind: "parametric Holland preview",
        impact: "community centroid and sampled roof mix",
        terrain: "not modelled",
        ...(historical ? {
          derived_from: {
            id: historical.id,
            name: historical.name,
            kind: historical.kind,
            size_source: historical.sizeSource,
          },
        } : {}),
      },
      scenario,
    }, null, 2);
    const href = URL.createObjectURL(new Blob([body], { type: "application/json" }));
    const anchor = document.createElement("a");
    anchor.href = href;
    anchor.download = `${slug(scenario.name)}.storm.json`;
    anchor.click();
    URL.revokeObjectURL(href);
  }, [scenario]);

  const canPlay = scenario.track.length >= 2 && frames.length >= 2;
  const inventoryCount = inventory.status === "ready"
    ? inventory.replay.districts.reduce((sum, district) => sum + (district.structures ?? 0), 0)
    : 0;
  const sourceOptions = useMemo((): SourceOption[] => {
    const query = catalogueSearch.trim().toLowerCase();
    const replayById = new Map(
      (library?.storms ?? []).map((entry) => [entry.id.toLowerCase(), entry] as const),
    );
    const catalogueIds = new Set(catalogue?.storms.map((entry) => entry.id) ?? []);
    const replayOnly: SourceOption[] = (library?.storms ?? [])
      .filter((entry) => !catalogueIds.has(entry.id.toLowerCase()))
      .map((entry): SourceOption => ({
        id: entry.id.toLowerCase(),
        label: entry.name,
        name: entry.name,
        year: Number(entry.from.slice(0, 4)),
        closestKm: 0,
        peakWindKt: 0,
        points: entry.advisories,
        provenance: entry.sizeSource,
        replayReady: true,
      }));
    const archive: SourceOption[] = (catalogue?.storms ?? []).map((entry) => {
      const replay = replayById.get(entry.id);
      return replay
        ? {
            ...entry,
            label: replay.name,
            name: replay.name,
            points: replay.advisories,
            provenance: replay.sizeSource,
            replayReady: true,
          }
        : { ...entry, replayReady: false };
    });
    return [...replayOnly, ...archive].filter((entry) => {
      if (historical?.id.toLowerCase() === entry.id) return true;
      return !query || [entry.label, entry.id, entry.provenance]
        .some((value) => value.toLowerCase().includes(query));
    });
  }, [catalogue, catalogueSearch, historical?.id, library]);
  const sourceCount = useMemo(
    () => new Set([
      ...(catalogue?.storms.map((entry) => entry.id) ?? []),
      ...(library?.storms.map((entry) => entry.id.toLowerCase()) ?? []),
    ]).size,
    [catalogue, library],
  );
  const sourceLabel = historical
    ? `${historical.kind === "advisory" ? "Advisory replay" : historical.kind === "hindcast" ? "Hindcast replay" : "Atlantic best track"} starting point · ${historical.sizeSource} source extent · current wind and impact are synthesised`
    : "Authored scenario · synthesised wind and impact · terrain excluded";

  return (
    <main className={styles.page} data-theme="dark">
      <header className={styles.header}>
        <div>
          <span className={styles.kicker}>Lighthouse planning instrument</span>
          <h1>Storm simulator</h1>
          <p>{sourceLabel}</p>
        </div>
        <nav className={styles.nav} aria-label="Simulator navigation">
          <Link href="/eoc">Operational replay</Link>
          <span data-numeric>{saveState === "saved" ? "Scenario saved locally" : "Unsaved changes"}</span>
        </nav>
      </header>

      <section className={styles.toolbar} aria-label="Scenario source and editing">
        <label className={styles.catalogueSearch}>
          <span>Find Atlantic storm</span>
          <input
            type="search"
            value={catalogueSearch}
            placeholder="Name, year or storm ID"
            onChange={(event) => setCatalogueSearch(event.target.value)}
          />
        </label>
        <label>
          <span>Starting point · {sourceCount} available</span>
          <select
            value={historical?.id ?? ""}
            disabled={loadingHistorical || (!catalogue && !library)}
            onChange={(event) => void loadHistorical(event.target.value)}
          >
            <option value="">{loadingHistorical ? "Loading historical source…" : "New authored storm"}</option>
            {sourceOptions.map((entry) => (
              <option key={entry.id} value={entry.id}>
                {entry.label} · {entry.provenance}{entry.peakWindKt ? ` · ${entry.peakWindKt} kt` : ""}{entry.points === 1 ? " · single source fix" : ""}{entry.replayReady ? " · full replay" : ""}
              </option>
            ))}
            {sourceOptions.length === 0 ? <option disabled>No matching storms</option> : null}
          </select>
        </label>
        <button
          type="button"
          aria-pressed={drawing}
          className={drawing ? styles.activeButton : undefined}
          onClick={() => setDrawing((value) => !value)}
        >
          {drawing ? "Drawing track" : "Edit track"}
        </button>
        <button
          type="button"
          disabled={scenario.track.length === 0}
          onClick={() => {
            const track = scenario.track.slice(0, -1);
            if (track.length === 0) setHistorical(null);
            updateScenario({ track });
          }}
        >
          Undo point
        </button>
        <button
          type="button"
          onClick={() => {
            setHistorical(null);
            updateScenario({ track: [] });
          }}
        >
          Clear track
        </button>
        <button
          type="button"
          onClick={() => {
            setHistorical(null);
            updateScenario(defaultScenario());
          }}
        >
          Reset scenario
        </button>
      </section>

      <div className={styles.workspace}>
        <aside className={styles.controls} aria-label="Storm controls">
          <label className={styles.nameField}>
            <span>Scenario name</span>
            <input
              value={scenario.name}
              maxLength={80}
              onChange={(event) => updateScenario({ name: event.target.value })}
            />
          </label>
          <Control
            label="Maximum sustained wind"
            value={scenario.maxWindKt}
            min={34}
            max={180}
            step={1}
            unit="kt"
            onChange={(value) => updateScenario({ maxWindKt: value })}
          />
          <Control
            label="34 kt wind extent"
            value={scenario.radius34Nm}
            min={25}
            max={320}
            step={5}
            unit="nm"
            onChange={(value) => updateScenario({ radius34Nm: value })}
          />
          <Control
            label="Forward speed"
            value={scenario.forwardSpeedKt}
            min={2}
            max={40}
            step={1}
            unit="kt"
            onChange={(value) => updateScenario({ forwardSpeedKt: value })}
          />

          <div className={styles.trackFacts}>
            <Fact label="Track points" value={nf.format(scenario.track.length)} />
            <Fact label="Track distance" value={`${nf.format(Math.round(trackDistance(scenario.track)))} nm`} />
            <Fact label="Model duration" value={`${nf.format(Math.round(frames.at(-1)?.elapsedHours ?? 0))} h`} />
            <Fact label="Preview inventory" value={inventoryCount ? nf.format(inventoryCount) : "Unavailable"} />
          </div>

          <div className={styles.actions}>
            <button type="button" onClick={save}>Save locally</button>
            <button type="button" onClick={exportScenario}>Export scenario</button>
          </div>

          <p className={styles.method}>
            Fast preview: parametric surface wind at each mapped community centroid,
            applied to the sampled local roof mix. It does not model terrain, surge,
            rainfall, individual buildings or forecast uncertainty.
          </p>
        </aside>

        <section className={styles.mapPanel} aria-label="Simulation surface">
          {inventory.status === "ready" && !mapFailure ? (
            <SimulatorMap
              scenario={scenario}
              frame={frame}
              parishes={inventory.replay.parishes}
              communities={impact.communities}
              drawing={drawing}
              playing={playing}
              reducedMotion={reducedMotion}
              imageryTemplate={imagery?.tiles}
              onTrackChange={(track) => updateScenario({ track })}
              onFailure={setMapFailure}
              onParticleStatus={(status, reason) => setParticleState({ status, reason })}
              onImageryStatus={setImageryStatus}
            />
          ) : (
            <div className={styles.mapFailure} role="status">
              <strong>Simulation map unavailable</strong>
              <span>{mapFailure ?? (inventory.status === "error" ? inventory.reason : "Loading mapped inventory…")}</span>
            </div>
          )}

          <div className={styles.mapStatus}>
            <span>{drawing ? "Click to add · drag a point to revise" : "Track locked · select Edit track to revise"}</span>
            <span>
              {particleState.status === "ready" && !reducedMotion
                ? "Synthesised particle wind"
                : reducedMotion
                  ? "Particle motion stopped by system preference"
                  : `Particle wind unavailable${particleState.reason ? ` · ${particleState.reason}` : ""}`}
            </span>
            <span>
              {historical
                ? imagery
                  ? `${imagery.source} · observed ${imagery.at.slice(0, 16).replace("T", " ")} UTC · ${imageryStatus}`
                  : "GOES imagery not staged for this time"
                : "Authored scenario · no observed imagery"}
            </span>
          </div>

          <div className={styles.mapKey} aria-label="Simulation map key">
            <span><i className={styles.wind34} />34 kt</span>
            <span><i className={styles.wind50} />50 kt</span>
            <span><i className={styles.wind64} />64 kt</span>
            <span><i className={styles.major} />Major+ ≥25%</span>
            <span><i className={styles.destroyed} />Destroyed ≥25%</span>
          </div>
        </section>

        <aside className={styles.impact} aria-label="Fast impact preview">
          <span className={styles.kicker}>Selected simulation hour</span>
          <time dateTime={eventAt} data-numeric>{eventAt.slice(0, 16).replace("T", " ")} UTC</time>
          <div className={styles.impactTotals}>
            <ImpactNumber label="Destroyed" value={impact.destroyed} kind="critical" />
            <ImpactNumber label="Major" value={impact.major} kind="elevated" />
            <ImpactNumber label="Minor" value={impact.minor} />
            <ImpactNumber label="34 kt exposed" value={impact.exposed34} />
          </div>
          <h2>Highest modelled impact</h2>
          {parishImpact.length > 0 ? (
            <ol className={styles.ranking}>
              {parishImpact.slice(0, 6).map((parish) => (
                <li key={parish.name}>
                  <span>{parish.name}</span>
                  <strong data-numeric>{nf.format(parish.majorPlus)}</strong>
                  <small>major + destroyed</small>
                </li>
              ))}
            </ol>
          ) : (
            <p className={styles.empty}>No parish has modelled major or destroyed damage at this hour.</p>
          )}
          {inventory.status === "error" ? <p className={styles.error}>{inventory.reason}</p> : null}
          {libraryReason ? <p className={styles.error}>{libraryReason}</p> : null}
        </aside>
      </div>

      <section className={styles.timeline} aria-label="Simulation timeline">
        <button
          type="button"
          disabled={!canPlay}
          onClick={() => {
            if (frameIndex >= frames.length - 1) setFrameIndex(0);
            setPlaying((value) => !value);
          }}
        >
          {playing ? "Pause" : frameIndex >= frames.length - 1 ? "Replay" : "Play"}
        </button>
        <label>
          <span>Simulation hour</span>
          <input
            type="range"
            min={0}
            max={Math.max(0, frames.length - 1)}
            value={Math.min(frameIndex, Math.max(0, frames.length - 1))}
            disabled={!canPlay}
            onChange={(event) => {
              setPlaying(false);
              setFrameIndex(Number(event.target.value));
            }}
          />
        </label>
        <output data-numeric>{Math.round(frame?.elapsedHours ?? 0)} h / {Math.round(frames.at(-1)?.elapsedHours ?? 0)} h</output>
        <span>{canPlay ? `${frames.length} frames · 1 h cadence` : "Draw at least two track points"}</span>
      </section>
    </main>
  );
}

function Control({
  label,
  value,
  min,
  max,
  step,
  unit,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  unit: string;
  onChange: (value: number) => void;
}) {
  return (
    <label className={styles.control}>
      <span>{label}</span>
      <output data-numeric>{nf.format(value)} {unit}</output>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
      />
    </label>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return <div><span>{label}</span><strong data-numeric>{value}</strong></div>;
}

function ImpactNumber({
  label,
  value,
  kind = "plain",
}: {
  label: string;
  value: number;
  kind?: "plain" | "critical" | "elevated";
}) {
  return (
    <div data-kind={kind}>
      <strong data-numeric>{nf.format(value)}</strong>
      <span>{label}</span>
    </div>
  );
}

function aggregateParishes(communities: CommunityImpact[]) {
  const values = new Map<string, { name: string; majorPlus: number }>();
  for (const community of communities) {
    const current = values.get(community.parish) ?? { name: community.parish, majorPlus: 0 };
    current.majorPlus += community.major + community.destroyed;
    values.set(community.parish, current);
  }
  return [...values.values()].filter((row) => row.majorPlus > 0).sort((a, b) => b.majorPlus - a.majorPlus);
}

function relevantReplayFrames(replay: Replay): Replay["frames"] {
  const located = replay.frames.filter(
    (frame) => Number.isFinite(frame.position.lon) && Number.isFinite(frame.position.lat),
  );
  if (located.length <= 1) return located;
  const distances = located.map((frame) => distanceNm(
    [frame.position.lon as number, frame.position.lat as number],
    [-77.3, 18.11],
  ));
  const inside = distances.flatMap((distance, index) => distance <= 500 ? [index] : []);
  const closest = distances.reduce((best, distance, index) => distance < best.distance ? { index, distance } : best, {
    index: 0,
    distance: Number.POSITIVE_INFINITY,
  }).index;
  const first = inside[0] ?? closest;
  const last = inside.at(-1) ?? closest;
  const start = Math.max(0, first - 1);
  const end = Math.min(located.length, Math.max(last + 2, start + 2));
  return located.slice(start, end);
}

function historicalTrack(frames: Replay["frames"]): LngLat[] {
  const positions = frames
    .map((frame) => [frame.position.lon, frame.position.lat] as const)
    .filter((point): point is readonly [number, number] => Number.isFinite(point[0]) && Number.isFinite(point[1]))
    .map(([lon, lat]) => [lon, lat] as LngLat);
  if (positions.length <= 18) return positions;
  const step = Math.ceil((positions.length - 1) / 17);
  const selected = positions.filter((_, index) => index % step === 0);
  const last = positions.at(-1) as LngLat;
  if (selected.at(-1)?.[0] !== last[0] || selected.at(-1)?.[1] !== last[1]) selected.push(last);
  return selected;
}

function estimateTrackSpeed(frames: Replay["frames"]) {
  let miles = 0;
  let hours = 0;
  for (let index = 1; index < frames.length; index += 1) {
    const previous = frames[index - 1];
    const current = frames[index];
    if (
      Number.isFinite(previous.position.lon)
      && Number.isFinite(previous.position.lat)
      && Number.isFinite(current.position.lon)
      && Number.isFinite(current.position.lat)
    ) {
      miles += distanceNm(
        [previous.position.lon as number, previous.position.lat as number],
        [current.position.lon as number, current.position.lat as number],
      );
      hours += Math.max(0, (Date.parse(current.at) - Date.parse(previous.at)) / 3_600_000);
    }
  }
  return Math.round(Math.min(40, Math.max(2, hours > 0 ? miles / hours : 12)));
}

function estimateRadius34(frames: Replay["frames"]) {
  const strongest = [...frames]
    .filter((frame) => frame.wind34 && Number.isFinite(frame.position.lon) && Number.isFinite(frame.position.lat))
    .sort((a, b) => (b.position.max_wind_kt ?? 0) - (a.position.max_wind_kt ?? 0))[0];
  if (!strongest?.wind34) return 145;
  const coordinates = flattenCoordinates(strongest.wind34.coordinates);
  const centre: LngLat = [strongest.position.lon as number, strongest.position.lat as number];
  const distances = coordinates.map((point) => distanceNm(centre, point));
  return Math.round(Math.min(320, Math.max(25, percentile(distances, 0.75) || 145)) / 5) * 5;
}

function flattenCoordinates(value: unknown): LngLat[] {
  if (!Array.isArray(value)) return [];
  if (value.length >= 2 && typeof value[0] === "number" && typeof value[1] === "number") {
    return [[value[0], value[1]]];
  }
  return value.flatMap(flattenCoordinates);
}

function percentile(values: number[], share: number) {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.floor((sorted.length - 1) * share))];
}

function trackDistance(track: LngLat[]) {
  return track.slice(1).reduce((sum, point, index) => sum + distanceNm(track[index], point), 0);
}

function slug(value: string) {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "storm-scenario";
}
