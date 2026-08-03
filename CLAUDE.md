# Project Lighthouse — agent instructions

Agentic disaster relief coordination for Jamaica. Monorepo: `apps/api` (FastAPI, `web` + `worker` on Render), `apps/console` (Next.js on Vercel), `packages/contracts` (frozen Pydantic + schema), `data/replay` (Melissa advisory cache).

Start here: [docs/engineering/lighthouse-build-spec.md](docs/engineering/lighthouse-build-spec.md) and [docs/engineering/lighthouse-project-phases.md](docs/engineering/lighthouse-project-phases.md).

## Before any UI or design work

**Read [docs/design/lighthouse-design-rules.md](docs/design/lighthouse-design-rules.md) first, every time.** It is binding on `apps/console` and the public portal. It bans the AI-default look explicitly and explains why, in this product, decorative styling is a correctness bug rather than a taste question. Do not write a component, choose a colour, or add an animation before reading it.

The pre-merge checklist in its Part 5 applies to every PR that touches frontend code.

## Before any map work

Read [docs/engineering/lighthouse-map-stack.md](docs/engineering/lighthouse-map-stack.md). It documents four traps that each cost hours, and every one of them fails silently — the map builds, the canvas sizes, the controls draw, and the result is wrong in a way that survives to a demo. It also has a debugging order for the blank map, which is this stack's default failure.

## Standing rules (from the build spec — these are not negotiable)

- **Agents propose, humans dispose, the ledger remembers.** Any code path that moves money without an `approved_by` is a bug. Two of these guarantees are enforced in the database, not in application code — keep them there.
- **The contracts in `packages/contracts` are frozen.** They double as the JSON schema for structured model output, so contract and prompt cannot drift. Changing them is a deliberate act, not a convenience.
- **Every agent output is stored raw**, including outputs a human overrides. That is the eval set for next season.
- **Synthetic data only** for the whole buildathon. No real household data until a data sharing agreement exists.
- **No PII in logs.** Phone numbers hashed everywhere except the StormFile row.
- **The replay seeder is the shared heartbeat.** If it breaks, fixing it outranks whatever else was in progress.

## Working style

Single developer. Do not attribute work to other people or defer tasks to a teammate — if it is in scope, it gets built here. Where existing docs assign work to named people, treat those as historical and read the assignment as "someone has to do this."

## Commands

```bash
cd apps/api && uv run pytest
```

```bash
cd apps/api && uv run alembic upgrade head
```

11 tests, green as of Aug 2. There is no Docker Compose and no local Postgres — tests run against a Neon branch, since no stock image carries PostGIS and pgvector together. `DATABASE_URL` must point at a dev branch, never at `main`'s database.
