# Lighthouse: Product Requirements Document (PRD / SRS)

| | |
|---|---|
| Version | 1.1 |
| Date | July 31, 2026 |
| Authors | Team Project Lighthouse (Raheem Wilson, Sammarieo Brown, Matthew Stone) |
| Status | Agreed — team decisions of July 31, 2026 folded in (see §11) |
| Related docs | Build Spec · Project Phases · Solution Spec · Concept Brief · Interactive Prototype |

**Priorities used throughout:** **P0** = buildathon MVP (3 weeks, must demo). **P1** = pilot season (first real deployment, one parish). **P2** = full product (national / regional). Every requirement carries one.

---

## 1. Product summary

Lighthouse gets help to people fast after a hurricane. Families register on WhatsApp before the season so the system knows who lives where, in what kind of house, and who is vulnerable. When a storm approaches they get targeted warnings in Patois and English. Afterwards, anyone can report damage with a voice note, photo, or text. AI agents verify each report against the storm's actual wind field, satellite imagery, neighbouring reports, and the pre-registered property profile. Verified families are matched to help from any payer on the platform: government relief, their insurance company, or pooled donations. A human must sign before any money moves, and every dollar is tracked on a public append-only ledger from receipt to delivery.

**One claim, many payers, one ledger.** The core loop: *the report becomes the claim becomes the payment.*

## 2. Problem

After Hurricane Melissa (Oct 2025), capital arrived at record speed (US$150M cat bond in ~10 days, US$415M IMF, J$1.44B donations) but 1.8% of donated cash had reached households four months later. The failure is the last mile: no infrastructure connects forecasts to households, households to verified claims, claims to allocations, or allocations to audited payments. No small state's civil service can hold 120,000 conversations, verify each against evidence, and match need to money in days. That cognition gap is what agents close.

## 3. Goals, non-goals, success metrics

### Goals
1. Reduce median Time to Relief (T2R: **claim filed → first relief in hand**) from months to **72 hours**. The clock starts when the household files, not when we finish verifying, so our own verification latency counts against us.
2. 100% of relief value on the platform carries a complete, public, tamper-evident audit trail.
3. One intake serves every payer: government, insurers, donors.
4. Every household interaction works on the phone people already have, in the language they already speak.

### Non-goals (explicitly out of scope)
- We do not run weather models (we consume NHC/ECMWF products).
- We do not adjudicate or price insurance claims (we deliver evidence packets; adjusters decide).
- We do not produce dollar-value damage estimates in v1 (category + severity only).
- We do not hold donated funds ourselves in production (fiscal sponsor holds funds; we are the rails and record).
- We do not replace PATH or any social registry (we integrate).
- We do not build routing/fleet optimization (we emit run sheets).

### Success metrics
| Metric | Target | Priority |
|---|---|---|
| Median T2R (claim filed → first relief confirmed) | ≤ 72h in pilot event | P1 |
| Median settlement latency (verified → first relief confirmed) | ≤ 24h — operational sub-metric, not the headline | P1 |
| Audit trail coverage of platform-disbursed value | 100% | P0 |
| Registration coverage in pilot parish | ≥ 30% of households season 1 | P1 |
| Verification precision (approved claims that are genuine) | ≥ 95% on replay/synthetic | P0 |
| Auto-verify rate (no human needed) | ≥ 70% of claims | P0 |
| Intake completion rate (started → filed claim) | ≥ 85% | P1 |
| Live demo: voice note → verified claim on console | ≤ 30s | P0 |

## 4. Personas

1. **Daphne, 68, householder, Newmarket St Elizabeth.** Feature phone until 2023, now low-end Android, WhatsApp daily, voice notes over typing, Patois first. Diabetic. Zinc roof. No insurance.
2. **Marcus, EOC Director, ODPEM.** Owns the national operational picture during activation. Needs triage he can defend and a picture he can brief the PM from. Accountable for alert decisions.
3. **Alia, Review Clerk (surge staff).** Adjudicates low-confidence claims with evidence in front of her. Not technical.
4. **Mr. Chen, Finance Officer, Ministry of Finance.** Signs disbursements. Answers to the Auditor General. Wants to never again reconstruct payments from paper.
5. **Keisha, Claims Manager, Island Mutual** (fictional carrier; real Jamaican carriers are named only in market sizing, never in demo UI — see §11.2). Wants early, evidenced FNOL (First Notice of Loss) packets and a portfolio damage heatmap while roads are still blocked.
6. **Andre, diaspora donor, Toronto.** Sent money after Melissa; read the 1.8% headlines. Will give again only if he can see it land.
7. **Auditor General's office.** Read-only, real-time, everything.

## 5. System overview (normative)

- **Core object:** the Household **Storm File**, one record per household, five states: `REGISTERED → AT-RISK → AFFECTED → VERIFIED → SETTLED`. A claim may terminate `CLOSED` (rejected/withdrawn).
- **Agents** (8): Forecast Sentinel, Risk Mapper, Alert Agent, Intake Agent, Verification Agent, Triage Agent, Logistics Agent, Ledger Agent. Autonomy is graduated; agents never write money rows.
- **Human gates** (hard requirement): alert cascades, allocation plans, and disbursement batches each require a signature from an authorized role before execution. No Storm File reaches SETTLED without a Finance signature.
- **Ledger:** append-only, hash-chained record of every state transition, allocation, and payment, across all payers.
- **Payers:** Government relief funds, insurers, donation pools. One claim is routed to one or more payers by explicit rules (§6.8).

---

## 6. Functional requirements

### 6.1 Registration & Registry (REG)

- **REG-01 (P0).** The system shall let a household register via WhatsApp conversation in ≤ 3 minutes, capturing: contact number, name of household head, location (shared pin or described location geocoded with confirmation), parish/community, structure profile (roof material, wall material, floors), household size, count of children and elderly, chronic medical needs (free text, coded), and consent. AC: a complete registration produces a Storm File in state REGISTERED with all fields populated; an incomplete one can resume where it left off.
- **REG-02 (P0).** Registration shall work in Patois and English, by voice or text, with the agent detecting language and matching it.
- **REG-03 (P0).** Consent shall be explicit, versioned, and stored with timestamp; the conversation must state what data is collected, who can see it, and how to revoke.
- **REG-04 (P1).** A household shall be able to update or revoke via WhatsApp at any time ("STOP" semantics); revocation freezes the file and excludes it from all non-legal processing within 24h.
- **REG-05 (P1).** Assisted registration: a community agent role can register households in bulk on their behalf (drives, church groups), flagged as assisted with the assistant's ID.
- **REG-06 (P1).** SMS minimal registration: structured SMS (`REG <NAME> <COMMUNITY> <PEOPLE>`) creates a thin Storm File flagged `thin=true` for later enrichment.
- **REG-07 (P0).** Vulnerability score: the system shall compute and store a 0–100 score from structure, exposure (elevation, coast/flood proximity), and household factors (elderly, children, medical). Formula must be documented and inspectable, not a black box.
- **REG-08 (P2).** PATH and national ID linkage via government data-sharing agreement; linkage is additive and never a registration requirement.
- **REG-09 (P0, demo).** Synthetic registry: the replay ships ~500 synthetic Storm Files distributed by real population density across St Elizabeth and Westmoreland. No real personal data anywhere in the buildathon build.

### 6.2 Hazard intelligence (HAZ)

- **HAZ-01 (P0).** Ingest NHC advisories, forecast track and cone, **wind speed probabilities (34/50/64kt)**, wind radii, and watches/warnings on each advisory cycle; parse shapefile/KML into PostGIS. AC: a new advisory is reflected in the hazard layer ≤ 5 min after publication.
- **HAZ-02 (P0).** Ingest NDBC buoy observations and Open-Meteo (ECMWF/GFS) fields for the Jamaica bounding box.
- **HAZ-03 (P0).** Maintain a national **posture**: QUIET, WATCH, READY, ACT, computed by documented rules on wind-speed probabilities and watch/warning polygons. Posture changes are ledger events and notify the Director.
- **HAZ-04 (P0).** Post-event ground truth: ingest observed best track, observed wind field, and rainfall accumulation; store per-point hazard values queryable by location (this feeds verification).
- **HAZ-05 (P1).** Ingest Sentinel-1/2 tiles for the affected area post-event; compute simple before/after change rasters. (P0 in demo as pre-cached tiles for the replay area.)
- **HAZ-06 (P0).** Melissa replay dataset: full advisory history cached locally; the system must run the entire lifecycle against it with zero external calls.
- **HAZ-07 (P2).** Multi-hazard: extend posture and hazard layers to flood (rainfall thresholds) and earthquake (USGS feeds).

### 6.3 Impact prediction (IMP)

- **IMP-01 (P0).** For each advisory, produce a RiskAssessment per registered Storm File: P(34/50/64kt at location) joined to structure profile → predicted damage band (NONE/MINOR/MAJOR/DESTROYED) and confidence. v1 is a transparent parametric lookup (wind band × roof type), documented as such.
- **IMP-02 (P0).** Aggregate impact preview per parish/community: expected affected households, expected needs by category (tarpaulins, water, med), shelter demand estimate. AC: preview updates within 60s of a new advisory in replay.
- **IMP-03 (P0).** Files whose 64kt probability crosses the configured threshold flip to AT-RISK (ledger event).
- **IMP-04 (P1).** Publish predicted vs observed vs verified comparison per event (the learning loop); store the triple per household for future curve fitting.
- **IMP-05 (P2).** Refit vulnerability curves from accumulated ground truth; version models; show model provenance on every prediction.

### 6.4 Alerts & anticipatory action (ALT)

- **ALT-01 (P0).** Alert Agent drafts targeted cascades scoped by geography and risk band, in English and Patois, text and voice-note variants, including nearest shelter and preparation steps. **Nothing sends without Director approval** (gate G1).
- **ALT-02 (P0).** Channel tiering: alerts go WhatsApp first with SMS fallback to numbers without WhatsApp delivery confirmation within 10 min. AC: per-recipient delivery status recorded.
- **ALT-03 (P1).** Community relay: designated relay contacts (pastors, councillors, shopkeepers) receive a printable/forwardable digest for their district.
- **ALT-04 (P0).** Anticipatory list: when posture reaches READY, generate the ranked list of pre-verified vulnerable households in the projected impact zone (top N by vulnerability × probability). Exportable; drives pre-positioning and (P2) pre-landfall cash. **Director-role only in P0** — the list identifies vulnerable individuals by location and is never surfaced on the public portal (§11.4).
- **ALT-05 (P2).** Anticipatory cash: on defined parametric trigger, draft pre-landfall disbursements to the anticipatory list for Finance signature.

### 6.5 Intake (INT)

- **INT-01 (P0).** Anyone can message the Lighthouse WhatsApp number post-event, registered or not; unregistered reporters get a thin Storm File created inline.
- **INT-02 (P0).** The Intake Agent shall accept voice notes, photos, text, and location pins; transcribe Patois/English voice (Patois model primary, base model fallback, both outputs stored); extract structured fields: location, damage type, household size, injuries, urgent medical needs, immediate needs. AC: end-to-end voice note → structured claim ≤ 15s at P50.
- **INT-03 (P0).** Adaptive follow-ups: ask only for missing fields, maximum 3 follow-ups, then file partial with `partial=true`.
- **INT-04 (P0).** **Safety-of-life bypass:** keywords (trapped, injured, bleeding, can't breathe, dying, baby sick, no water for days) immediately page the on-duty human and tag the claim SOL, skipping all queues. Built and tested first.
- **INT-05 (P0).** Claim record: every filed claim stores raw media (R2), transcript(s), extraction, geotag, timestamps, and channel. Claim IDs are human-readable (parish prefix + number).
- **INT-06 (P1).** SMS intake tier: structured SMS (`DAMAGE <TYPE> <PEOPLE> <COMMUNITY>`) files a thin claim flagged low-evidence; the system requests media later when connectivity returns.
- **INT-07 (P1).** Duplicate-report merging: repeat messages from the same number about the same event attach to the open claim instead of creating new ones.
- **INT-08 (P2).** Additional channels behind the same intake interface: Telegram, IVR voice line, walk-in kiosk mode for shelters.
- **INT-09 (P0).** Status queries: a household can ask "weh mi claim deh?" any time and get current state in plain language.

### 6.6 Verification (VER)

- **VER-01 (P0).** The Verification Agent shall score each claim on five independent signals, each 0–1 with stored evidence: hazard sufficiency (observed wind/rain at the point), satellite change (where tiles exist), neighbour corroboration (independent claims within 300m), registry consistency (damage type vs structure profile), media integrity (perceptual-hash duplicate check, basic manipulation heuristics). Signals run in parallel.
- **VER-02 (P0).** Combined confidence with documented weighting. Thresholds: ≥ 0.85 auto-verify; 0.50–0.85 human review queue; < 0.50 flagged. Thresholds are per-event configurable and every change is a ledger entry.
- **VER-03 (P0).** Review queue UI: clerk sees claim, media, transcript, all five signals with per-signal evidence, and map context; actions: approve → VERIFIED, reject → CLOSED (with reason), request more info (sends WhatsApp follow-up). Every adjudication records clerk ID and rationale.
- **VER-04 (P0).** Missing-signal handling: absent satellite or thin SMS claims redistribute weight and cap max confidence at 0.80 (forcing human review for low-evidence claims).
- **VER-05 (P1).** Cross-payer dedupe: before any payer settles a claim, check ledger for prior settlements to the same household for the same event and need category; collision blocks with override + reason.
- **VER-06 (P1).** Appeal path: a rejected household can reply APPEAL; file reopens into the review queue flagged, with one appeal per claim.
- **VER-07 (P0).** Every agent verdict is stored raw, including those humans override (future eval/training set).

### 6.7 Triage (TRI)

- **TRI-01 (P0).** Verified claims receive severity (URGENT/HIGH/MED) and rank: medical urgency first, then habitability, then property; vulnerability score breaks ties. Ordering rules documented.
- **TRI-02 (P0).** The triage queue is live-sorted on the console; SOL claims pin to top regardless.

### 6.8 Payer routing (RTE)

- **RTE-01 (P0).** After verification, the Router asks (or reads from registration): "Yuh have insurance pon di house? Which company?" Routing outcomes: `GOV_RELIEF` (default, uninsured), `INSURER(name)` (insured, with consent to share), `BOTH` (insured for structure, relief for immediate needs), `DONOR_POOL` (donation-funded allocations, orthogonal to the other two as a funding source for GOV_RELIEF-path claims).
- **RTE-02 (P0).** Routing decisions are explicit ledger events with the household's consent snapshot.
- **RTE-03 (P1).** A claim routed to an insurer still receives immediate-relief eligibility (tarpaulin, water) from the government/donor path; insurance covers reconstruction. One claim, parallel payers, dedupe per need category (VER-05).

### 6.9 Insurance surface (INS)

- **INS-01 (P0, demo simulated).** FNOL packet generation: for insured verified claims, compile First Notice of Loss: policyholder name + contact, property location + registered structure profile (+ year if captured), event ID and timestamps, observed hazard at the location (peak wind, rainfall), damage category + severity, media evidence, verification signals and confidence, claim ID. Delivered as JSON via API + human-readable PDF.
- **INS-02 (P1).** Insurer portal + API: authenticated insurers receive FNOLs for their policyholders only (consent-gated), acknowledge receipt, and post status updates (received / adjuster assigned / paid) which write back to the household's Storm File and ledger, and notify the household on WhatsApp.
- **INS-03 (P1).** Portfolio heatmap: per-insurer aggregate damage-density map and counts by parish/category for their book only; platform-wide anonymized heatmap available as a data product.
- **INS-04 (P2).** Exposure pre-storm product: anonymized aggregate exposure by area and structure class; ground-truth event datasets for pricing (aggregated, never household-identifiable without consent).
- **INS-05 (P0).** Damage values: the system shall not emit dollar estimates in v1; category + severity + evidence only.

### 6.10 Logistics (LGX)

- **LGX-01 (P0).** Stock registry: warehouses with item counts (tarpaulins, water, food packs, zinc, med kits), manually adjustable, decremented by approved allocations.
- **LGX-02 (P0).** Allocation planning: the Logistics Agent matches verified claims to stock and cash by triage order and proposes an allocation plan (who gets what from where) + distribution run sheets grouped by area. **Plans execute only after Director approval** (gate G2).
- **LGX-03 (P1).** Delivery confirmation: field teams confirm per-stop via WhatsApp (code or photo); confirmations write to ledger and flip goods-claims toward SETTLED.
- **LGX-04 (P1).** Shelter registry: capacity and occupancy per shelter, updated by shelter managers via WhatsApp.
- **LGX-05 (P2).** Route optimization and fleet integration (explicit non-goal until then; run sheets only).

### 6.11 Settlement & disbursement (PAY)

- **PAY-01 (P0).** Disbursement batching: allocations group into batches by channel (bank transfer, mobile money, voucher, goods). A batch presents: total, count, payer source, dedupe check result. **Execution requires Finance signature** (gate G3); the signature is a ledger entry naming the signer.
- **PAY-02 (P0, demo).** Simulated rails for buildathon: execution mocks channel latencies and confirmations end to end.
- **PAY-03 (P1).** Real rails: at least one cash channel (bank file or mobile money API; Powertranz gateway candidate) with per-payment confirmation webhooks; failed payments retry with alerting and never silently drop.
- **PAY-04 (P0).** SETTLED transition: a Storm File reaches SETTLED only when at least one confirmed disbursement or confirmed delivery exists. **T2R clock starts at claim creation and stops at first confirmation**; the verified → confirmed interval is recorded separately as the settlement-latency sub-metric.
- **PAY-06 (P0).** Standard relief amounts for P0: a flat **J$45,000 cash grant** per verified household, plus goods tiered by triage severity (URGENT / HIGH / MED baskets). Flat cash keeps the demo explainable and removes per-claim valuation judgement from the agent path; tiering lives in goods where stock constraints already force it. Amounts are admin-config (ADM-03), not hardcoded (§11.1).
- **PAY-05 (P1).** Household notification at every money state change, with amounts and what to do if something is wrong (human callback path).

### 6.12 Donations (DON)

- **DON-01 (P0, demo simulated).** Donation intake: a donor can give an amount to a scope via web portal. **P0 scopes are event-wide and parish only**; need-category scoping is P1 (§11.3) — narrower scoping fragments pools and constrains the allocation agent before we have the volume to justify it. Production funds flow: card/bank → **fiscal sponsor** account (registered charity partner); the platform records and directs, it does not hold funds. Buildathon simulates the processor.
- **DON-02 (P0).** Every donation is a ledger entry: donor reference (pseudonymous public handle), amount, scope, timestamp. Pool balances are public in real time.
- **DON-03 (P0).** Pool → allocation: donation pools are a payer source selectable in allocation plans; allocations draw down pool balances visibly.
- **DON-04 (P0).** Donor journey view: a donor can follow their donation: received → pooled → allocated (n households, anonymized) → disbursed → confirmed, with dates. AC: demo shows "your J$10,000 became a tarpaulin + water in Black River, delivered T+52h."
- **DON-05 (P1).** Compliance: KYC thresholds per fiscal sponsor policy, AML screening on large donations, receipts issued by the sponsor.
- **DON-06 (P2).** Diaspora rails: remittance-provider integration; recurring "season fund" subscriptions.

### 6.13 Ledger & transparency portal (LGR)

- **LGR-01 (P0).** Append-only ledger of: every state transition, posture change, threshold change, alert approval, routing decision, allocation, signature, disbursement, confirmation, and agent verdict. Each row: actor (agent or named human), action, subject, payload hash, previous-row hash. `verify_chain()` must pass at any time.
- **LGR-02 (P0).** Public portal: real-time aggregates: funds in by source, funds out by parish and category, pool balances, claim counts by state, median T2R. **Never** named beneficiaries or precise addresses; minimum aggregation bucket ≥ 10 households.
- **LGR-03 (P0).** Auditor role: read-only access to full detail, exportable (CSV), no ability to write anything.
- **LGR-04 (P1).** Reconciliation: the Ledger Agent matches disbursement confirmations to bank/mobile-money records, flags anomalies (duplicates, orphans, amount mismatches) to humans; flags are themselves ledger entries.

### 6.14 EOC console (CON)

- **CON-01 (P0).** Live map: hazard layers (track, cone, wind probabilities), Storm File dots by state, shelters, selectable layers; click any dot for its Storm File.
- **CON-02 (P0).** Queues: triage, verification review, SOL alerts; gate surfaces for approvals (alert cascade, allocation plan, disbursement batch) with pending-signature indicators.
- **CON-03 (P0).** T2R counter and headline stats always visible.
- **CON-04 (P1).** Offline-first: console is a PWA; read views and queued approvals survive connectivity loss and sync on reconnect with conflict rules (server wins on state, queue holds signatures).
- **CON-05 (P0).** Replay controls (demo): play/pause/speed/jump on the Melissa timeline.
- **CON-06 (P1).** Parish view: scoped console for parish coordinators (their area only).

### 6.15 Roles & administration (ADM)

- **ADM-01 (P0).** Roles: Director, Review Clerk, Finance Officer, Auditor, Admin, (P1) Parish Coordinator, Shelter Manager, Field Team, Insurer User, Donor. Every privileged action is role-checked and logged.
- **ADM-02 (P0).** Signature actions require re-authentication (password or WebAuthn) at the moment of signing.
- **ADM-03 (P1).** Config: thresholds, posture rules, allocation amounts per need category are admin-editable; every change is a ledger entry.

### 6.16 Replay & demo mode (RPL)

- **RPL-01 (P0).** Deterministic replay: fixed seed, cached advisories, pre-cached satellite tiles and wind fields; zero external network calls; full run unattended in ≤ 10 minutes at demo speed.
- **RPL-02 (P0).** Live-inject: a real phone can join (Twilio sandbox) mid-replay and its voice note flows the identical pipeline. Fallback pre-recorded voice note available offline.

---

## 7. Non-functional requirements

### Performance & scale
- **NFR-P-01 (P0).** Voice note → structured claim: ≤ 15s P50, ≤ 30s P95 (demo hardware).
- **NFR-P-02 (P1).** Burst: sustain 10,000 intake messages/hour with graceful queueing; nothing dropped, backlog visible on console; degrade by delay, never by loss.
- **NFR-P-03 (P0).** Advisory processing (ingest → risk refresh → map) ≤ 60s for 10k Storm Files.
- **NFR-P-04 (P1).** Console map interactive at 50k Storm Files (clustering).

### Availability & resilience
- **NFR-A-01 (P1).** Core intake and ledger: 99.5% availability during an activation; single managed instance per service (Render) acceptable for buildathon, documented path to HA and to in-country hosting for data residency.
- **NFR-A-02 (P1).** All inbound messages durable-queued at the edge before processing (webhook receiver survives downstream failure).
- **NFR-A-03 (P1).** Nightly encrypted backups; restore drill before pilot; RPO ≤ 24h (P2: ≤ 1h).

### Security & privacy
- **NFR-S-01 (P0).** TLS everywhere; encryption at rest for PII and media; secrets in env/manager, never in repo.
- **NFR-S-02 (P0).** No PII in logs; phone numbers hashed outside the Storm File row; media URLs signed and expiring.
- **NFR-S-03 (P0).** Consent-first; data minimisation; purpose limitation documented per field.
- **NFR-S-04 (P1).** Jamaica Data Protection Act (2020) mapping: lawful basis register, DPO contact, breach process, retention schedule (claims media 24 months then archive/delete unless legal hold).
- **NFR-S-05 (P1).** Insurer data isolation: an insurer sees only consent-shared claims for its own policyholders; enforced at query layer with tests.
- **NFR-S-06 (P0).** Buildathon uses synthetic data exclusively.

### Localization & accessibility
- **NFR-L-01 (P0).** All household-facing flows in Jamaican Patois and English, voice and text; language auto-detected, switchable any time.
- **NFR-L-02 (P1).** Voice-first parity: every household flow completable by voice alone (low-literacy support).
- **NFR-L-03 (P2).** Additional creoles/languages per country expansion (Haitian Creole, Spanish, Dutch).

### Auditability & ML governance
- **NFR-G-01 (P0).** Every agent input/output stored raw; every override recorded; eval set never trained on.
- **NFR-G-02 (P0).** Model/threshold versions stamped on every verdict; changes are ledger events.
- **NFR-G-03 (P1).** Patois ASR eval report (WER + field-extraction accuracy on held-out set) published per model version.

### Cost (P1 guidance)
- **NFR-C-01.** Small models on high-volume paths (intake ASR/extraction); frontier reasoning only on verification and planning; target marginal cost ≤ US$0.05 per processed claim at pilot scale.

---

## 8. External data & integrations

| Integration | Direction | Priority | Notes |
|---|---|---|---|
| NHC advisories, cone, wind-speed probs, radii (GIS/RSS) | in | P0 | shapefile/KML, per-advisory |
| NDBC buoys, Open-Meteo (ECMWF/GFS), CHIRPS | in | P0 | polling |
| Copernicus Sentinel-1/2 | in | P1 (P0 cached demo) | change detection |
| OpenStreetMap footprints, WorldPop, SRTM | in | P0 | static, cached |
| WhatsApp Business Cloud API (Meta) | both | P0 | prod path; business verification lead time |
| Twilio (WhatsApp sandbox + SMS) | both | P0 | demo path + SMS tier |
| Payment rails (Powertranz / mobile money / bank files) | out | P1 | simulated in P0 |
| Insurer API (FNOL out, status in) | both | P1 (P0 simulated) | JSON + PDF packet |
| Fiscal sponsor donation processor | in | P1 (P0 simulated) | funds never held by platform |
| PATH / national ID | in | P2 | via data-sharing agreement |

## 9. Buildathon acceptance test (the demo IS the test)

1. Start replay. Posture rises QUIET→WATCH→READY→ACT as Melissa's real advisories play; parish risk and at-risk counts climb. (HAZ, IMP)
2. Gate: Director approves alert cascade; alert lands in the WhatsApp mock in Patois with shelter info; anticipatory list generates. (ALT)
3. Landfall. Claims stream in; live phone joins the Twilio sandbox and sends a Patois voice note; ≤ 30s later it is a structured, verified, triaged claim on the map. (INT, VER, TRI)
4. Routing asks about insurance; the insured demo claim produces an FNOL packet (JSON + PDF) opened on stage; the uninsured claim routes to relief. (RTE, INS-01) *No insurer portal or heatmap in P0 — INS-02/03 are P1.*
5. Gate: clerk adjudicates a low-confidence claim from the evidence bundle. (VER-03)
6. A simulated donation is made to "St Elizabeth pool" and appears on the public ledger. (DON)
7. Gate: Director approves the allocation plan (mixed payer sources incl. the donor pool); stock decrements; run sheets emit. (LGX)
8. Gate: Finance signs the disbursement batch; payments execute on simulated rails; files reach SETTLED; the donor journey view shows the donation delivered; T2R reads real hours; `verify_chain()` passes on stage. (PAY, DON, LGR)

## 10. Risks & mitigations (product-level)

| Risk | Mitigation |
|---|---|
| WhatsApp/data down post-storm | SMS tier (INT-06/ALT-02), relay contacts (ALT-03), pre-season registration so location is already known, offline console |
| Meta business verification delays | Twilio sandbox demo path; verification started week 1 in parallel |
| Fraud / duplicate claims | 5-signal verification, cross-payer dedupe, human review band, appeal + audit trail |
| Over-claiming prediction ability | Impact = hazard × registry framing; v1 lookup documented; no dollar estimates |
| Holding money = regulatory exposure | Fiscal sponsor holds funds; platform is rails + record |
| Patois ASR quality | Eval-first (NFR-G-03); confidence gating; errors reduce coverage, never move money |
| Gov adoption speed | Parish + NGO entry; insurer and donor surfaces create independent pull |

## 11. Resolved decisions (team, July 31, 2026)

These were open questions in v1.0. All five are now closed; the requirements above reflect them.

1. **Allocation amounts.** Flat J$45,000 cash grant per verified household, plus goods tiered by triage severity. Cash stays flat so no agent makes a per-claim valuation judgement and the stage story is one number; tiering lives in goods, where stock constraints already force it. → PAY-06, ADM-03.
2. **Insurer naming.** Fictional carrier ("Island Mutual") everywhere a judge can see it. Real Jamaican carriers appear only in market sizing and pipeline conversations, never in demo UI, so nothing on screen implies an endorsement we don't have. → §4 persona 5, INS-01.
3. **Donor scoping.** Event-wide and parish only in P0; need-category scoping is P1. → DON-01.
4. **Anticipatory list visibility.** Director-role only in P0. The list is a ranked register of vulnerable people and where they live; publishing it would invert the privacy posture the rest of the platform argues for. Transparency obligations are met by the aggregate portal (LGR-02). → ALT-04.
5. **Claim ID scheme.** Parish prefix + sequential, e.g. `SE-4102`. Already used in the solution spec and prototype, and a household has to be able to read it back over a bad phone line. → INT-05.

6. **Job queue mechanism.** Postgres `SKIP LOCKED`, no Redis. The decisive reason is transactional: enqueueing an agent job and writing the state transition happen in one transaction, so a Storm File cannot land in a new state with no worker coming and nothing in the ledger recording that anything went wrong. On Redis those are two systems and that failure is possible — an orphaned claim is the worst bug this platform could ship. Secondary: one less service to run, jobs are queryable in SQL when something stalls mid-demo, and at replay volume throughput is not a constraint. Workers wake on `LISTEN/NOTIFY` rather than tight-polling, which also keeps Neon from burning compute hours. Revisit only if throughput or pub/sub fan-out becomes real.

*No open questions remain at v1.1.*

## 12. Glossary

**Storm File** household record and lifecycle. **T2R** median hours, claim filed → first relief confirmed (the clock includes our own verification time). **Settlement latency** the verified → confirmed sub-interval, tracked separately. **FNOL** First Notice of Loss packet to an insurer. **Posture** national readiness level (QUIET/WATCH/READY/ACT). **SOL** safety-of-life claim. **Gate** human signature required to proceed. **Anticipatory list** ranked vulnerable households in the projected impact zone, generated pre-landfall. **Fiscal sponsor** registered charity partner that legally receives and holds donated funds.
