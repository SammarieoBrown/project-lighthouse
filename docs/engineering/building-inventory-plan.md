# Building inventory and exposure contract

Status: **implemented**. This page is the contract for the structure tiles,
aggregate counts, and advisory-specific exposure data.

## The distinction that keeps the map honest

Lighthouse carries three different kinds of information. They must not be
collapsed into one property or one label.

| Kind | What it establishes | Lifetime |
|---|---|---|
| **Inventory** | A mapped source footprint exists at a location | Independent of every storm |
| **Exposure** | A structure centroid is inside one advisory's forecast wind field | Keyed to one event and one advisory |
| **Damage** | A structure was observed or assessed to have suffered damage | Keyed to evidence; not provided by footprints |

A forecast polygon is not observed impact. A footprint is not a household and
carries no occupant, roof material, vulnerability, or damage assessment.

The 2,000 Storm Files remain synthetic households used to exercise the claim
lifecycle. They are not the denominator for Jamaica's built environment.

## Source inventory

The source is VIDA's country-level Jamaica GeoParquet, a deduplicated blend of
Google Open Buildings, Microsoft GlobalML Buildings, and OpenStreetMap.

| | |
|---|---|
| Structures | **1,844,379** |
| Source mix | Google 87.9%, OSM 7.7%, Microsoft 4.5% |
| Median footprint area | 47.2 m² |
| CRS | EPSG:4326 |
| Licence | ODbL |
| Cached source | `data/buildings/cache/jamaica-buildings.parquet` |

`data/buildings/cache/manifest.sha256` pins the fetched GeoParquet by SHA-256.
Generated GeoJSON Lines files are excluded by the cache's committed
`.manifestignore`; they are validated by their own build state instead. This
separation matters: fetching source data must never certify a derived output
merely because it happens to be in the same directory.

Fetchers reuse an existing source, basemap, glyph, or sprite only after its full
digest matches the committed manifest. Downloads and PMTiles extraction write a
separate partial file and promote it atomically; a non-force download that no
longer matches its committed pin fails for review instead of silently repinning
changed upstream bytes. `--force` is the explicit accept-and-repin path.

## Structure tile contract

`data/tiles/cache/structures-z15.pmtiles` is a **storm-independent reference
inventory**. It has two source layers and no other semantic fields.

### `structure_points`, z9-13

Individual footprints are far below a pixel at national and parish zooms. The
wide layer therefore uses a deterministic 0.005-degree grid, roughly 0.5 km at
Jamaica's latitude.

For every occupied grid cell it emits one point at the mean coordinate of the
source centroids. The only property is:

| Property | Meaning |
|---|---|
| `w` | Exact number of source structures represented by the point |

The build asserts that the sum of `w` equals the source footprint count. This is
an aggregated building-distribution layer. It must never be labelled
"individual buildings" or "every building".

As with ordinary buffered vector tiles, an edge feature can appear in both
adjacent tile payloads so circles render without seams. Do not sum decoded tile
instances for analytics; `w` is conserved across the 25,642 logical source
aggregates, and operational totals come from the manifest or database.

Each feature carries top-level tippecanoe metadata limiting it to z9-13. Layer
min/max keys inside tippecanoe's `-L` input JSON are not used: that syntax
silently ignores them and previously caused footprints to leak into low zooms.

### `structures`, z14-15

The close layer emits one tile feature for every mapped source footprint. Its
geometry is quantized and simplified for vector-tile rendering, so it is a
mapped footprint representation rather than an exact copy of the source
geometry. Its optional properties are:

| Property | Meaning |
|---|---|
| `d` | OCHA admin-2 district containing the footprint centroid |
| `c` | OCHA admin-3 community containing the footprint centroid |

A centroid outside every published boundary is retained with those properties
absent. An inner join would silently delete coastal cays and boundary-gap
structures.

Each footprint carries top-level tippecanoe metadata limiting it to z14-15.
The build disables point drop-rate thinning and tiny-polygon reduction, so the
weighted grid is not silently sampled and close-zoom footprints are not merged
into synthetic dust features.

### Fields that are forbidden in both layers

No advisory number, event id, wind band, first-entry index, cumulative-hit flag,
exposure result, or damage result belongs in the archive. In particular,
`f34`, `f50`, and `f64` are retired. Their old meaning depended on the implicit
array index of an unscoped advisory query, mixed forecast history into reference
data, and could not represent NHC identifiers such as `15A` safely.

The map renders structures neutrally. A selected advisory's wind polygons are a
separate overlay. That visual composition states the defensible claim: these
mapped structures lie under this forecast field.

## Exposure aggregate contract

`python -m app.registry.buildings --event al132025` builds two small aggregate
tables from the same source inventory:

- `place_structures` is the storm-independent denominator by parish, district,
  and community.
- `place_exposure` is keyed by `advisory_id`. Each build explicitly selects one
  `hazard_event.external_ref`; rebuilding one event deletes and replaces only
  that event's exposure rows.

Two completion records make those sparse tables safe to interpret.
`place_structure_build` identifies the current mapped inventory from the source
SHA-256, boundary SHA-256, recipe version, fingerprint, and exact aggregate
counts plus a canonical digest of every persisted place row and material field.
`place_exposure_build` binds one complete event build—including
advisories with no non-zero rows—to that inventory and to an advisory-geometry
fingerprint, and carries its own canonical digest of every event-scoped sparse
row plus the exact inventory-row digest it used. Export emits zero arrays only
when both records, counts, totals, and exact
row digests still match; missing, partial, redistributed, wrong-inventory, or
stale-advisory state fails closed as unavailable. The canonical representation
is a UTF-8 JSON object with an explicit domain and version, sorts full rows,
preserves exact text, and writes finite `built_m2` values as their exact
big-endian IEEE-754 bits. Exposure digests additionally bind the canonical event
UUID, so even two empty event row sets have distinct identities. Order is
irrelevant but every material column is covered without delimiter or
float-format ambiguity.

Export holds PostgreSQL `SHARE` locks on the advisory, inventory, exposure, and
available marker tables for the transaction, so a `READ COMMITTED` export
cannot mix rows from opposite sides of an atomic rebuild. It also requires the
stored recipe to be the current recipe, recomputes the inventory fingerprint
from the stored source and boundary digests, rejects district exposure above
that district's mapped count, recomputes both derived-row digests, and omits the
whole inventory/exposure product if any replay district lacks an aggregate.

The rollout has one pinned legacy bridge for the already-built Melissa data:
when both marker tables are absent or both are globally empty, export accepts
only the exact known 775 place rows, 1,842,165 placed footprints, forecast set
1–41, and non-zero exposure set 1–31. Once either marker table has a row, that
inference is disabled and normal fingerprint validation is mandatory.

Advisories are sorted with NHC semantics: `9`, `10`, `15`, `15A`, `16`. They are
not cast to integers in SQL, and forecast advisories from another event cannot
enter the calculation.

For each advisory, a building is assigned by centroid to the strongest forecast
wind threshold that contains it. The resulting row bands are mutually
exclusive:

```text
64 kt, otherwise 50 kt, otherwise 34 kt, otherwise unexposed
```

These values describe the selected advisory's forecast envelope. They are not
cumulative across earlier advisories. A future cumulative product must have an
explicit time window and a field and label containing the word `cumulative`; it
must not be inferred from tile properties.

Centroid assignment means a building straddling a wind boundary is assigned as
one structure. That approximation is acceptable for a median 47.2 m² footprint
against wind fields tens of kilometres across, but it remains an approximation.

## Reproducible build and publication

Build the inventory archive with:

```bash
cd apps/api
uv run python -m app.registry.building_tiles
```

The build fingerprint includes:

- SHA-256 of the VIDA GeoParquet;
- SHA-256 of the OCHA boundary archive;
- the DuckDB version;
- the boundary join, property allowlists, grid policy, deterministic GeoParquet
  row order, boundary tie-breaker, and recipe version;
- zoom/layer policy, the tippecanoe and PMTiles CLI versions, and the resolved
  PMTiles executable SHA-256 (its local build reports only `dev, commit none`).

`structures.geojsonl` and `structure-points.geojsonl` are reused only if their
state file has the current fingerprint **and** both full content hashes match.
Existence or byte size is never a reuse gate. Partial outputs use separate names
and replace the prior output only after a successful write.

After conversion, `data/tiles/structures.manifest.json` records the source and
tile recipes, feature counts, archive byte count, and archive SHA-256. It is
committed even though the large archive is ignored, like a lockfile for the
derived asset. Tippecanoe runs with fixed name/description values and stable
repo-relative paths; its disposable MBTiles target is removed after conversion,
so random temporary-directory names cannot change otherwise identical archive
metadata and checksums. Polygon features are emitted in pinned GeoParquet row
order, ambiguous boundary matches use a fixed p-code/FID tie-breaker, and grid
coordinates use integer nanodegree accumulation before division. Tippecanoe is
instructed to preserve that input order inside tiles.

Publish with:

```bash
python3 data/tiles/upload_basemap.py
```

The publisher checks local bytes against their manifest before upload. A remote
object is skipped only after its full SHA-256 matches, not because its size
matches. `--verify` streams and hashes the public objects and separately checks
that PMTiles byte-range requests return `206` and a valid header. Stable object
URLs are published with `Cache-Control: public, max-age=0, must-revalidate`, and
verification rejects missing or semantically different cache directives.

## Verification

```bash
cd apps/api
uv run pytest -q tests/test_building_data.py

cd ../..
python3 data/buildings/fetch_footprints.py --verify
python3 data/tiles/fetch_basemap.py --verify
pmtiles verify data/tiles/cache/structures-z15.pmtiles
```

The focused tests protect the event-free tile property allowlist, weighted-grid
policy, input fingerprint invalidation, intermediate content hashes, event
scoping, suffix-safe advisory order, manifest exclusions, and checksum-based
upload decisions.

## Remaining analytical work

The aggregate inventory enables useful operational measures without changing
the tile contract:

- exposure share per district or community;
- built density;
- advisory-to-advisory exposure change;
- modelled population exposure, clearly labelled as modelled.

Those belong in replay frames or an event-scoped API response. They never belong
in the static building archive.
