# replay data

Cached Hurricane Melissa advisory history (NHC), pre-cached hazard rasters and satellite tiles for the replay area, and the synthetic Storm File registry seeder (fixed seed, ~500 households).

**No real personal data may ever be committed here.**

## The cache is committed on purpose

`cache/` is versioned, not gitignored. The replay has to be byte-identical on three laptops and the demo machine, and it has to run with zero external network calls (PRD RPL-01). A cache that each developer fetches for themselves is not a deterministic replay — it is four slightly different replays that happen to agree most of the time.

- `fetch_advisories.py` regenerates the cache from NHC archives.
- `manifest.sha256` pins what the cache should contain; CI verifies it.
- If the manifest fails, do not "fix" it by re-fetching. Find out what changed first — the whole point is that it should not.

The registry seeder uses a fixed seed for the same reason.
