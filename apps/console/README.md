# console

Next.js EOC console and public transparency portal. Deploys to Vercel from `main` with this directory as the project root.

```bash
cd apps/console && npm install && npm run dev
```

## Before you write a component

Read [the design rules](../../docs/design/lighthouse-design-rules.md). They are binding, they ban the AI-default look explicitly, and they explain why decorative styling in an operations console is a correctness bug rather than a taste question.

The short version:

- **Every colour, size and duration comes from [`app/tokens.css`](app/tokens.css).** No component writes a literal hex, px or ms value.
- **Three registers, one substrate.** Synoptic for the map, signage for console chrome, register for the ledger and portal. One register dominates per surface, never two on a screen.
- **Four hues for the whole product**, named for what they mean. A hue may never be borrowed for decoration, a button, a link, or a chart series.
- **Only three things may move**: posture escalation, an open and unactioned human gate, a live write landing. Anything else that animates is a bug.

No Tailwind and no component library — the substrate is plain custom properties, which is the version of the "don't ship framework defaults" rule with nothing to get wrong.

## What is here

`/` is a specimen sheet, not product UI: the type settings for all three registers, the meaning vocabulary in both grounds, the scales, the mark, and the transition line at its three levels of detail. It exists so the substrate can be looked at directly and caught drifting, and so the first thing deployed from `main` is an honest artefact rather than a placeholder pretending to be a console.

Nothing on it is wired to the API yet. The API is not deployed.
