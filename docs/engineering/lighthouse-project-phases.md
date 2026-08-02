# Lighthouse: Project Phases

**Built solo by Sammarieo Brown.** Every workstream below is mine. The lane names — data spine, agents, console — are kept because they describe genuinely different kinds of work with different failure modes, not because different people do them. Any reader who finds another name attached to a task in an older document should read it as "someone has to do this," and that someone is me.

Five phases. Phase 0 starts now. Phases 1 to 3 are the buildathon. Phase 4 is what happens after.

Every phase has an exit criterion that is testable, not a feeling. If the exit criterion does not pass, the phase is not done and the next one starts anyway with a known debt.

**What being solo changes.** These phases were laid out for three people working parallel lanes. One person cannot run three lanes at once, so the lanes become a sequence and the cut list stops being a contingency and starts being a plan. The contract freeze is *more* valuable, not less: it now protects against me contradicting myself across a two-week gap rather than against three people colliding. Capacity is the live risk in this document, and it is tracked at the end of each phase rather than discovered during rehearsal week.

---

## Phase 0: Foundations

**When:** Now through day 2. Some items start today.
**Goal:** Nobody can block anybody after this phase ends.

### Start immediately, outside the code

These have external latency and cannot be compressed later.

1. ~~**Twilio WhatsApp sandbox.**~~ **Done (July 31).** Trial account live, sandbox number `+1 415 523 8886`, join code `join bill-flies`, one Jamaican participant joined. Inbound webhook still points at Twilio's stock demo responder and must be repointed at Render. Three sandbox constraints change how the demo is sequenced — read [environment setup §2.3](lighthouse-environment-setup.md) before writing the run sheet.
2. **Meta WhatsApp Cloud API.** Register the app, get the test number, add team phones as test recipients. Start business verification in parallel as a bonus track. Assume it does not land.
3. **Hosting + domain.** Render for the API (`web` service + `worker` background service), Neon for Postgres, Vercel for the Next.js console and public portal; DNS pointed at both. Neon is already provisioned (Postgres 18, us-east-2, empty schema) — connection string is in `.env`; keep Render in a US East region to sit next to it. Both platforms terminate TLS for us, so there is no Caddy and no VM to babysit — WhatsApp webhooks need HTTPS and this gets it on day one. Still on the critical path because the webhook URL has to be stable before Meta and Twilio are configured against it.
4. **Anthropic API keys, R2 bucket, H200 access confirmed.**
5. **Patois eval recording kickoff.** Send the utterance list to family and friends. Audio takes days to collect because it depends on other people. Start before you need it.

### ~~The contract freeze~~ — **Done (Aug 1).**

The single highest-leverage work in the project. All four artifacts are committed and frozen in `packages/contracts` and `apps/api/alembic/versions/0001_initial.py`. Migration applied to Neon: 23 tables.

- `schema.sql` / Alembic initial migration: StormFile, HazardEvent, Claim, Evidence, Verification, Allocation, Disbursement, LedgerEntry, Approval
- **Transition table**: every legal state change, what triggers it, which agent may perform it, whether a human signature is required
- **Pydantic contracts**: input and output model for all eight agents. These double as the JSON schema for structured LLM output, so the contract and the prompt cannot drift
- **Event catalogue**: the event names agents emit and consume (`hazard.posture_changed`, `claim.created`, `claim.verified`, `allocation.approved`, `disbursement.confirmed`)

### Repo and skeleton

Monorepo. Two Python deployables (`web`, `worker`) on Render, plus the Next.js console on Vercel. Neon Postgres 18 with PostGIS and pgvector — the initial migration must `CREATE EXTENSION` for both, since neither is installed on a fresh Neon database. GitHub Actions runs CI, and both platforms deploy from the repo on push to `main`.

**No Docker Compose (decided Aug 2).** Local development and CI both run against Neon branches rather than a local Postgres container. The reason is narrow and practical: no stock image ships PostGIS *and* pgvector together, so Compose would mean maintaining a custom image whose extension versions drift from the ones Neon actually runs — and a local database that is subtly not the production database is worse than no local database at all. Branches are copy-on-write, so a dev branch costs almost nothing and is byte-identical to prod. CI creates an ephemeral branch per run and deletes it after.

**The cost, stated plainly:** development now requires a network connection. That is an uncomfortable trade for a product whose entire argument is functioning when the network is gone. It is acceptable only because the offline requirement lives in the *console* (service worker, IndexedDB write queue, sync on reconnect) rather than in the API's development loop — the API is never the thing running in a blacked-out EOC. If this starts costing real time, the fallback is a custom Compose image and this decision gets reversed.

Job queue is Postgres `SKIP LOCKED`, no Redis (decided July 31 — PRD §11.6). Jobs enqueue in the same transaction as the state transition they follow from, so no Storm File can change state with its next agent job silently lost. Workers wake on `LISTEN/NOTIFY`.

### Exit criteria — **1 of 5**

- [ ] A clean clone reaches a working stack in one documented command, against its own Neon branch
- [x] **An integration test drives one synthetic StormFile through all five states and the ledger hash chain validates.** Done Aug 1. `tests/test_transitions.py::test_storm_file_walks_all_five_states_and_chain_validates`; 11 tests green, re-verified Aug 2.
- [ ] A WhatsApp message from my phone lands in a logged webhook handler on the deployed Render service over HTTPS
- [ ] The console is deployed on Vercel against the Render API, from `main`
- [ ] CI is green on `main`

The last criterion was "all three developers have pushed code and CI is green." Solo, the parallel-work half of it is moot; what survives is that CI actually runs and actually passes, which is the part that was load-bearing anyway.

**Outcome:** A skeleton that does nothing useful, wired end to end, with contracts frozen. The frozen contracts now buy consistency across time rather than across people — the console I write in week 3 talks to the schema I froze in week 0 without my having to remember it.

---

## Phase 1: The Spine

**When:** Week 1.
**Goal:** Real NOAA data drives real risk scores over a real registry, and every state change is recorded.

### Workstreams

**Data spine.** NHC ingestion workers: advisories, forecast cone, **wind speed probabilities** (the 34/50/64 knot product, not the cone, since the cone describes the storm center and not the impact area), wind field radii, watches and warnings. Parse the shapefile and KML feeds into PostGIS. Build the exposure layer: population grid, building footprints, elevation, joined to registered households. **Commit Melissa's full advisory history to `data/replay/cache/`** with a `fetch_advisories.py` that regenerates it and a checksum manifest — the cache is versioned, not gitignored, because a laptop and a demo machine each fetching their own copy is how a "deterministic" replay stops being deterministic. ~~Ledger implementation with hash chaining and a `verify_chain()` routine.~~ **Done Aug 1** — `apps/api/app/ledger.py`, with the database refusing updates and deletes to ledger rows and four tests covering chain linkage, immutability, tamper detection, and key-order independence.

**Agents.** Forecast Sentinel: poll feeds, compute national posture (Quiet, Watch, Ready, Act), emit `hazard.posture_changed`. Risk Mapper: hazard times exposure produces a RiskAssessment per StormFile. Version 1 of the impact function is a transparent vulnerability lookup table keyed on wind probability band by roof type, not a trained model. Document it as such. In parallel, build the Patois eval set: roughly 100 utterances, hand-labeled with both correct transcript and correct structured extraction, held out and never trained on.

**Console.** Read [the design rules](../design/lighthouse-design-rules.md) before the first component; the token file is committed before the second. Next.js shell with auth roles (Director, Clerk, Finance, Auditor). MapLibre map rendering the cone, wind probability bands, and household dots colored by risk. Replay controller UI with play, pause, speed, and jump to timestamp. The T2R counter component, wired to zero.

### Non-negotiable this week

**The replay seeder.** Roughly 500 synthetic StormFiles distributed across St Elizabeth and Westmoreland by real population density, fixed random seed, plus a driver that walks Melissa's cached advisories through the system at configurable speed. Every subsequent phase demos through this. Building it in week 3 is how demos die.

### Exit criteria

- Replaying Melissa advisory 15 produces RiskAssessments for all 500 households in under 5 seconds
- The console renders the storm, the wind probability bands, and the households, and the replay controller scrubs through the timeline
- Every state transition in the replay appears in the ledger and `verify_chain()` passes
- The Patois eval set exists, is labeled, and has a baseline word error rate measured against stock Whisper

**Outcome:** You can show a hurricane approaching Jamaica and watch parish risk climb over a real registry. Half the pitch is already demoable and no agent has processed a single claim yet.

---

## Phase 2: The Loop

**When:** Week 2.
**Goal:** A voice note becomes a verified, triaged claim on the map.

### Workstreams

**Agents.** Intake Agent on the WhatsApp channel: transcribe with faster-whisper, extract location, damage type, household size, injuries, and medical needs, ask up to three adaptive follow-ups, then submit partial. Safety-of-life keyword bypass pages a human immediately and is built first, not last. Verification Agent with all five signals running **in parallel**: hazard sufficiency from the observed wind field, satellite change detection, neighbour corroboration within 300m, registry structure match, and media integrity via perceptual hash. Returns a confidence score. Triage Agent scores severity and urgency.

Then the measurement that decides where GPU time goes: run the eval set, and split the error between transcription and extraction. Fine-tune Whisper with LoRA on the H200 **only if transcription is the bottleneck**. If extraction is the bottleneck, it is a prompting problem and costs an afternoon rather than a week.

**Infra.** WhatsApp webhook receiver and sender, media download and R2 storage, queue plumbing, and burst handling. Observed hazard ingestion (best track, actual wind field, rainfall) since verification depends on what actually happened rather than what was forecast. Pre-cache Sentinel tiles for the replay area so verification is a lookup and not a fetch at demo time.

**Console.** Live needs map with severity layers. Triage queue. **Verification review queue**, which is the screen that proves human-in-the-loop to judges: the claim, the agent's confidence, the five signals with their individual scores, the evidence bundle, and approve or reject buttons. Approvals UI scaffolding for the Director role.

**Logistics Agent** (if time; the UI can ship stubbed against seeded data and be filled in later): seed warehouse stock, match verified needs to items, propose an allocation plan.

### Exit criteria

- A Patois voice note sent from a phone becomes a structured claim in the database in under 15 seconds
- Verification returns a confidence score with all five signals populated and visible
- High-confidence claims auto-verify and appear on the console map; low-confidence claims land in the review queue and a human can approve one through the UI
- The eval report exists: baseline versus current word error rate, plus field-level extraction accuracy
- Fifty concurrent conversations do not break the gateway (validate OpenClaw here, drop to Cloud API directly if it strains)

**Outcome:** The emotional core of the demo works. Someone speaks, and thirty seconds later an emergency operations center sees a verified need. This is the moment judges will remember, so it exists a full week before demo day.

---

## Phase 3: The Proof

**When:** Week 3.
**Goal:** Money moves under human signature, the ledger proves it, and the whole thing runs as one rehearsed story.

### Workstreams

**Console and portal.** Approval gates wired for real: Director approves alert cascades and allocation plans, Finance Officer signs disbursement batches. No StormFile reaches SETTLED without a signature row. Public ledger portal showing aggregate flows by parish and need category, with no named beneficiaries. T2R counter reading live from the data (claim filed → first confirmation). **Donation portal page** (DON-01, simulated processor) and **public pool balances** (DON-02), plus the **donor journey view** (DON-04): received → pooled → allocated → disbursed → confirmed. The donor journey is the closing beat of Act 3, so it is a demo dependency, not a nice-to-have.

**Data spine and hardening.** Disbursement execution (simulated channels for the demo: bank, mobile money, voucher, goods), confirmation handling, and Ledger Agent reconciliation with duplicate detection across payers. **Donation ledger plumbing** (DON-02/03): donations as ledger entries, pools as a selectable payer source in allocation plans, visible draw-down. **FNOL packet generation** (INS-01): JSON + one PDF template assembled from the Storm File, observed hazard at the point, and the verification bundle — all of it data that already exists by week 3, so this is a serializer, not a subsystem. Demo hardening: everything cached locally, zero external API calls during the replay, fixed seeds, and a documented recovery path if a step fails mid-demo.

**Agents.** Thin anticipatory action flow: pre-season registration on WhatsApp, and an auto-generated pre-landfall list of vulnerable households when posture crosses Ready (Director-only view). **Payer routing** (RTE-01/02): the post-verification insurance question in the intake agent, the routing decision as an explicit ledger event with the consent snapshot, and the four routing outcomes (GOV_RELIEF / INSURER / BOTH / DONOR_POOL). Finalize the Patois eval chart as a slide-ready artifact. Tune verification thresholds against the replay and record every threshold change as a ledger entry.

**Why this section grew.** PRD §6.8 (routing), §6.9 (FNOL) and §6.12 (donations) are P0 and appear in the buildathon acceptance test at steps 4, 6 and 8 — but until July 31 they appeared nowhere in this phase plan at all. They are named here so I do not discover them during rehearsal week. The insurer portal and portfolio heatmap (INS-02/03) stay P1 and are explicitly **not** in the demo.

### Rehearsal, from day 4 of this week

Run the full three-act demo end to end at least six times, including once on the venue network if you can get on it, and once fully offline to prove it survives a dead connection. Prepare the fallback pre-recorded voice note. Time every act.

### Cut list, in this order

If you are behind, cut in exactly this sequence and do not improvise a new order under pressure:

1. Public portal styling (keep the data, lose the polish)
2. Anticipatory registration flow
3. Satellite change signal (drop to four verification signals and say so honestly)
4. Logistics routing (keep needs-to-stock matching, drop route optimization)

### Exit criteria

- The full replay runs unattended start to finish without a manual intervention
- Allocation approved, disbursement signed, StormFile reaches SETTLED, and the public ledger shows the flow
- The T2R counter reads a real computed number from the replay data, measured **claim filed → first confirmation**
- A routed insured claim produces an FNOL packet (JSON + PDF) that opens cleanly on stage
- A simulated donation lands on the public ledger, funds an allocation, and the donor journey view traces it to a delivered household
- A phone that has never touched the system can join the sandbox and send a voice note that completes the loop
- `verify_chain()` passes over the entire replay ledger

**Outcome:** The three-act demo. Act 1, posture rises and alerts cascade at T minus 5 days. Act 2, a judge sends a live voice note and watches it become a verified claim with a matched allocation. Act 3, the ledger shows every dollar and the T2R counter reads hours instead of months.

---

## Phase 4: After the Buildathon

**When:** Post-event, contingent on traction.

**4a, pilot readiness.** Replace simulated disbursement with a real payment rail. Formalize data protection: consent flows, retention policy, role audit. Approach one parish council and one NGO partner rather than starting at the national level, since they move faster and give you real registrations.

**4b, first real registry.** Run a registration campaign before the next season through community events, radio, and church networks. Target a few thousand real households in one parish. This is the moment the registry stops being synthetic and starts being the asset.

**4c, the learning loop closes.** After the first real event, you hold predicted impact, observed hazard, and verified damage at household resolution for the same set of households. Refit the vulnerability curves on your own ground truth. This is the point where the impact model stops being a lookup table and starts being defensible intellectual property, and it is the first thing an insurer or catastrophe bond structurer will pay for.

---

## Standing rules across all phases

- Agents propose, humans dispose, the ledger remembers. Any code path that moves money without an `approved_by` is a bug.
- Every agent output is stored raw, including the ones humans override. That is the eval set for the next season.
- No real household data for the entire buildathon. Synthetic only, until a data sharing agreement exists.
- No personally identifying information in logs. Phone numbers hashed everywhere except the StormFile row.
- The replay seeder is the shared heartbeat. If it breaks, fixing it outranks whatever else you were doing.
