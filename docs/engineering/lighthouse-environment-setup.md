# Lighthouse: Environment & Integrations Setup

Phase 0 runbook. What is provisioned, how it is configured, and the constraints that follow from those choices — several of which change how the demo has to be run, so read §2.3 before you plan the stage sequence.

Credentials live in `.env` (gitignored). `.env.example` is the committed template; if you add a variable, add a placeholder there in the same commit.

Last verified: July 31, 2026.

---

## 1. Database — Neon serverless Postgres

**Status: provisioned and reachable. Schema is empty.**

| | |
|---|---|
| Provider | Neon (serverless Postgres) |
| Region | `us-east-2` — AWS Ohio |
| Version | **PostgreSQL 18.4** |
| Database / role | `neondb` / `neondb_owner` |
| Endpoint | `ep-ancient-queen-ayp4srco.c-5.us-east-2.aws.neon.tech` |
| Connection string | `DATABASE_URL` in `.env` |

Verified on the live instance: `public` schema has **0 tables**, and the only installed extension is `plpgsql`. Clean slate for the Phase 0 migration.

### Extensions

Both required extensions are **available but not installed**. The initial Alembic migration must create them:

```sql
CREATE EXTENSION IF NOT EXISTS postgis;   -- 3.6.0 available
CREATE EXTENSION IF NOT EXISTS vector;    -- 0.8.1 available (pgvector)
```

`pg_trgm` (1.6) and `uuid-ossp` (1.1) are also available if we want them.

### Three things that will bite

**1. `asyncpg` rejects `sslmode`.** The connection string Neon hands you is libpq-flavoured. If you drive it with SQLAlchemy async or asyncpg directly, `?sslmode=require` raises `TypeError: connect() got an unexpected keyword argument 'sslmode'`. Use `?ssl=require` instead — `DATABASE_URL_ASYNC` in `.env` is already in that form. Keep both: Alembic runs sync, the app runs async.

**2. Direct connections are limited; there is a pooled endpoint.** The string in `.env` is the direct endpoint. The worker will open a connection per agent job, and if we end up using Postgres `SKIP LOCKED` as the job queue (still an open decision — PRD §11), connection count is exactly the thing that will strain. The pooled endpoint is the same host with `-pooler` appended to the endpoint ID; confirm the exact hostname in the Neon console rather than trusting the commented line in `.env`.

**3. Neon autosuspends on idle.** A cold start adds latency to the first query after a quiet period. That is fine in development and unacceptable at 11am on demo day, when the replay's first advisory lands on a sleeping database. Add a keep-warm ping to the week 3 hardening checklist alongside the Render tier change.

### Docs updated for this

Every doc that said "Postgres 16, Render managed" now says Neon 18. Render still runs the API and worker; it no longer runs the database. Keep the Render services in a **US East region** so they sit next to Ohio rather than across the continent from it.

### Rotate this credential

`DATABASE_URL` has been pasted into chat and terminal history. That is acceptable while the registry is synthetic, and it stops being acceptable the moment Phase 4b puts a real household in it. **Rotate the Neon password before the first real registration**, not after. This is the same line the project already draws in its standing rules: synthetic data only until a data-sharing agreement exists.

---

## 2. Twilio — WhatsApp sandbox (demo path)

**Status: configured, one participant joined, webhook still pointing at Twilio's demo endpoint.**

| | |
|---|---|
| Account SID | `TWILIO_ACCOUNT_SID` in `.env` |
| Account type | **Trial** |
| Sandbox number | `+1 415 523 8886` (shared Twilio sandbox, US) |
| Join code | `join bill-flies` |
| Participants | 1 — `whatsapp:+18767901189` |
| Trial SMS sender | `+1 978 783 4764` (US) |

### 2.1 Webhook configuration — needs changing

Inbound webhook currently points at Twilio's stock demo responder:

```
When a message comes in:   https://timberwolf-mastiff-9776.twil.io/demo-reply   POST
Status callback URL:       (empty)                                              POST
```

Both need to point at our Render service once it is deployed. Target shape:

```
When a message comes in:   https://<render-service>.onrender.com/webhooks/twilio/whatsapp   POST
Status callback URL:       https://<render-service>.onrender.com/webhooks/twilio/status     POST
```

The status callback is not optional decoration — **ALT-02 requires per-recipient delivery status**, and delivery status is exactly what that callback carries. Wire it in Phase 0, not week 3.

Twilio posts `application/x-www-form-urlencoded`, not JSON. Signature validation uses the `X-Twilio-Signature` header against the full public URL; if Render sits behind a proxy that rewrites the scheme, validation fails on a URL mismatch and the symptom looks like a credential problem rather than a URL problem.

### 2.2 Sandbox constraints, and which ones hurt

| Constraint | Consequence for us |
|---|---|
| **Participants must re-join every 72 hours** of inactivity | Every phone in the demo — ours and any judge's — must have messaged the sandbox inside the last 3 days. Re-join on the morning of demo day as a matter of routine, not as a fix after it breaks. |
| **24-hour session window** | Outside 24h of a user's last inbound message, WhatsApp permits only pre-approved templates. The sandbox has a small fixed set, and none of them is a hurricane alert in Patois. See §2.3 — this one shapes the demo. |
| **Shared sandbox number** | `+1 415 523 8886` is used by every Twilio developer worldwide. Not brandable, and the "Lighthouse" identity on stage is our UI, not the sender. |
| **International delivery is best-effort** | Twilio's own console warns the sandbox may not reliably deliver internationally. Our users and our demo are Jamaican numbers on a US sandbox number. Highest-risk item on this page. |
| **Trial account prepends text to SMS** | Outbound SMS arrives as `Sent from your Twilio trial account - <body>`. Confirmed in the API response. Our SMS fallback tier (ALT-02, INT-06) will show this on stage until the account is upgraded. |
| **Trial account restricts SMS recipients** | Trial can only send SMS to verified numbers. Verify every number that will receive one during the demo, in advance. |

Media works: inbound voice notes, photos and documents all reach the webhook as `MediaUrl0..N`, which is what the Intake Agent needs. That part of the sandbox is not a limitation.

### 2.3 The 24-hour window changes the demo run sheet

This is the operationally important finding on this page.

Act 1 sends an **alert cascade** (ALT-01) — free-form Patois text with shelter info, delivered before landfall. Act 2 has a judge send a **voice note** and watch it become a verified claim.

But a judge's phone has never messaged us before the demo starts. At Act 1 time it is outside the 24-hour window, so a free-form alert to that phone **will not deliver** — it would need an approved template, and we do not have one that says what the alert says.

**The fix is a sequencing change, not an engineering one.** The judge must send `join bill-flies` *before Act 1*, not before Act 2. That single message does two things at once: it enrolls them as a sandbox participant, and it opens the 24-hour window that makes the Act 1 alert deliverable as ordinary text. Fold it into the opening beat — "send this code and you're in the registry" is a better piece of theatre than a mid-demo setup step anyway.

Test this end to end before rehearsal week. It is the kind of thing that works on every team phone (all long since joined) and fails on the one phone that matters.

### 2.4 Mitigations for the international-delivery risk

1. Test from Jamaican numbers repeatedly and at different times of day. One participant is already a `+1876` number — use it as the canary, not as proof.
2. Keep the pre-recorded voice note fallback (PRD RPL-02) loaded and one keypress away.
3. Prioritise Meta Cloud API business verification (§3) — it removes the shared-number and international-reliability problems together.
4. Decide a stage rule now: if the live voice note has not arrived within N seconds, the presenter moves to the fallback without commentary. Agree N in rehearsal. Waiting silently on stage for a message that is not coming is the worst version of this.

---

## 3. Meta WhatsApp Cloud API (production path)

Register the app, obtain the test number, add team phones as test recipients, and start business verification in parallel. Env vars are stubbed in `.env`.

Plan as though verification does not land before demo day. It is the bonus track; Twilio is the demo path. Both providers sit behind the same internal messaging interface so the switch is a config change, not a rewrite — build that seam in Phase 0 while there is only one implementation to shape it around.

---

## 4. Still to provision

- [ ] `TWILIO_AUTH_TOKEN` into `.env` (never into a doc or a commit)
- [ ] Render services — API `web` + `worker`, US East region
- [ ] Vercel project for the console
- [ ] Repoint both Twilio webhooks at the Render service (§2.1)
- [ ] Anthropic API key
- [ ] Cloudflare R2 bucket + credentials
- [ ] H200 access confirmed
- [ ] Patois eval utterance list sent out — longest external lead time, start first

---

## 5. Demo-day checklist (seed for week 3)

- [ ] All demo phones re-joined the sandbox within 72h
- [ ] Judge's phone joins **before Act 1** (§2.3)
- [ ] Render services on a tier that does not sleep
- [ ] Neon warmed, not autosuspended
- [ ] SMS recipient numbers verified on the trial account
- [ ] Pre-recorded voice note fallback loaded and tested
- [ ] Fallback trigger threshold agreed and rehearsed
- [ ] Full replay runs with zero external calls
