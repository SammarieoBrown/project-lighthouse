# The replay export contract

The console reads one generated file. This document is the agreement between the
thing that writes it (`apps/api`) and the thing that reads it (`apps/console`).
Both sides are built against this page; neither side invents a field.

## Why a file and not an endpoint

The console has to survive a venue network that may disappear. A successful
production visit installs a read-only service worker that warms the console
shell, replay and already-loaded same-origin assets. That makes a subsequent
reload usable offline; it is deliberately not a promise that a cold browser can
open the console without first loading it.

PMTiles byte ranges and the online reference imagery are not service-worker
cached. If the interactive basemap cannot load, the map switches to a committed
SVG rendering of the same selected forecast and parish-level modelled impact.

Live operation against the API comes next, and the shape below is what that
endpoint will return — so the console's reader does not change when it arrives.

## Size, measured

Per advisory only the forecast and model state varies; parish outlines,
district metadata and the synthetic registry are emitted once. The current
41-advisory Melissa replay is about **1.3 MB**, which fits in a browser once and
then scrubs instantly. Splitting static geometry from per-advisory frames is the
reason it stays that size.

## Location

Written to `apps/console/public/replay/replay.json`, fetched by the console at
`/replay/replay.json`. Regenerate with:

```bash
cd apps/api && uv run python -m app.console.export
```

**It is committed.** That reads wrong for a generated file, so here is why: the
console deploys from `apps/console` with `npm run build`, and that build has no
Python, no `uv`, and no `DATABASE_URL`. A gitignored export means production
ships with no replay. Basemap and structure archives have their own checksummed
publication path; replay determinism does not depend on whether those optional
interactive assets are present.

So treat it like a lockfile: derived, deterministic, checked in. The determinism
test below is what makes that safe — a regenerated file is byte-identical, so
drift is detectable rather than silent.

Even so, the console must render when the file is missing: show the shell,
disable the transport, and say why. A fresh clone before the first export is a
normal state, and a blank screen is not an acceptable way to express it.

## Shape

```jsonc
{
  "generated_at": "2026-08-03T04:00:00Z",   // ISO 8601, UTC, Z-suffixed
  "event": {
    "id": "AL132025",
    "name": "Melissa",
    "advisory_count": 41                     // forecast advisories; best_track excluded
  },

  // ---- static: emitted once, never varies by advisory ----
  "parishes": [
    { "name": "Clarendon", "registry": true, "geometry": { /* GeoJSON Polygon */ } }
  ],
  "districts": [
    { "id": 0, "parish": "Saint Catherine", "district": "Spanish Town",
      "n": 140, "lon": -76.9798, "lat": 18.0117,
      "structures": 42264 }
  ],
  "households": [
    { "id": 0, "lon": -77.29603, "lat": 18.09669,
      "parish": "Clarendon", "community": "Beckford Kraal", "roof": "zinc" }
  ],

  // ---- per advisory: one frame per forecast advisory, ascending by number ----
  "frames": [
    {
      "n": "25",                              // advisory number, as printed by NHC
      "at": "2025-10-27T15:00:00Z",
      "posture": "ACT",                       // QUIET | WATCH | READY | ACT
      "watch_codes": ["HWR"],                 // raw NHC codes; console maps to words

      "position": { "lon": -78.2, "lat": 16.4,
                    "max_wind_kt": 145, "gust_kt": 175, "pressure_mb": 908 },

      "wind34": { /* GeoJSON MultiPolygon */ },
      "wind50": { /* GeoJSON MultiPolygon */ },
      "wind64": { /* GeoJSON MultiPolygon */ },
      "cone":   { /* GeoJSON Polygon */ },
      "track":  { /* GeoJSON LineString */ },

      // Cumulative probability of 64 kt, keyed by location then forecast hour.
      // Locations drop out once the storm has passed them — absence is normal
      // and the console must not render a zero for a missing key.
      "probabilities": { "MONTEGO BAY": { "48": 64 } },

      "totals": { "destroyed": 163, "major": 584, "minor": 1253, "none": 0 },

      // Parallel to `districts`, same order, same length. Four counts each.
      "district_counts": [[0, 7, 81, 52]],    // [destroyed, major, minor, none]

      // Parallel to `households`, same order, same length. One character each:
      //   d = DESTROYED, m = MAJOR, n = MINOR, o = NONE
      // A 2,000-character string rather than 2,000 objects — the difference
      // between 82 KB and 3 MB across 41 frames.
      "household_bands": "onmd…",

      // Parallel to `districts`: mutually exclusive mapped-structure counts
      // in [64, 50, 34] kt forecast bands. Present on every frame only after a
      // complete marker-backed event build; valid no-hit frames contain zeros.
      "district_exposed": [[120, 380, 910]]
    }
  ]
}
```

## Rules that are not negotiable

1. **Ordering is the join.** `district_counts[i]` belongs to `districts[i]`;
   `household_bands[i]` to `households[i]`. Both arrays are emitted in a stable
   order — `districts` by `(parish, district)`, `households` by primary key —
   and the exporter asserts the lengths match before writing. A silent
   off-by-one here mislabels synthetic household records, so it fails loudly
   instead.

2. **Frames ascend by advisory number**, parsed by numeric prefix and optional
   suffix, not as a string or an integer cast. `"10"` sorts before `"9"` as
   text, while an integer cast rejects NHC intermediate advisories such as
   `"15A"`. The required order is `15`, `15A`, `16`.

3. **`best_track` is not a frame.** It is observed truth, not forecast, and
   mixing it into the timeline would let the console claim foresight it did not
   have. It is excluded here and belongs in the predicted-vs-observed view.

4. **No nulls for absent data.** Omit the key. `probabilities` for a location
   the storm has passed, a missing intensity reading, an unlocated household or
   district, and unavailable structure exposure are all absent rather than
   zero. Zero is a result; absence means the analysis is unavailable.

5. **Timestamps are UTC with a `Z`.** The console formats fixed to `en-JM`; a
   naive local timestamp would render differently on the server and the client
   and take the whole tree down as a hydration mismatch.

6. **Coordinates are `[lon, lat]`**, GeoJSON order, WGS84.

7. **Coordinates are paired and optional.** `lon` and `lat` must either both be
   present and valid or both be omitted. Synthetic household and district
   coordinates support registry calculations and map navigation; they are not
   rendered as observed homes or observed district locations.

8. **Structure counts are mapped reference inventory.**
   `districts[i].structures` is optional. `district_exposed` is all-or-none
   across the replay: a matching inventory fingerprint, complete event marker,
   exact advisory fingerprint, matching sparse-row totals, and canonical
   SHA-256 digests of every material inventory and event-exposure row put it on
   every frame; otherwise it is omitted from every frame. The row digests catch
   redistribution even when row counts and national totals remain unchanged;
   the event marker also binds the exact inventory-row digest, not only its
   source-input fingerprint.
   Each row follows district
   order as `[64, 50, 34]`, the bands are mutually exclusive, and their sum
   cannot exceed that district's inventory. A completed no-hit advisory is an
   array of zeros. The UI must say unavailable—not zero—when the build cannot
   be validated. A one-time legacy bridge accepts only Melissa's exact pinned
   pre-marker aggregate shape while both completion tables are absent or both
   are empty; it is disabled as soon as either table contains a marker.
   The exporter holds compatible table read locks through this validation, so
   an atomic inventory rebuild cannot commit between marker and aggregate reads.

9. **The browser validates before rendering.** Unknown household band codes,
   invalid geometry, inconsistent totals, length mismatches, invalid advisory
   order, unpaired coordinates, and exposure above inventory reject the entire
   replay. They never degrade into plausible-looking `NONE` or zero values.

## Determinism

Two runs against the same database produce byte-identical output. That means no
timestamp inside the payload beyond `generated_at`, no dict iteration order that
depends on insertion, and floats rounded on write — six decimal places for
coordinates, which is about 10 cm and far finer than anything here is known to.

The test asserts this by exporting twice and comparing.
