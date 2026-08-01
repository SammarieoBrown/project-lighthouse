# Lighthouse: Solution Specification

**The resilience operating system for the Caribbean. The rail between disaster capital and households.**

Team Project Lighthouse: Raheem Wilson (Product & Software Engineer, Norus / Intelibus) · Sammarieo Brown (Product & Software Engineer, banking and payments) · Matthew Stone (Product & AI Engineer, Founder & President, Jamaica AI Association)

Track 04: Climate Risk & Disaster Coordination | FutureCaribbean Buildathon 2026

Version 1.0, July 24, 2026. This is the build reference: what the final product is, how it works, and who it serves. The pitch narrative lives in the concept brief; the agent diagram ships with the application.

---

## 1. Executive summary

When Hurricane Melissa hit Jamaica in October 2025, the world's money arrived at record speed: a US$150M catastrophe bond paid out in about ten days, the IMF approved US$415M, and the public donated J$1.44 billion. Four months later the Auditor General found that 1.8% of the donated cash had reached households. The failure was not capital and it was not corruption by default. It was the absence of infrastructure: nothing connects the forecast to the household, the household to a verified damage claim, the claim to an allocation, or the allocation to an audited payment.

Lighthouse is that missing rail. It is a national platform built around one atomic object, the Household Storm File, a single record per household that moves through five states: **registered → at-risk → affected → verified → settled**. AI agents move files between states (forecast monitoring, WhatsApp intake, evidence-based verification, triage, logistics matching, ledger auditing) while humans hold every gate that moves money. Households interact entirely through WhatsApp, in Jamaican Patois or English, with no app to download. Every allocation and payment lands on an append-only public ledger.

North star metric: **Time to Relief (T2R)**, the median hours from a household filing a claim to first relief in hand. The clock starts when they file, so our own verification time counts against us. Melissa's T2R was months to never. Lighthouse targets 72 hours.

## 2. The problem in one sentence

Disaster capital moves at bond speed; relief moves at paper speed; the gap is a cognition gap that only agentic AI can close affordably for a small state: no civil service can hold 120,000 conversations, verify each claim against evidence, and match verified need to money within days.

## 3. The core object: the Household Storm File

Everything in Lighthouse is machinery around one record. A Storm File contains:

- **Identity**: household head name, contact (WhatsApp number), optional national ID reference, consent records
- **Location**: geocoded point plus parish and community, captured at registration and refreshed at claim time
- **Structure profile**: dwelling type, roof material, floors, approximate age (drives vulnerability scoring and later verification)
- **People**: household size, children, elderly, disabilities, chronic medical needs (insulin, dialysis, oxygen)
- **Vulnerability score**: computed from structure, exposure (elevation, flood zone, coastal proximity), and social factors
- **Claims history**: every damage report with evidence, verification verdicts, and severity
- **Settlement history**: every allocation and disbursement the household has received, across all payers

### The five states

| State | Meaning | Entered when | Moved by |
|---|---|---|---|
| REGISTERED | Household exists in the registry with consent | Pre-season WhatsApp registration | Intake Agent |
| AT-RISK | An approaching hazard is forecast to affect this household | Forecast posture crosses threshold for their location | Forecast Sentinel + Risk Mapper |
| AFFECTED | The household has reported damage or need | Post-event WhatsApp report (voice, photo, text) | Intake Agent |
| VERIFIED | The claim is confirmed against independent evidence | Verification confidence above threshold, or human review approval | Verification Agent / Review Queue |
| SETTLED | Relief has been delivered and recorded | Disbursement executed and confirmed | Ledger Agent, after Finance Officer signature |

The governing rule of the whole system: **agents do the cognition, the state machine constrains their authority, and humans hold every gate that moves money.**

## 4. The agent team

Eight specialized agents work the state machine. Autonomy is graduated by consequence.

1. **Forecast Sentinel** (fully autonomous). Watches NOAA NHC advisories, NDBC buoys, ECMWF/GFS ensembles, and CHIRPS rainfall around the clock. Maintains a national posture level (Quiet, Watch, Ready, Act) and escalates to humans when posture changes.
2. **Risk Mapper** (fully autonomous). Converts forecast tracks and intensities into parish and community level impact previews using elevation, population grids, building footprints, and registry structure profiles. Flips exposed Storm Files to at-risk.
3. **Alert Agent** (propose only). Drafts targeted alert cascades per community, in English and Patois, text and voice note formats, including shelter locations and preparation guidance. Nothing sends until the EOC Director approves the cascade.
4. **Intake Agent** (autonomous, tool-using). Runs every WhatsApp conversation: pre-season registration, preparedness questions, and post-storm damage reports. Handles voice notes in Patois via the fine-tuned speech model, requests photos and locations, asks adaptive follow-ups, and writes structured claims. Escalates safety-of-life messages instantly.
5. **Verification Agent** (confidence gated). For each claim, gathers evidence: was the hazard at that location sufficient (wind field, rainfall), does satellite imagery show change, do neighbouring claims corroborate, does the damage match the registered structure profile, is the photo authentic and unduplicated. Emits a confidence score. High confidence auto-verifies; low confidence routes to the human review queue with the evidence bundle attached.
6. **Triage Agent** (autonomous). Scores verified claims for severity and urgency (medical needs first, then habitability, then property), producing the priority ordering the EOC sees.
7. **Logistics Agent** (propose only). Matches verified needs against warehouse stock and shelter capacity, proposes allocation plans and distribution run sheets with routing. The EOC Director approves plans before anything moves.
8. **Ledger Agent** (autonomous audit). Records every allocation, disbursement, and delivery confirmation on the append-only ledger, reconciles against bank and mobile money confirmations, detects duplicates and anomalies, and flags them to humans. It is the auditor that never sleeps.

### Human roles

- **EOC Director**: approves alert cascades and allocation plans; owns the operational picture.
- **Review clerks**: adjudicate the low-confidence claim queue (approve to verified, reject to closed) with the agent's evidence bundle in front of them.
- **Finance Officer**: signs every disbursement batch. No Storm File reaches settled without this signature.
- **Auditor / oversight**: read-only access to the full ledger, in real time, without asking anyone.

## 5. How it works: the three phases

### Phase 1: Foresight (before the storm)

Lighthouse does not out-forecast NOAA; it makes global forecasts actionable locally. In the quiet season, the platform runs registration campaigns (community events, radio, church networks, parish councils) driving households to register on WhatsApp in under three minutes. Registration seeds the registry, computes vulnerability scores, and produces the planning layer government uses for stockpiling, shelter readiness, and drills.

When the Sentinel raises posture on an approaching system, the Risk Mapper produces impact previews (which parishes, how many registered households, what severity band), files flip to at-risk, the Alert Agent drafts cascades, the Director approves, and targeted warnings go out with shelter locations and preparation steps. The anticipatory action list generates here: pre-verified vulnerable households (elderly, medical needs, weak structures in the projected path) who should receive help before landfall.

### Phase 2: Respond (during and after)

After the storm, anyone can message Lighthouse on WhatsApp, registered or not. A typical exchange:

> **Household** (voice note): "Mi deh a Newmarket. Di whole roof gone offa di house and mi madda diabetic, har insulin spoil."
>
> **Lighthouse**: "Mi sorry fi hear dat. Mi have yuh location as Newmarket, St Elizabeth. Quick ting: anybody hurt right now?"
>
> **Household**: "No, everybody safe."
>
> **Lighthouse**: "Ok good. Send mi one photo of di roof if yuh can, and tell mi how much people live a di house."
>
> **Household**: (photo) "Five a wi."
>
> **Lighthouse**: "Thanks. Mi log di claim: roof loss, five people, insulin need flagged URGENT. Yuh claim number is SE-4102. Yuh wi hear back within 24 hours. Stay safe."

Behind that conversation: the Intake Agent transcribed the Patois voice note, geocoded the location, created the claim, and flagged the medical need to triage immediately. The Verification Agent then checked the wind field at Newmarket, pulled satellite change detection, found four corroborating claims within 300 metres, matched the roof type to the registered structure profile, and auto-verified at 0.91 confidence. The Triage Agent ranked it urgent (medical). The EOC saw it on the live map minutes after the voice note. The Logistics Agent proposed insulin from the nearest cold-chain point and a tarpaulin from the parish warehouse on run sheet 7; the Director approved the plan.

The EOC console shows: live needs map with severity layers, triage queue, verification review queue, stock levels per warehouse, run sheet status, shelter occupancy, and the T2R counter. It is offline-first: it keeps working through connectivity drops and syncs when the network returns.

### Phase 3: Ledger (recovery)

Every verified claim becomes a settlement obligation. Allocations (cash via bank or mobile money, vouchers, or goods) are batched, signed by the Finance Officer, executed, and confirmed. The Ledger Agent reconciles confirmations and watches for duplicates across payers: if the Red Cross settled a roof grant for claim SE-4102, government sees it before issuing its own, and vice versa. The public transparency portal shows aggregate flows in real time: how much came in, from whom, how much has moved, to which parishes, for which need categories, with what median T2R. Named beneficiaries are never public; auditors and payers see detail under role-based access.

## 6. Use cases by stakeholder

- **Households**: register in minutes, get hyper-local warnings in their own language, report damage with one voice note, receive help in days with a claim number they can track, never need to stand in a line to be counted.
- **Government EOC (ODPEM)**: a single operational picture; burst processing capacity no staffing plan could provide; defensible, auditable decisions; a T2R number to report publicly.
- **Ministry of Finance**: signature control over every dollar; a real-time answer to "where did the relief money go"; clean reporting to Parliament, the Auditor General, and international lenders whose disbursements increasingly demand exactly this accountability.
- **Auditor General**: continuous read access instead of forensic reconstruction months later. The platform the audit points to instead of writes about.
- **Parish councils and community leaders**: registration drives, community-level dashboards, shelter and distribution coordination.
- **NGOs (Red Cross, United Way, Direct Relief)**: consume the verified needs registry instead of duplicating assessments; disburse through the ledger so their aid is counted and deduplicated; report to their own donors with platform data.
- **Insurers and CCRIF**: pre-storm exposure snapshots; post-storm verified ground truth at household resolution that accelerates claims and validates parametric triggers; over seasons, Caribbean-calibrated vulnerability curves no global cat model has.
- **Diaspora donors**: give to a specific parish or need category and watch it arrive on the public portal. The people wiring money home are exactly the people reading the 1.8% headlines.
- **Utilities and telecoms**: outage-area prioritization from the needs map (roadmap integration).

## 7. Data model (core entities)

- **StormFile** (household): identity, contact, location, structure profile, people, vulnerability score, state, consent
- **HazardEvent**: storm or flood event, track, wind field snapshots, rainfall grids, posture timeline
- **RiskAssessment**: StormFile × HazardEvent, exposure band, predicted impact
- **Claim**: StormFile reference, reported needs and damage, evidence set, transcript, geotag, timestamps
- **Evidence**: voice note, transcription, photos with integrity checks, satellite tiles, corroboration links
- **Verification**: signals, confidence score, verdict, verifier (agent or human), evidence bundle
- **Allocation**: claim reference, resource (cash amount / item), source payer, approval chain
- **Disbursement**: allocation reference, channel (bank, mobile money, voucher, goods), execution status, confirmation
- **LedgerEntry**: append-only record of every state transition and money movement, actor (agent or human), hash-chained for tamper evidence
- **Warehouse / StockItem / RunSheet / Shelter**: logistics primitives

## 8. Architecture and stack

- **Messaging**: WhatsApp Business Cloud API; intake agent built on OpenClaw; Twilio SMS fallback for degraded connectivity
- **Speech**: Whisper-class model fine-tuned on Jamaican Patois voice notes (H200 compute); this model is a durable asset nobody else has
- **Agents**: Claude / GPT class APIs for reasoning stages (verification, allocation planning); small fine-tuned models on the high-volume intake path; every agent runs as a worker consuming state-change events
- **Orchestration**: a typed state machine over Postgres; every transition is an event; agents subscribe to events; human gates are just transitions that require a signature
- **Data**: PostgreSQL 18 + PostGIS (geo) and pgvector (retrieval) on Neon serverless; the job queue is Postgres `SKIP LOCKED` so agent jobs enqueue transactionally with the state transitions that trigger them; append-only ledger tables with hash chaining
- **Geo and hazard ingestion**: workers pulling NHC, NDBC, Open-Meteo (ECMWF/GFS), CHIRPS, Sentinel-1/2 tiles, SRTM, OpenStreetMap, WorldPop
- **Consoles**: Next.js EOC console (offline-first PWA, Mapbox/Leaflet), public transparency portal, admin
- **Burst design**: intake is queue-buffered; claims are processed asynchronously; the system degrades gracefully by queueing, never by dropping; surge capacity is agent workers, which scale horizontally
- **Deployment**: Docker for local parity, GitHub Actions CI, Vercel for the consoles and Render for the API and workers initially, with a deliberate path to in-country or sovereign hosting for data residency — the container boundary is kept clean precisely so a government buyer can move the whole thing onshore without a rewrite

## 9. Verification and anti-fraud (the trust core)

Signals per claim: hazard sufficiency at the location (did damaging winds or rainfall actually occur there), satellite change detection, neighbour corroboration (spatial clustering of independent reports), registry consistency (structure existed and matches damage type), media integrity (duplicate detection, metadata checks), identity and phone history, cross-payer dedupe. Confidence thresholds: auto-verify above ~0.85 with hazard consistency; human review between ~0.5 and 0.85; below that, flagged. Thresholds are tunable per event and audited. Fraud is not a footnote; verification is the product feature that makes a shared ledger trustworthy enough to become the single rail.

## 10. Privacy, security, compliance

Consent-first registration with revocation. Data minimisation. Encryption in transit and at rest. Role-based access with every read and write logged. Pseudonymised analytics and training data. Aggregate-only public reporting. Data-sharing agreements with government owners; alignment with Jamaica's Data Protection Act (2020) and humanitarian data protection standards for vulnerable populations. During the buildathon: synthetic households only, replayed against Melissa's public storm timeline.

## 11. Metrics

- **North star: Time to Relief (T2R)**, median hours from claim filed to first relief in hand. Target 72. Settlement latency (verified → relief confirmed) is tracked separately as the operational sub-metric.
- Percentage of relief value with a complete end-to-end audit trail (target 100%)
- Registration coverage per parish (pre-season)
- Verification precision and recall (measured on replay and synthetic sets); human review queue rate
- Alert reach and acknowledgement rate
- Forecast-to-actual damage calibration error (improves every season; the data product insurers buy)

## 12. What we are deliberately not building

No weather models (we consume NHC and ECMWF). No fleet routing engines (we emit run sheets and integrate). No donation collection (supportjamaica.gov.jm exists; we are the accountability layer it lacks). No insurance underwriting (we sell evidence, not adjudication). We integrate PATH rather than replace it: PATH targets chronic poverty on slow rolls; shock response needs geospatial, structure-aware, claims-capable records.

## 13. Buildathon MVP (three weeks)

- **Week 1, the spine**: Storm File data model, state machine, ledger core; NHC/NDBC ingestion; parish risk dashboard for Jamaica
- **Week 2, the loop**: WhatsApp intake end to end (voice, photo, text, Patois), verification signals v1 (hazard sufficiency + clustering + registry match), triage, live EOC map, logistics matching
- **Week 3, the proof**: ledger views and public portal, thin anticipatory registration, Patois model fine-tune, Melissa replay mode, live T2R counter, demo polish

**Demo, one storm in three acts**: replay Melissa's real timeline. Act 1: posture rises at T-5 days, risk scores climb, alerts cascade, the anticipatory list generates. Act 2: a judge sends a live Patois voice note and watches it become a verified, triaged claim with a matched allocation in about thirty seconds. Act 3: the ledger shows every dollar from donation to disbursement and the T2R counter reads hours, not months. Closing line: after Melissa, the bond paid in ten days and the people waited a year; Lighthouse closes that gap.

## 14. Beyond the buildathon

Season one: pilot with one parish plus one NGO partner in Jamaica, real registrations, tabletop exercise with ODPEM. Year one: national coverage, Ministry of Finance ledger adoption, first insurer data agreement. Year two: anticipatory cash before landfall tied to parametric triggers; second CDEMA state. Year five: the settlement layer for disaster capital across the 19 CDEMA states and the wider small island world, with T2R reported publicly after every event the way central banks report inflation.

## 15. Business model and moats (summary)

One national platform sale per state (single procurement, phased rollout across a season cycle), plus per-disbursement settlement fees from non-government payers, plus data licensing to insurers and CCRIF. Moats, in order: the single-ledger network effect (dedup and audit only work if every payer settles on one registry, so anchor payers force the rest on), the compounding forecast-to-outcome dataset (Caribbean-calibrated vulnerability curves at household resolution), system-of-record embedding in government operations, and the Patois speech model plus registry as near-term differentiators.

## 16. Risks and mitigations

- **Post-storm connectivity**: pre-season registration (we already know who and where people are), SMS fallback, community relay points, offline-first console, store-and-forward queueing
- **Fraud and duplicate claims**: the verification agent and cross-payer dedupe are core product, with human adjudication on the margin
- **Government adoption speed**: buildathon credibility, NGO and parish entry points, and the live audit scandal creating demand for exactly this accountability
- **Sensitive data**: privacy-first design above; synthetic data until agreements exist
- **Model errors in Patois understanding**: confidence-gated, human review queue, and every transcript stored for correction; errors reduce coverage, never money movement, because humans hold the money gates
