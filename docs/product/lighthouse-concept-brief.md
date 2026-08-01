# Lighthouse

**The resilience operating system for the Caribbean — warns before, guides during, stands after.**

Track 04 — Climate Risk & Disaster Coordination
Team: Raheem Wilson (Product & Software Engineer) · Sammarieo Brown (Product & Software Engineer) · Matthew Stone (Product & AI Engineer)

---

## Why now: the money is fast. The distribution is broken.

Hurricane Melissa (October 2025) killed 45 people in Jamaica, affected 626,000+, tore roofs off ~120,000 buildings, damaged 450 schools, and caused $8–15B in losses — nearly a quarter of GDP.

Then the capital arrived at record speed. Jamaica's $150M World Bank catastrophe bond paid out 100%, announced roughly ten days after landfall. The IMF approved US$415M. Public donations reached J$1.44 billion. And by late February 2026 — four months after the storm — the Auditor General found that **1.8%** of the donated cash had reached households. J$138.8M donated after Beryl (2024) was still unspent when Melissa hit. J$34M in building supplies had no delivery documentation. The story is on Jamaican front pages this week.

**$150M in ten days; 1.8% in four months.** The Caribbean does not have a disaster-capital problem. It has a last-mile settlement problem: nothing connects the forecast to the household, the household to the damage claim, the claim to the allocation, or the allocation to the payment. Lighthouse is the rail between capital and household.

And the failure was never a software gap — a donation portal existed, spreadsheets existed. It was a **cognition gap**: no agency on earth can hold 120,000 conversations with desperate people, verify each claim against evidence, and match verified need to money in days, with a civil service of a few hundred. The audit scandal is the market why-now; agentic AI is the technology why-now. Lighthouse could not have been built in 2022.

## The product: the Household Storm File

One atomic object. Every household has a Storm File — identity, location, structure profile, vulnerability, claims, payments — that moves through five states:

**registered → at-risk → affected → verified → settled**

Everything else in the platform is machinery around that file. WhatsApp is a household's interface to its own file — voice note, photo, or text, in Patois or English, no app download. The EOC console is queries over files. The ledger is the append-only log of state transitions. The agents are workers that move files between states. And no transition that moves money happens without a human signature. **The report becomes the claim becomes the payment.**

North-star metric: **Time-to-Relief (T2R)** — median hours from verified claim to first relief in hand. Melissa's T2R was months-to-never; Lighthouse targets 72 hours. Supporting metrics: % of aid with a complete audit trail (target 100%), % of households pre-registered per parish, and forecast-to-actual damage calibration error.

## One storm, three acts

**Act 1 — Foresight (before).** We do not out-forecast NOAA; we make its forecasts actionable locally. Ingest NHC/ECMWF ensembles and seasonal outlooks; overlay elevation, population, and structure data; produce parish-level impact previews. Months out this drives seasonal posture — stockpiling, drills, insurance windows, registration campaigns. Days out it drives targeted alerts in English and Patois and flips exposed Storm Files to *at-risk*.

**Act 2 — Respond (during + after).** A voice note becomes a structured, geolocated claim. The verification agent dedupes, cross-checks satellite damage imagery and neighboring reports, and flags fraud; triage scores severity; the EOC sees a live needs map; the logistics agent matches verified needs to warehouse stock and emits distribution run sheets. Files move *affected → verified*.

**Act 3 — Ledger (recovery).** Every allocation and disbursement — bank, mobile money, voucher, goods — lands on an auditable public ledger with duplicate-payment detection. Government publishes progress in real time; diaspora donors see exactly where money went. Files reach *settled*. Framing: Lighthouse helps government rebuild public trust — the state is our customer, not our villain.

## The headline differentiator: anticipatory action

Households register on WhatsApp before the season — location, household size, roof type, medical needs, consent. Pre-registration solves post-storm connectivity (we know who and where people are even if towers fall), seeds the registry, and unlocks where disaster finance is heading: aid triggered *before* landfall to pre-verified vulnerable households, eventually tied to parametric triggers. Melissa proved the trigger side works — the cat bond paid in days. Lighthouse supplies the missing half: verified households to pay. Week 3 ships a thin version — registration flow plus an auto-generated anticipatory list when storm posture rises.

## Defensibility

**1. The single-ledger network effect (lead moat).** Deduplication and audit only work if every payer settles on one registry. After Melissa, households received aid from ODPEM, churches, NGOs, and diaspora remittances — nobody knows who got what, so no payer can prove reach. The first ledger to enroll two or three anchor payers forces the rest on: disbursing outside it means unverifiable, duplicable aid — politically radioactive after this audit. A clearinghouse dynamic: winner-take-most per country, deepening every season.

**2. The forecast-to-outcome dataset.** Each storm we capture hazard intensity at a location + structure profile + verified damage, at household resolution. That triple trains Caribbean-calibrated vulnerability curves that global catastrophe models lack — their generic curves fit the region's informal building stock poorly. The model compounds every season; insurers, CCRIF, and cat-bond structurers are the buyers; nobody can rebuild the dataset without our registry and intake.

**3. System-of-record embedding.** National procurement, data residency, trained EOC staff, and integration with PATH and the national ID system as it matures — classic govtech switching costs, plus the trust position: the platform the Auditor General points to instead of writes about.

The fine-tuned Patois voice/intent model (built on H200 compute) is our near-term differentiator and data-collection edge — judges will remember it — but we claim the three moats above as the durable ones.

## Scope discipline — what we don't build

No weather models (we consume NHC/ECMWF). No fleet routing (we emit run sheets and integrate). No donation collection (supportjamaica.gov.jm exists — we are the accountability layer it lacks). No insurance underwriting (we sell evidence packets, not adjudication). And we integrate PATH rather than compete: PATH targets chronic poverty on slow-updating rolls; shock response needs geospatial, structure-aware, claims-capable records.

## Competitive landscape

WFP SCOPE and RedRose: single-agency internal aid tools, deployed post-hoc, no forecast link, no cross-payer dedup, not government-owned. PDC DisasterAWARE: situational awareness — no household layer, no money. Ushahidi-style mapping: unverified crowd pins, no settlement. Met-office products: hazard only. Nothing in market is national, multi-payer, forecast-linked, and WhatsApp-native.

## Market & model

Citizens use it free on WhatsApp. **We sell Lighthouse as one national resilience platform — a single procurement, phased rollout across one season cycle** (registry + ledger stand up first as the spine, response console live by season peak). Buyer: ODPEM/ministry level in Jamaica, CDEMA as the regional path. Revenue: national platform license + per-disbursement settlement fee from non-government payers (NGOs, diaspora funds) + data licensing to insurers/CCRIF. Market: 19 CDEMA member states, then ~58 small island developing states facing the same capital-to-household gap.

## Agent architecture (50% of the judging score)

Agentic AI is the load-bearing wall of Lighthouse, not decoration. The disaster domain *forces* agency for four reasons: the inputs are unstructured and chaotic (a trembling Patois voice note, a blurry photo, a text missing everything important — forms are exactly what fail in disasters); the workload is violently bursty (near-zero for months, then 120,000 claims in 72 hours — you cannot staff the spike, only scale agents elastically); verification is multi-source judgment, not lookup (does this claim square with the wind field at that location, the satellite imagery, the neighbors' reports, the pre-registered structure profile?); and the system must act while humans sleep (feeds are watched 24/7; posture changes can't wait for office hours).

The Storm File state machine *is* the orchestration: each agent is a worker authorized to move files through specific transitions, and every state change triggers the next agent. Autonomy is graduated by consequence:

- **Forecast Sentinel** — fully autonomous monitoring of NHC/ECMWF and rainfall feeds; flips files to *at-risk*, raises posture, escalates to humans. No gate — no harm possible.
- **Risk Mapper** — autonomous; converts forecasts + elevation + population + structure data into parish-level impact previews.
- **Intake Agent** — autonomous multi-turn conversation per household ("Yuh seh di roof gone — anybody hurt? Send mi yuh location"), deciding what to ask next based on what's missing, calling tools (geolocation, photo analysis, registry lookup); creates the claim, moves files to *affected*.
- **Verification Agent** — autonomous evidence-gathering across satellite, weather, registry, and neighboring reports; emits confidence-scored verdicts. High confidence → *verified*; low confidence → human review queue.
- **Alert & Logistics Agents** — *propose only*: drafted alert cascades and allocation plans under changing constraints (flooded roads, shifting stock), approved by the EOC director.
- **Ledger Agent** — continuous recording, reconciliation, and anomaly-flagging; the auditor that never sleeps. Flags go to humans.

The governing rule: **agents do the cognition, the state machine constrains their authority, and humans hold every gate that moves money.** A finance officer signs before any file reaches *settled*. That is our human-in-the-loop design, and it doubles as the fraud and public-trust answer.

Compute is right-sized per the rubric's "thoughtful, distinctive, efficient" guidance: the Sentinel runs small and cheap; the high-volume intake path runs our fine-tuned Patois speech/intent model (the H200 allocation) instead of a frontier model per voice note; frontier-class reasoning is reserved for where judgment is dense — verification and allocation planning. Open-source build; intake runs on OpenClaw (WhatsApp-native agent framework — first on the buildathon's own tools list).

One closing connection: the moat depends on the agents. The single-ledger network effect only works if verification is trusted at 120,000-claim scale, and only agents make verification affordable at that scale. The agentic layer is not a feature of the clearinghouse — it is what makes a clearinghouse possible for a small state.

## Three-week build

- **Week 1 — the spine.** Storm File data model + ledger core; live storm-feed dashboard with parish risk scores for Jamaica. (Raheem: platform/console · Sammarieo: data pipeline/infra · Matthew: forecast + risk agents)
- **Week 2 — the loop.** WhatsApp intake → verification → triage → live EOC map → logistics matching, end to end. (Matthew: agents + Patois model · Raheem/Sammarieo: WhatsApp infra, console)
- **Week 3 — the proof.** Ledger views + anticipatory registration (thin) + Melissa replay mode + live T2R counter + demo polish.

## The demo: one storm, three acts

Replay Hurricane Melissa's real timeline through Lighthouse. **Act 1 (T−5 days):** posture rises, parish risk scores climb, alerts cascade, the anticipatory list generates. **Act 2 (landfall +1):** a judge takes a phone and sends a live voice note — *"Mi roof gone, mi deh a Black River, mi have two pickney"* — and thirty seconds later it is a verified, triaged claim on the EOC map with a matched allocation. **Act 3 (T+30 days):** the ledger shows every dollar from donation to disbursement, and the T2R counter reads hours, not months. Closing line: *"After Melissa, the bond paid in ten days and the people waited a year. Lighthouse closes that gap."*

## Five years out

The settlement layer for global disaster capital in small states: all 19 CDEMA members, then the wider SIDS world. The registry becomes the region's anticipatory social-protection backbone — cash reaching verified households before landfall. Insurers and cat-bond structurers price on Lighthouse's ground-truth vulnerability data. Every climate-vulnerable region that looks like ours is the expansion map.

## Risks and our answers

Post-storm connectivity → pre-registration, SMS fallback, community relay points, offline-first EOC console. Fraud/duplicates → verification agent with satellite cross-check and community validation; dedup is the product, not a patch. Sensitive data (IDs, locations of vulnerable people) → consent-first registration, encryption, data-sharing agreements; we answer "yes" honestly on the form's sensitive-data question, with this plan. "Governments can't buy a whole platform" → one procurement, phased rollout across a single season cycle; buildathon credibility plus NGO/parish entry points while national procurement matures.

## Evidence for the pitch

- World Bank — [Hurricane Melissa triggers 100% payout of $150M catastrophe bond](https://www.worldbank.org/en/news/press-release/2025/11/07/hurricane-melissa-triggers-100-payout-of-150-million-world-bank-catastrophe-bond-for-jamaica)
- IMF — [US$415M disbursement to Jamaica for Melissa](https://www.imf.org/en/news/articles/2026/01/16/pr-26008-jamaica-imf-approves-a-usd-415-million-disburse-to-address-hurricane-melissa)
- Jamaica Gleaner — [Relief red flag: ODPEM spent only 1.8% of cash donations](https://jamaica-gleaner.com/article/news/20260513/relief-red-flag-february-odpem-spend-only-18-cent-cash-donations-meant-assist)
- Jamaica Observer — [Too late to explain?](https://www.jamaicaobserver.com/2026/07/23/too-late-to-explain/) (July 23, 2026)
- UN News — [Fifty days on, Jamaica struggles to rebuild](https://news.un.org/en/story/2025/12/1166621)
- Atlantic Council — [Hurricane Melissa left $8 billion in damage](https://www.atlanticcouncil.org/blogs/new-atlanticist/hurricane-melissa-left-8-billion-in-damage-jamaica-needs-us-support-to-get-back-on-its-feet/)
- FutureCaribbean — [Judging rubric](https://futurecaribbean.com/judging-rubric) (50% business / 50% agentic AI)
