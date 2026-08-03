# The replay export contract

The console reads one generated file. This document is the agreement between the
thing that writes it (`apps/api`) and the thing that reads it (`apps/console`).
Both sides are built against this page; neither side invents a field.

## Why a file and not an endpoint

The console has to work on a venue network that may not exist. The map already
carries its basemap offline for that reason, and a console whose timeline stops
moving when the wifi drops is worse than one that never claimed to be live.

Live operation against the API comes next, and the shape below is what that
endpoint will return — so the console's reader does not change when it arrives.

## Size, measured

Per advisory only **15.8 KB** actually varies; parish outlines and household
positions are 288 KB that never move. So the whole 41-advisory storm is about a
megabyte, which fits in a browser once and then scrubs instantly. Splitting
static geometry from per-advisory frames is the entire reason it fits.

## Location

Written to `apps/console/public/replay/replay.json`, fetched by the console at
`/replay/replay.json`. Regenerate with:

```bash
cd apps/api && uv run python -m app.console.export
```

**It is committed.** That reads wrong for a generated file, so here is why: the
console deploys to Vercel from `apps/console` with `npm run build`, and that
build has no Python, no `uv`, and no `DATABASE_URL`. A gitignored export means
production ships with no data. The basemap archives are already absent in
production for exactly this reason — they 404, and the deployed console silently
falls back to the static SVG map.

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
      "n": 140, "lon": -76.9798, "lat": 18.0117 }
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
      "household_bands": "onmd…"
    }
  ]
}
```

## Rules that are not negotiable

1. **Ordering is the join.** `district_counts[i]` belongs to `districts[i]`;
   `household_bands[i]` to `households[i]`. Both arrays are emitted in a stable
   order — `districts` by `(parish, district)`, `households` by primary key —
   and the exporter asserts the lengths match before writing. A silent
   off-by-one here mislabels real homes, so it fails loudly instead.

2. **Frames ascend by advisory number**, parsed as an integer, not a string —
   `"10"` sorts before `"9"` otherwise, and the timeline would render the storm
   out of order while looking entirely plausible.

3. **`best_track` is not a frame.** It is observed truth, not forecast, and
   mixing it into the timeline would let the console claim foresight it did not
   have. It is excluded here and belongs in the predicted-vs-observed view.

4. **No nulls for absent data.** Omit the key. `probabilities` for a location
   the storm has passed is missing, not zero — a zero would state that the
   chance is nil, which is a different and false claim.

5. **Timestamps are UTC with a `Z`.** The console formats fixed to `en-JM`; a
   naive local timestamp would render differently on the server and the client
   and take the whole tree down as a hydration mismatch.

6. **Coordinates are `[lon, lat]`**, GeoJSON order, WGS84.

## Determinism

Two runs against the same database produce byte-identical output. That means no
timestamp inside the payload beyond `generated_at`, no dict iteration order that
depends on insertion, and floats rounded on write — six decimal places for
coordinates, which is about 10 cm and far finer than anything here is known to.

The test asserts this by exporting twice and comparing.
