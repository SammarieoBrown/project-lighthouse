# infra

Local development uses Docker Compose (Postgres 18 + PostGIS, plus the API `web` and `worker` entrypoints). It exists for parity, not for production.

Production is managed hosting:

- **Render** — `api` web service and `api` background worker. Configured in `render.yaml`. Terminates TLS, so WhatsApp webhooks work without a reverse proxy. Use a **US East region** to sit next to the database.
- **Neon** — serverless Postgres 18 with PostGIS and pgvector, region `us-east-2`. Provisioned; connection string in `.env`.
- **Vercel** — the Next.js EOC console and public transparency portal.

Render and Vercel deploy from `main`. CI lives in `.github/workflows`.

Full setup runbook, including the Twilio webhook configuration and the asyncpg `sslmode` gotcha: [`docs/engineering/lighthouse-environment-setup.md`](../docs/engineering/lighthouse-environment-setup.md).

Before demo day, move the Render services off any tier that sleeps on idle, and keep Neon warm. A cold start in front of judges is a self-inflicted wound.
