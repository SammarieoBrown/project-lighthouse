# Lighthouse: Build Spec

For Raheem, Sammarieo, Matthew. Three week buildathon scope. Track 04.

## What we are building

A pipeline that turns a WhatsApp voice note from a hurricane victim into a verified, funded, audited relief payment. One record per household (the Storm File) moves through five states. Agents move it. Humans sign anything that costs money.

```
REGISTERED -> AT-RISK -> AFFECTED -> VERIFIED -> SETTLED
```

The report becomes the claim becomes the payment. That closed loop is the whole product.

## Core model

```
StormFile        id, phone, name, point(geog), parish, structure{roof,walls,floors},
                 people{total,children,elderly,medical[]}, vuln_score, state, consent_at

HazardEvent      id, name, track[], wind_field snapshots, rainfall grid, posture

Claim            id, storm_file_id, event_id, reported_needs[], damage_type,
                 transcript, lang, point(geog), created_at

Evidence         id, claim_id, kind(audio|photo|satellite|neighbour|registry), payload, hash

Verification     id, claim_id, signals{}, confidence float, verdict, actor(agent|human)

Allocation       id, claim_id, resource(cash|item), amount, payer, approved_by, approved_at

Disbursement     id, allocation_id, channel, status, confirmed_at

LedgerEntry      id, prev_hash, hash, actor, action, subject_type, subject_id, payload, ts
```

Every state transition writes a LedgerEntry. The ledger is append only and hash chained. Nothing mutates in place except `StormFile.state`, and that change is itself a ledger entry.

## Services

Five deployables. Keep them boring.

| Service | Does |
|---|---|
| `ingest` | Cron workers pulling NHC, NDBC, Open-Meteo, CHIRPS, Sentinel tiles. Writes HazardEvent. |
| `orchestrator` | The state machine. Consumes events, dispatches agent jobs, enforces transition rules and human gates. |
| `agents` | Agent workers. Each subscribes to job types off Redis. Stateless, horizontally scalable. |
| `whatsapp` | Webhook receiver and sender. OpenClaw intake loop lives here. |
| `console` | Next.js. EOC map + queues + approvals, and the public ledger portal. |

Postgres + PostGIS is the single source of truth. Redis for queues and burst buffering. No Kafka, no microservice mesh, we have three weeks.

## Agent contracts

Every agent is a function: job in, structured result out, ledger entry written by the orchestrator (not the agent). Agents never write money rows directly.

```
ForecastSentinel   in: feed poll            out: {posture, affected_geo[]}       autonomous
RiskMapper         in: HazardEvent          out: RiskAssessment[] per StormFile   autonomous
AlertAgent         in: RiskAssessment[]     out: draft cascade                    PROPOSE ONLY
IntakeAgent        in: WhatsApp message     out: Claim + Evidence[]               autonomous
VerificationAgent  in: Claim                out: Verification{signals,confidence} gated
TriageAgent        in: Verification         out: {severity, rank}                 autonomous
LogisticsAgent     in: verified claims[]    out: draft allocation plan            PROPOSE ONLY
LedgerAgent        in: disbursement events  out: reconciliation + anomaly flags   autonomous
```

Three human gates, implemented as a single `approvals` table with a role check:

1. EOC Director approves alert cascades before send.
2. EOC Director approves allocation plans before stock moves.
3. Finance Officer signs disbursement batches before `SETTLED`.

## Intake flow

WhatsApp webhook -> queue -> IntakeAgent loop:

1. If audio, transcribe. Patois model first, fallback to Whisper base, keep both outputs.
2. Extract: location, damage type, household size, injuries, medical needs.
3. Ask for whatever is missing. Max three follow ups, then submit partial.
4. Geocode. Match to existing StormFile by phone, else create one in `AFFECTED`.
5. Write Claim + Evidence. Emit `claim.created`.

Safety of life keywords (trapped, injured, dying, no water for X days) bypass everything and page a human immediately. Build this on day one, not week three.

## Verification signals

VerificationAgent scores each claim on independent evidence, returns confidence 0 to 1:

- `hazard_sufficiency` did damaging wind or rain actually reach that point, from the wind field
- `satellite_change` Sentinel before/after tile delta at the location
- `neighbour_corroboration` count of independent claims within 300m
- `registry_match` does reported damage fit the registered structure profile
- `media_integrity` perceptual hash against all prior photos, catches reuse

Thresholds: `>= 0.85` auto verify, `0.5 to 0.85` human review queue, `< 0.5` flag. Tunable per event, and every threshold change is a ledger entry.

## Stack

Python (FastAPI) for `ingest`, `agents`, `orchestrator`. TypeScript (Next.js) for `console`. Postgres 16 + PostGIS + pgvector. Redis. Docker Compose local, GitHub Actions to a single cloud VM. Whisper fine tune on the H200 for Patois. Claude API for verification reasoning and allocation planning, small local models on the intake path.

Offline first console: service worker, IndexedDB write queue, sync on reconnect. The EOC loses power and internet in exactly the conditions we exist for.

## Three weeks

**Week 1, spine.** Schema and migrations, state machine with transition tests, ledger with hash chaining, NHC/NDBC ingestion, parish risk dashboard rendering real storms.
Raheem: console shell + map. Sammarieo: DB, ingest, infra, CI. Matthew: Sentinel + RiskMapper.

**Week 2, loop.** WhatsApp webhook live, IntakeAgent end to end with audio, VerificationAgent with all five signals, TriageAgent, live needs map, LogisticsAgent matching against seeded stock.
Matthew: agents + Patois fine tune. Sammarieo: WhatsApp infra + queues. Raheem: EOC console, queues, approvals UI.

**Week 3, proof.** Approval gates wired, disbursement + public ledger view, thin pre season registration flow, Melissa replay mode, T2R counter, demo rehearsal.

Cut list if we are behind, in this order: public portal styling, anticipatory registration, satellite signal (drop to four verification signals), logistics routing (keep matching, drop routes).

## Replay mode

The demo depends on this, so build it in week 1, not week 3. A seeder that walks Melissa's real NHC advisory history through the system at configurable speed, with ~500 synthetic StormFiles distributed across St Elizabeth and Westmoreland by real population density. A judge sends a live voice note mid replay and it lands in the same pipeline as the synthetic ones.

## Rules

- No PII in logs. Phone numbers hashed everywhere except the StormFile row.
- Every agent output is stored raw, including the ones we override. That is our eval set.
- Agents propose, humans dispose, ledger remembers. If you are writing code that moves money without an `approved_by`, stop.
- Synthetic data only for the whole buildathon. No real households until we have a data sharing agreement.

## Metric

Time to Relief: median hours from `VERIFIED` to `SETTLED`. Put it on the console in a big number from week 1. It is the number we demo and the number we are judged on.
