# The household journey

How a household gets into the registry, what it agrees to, what it receives, and
how a warning becomes a claim.

Read this before touching `apps/api/app/intake/` or anything that sends a
message to a household.

Decided Aug 7. Everything below is a decision with a reason next to it. Where a
decision has a cost, the cost is written down rather than left to be discovered
during rehearsal week.

---

## Why this document exists

The platform could ingest a claim, verify it against five signals, allocate
relief and settle it on a hash chain before anyone had reasoned about how a
household arrives in the first place. Registration was a state on an enum —
`REGISTERED`, the first of five — and nothing wrote it except a seeder.

That gap was not cosmetic. Two of the arguments the product makes depend
entirely on it: anticipatory action needs to know who is vulnerable *before*
landfall, and the neighbour-corroboration signal needs households to exist near
each other. Both were being demonstrated against a synthetic population with no
path for a real one.

## What the frozen contracts already settled

Worth stating, because these were not open questions and re-litigating them
wastes time:

- **A household is a phone number.** `storm_file.phone_hash` is unique. `phone`
  is nullable, so the plaintext can be dropped while the identity survives.
- **Registration is state one**, not a separate concept:
  `REGISTERED → AT_RISK → AFFECTED → VERIFIED → SETTLED`.
- **`thin: bool` and `assisted_by` already exist**, so partial records and
  registration-on-behalf-of were anticipated in the schema.
- **Nine operator roles are frozen** in `AppRole`.
- **NFR-L-02**: every household-facing flow must work without reading. That
  rules out a web form as a primary path, whatever else is decided.

---

## The decisions

### 1. Registration exists for anticipatory action

Anyone can claim without registering — Phase 3's exit criteria require that a
phone which has never touched the system can complete the whole loop. So
registration has to buy something else, and what it buys is *knowing who is
vulnerable before the storm arrives*: the Director-only pre-landfall list,
targeted alerts, and a registry structure match that is one of the five
verification signals.

This has a consequence that constrains everything downstream. **If alerts are
not targeted at the individual household, registration buys the household
nothing** — a national posture broadcast needs no registry and could go out on
the radio. Targeting is the entire return on registering, so the alert trigger
below keys on the household's own exposure and not on national posture.

### 2. WhatsApp self-registration only

One channel, one code path, already built, and it ships.

**The cost, stated plainly.** The households most worth having on a pre-landfall
vulnerable list — elderly, low-literacy, poorly-built housing — are the least
likely to self-register through a chat flow. A self-serve-only registry
systematically under-represents the people the list exists for.

Assisted registration by `PARISH_COORDINATOR` and `FIELD_TEAM` is the fix, the
schema already carries `assisted_by` for it, and it is deliberately out of scope
for now. This limitation is not a defect to be discovered later; it is a known
skew, and any claim made about registry coverage must carry it.

### 3. Three questions, and the record says so

Location, roof, and who lives here. Nothing else.

The vulnerability score has a maximum of 95 points and they are not evenly
distributed:

| field | max points | share |
|---|---|---|
| roof | 30 | 32% |
| walls | 20 | 21% |
| medical need | 15 | 16% |
| build era | 15 | 16% |
| elderly | 10 | 11% |
| children | 5 | 5% |

Roof is the largest single lever, and it is the same field the simulator's
fragility curves key on — one question doing two jobs. The three people flags
total 30 points and one question captures all of them. Together with location
that is **63% of the vulnerability signal in three turns**.

Build era is 16% of the score and is the question a person is least likely to
answer accurately about their own house. High friction, low value, not asked.

The 37% that is not collected is not silently treated as zero. The record is
written `thin = true`, which is the schema's existing way of saying the score is
partial. A thin record must never be presented as a complete assessment.

### 4. Consent is explicit, and says what it actually does

Registration puts a household on a ranked register of vulnerable people and
where they live. That register is Director-only precisely because publishing it
would invert the privacy posture the rest of the platform argues for.

**If it is sensitive enough to hide from most operators, the person on it is
told they are on it.** In Patois and English, the agent states what this is,
that it puts them on a list emergency services use to reach them first, who sees
it, and that replying `STOP` removes them at any time. They agree to that
specifically.

This writes a `consent` row. The table exists in the database and has no model
class — it was created in the initial migration and never wired up. Wiring it is
part of this work, and the row is load-bearing for reasons given in §7.

### 5. Location is a pin, taken at face value

WhatsApp's native location message is the best-fitting question in the entire
flow: one tap, no typing, no literacy requirement, no Patois transcription risk.
Twilio posts it as `Latitude` and `Longitude` on the inbound webhook. The intake
code parses neither today.

The pin is stored as given. It is not confirmed back to the household.

**The cost.** A pin is the device's GPS at send time. Someone registering from a
workplace, a bus or a relative's yard records that place as their dwelling, and
nothing catches it. The error stays invisible until a verification signal
misfires or relief is sent somewhere nobody lives. This was chosen for speed of
registration over accuracy of record, and it is the right trade for a
demonstration and the wrong one for a real registry.

One free mitigation is taken because it costs no conversational turn: the pin is
resolved against the real community boundaries already loaded by
`registry/geography.py`, and a pin falling outside every known boundary is
flagged. That catches a point in the sea. It does not catch a workplace.

**Live location is never requested.** Continuous tracking is disproportionate to
every purpose this platform has.

**The point never leaves the API at household resolution.** Design rule C5 bans
household dots on the public portal, and that is not relaxed here.

### 6. Alerts fire on every advisory the household is exposed to

For each advisory where the household's point falls inside a forecast wind
field, a message goes out.

**The cost, in arithmetic.** Melissa has 41 advisories. Against 2,000 households
that is up to 82,000 messages per replay. A Twilio trial sandbox will not
deliver that volume, and Meta Cloud API bills per message. At 21,600× playback
the replay would fire 41 messages in roughly forty seconds.

Two things follow, and neither is optional:

- **Live sending sits behind an explicit flag that replay never sets.** A replay
  that sends real messages is not a demonstration, it is an incident.
- Alert fatigue is the mechanism by which the message that mattered goes
  unread. This is the same argument design rule M1 makes about moving pixels,
  and it applies harder to a phone that buzzes. The volume is a known risk
  carried deliberately, not an oversight.

Alerts are **pre-approved templates**, which is WhatsApp's rule rather than a
design choice: a household that registered months ago is far outside any
24-hour customer-service window, and only templates may be sent there.

### 7. The reply is the hinge

The alert states the risk and the timing, then asks for one thing back:
*reply 1 if you are safe, 2 if you need help.*

This is the most useful sentence in the flow, and not because of what it
collects. **Any reply opens the 24-hour window.** A household that answers is
reachable free-form for the next day, which means the claim conversation
afterwards can be Patois voice rather than templates. The sequencing problem
recorded in the Aug 1 logbook — that Act 1's alert cannot reach a phone which
has never messaged us — is solved by the alert itself, provided it invites an
answer.

- `1` is logged. The window is open; nothing else happens.
- `2` hands directly to the existing intake agent, which asks its normal
  questions and builds a claim. Safety-of-life keyword detection stays live
  throughout, so *trapped* or *cyaan breathe* still bypasses everything and
  pages a human.
- `STOP` sets the household inactive and alerts cease. The consent and ledger
  history remain, because a record of what was agreed cannot honestly be
  deleted by the act of withdrawing.

---

## The `synthetic` flag now carries two meanings, and one of them is new

This is the part most likely to be misread by whoever touches this next.

Demo registrations — including real people using real phone numbers — are
written **`synthetic = true`**. That is not a labelling convenience. The
neighbour-corroboration signal partitions on this flag:

```sql
AND other_sf.synthetic = target.synthetic     -- verification_service.py:344
```

Corroboration only counts neighbours whose flag matches. A demo registration
marked `synthetic = false` would have zero eligible neighbours, that signal
would return nothing, confidence would fall, and the claim would land in the
review queue instead of auto-verifying. Marking demo registrations synthetic is
what lets them corroborate against the seeded population around them.

Note also that `intake/service.py` already writes `synthetic = false` for a
claim arriving from an unknown phone, so "synthetic only" was never true of the
thin-on-contact path.

The flag therefore means **"member of the demo population"**. It does *not*
mean "contains no real personal data", and the registry docstring which says
otherwise is now describing the seeder only.

**Consent is the marker for real data.** A seeded household has no `consent`
row — it never agreed to anything, because it does not exist. A real participant
has one. So:

| | |
|---|---|
| `synthetic = true` | member of the demo population; neighbour corroboration works |
| has a `consent` row | a real person really agreed; **real PII is on this row** |

This is the correct semantics rather than a workaround: no consent, no real
data. It also gives an exact deletion target. Every row with a consent row is
one that can be purged on request or after the event, and the synthetic
population is untouched by that purge.

`test_every_household_is_synthetic` continues to pass, and must not be read as
a guarantee that no real personal data is present.

---

## What gets built

| Component | Note |
|---|---|
| `Consent` model and writes | Table exists since the initial migration; no model class. |
| Location pin ingestion | Parse `Latitude`/`Longitude` from the Twilio webhook; resolve against community boundaries; flag out-of-bounds. |
| Registration conversation | Consent statement plus three questions, in the intake agent. Writes `StormFile` at `REGISTERED`, `thin = true`, `synthetic = true`. |
| Exposure evaluation | Per household, per advisory, against the forecast wind fields. |
| Alert templates | Pre-approved; Patois and English. |
| **Send gate** | Live sending behind an explicit flag. Replay never sets it. |
| Reply router | `1`, `2`, `STOP`. `2` hands to intake with safety-of-life detection live. |

## What this is not

- **Not assisted registration.** `assisted_by` stays unused. The coverage skew
  in §2 is the price.
- **Not a complete vulnerability assessment.** 37% of the signal is uncollected
  and `thin = true` says so.
- **Not a confirmed location.** §5.
- **Not a real registry.** No household data outside consenting demo
  participants, and those rows are identifiable and deletable by their consent
  row.

## Verification

- A phone that has never contacted the system can register and reach
  `REGISTERED` with `thin = true`, `synthetic = true`, and a `consent` row.
- A pin dropped in the sea is flagged rather than stored as a dwelling.
- A registered household inside a forecast wind field receives one alert per
  advisory, and a replay run produces **zero** outbound messages.
- Replying `2` opens an intake conversation that produces a claim, and that
  claim's neighbour signal finds corroborating households.
- Replying `STOP` stops alerts and leaves the consent row intact.
- Deleting every row that has a consent row leaves the seeded population and the
  ledger chain valid.
