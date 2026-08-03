# replay data

Cached Hurricane Melissa advisory history (NHC), pre-cached hazard rasters and satellite tiles for the replay area, and the synthetic Storm File registry seeder (fixed seed, ~500 households).

**No real personal data may ever be committed here.**

## The cache is committed on purpose

`cache/` is versioned, not gitignored. The replay has to be byte-identical on every machine and on the demo laptop, and it has to run with zero external network calls (PRD RPL-01). A cache each machine fetches for itself is not a deterministic replay — it is several slightly different replays that happen to agree most of the time.

- `fetch_advisories.py` regenerates the cache from NHC archives.
- `manifest.sha256` pins what the cache should contain; CI verifies it on every push.
- If the manifest fails, do not "fix" it by re-fetching. Find out what changed first — the whole point is that it should not.

The registry seeder uses a fixed seed for the same reason.

## What is cached

**Hurricane Melissa — `AL132025`**, the thirteenth Atlantic storm of 2025. Formed as a tropical storm on 21 October 2025 and made landfall on Jamaica on 28 October as a category 5. 41 numbered advisories, 294 files, 6.6 MB.

```
cache/al132025/
  text/
    fstadv/    41   Forecast/Advisory — centre position, max wind, wind radii,
                    forecast track. The machine-readable backbone.
    wndprb/    41   Wind speed probabilities, the 34/50/64 kt product.
    public/    41   Public advisory — watches and warnings in prose.
    public_a/  39   Intermediate public advisories, issued between the
                    six-hourly ones as landfall approached.
    discus/    41   Forecast discussion. Not parsed; the best narration
                    available for the demo.
    update/     8   Tropical cyclone updates, numbered by timestamp.
  gis/
    5day/      41   Cone polygon, track line, forecast points and
                    watch/warning segments, one zip per advisory.
    fcst/      41   Forecast wind radii.
    best_track/ 1   What actually happened, not what was forecast.
cache/manifest.sha256
```

**The probability product is the one that matters.** The cone describes where the storm *centre* might go, which is a different and much less useful question than who gets hit. `wndprb` is what says Montego Bay had a 29% chance of hurricane-force wind at advisory 25 — that is what drives a risk score for a household, and it is the number a judge will ask about.

**Best track is cached because verification needs it.** Phase 2 compares a claim against the wind field that was actually observed at that point, not the one that was forecast. Without the post-storm truth there is nothing to verify against.

## Regenerating

```bash
python3 data/replay/fetch_advisories.py            # fetch what is missing
python3 data/replay/fetch_advisories.py --force    # refetch everything
python3 data/replay/fetch_advisories.py --verify   # check manifest, no network
```

Standard library only, so it runs from a clean clone without installing the API environment first. On a python.org macOS build it will use `certifi` if importable, because that Python does not read the system keychain and will otherwise fail TLS verification against NOAA.

## Two things worth knowing before you go looking

- **There is no standalone watches/warnings GIS archive.** Those segments ship inside the `5day` bundle.
- **The gridded wind-speed-probability rasters are not in the public archive** under any of the obvious paths. The `wndprb` text product carries the same probabilities per location, which is what the risk model consumes anyway.
