# Project Lighthouse

**The rail between disaster capital and households.**

Lighthouse is a system that gets help to people fast after a hurricane. Before the storm, families register on WhatsApp so we know who lives where, in what kind of house, and who is vulnerable. When a storm is coming, they get warnings in their own language. After it hits, anyone can send a voice note or text, even in Jamaican Patois, saying what happened, and AI agents verify the report against the storm's actual wind field, satellite imagery, and neighbouring reports. Every verified family is matched to help, whether the money comes from government relief, their insurance company, or diaspora donations, and a human must sign before any money moves. Every dollar is tracked on a public, append-only ledger from the moment it arrives to the moment it reaches a family.

**One claim, many payers, one ledger. The report becomes the claim becomes the payment.**

North-star metric: **Time to Relief (T2R)**, median hours from verified claim to first relief in hand. After Hurricane Melissa (2025) it was months to never; Lighthouse targets 72 hours.

## Why

Melissa proved disaster capital is fast: Jamaica's US$150M catastrophe bond paid out about ten days after landfall. Distribution is what is broken: four months later, the Auditor General found 1.8% of J$1.44B in cash donations had reached households. The missing piece is the last mile, and closing it requires cognition at a scale no small state can staff: 120,000 conversations, verified against evidence, matched to money, in days. That is what agents make affordable, with humans holding every gate that moves money.

## Status

Buildathon application, **FutureCaribbean 2026, Track 04: Climate Risk & Disaster Coordination**.
Team: Raheem Wilson · Sammarieo Brown · Matthew Stone (Team Project Lighthouse, Jamaica).

## Documentation

| Doc | What it is |
|---|---|
| [PRD / SRS](docs/product/lighthouse-prd.md) | Granular requirements with priorities and acceptance criteria |
| [Solution spec](docs/product/lighthouse-solution-spec.md) | What the product is, how it works, use cases |
| [Concept brief](docs/product/lighthouse-concept-brief.md) | The pitch narrative and defensibility |
| [Build spec](docs/engineering/lighthouse-build-spec.md) | Engineering reference: model, services, agent contracts, stack |
| [Project phases](docs/engineering/lighthouse-project-phases.md) | Phase plan with testable exit criteria |
| [Interactive prototype](docs/prototype/lighthouse-prototype.html) | Open in a browser: the Hurricane Melissa replay simulation |
| [Workflow diagram](docs/assets/lighthouse-agentic-workflow.pdf) | Agents, human gates, data sources, outputs, decision points |

## Repository structure

```
docs/        product, engineering and buildathon documentation + assets
apps/api     Python modular monolith (FastAPI): web + worker entrypoints
apps/console Next.js EOC console + public transparency portal
packages/contracts  the Phase 0 freeze: schema, state machine, agent I/O models
infra/       compose, Caddy, deploy
data/replay  Melissa advisory cache + synthetic registry seeds (synthetic only, always)
```

## Principles

1. Agents propose, humans dispose, the ledger remembers. No code path moves money without a signature.
2. Every agent output is stored raw, including the ones humans override. That is next season's eval set.
3. Synthetic data only until a data-sharing agreement exists. No PII in logs, ever.
4. We consume forecasts (NOAA/ECMWF), we do not make them. We evidence claims, we do not price them.

## License

Apache-2.0. See [LICENSE](LICENSE).
