# infra

Local development uses a Neon development branch. There is no Docker Compose or
local Postgres; see the environment runbook for why the PostGIS + pgvector
extension pair makes a real Neon branch the safer parity target.

Production is managed hosting:

- **Render** — `api` web service and `api` background worker. Configured in
  [`render.yaml`](render.yaml). The Blueprint uses Render's `ohio` region to sit
  next to Neon in AWS `us-east-2`, paid Starter instances that do not idle-spin
  down, and deploys `main` only after its GitHub checks pass. Render terminates
  TLS, so WhatsApp webhooks work without a reverse proxy.
- **Neon** — serverless Postgres 18 with PostGIS and pgvector, region `us-east-2`. Provisioned; connection string in `.env`.
- **Vercel** — the Next.js EOC console and public transparency portal.

Render and Vercel deploy from `main`. CI lives in `.github/workflows`.

Full setup runbook, including the Twilio webhook configuration and the asyncpg `sslmode` gotcha: [`docs/engineering/lighthouse-environment-setup.md`](../docs/engineering/lighthouse-environment-setup.md).

## First deploy

In Render, create a Blueprint from
`SammarieoBrown/project-lighthouse`, branch `main`, with Blueprint path
`infra/render.yaml`. Supply these four web-service values when prompted:

- `DATABASE_URL`: the **direct, production-branch** Neon URI. Do not paste the
  repo-root `.env` value; that value is deliberately the development branch.
- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_WHATSAPP_FROM`

The Blueprint binds live intake to `INTAKE_HAZARD_EXTERNAL_REF=al132025`. That
is the exact external reference of the single open production event (Hurricane
Melissa) verified before this config was written. Change it deliberately when
the active demo/event changes; the comparison is case-sensitive.

The worker inherits `DATABASE_URL` from the web service, so the credential has
one source of truth. Alembic runs once as the paid web service's pre-deploy
command over that direct connection. Neon recommends a direct connection for
schema migrations; switch application traffic to a separate pooled setting
only after the app supports separate migration and runtime URLs.

After the Blueprint is healthy, verify these exact Render-assigned routes (the
expected service name is shown; use the hostname Render actually assigns):

```text
https://project-lighthouse-api.onrender.com/health
https://project-lighthouse-api.onrender.com/webhooks/twilio/whatsapp
https://project-lighthouse-api.onrender.com/webhooks/twilio/status
```

Set Vercel's server-only `LIGHTHOUSE_API_URL` to the same HTTPS origin for the
Production environment. Leave Preview unset unless it points to an isolated
non-production API and database. Never rename it to `NEXT_PUBLIC_*`; the console
proxies authenticated operator requests through `/api/lighthouse/*` specifically
to keep credentials out of browser bundles.

Do not repoint Twilio merely because `/health` is green. First verify that the
deployed build rejects a missing or invalid `X-Twilio-Signature`, that
`PUBLIC_BASE_URL` exactly matches Render's HTTPS origin, and that webhook retry
idempotency is in place. Then update the sandbox's inbound and status callback
URLs and send one signed canary message.

Before demo day, keep Neon warm or disable scale-to-zero on an eligible Neon
plan. A paid Render service stays up, but it does not by itself guarantee that
the external database is warm.
