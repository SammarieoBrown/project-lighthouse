# Building inventory and the structures layer

How the console stops saying "413 of our 500 synthetic homes" and starts saying
"N real structures in the 64 kt field."

Status: **plan**. Nothing here is built. Implementation starts once the exporter
and console-interactivity work lands, because three of the four deliverables
touch files those are currently editing.

---

## Why this matters

The registry is 2,000 synthetic households scattered inside community polygons by
`ST_GeneratePoints`. Every one sits at coordinates where nothing necessarily
stands. That is honest for a seeded demo and useless as an exposure denominator.

Meanwhile the map already draws real buildings underneath those invented points.
We are guessing on top of data.

Footprints fix **exposure** — how many structures, where, how big — completely.
They fix **vulnerability** not at all, because a footprint carries no roof
material. Keep those two words separate for the whole of this document; conflating
them is the failure mode this plan exists to avoid.

---

## Measured facts

Taken against the live sources, not estimated.

| | |
|---|---|
| Buildings in Jamaica (VIDA: Google + Microsoft + OSM, deduplicated) | **1,844,379** |
| By source | Google 87.9%, OSM 7.7%, Microsoft 4.5% |
| Footprint area | min 0.3 m², **median 47.2 m²**, mean 78.1 m², max 49,571 m² |
| Source file | one GeoParquet, 231,863,933 bytes, `accept-ranges: bytes` |
| Columns | `boundary_id`, `bf_source`, `confidence`, `area_in_meters`, `s2_id`, `geohash`, `geometry` (EPSG:4326), `bbox` |
| Licence | ODbL (inherits OSM) |

Two things worth noticing. The median footprint is 47 m², which is a plausible
Jamaican house — the data passes a smell test before we build on it. And the total
is **1.8× the OSM-only count** of 1,022,977, because the ML-derived sources fill in
the rural coverage OSM has never mapped. That gap is precisely the population our
registry claims to serve, so OSM alone would have been a quiet undercount.

`confidence` is only meaningful for the ML sources; OSM rows are human-drawn.
Filtering on it uniformly would silently discard hand-mapped buildings.

---

## What already exists (and a surprise)

**The console already renders buildings.** They ship inside
`jamaica-z15.pmtiles` and draw today. The reason nobody can see them is one line
in `apps/console/app/eoc/map/flavor.ts`:

```ts
buildings: mix(land, t.figure, 0.12)
```

Twelve percent toward the foreground — deliberately near-invisible so they do not
fight the hazard bands. A previous session counted 704 of them in a single screen
near Black River, on layers `island-buildings` / `region-buildings`.

So deliverables 1 and 2 are **styling work on data we already ship**, not an
ingest. Only 3 and 4 need new data.

Constraint: buildings live in the island archive only. `caribbean-z11.pmtiles`
stops at z11 and carries no buildings, so the structures view is meaningful over
Jamaica at roughly z14+ and must say so rather than rendering an empty screen.

---

## The four deliverables

### D1 — Buildings visible on the map

**Spec.** Two visual weights for the same layer: the current near-invisible
value in the normal view, and a legible one in the structures view. Buildings are
context in the first and the subject in the second.

**Implementation.** Change the paint property at runtime with
`setPaintProperty`, never by rebuilding the style. Rebuilding re-creates every
source and re-downloads tiles — the map-stack doc's standing rule is that the map
is created once.

**Verify.** `queryRenderedFeatures` returns a non-zero building count at z15 over
Black River, and the animated-element count on screen is unchanged.

**Cost:** hours.

---

### D2 — Structures-only view

**Spec.** The map toggle becomes three states: `BASEMAP · SATELLITE · STRUCTURES`.
In `STRUCTURES`, every basemap layer is hidden except buildings, plus a water
hairline for orientation. Our own layers — wind bands, districts, households —
stay in all three.

**The one design call.** The standalone damage viewer dropped the coastline
entirely and stayed readable, because settlement patterns trace the island. Do
not copy that here. A viewer is looked at; an operations map is navigated, and an
EOC map with no coast asks the operator to work out where they are. Keep water as
a hairline.

**Implementation.** `setLayoutProperty(id, "visibility", "none")` across the
flavor-generated layer ids. Collect that list once at style load rather than
guessing at prefixes.

**Verify.** Screenshot at all three states; console clean; the label states what
is being shown, as the existing "Districts · zoom in for homes" note does.

**Cost:** hours.

---

### D3 — Real structure counts per district

The first one needing new data, and the one that changes the numbers on screen.

**Store centroids, not polygons.** 1.84M polygons is roughly 370 MB before
indexes, which is a real question on a Neon dev branch. Centroids plus area are
~50 MB and are sufficient for every question we actually ask: counting, and
which wind band a structure falls in. Rendering comes from the tiles we already
ship, so the polygon would be stored for nothing.

Two caveats to state in the code rather than discover later: a centroid can fall
outside a concave footprint, and a building straddling a wind-band boundary is
assigned wholly by its centroid. Both are acceptable at 47 m² median footprint
against wind fields tens of kilometres across. Neither is acceptable silently.

**Schema.** Two new tables.

```sql
-- Reference geography, currently read from files at runtime by
-- app/registry/geography.py. Counting a million buildings per district needs
-- the polygons in the database, not in a JSON file the API parses on boot.
CREATE TABLE geography (
  id          bigserial PRIMARY KEY,
  kind        text NOT NULL,          -- 'parish' | 'community'
  name        text NOT NULL,
  parish      text,                   -- null for kind='parish'
  population  integer,
  geom        geometry(MultiPolygon, 4326) NOT NULL
);
CREATE INDEX geography_geom_idx ON geography USING gist (geom);
CREATE UNIQUE INDEX geography_kind_name_idx ON geography (kind, name, coalesce(parish, ''));

CREATE TABLE building (
  id          bigserial PRIMARY KEY,
  centroid    geometry(Point, 4326) NOT NULL,
  area_m2     double precision NOT NULL,
  confidence  real,                   -- ML sources only; null for OSM
  source      text NOT NULL,          -- google | microsoft | osm
  parish      text,
  community   text
);
CREATE INDEX building_centroid_idx ON building USING gist (centroid);
CREATE INDEX building_place_idx ON building (parish, community);
```

**Loader.** `data/buildings/fetch_footprints.py`, sibling in style to
`fetch_basemap.py` — cache under `data/buildings/cache/`, pinned by the shared
manifest, fetch-only.

DuckDB does the reading: it handles GeoParquet natively, and both the `spatial`
and `httpfs` extensions install cleanly (verified, DuckDB 1.5.5). Stream
`ST_Centroid(geometry)` as WKB plus the scalar columns, then `COPY` into Postgres.
No geopandas, no GDAL, neither of which is installed and neither of which we
should add for one script.

Then assign place in SQL, once, with the GIST index doing the work:

```sql
UPDATE building b SET parish = g.name
  FROM geography g WHERE g.kind = 'parish' AND ST_Within(b.centroid, g.geom);
```

**Expected shape of the result.** 1.84M rows. If a parish comes back with zero
buildings, or the sum across parishes is materially below 1.84M, the join is
wrong — most likely the p-code/name mismatch already documented in
`geography.py`, where 11 of 14 parishes disagree between the OCHA boundary and
population files. Assert the total, do not eyeball it.

**Verify.** Counts per parish are non-zero and sum to within a rounding error of
the table total; spot-check Black River against the standalone damage viewer,
which covers the same ground from an independent source.

**Cost:** ~1 day.

---

### D4 — Buildings coloured by exposure

**The framing that keeps this honest.** Do not colour buildings by damage. We
have not assessed them, and a coloured building reads as a claim about that
building. Colour by **exposure** — which wind band the structure sits in. That is
a spatial fact, computable for every structure on the island, and requires no
vulnerability model at all.

It turns the headline from *"413 of our 500 synthetic homes"* into *"**N real
structures in the 64 kt band**"*, which is a sentence a ministry can act on.

**Do not store per-building-per-advisory.** 1.84M × 41 is 75M rows to answer a
question that has 21,484 distinct answers. Aggregate instead:

```sql
-- one row per (advisory, community, band)
SELECT b.community, count(*) FROM building b
WHERE ST_Within(b.centroid, :wind64) GROUP BY 1;
```

131 communities × 41 advisories × 4 bands ≈ 21K rows, or computed on the fly
during export and never stored at all. Prefer the latter until something needs it
at query time.

**Rendering, three options.**

1. **Hazard over structures.** Draw wind bands translucent above the building
   layer. Free, no join, and it is how hazard maps have always looked. The
   building's own colour carries no claim; the band does.
2. **Feature-state join.** Ship our own building tiles with stable ids, then
   `setFeatureState` per visible building from an id→band map. This is the real
   product answer and the standalone viewer already proved the render budget
   (137,666 footprints, 111 ms a frame).
3. **Viewport fetch.** Ask the API for buildings in the current bounds with their
   band. Cleanest data model, needs the API deployed, and it breaks the
   offline-first premise the map was built around.

**Recommendation: option 1 now, option 2 later.** Option 1 gets most of the
visual effect for hours of work, and the *numbers* — which are the part anyone
acts on — come from PostGIS and are exact either way.

**Contract change.** Per-frame structure counts are additive to
`replay-export-contract.md`: new optional keys, so the exporter work already in
flight does not break. Ordering rules there apply unchanged — counts parallel to
`districts`, asserted equal in length.

**Cost:** ~1 day.

---

### D5 — The analytical layer

A count is a denominator, not an answer. "1,844,379 buildings in Jamaica" tells
an operator nothing. "Black River: 68% of structures in the 64 kt field" tells
them where to go.

**We already have more geography than we use.** The cached OCHA bundle carries
four levels, and the console only touches the middle one:

| Level | Features | Use |
|---|---|---|
| admin1 — parish | 14 | strategic; national posture |
| admin2 — district | **131** | operational; the only level in use today |
| admin3 — community | **775** | tactical; Black River, Drax Hall |

The 775 communities are cached and unused. Drilling parish → district →
community needs no new download.

**The measures.** Raw count is the input; these are the outputs worth showing.

1. **Exposure share** — percentage of a place's structures inside a wind band.
   The critical property is that a percentage is comparable across places of
   wildly different size, and a count is not. A 1,240-structure village and a
   40,000-structure town cannot be ranked by count without ranking by size.
2. **Population exposed** — the number a decision actually turns on.
3. **Built density** — footprint m² per km². Separates a town from scattered
   rural settlement, which changes how a team deploys.
4. **Escalation** — exposure share across the 41 advisories, per place. This is
   the storm arriving, seen from one village.
5. **Structures per person** — less an insight than a tripwire. A community
   showing eight structures per person means the boundary or the population is
   wrong, and it is better to find that here than on stage.

**The unlock: population where we have none.** OCHA gives population at parish
level only — 14 numbers. There is no community-level population anywhere we can
reach.

Footprints supply the missing weight. Distribute each parish's population across
its communities in proportion to **total footprint area** — dasymetric mapping,
a standard technique, using structures as the density surface. That converts
*"St Elizabeth has 150,000 people"* into *"Black River has roughly N."*

Weight by area rather than by count: a count treats a warehouse and a one-room
house as one person each, and area at least tracks the space people occupy.

**State it as modelled.** Occupancy per square metre is not uniform — apartment
blocks and single dwellings differ by an order of magnitude — so community
population is an estimate derived from a measured parish total, and the screen
must say so. Rule C3 bans fabricated precision: show it rounded, never to the
person.

**Charts: resist the dashboard.** The design rules treat decoration as a
correctness problem, and a wall of charts buries the one line that matters.
Three earn their place:

- **The ranked list already on screen.** It needs real places and exposure share
  instead of synthetic homes, and then it is finished. A ranked list *is* the
  chart that answers "where do we go first".
- **One time series** — exposure share across advisories for the selected place.
- **The band distribution** for the selected place, which is what the scrub-bar
  ticks already do for the nation.

Anything beyond these three needs to justify itself against the question an
operator is actually asking at 3am.

---

## Visual design

The reference is the standalone damage viewer: buildings on a black ground,
carrying colour, with no basemap at all. It works because exactly one thing is on
screen — settlement pattern emerges as texture, and the eye goes straight to
colour. That legibility is the *reason* it works, and it is also the constraint:
the moment three other layers join it, it stops working.

So **structures is a mode, not a layer.** Entering it hides the basemap and makes
buildings the figure. Hazard stays, but as translucent fill and hairline edge, so
it frames rather than competes.

### Two ramps, two meanings

The console already defines a cool hazard ramp — `--lh-hazard-34/50/64` — carried
in `tokens.css` as a stated scoped exception to rule C1. The damage viewer used a
warm one. Keep both, and make the split carry meaning:

| Ramp | Means | Used for |
|---|---|---|
| **Cool** (`--lh-hazard-*`) | forecast | which wind band a structure sits in |
| **Warm** (slate → gold → amber → brick) | observed | measured damage, the predicted-vs-observed view |

An operator can then tell at a glance whether they are looking at a **prediction
or a measurement**, without reading a legend. That is worth more than either
palette on its own, and it costs nothing to adopt now.

### Exposure gets a wash, damage gets a fill

A per-building colour reads as a claim about that building. For **exposure** that
would be wrong: the wind band is a continuous field, and every structure inside
the polygon shares the same exposure. So exposure is drawn as a translucent
hazard fill *above* neutral structural buildings — which is both cheaper and
truer than tinting each footprint individually.

For **damage**, per-building colour is correct, because damage genuinely is
per-building. That is where the warm ramp and our own tiles belong.

### The zoom constraint — read this before promising the wide shot

Buildings live only in `jamaica-z15.pmtiles`, and Protomaps carries them from
roughly **z14**. `caribbean-z11.pmtiles` stops at z11 and has none.

The zoomed-out reference image — a whole region of settlement at ~46 m/px, over
110,000 structures at once — is around z11–12. **That view cannot be built from
the tiles we ship.** It is not a styling change and it is not hours.

What each zoom can honestly show:

| Zoom | Source | Shows |
|---|---|---|
| below ~z11 | our data, aggregated | parish / district choropleth by exposure share |
| ~z11–14 | **needs a new tileset** | settlement pattern — the wide reference shot |
| z14+ | tiles we already ship | individual buildings, free |

Getting the middle band means generating our own tileset from the 1.84M
centroids — tippecanoe to MBTiles to PMTiles, hosted on R2 beside the basemap,
plausibly 30–80 MB. That is a real piece of work with a real payoff, and it
should be costed separately rather than folded into D1 as though it were free.

**Interim answer that is not a compromise:** below z14, show the choropleth. A
district shaded by exposure share answers "where do we go" better than a carpet
of dots does, and it is the view an operations room actually needs. The wide
structures shot is the more arresting image; the choropleth is the more useful
one. Build the choropleth first and the tileset when there is time.

---

## Decisions needed before starting

1. **Extending the frozen schema.** `CLAUDE.md` says the contracts in
   `packages/contracts` are frozen and changing them is a deliberate act. My
   reading: the freeze protects the *claim lifecycle* and the agent I/O contracts
   — StormFile, Claim, Evidence, Verification, Allocation, Disbursement,
   LedgerEntry, Approval — and the state machine over them. `building` and
   `geography` are reference data; nothing frozen depends on them, and adding
   them changes no existing column. That still needs to be an explicit yes, not
   an assumption.

2. **Centroids or full polygons.** Recommendation above is centroids. Full
   polygons cost ~370 MB and buy only a rendering path we already have.

3. **Do households move onto real buildings?** Snapping the 2,000 synthetic
   households to real footprints would make every dot a place something stands.
   Tempting, and it is a change to the seeder — which means the replay, the risk
   assessments and the committed export all regenerate. Worth doing, but as its
   own step after D3, not folded into it.

---

## What stays true regardless

- **Synthetic data only** is not violated. Building footprints are public
  geospatial features with no occupant attached. This parameterises the generator
  with real geography; it does not ingest real households.
- **Exposure is not vulnerability.** Footprints say a structure is there and how
  big it is. They say nothing about how it fails. The roof material stays
  modelled, and the census work is what improves it.
- **OSM alone would undercount rural Jamaica by roughly half.** That is why the
  combined source is the right one, and it is worth saying out loud when
  presenting a number.

## Sequencing

D3 and D4 touch `apps/api` and the database and can begin the moment the current
exporter work lands. D1 and D2 touch `flavor.ts`, `layers.ts` and `MapPanel.tsx`
— the exact files the console agent is editing — so they wait.

Suggested order: **D3 → D1 → D2 → D4.** Counts first, because they are the thing
that changes what the console can honestly claim, and because they are the only
part with a real chance of surprising us.
