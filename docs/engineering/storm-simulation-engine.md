# The storm simulation engine

Where the storms come from, why the physics is shaped the way it is, and what
the research said about making one look real.

Read this before touching `apps/api/app/storms/` or `data/storms/`.

---

## The request

> "A person can create a storm, define the metrics, draw the path the storm
> would take, and speed, wind size etc, and we watch the storm play out in
> realtime… or select any major hurricane in the past which pulls its metrics
> and replays the simulation, and then users can tweak and expand or edit the
> storm, and see how it impacts. I want it to look super realistic — the eye,
> the wind, the storm swirling, just like Google Earth."

The product replayed exactly one hurricane. Melissa arrives as NHC teletype and
every stage below it is reachable only through that path, so the demo was a
recording of a single storm.

## Decisions taken

- **Sequence:** simulator first, then a stripped Act 2 and Act 3 on top.
- **Realism:** GOES satellite imagery for historical storms, particle wind field
  over it, restrained stylised cloud for authored storms. Not procedural
  photorealism — see the research below for why.
- **Catalogue:** curated Caribbean storms plus any Atlantic storm with enough
  data. HURDAT2 merged with the Extended Best Track.
- **Camera stays flat.** `pitch: 0`, `dragRotate: false` are kept. "Google
  Earth" here means the appearance of the storm, not a tilted globe — at
  Jamaica scale a globe buys nothing and costs real shader work.

---

## Research: where storm data comes from

### The archives, and why it takes two

**HURDAT2** — NHC's reanalysed Atlantic best track, 1851 to present, ~7 MB of
plain text. Authoritative on where a storm was and how strong.

It carries **quadrant wind radii only from 2004** and **radius of maximum wind
only from 2021**. Before that the columns are `-999`. Measured completeness for
North Atlantic rows at hurricane strength:

| Field | 1851–1978 | 1979–2003 | 2004–2020 | 2021– |
|---|---|---|---|---|
| Central pressure | 14.7% | 100% | 100% | 100% |
| Radius of max wind | 0.8% | 10.6% | 88.8% | 100% |
| 34 kt radii | 0% | 9.5% | **100%** | **100%** |
| 64 kt radii | 0% | 0% | 99.6% | 99.8% |

Read that carefully — it inverts the usual worry. For a storm at hurricane
strength in 2004 or later the radii are essentially always present. The gap is
a *date* problem, not a *field* problem.

**Which makes Gilbert unusable on its own.** The storm every Jamaican remembers
is 1988, and in HURDAT2 it is:

```
19880912, 1800,  , HU, 17.7N,  76.5W, 110,  960, -999, -999, ... -999
```

**EBTRK** — CIRA/Colorado State's Extended Best Track, fixed-width ASCII, exists
precisely because HURDAT2 lacks pre-2004 radii and RMW. Operational records
digitised back to 1988. Same storm, same hour:

```
AL081988 GILBERT  091218 1988  17.7  76.5 110  960  22 ... 250200250250 ...
```

Radius of maximum wind 22 nmi, and the 34/50/64 kt radii by quadrant. Without
this file the catalogue starts in 2004 and Jamaica's defining hurricane is not
in it.

### Two traps in EBTRK

**Fixed-width parsing is mandatory.** The radii are packed as four 3-character
fields with no separator — `250200250250` is 250/200/250/250. Whitespace
splitting works until a two-digit value pads to ` 50` and the block becomes
several tokens; token counts across the real file run from **18 to 27**.

**Do not trust eye diameter, POCI or ROCI in the older records.** Gilbert
reports an outermost closed isobar of 12 hPa, which is not a pressure. The
loader ignores those columns and uses a constant ambient pressure of 1010 hPa,
which is standard practice anyway.

### Considered and rejected

**IBTrACS** — the global archive, 174 columns, NetCDF or a 315 MB CSV. Correct
for a global product; for an Atlantic-only one it is the same `USA_*` data
HURDAT2 carries, wrapped in a larger file and a NetCDF dependency.

**ATCF b-decks** — the raw operational source the others derive from. No
advantage here; use it only for real time.

---

## Research: the wind model

### What we implement

Holland (1980) radial profile, with forward-motion asymmetry and inflow angle:

```
V(r) = sqrt( (B/rho)(Rmax/r)^B * dP * exp(-(Rmax/r)^B) + (rf/2)^2 ) - rf/2
```

- **Forward motion** added as a vector, damped by `min(1, Rmax/r)`, so the storm
  carries its speed near the core and less of it far out. This is why the
  right-hand side of a northward-moving Atlantic hurricane is the dangerous one.
- **Inflow angle** — Zhang & Uhlhorn (2012) measured a mean of 22.6° at the
  surface from 1,600 dropsondes. One rotation, and the cheapest thing that makes
  a field read as a hurricane rather than as concentric rings.

### The correction that mattered

Holland's B derived from peak wind alone **systematically under-predicts the
outer field**, which the literature says and our own data confirmed:

| Storm | 34 kt radius measured | B from vmax alone |
|---|---|---|
| Gilbert 1988 | 250 nm | 64 nm |
| Matthew 2016 | 170 nm | 20 nm |

Size is a second, independent property of a storm — two hurricanes of identical
intensity can differ threefold in extent — and a model with one free parameter
cannot express both. **B is fitted to an outer radius rather than derived**, and
the core scales with it. That is also what gives the authoring UI a meaningful
size control.

B is floored at 1.0, the literature's lower bound. Without the floor the fit
will flatten a major hurricane until its 64 kt field disappears in order to
reach a wide 34 kt radius.

### A bug worth remembering

The tangential direction was **added** rather than subtracted. Cyclonic flow in
the northern hemisphere means the wind at a point due north of the eye blows
west — bearing zero minus ninety. Adding returned east-southeast: the storm spun
backwards, and because the quadrant radii are sampled from that field, the
largest radius was written into the wrong quadrant.

Nothing about that fails loudly. It draws as a plausible hurricane, mirrored,
and warns the wrong parish. A test caught it; nothing else would have.

### On licences

CLIMADA and TCRM implement the same mathematics and are both **GPL-3.0**.
Neither was read. The equations are in the papers and mathematics is not
copyrightable — but do not vendor their code.

- Holland (1980), MWR 108(8) — the profile
- Holland (2008), MWR 136(9) eq. 11 — the shape parameter from pressure
- Vickery & Wadhera (2008), JAMC 47(10) — radius of maximum wind
- Zhang & Uhlhorn (2012), MWR 140(11) — surface inflow angle
- Nederhoff et al. (2019), NHESS — fitting profiles to observed radii

### What this is not

Not a boundary layer model. FEMA's Hazus and the insurance industry solve a
translating slab over the pressure field with surface friction. And it has no
terrain: the Blue Mountains rise to 2,256 m and will do things to a wind field
that no parametric profile expresses. The output is a smooth idealisation, and
the screen says so.

---

## Research: making it look real

**The blunt finding: there is no published technique, no open demo and no
library for a convincing procedural hurricane in a web map.** The nearest
published thing — Mapbox's 3D animated hurricane — is not procedural at all; it
is NEXRAD radar contoured, extruded and draped over terrain, with no source
released.

"Like Google Earth" describes photography. Competing with it using noise
functions is an open-ended research task with a subjective completion
criterion, and it is where a schedule dies.

**So invert it: use the photograph.** GOES-16/19 imagery is public domain, on
AWS Open Data at `s3://noaa-goes{16,19}/`, verified accessible including
Melissa's landfall. Real eye, real bands, real convection, because it *is* the
storm. Full Disk every 10 minutes; mesoscale sectors every minute, and NHC
parks one on an active hurricane.

The catch is size: each full-disk file is ~360 MB of NetCDF, so a fixed
catalogue must be tiled offline. Open-ended "any storm, any time" is not viable.

**Particle advection is the other half and it is a solved problem.** Note that
earth.nullschool.net does *not* use WebGL — it is Canvas 2D at ~5,000 particles.
The GPU technique people attribute to it is Agafonkin's `webgl-wind`, which does
a million at 60 fps using ping-pong framebuffers. `sakitam-fdd/wind-layer` is
MIT and ships a MapLibre package; `astrosat/windgl` is the most-cited answer
online but predates MapLibre v5/v6.

### Effort, honestly

| | |
|---|---|
| Archive ingest, wind model, asymmetry | straightforward — closed-form arithmetic |
| Particle advection | straightforward — solved, MIT implementation exists |
| MapLibre custom layer plumbing | real effort — GL state hygiene in `prerender` |
| GOES replay, fixed catalogue | real effort — offline tiling pipeline |
| **Procedural eye and spiral bands** | **research project** |

The physics is roughly a fifth of the effort. The rendering is the rest.

---

## The design-rules problem

The console contains **zero animation** — not one keyframe, transition or
`requestAnimationFrame` — deliberately, with the reason written in five places.
Rule M1: *"Animation is reserved for exactly three triggers, and any other
moving pixel is a bug."*

An animated wind field would be **the first motion exception ever written**.

The doc has a formal mechanism and a template — the `--lh-structure` exception,
design-rules lines 83–88. A written exception goes **into that file** before the
layer ships, following its anatomy: scope, what it encodes, why the existing
vocabulary could not carry it, what it must never acquire, containment boundary.

The argument that makes it honest: **M1's own thesis is that motion means state
changed.** A moving wind field *is* the state changing. That is the same
reasoning already written at `MapView.tsx:451` — "a playing timeline is a state
change, not a decoration". The exception is scoped to the simulation surface,
driven only by simulated time, and stopped when the replay is paused.
`prefers-reduced-motion` must be read in JS, because the CSS block only shrinks
durations and cannot reach a shader.

Two constraints kept, not waived: the SVG fallback must still state the same
evidence, and a rendered storm is a **fourth evidence category** — synthesised
imagery — that the map's observed/forecast/modelled vocabulary has to name
rather than blur.

---

## What is built

- `data/storms/fetch_tracks.py` — both archives, manifest-pinned
- `apps/api/app/storms/tracks.py` — parsers and the merge
- `apps/api/app/storms/catalogue.py` — 195 hurricanes within 500 km of Jamaica,
  each labelled measured or modelled
- `apps/api/app/storms/wind.py` — the parametric model
- `apps/api/app/storms/synthesize.py` — track → advisories
- `apps/api/app/console/library.py` — multi-storm export and index
- Console storm picker, with provenance shown beside the name

**The integration seam is `advisory.raw`.** Nothing below it knows where a storm
came from, so a historical storm is written as a `ForecastAdvisory` — the same
dataclass the teletype parser produces — and `_wind_fields` and `_raw_payload`
are reused verbatim. One shape, one code path, nothing to drift.

**Two horizons, deliberately.** A hindcast forecasts perfectly, so unioning the
true track across 120 hours put Jamaica inside Gilbert's wind field three days
out and every advisory reported maximum damage — a flat line where the whole
point is escalation. The full track stays in `raw` so posture still sees a 64 kt
arrival at 72 hours; the wind field unions only the next 48.

## What is not built

- **Authoring** — draw a track, set intensity/size/speed. The engine accepts
  these parameters and `ingest_track` accepts any track; what is missing is the
  UI and a fast path. The current pipeline is ingest → 98,000 risk assessments →
  a four-minute exposure build, which is fine for a catalogue storm and useless
  for dragging a slider. Interactive authoring needs a client-side wind field
  and community-level impact.
- **Particle wind rendering**
- **GOES imagery pipeline**
- **The motion exception**, written into the design rules

## Verification

```bash
cd apps/api && uv run pytest tests/test_storms.py
```

Load Gilbert and confirm it produces wind fields at all — the EBTRK merge is the
thing most likely to be silently wrong. Two tests guard things that should stay
true rather than things that merely are: that HURDAT2 alone cannot size Gilbert,
so the second archive keeps earning its place, and that measured radii are never
overwritten by the model.
