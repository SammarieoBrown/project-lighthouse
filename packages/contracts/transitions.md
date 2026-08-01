# Storm File state machine — transition table

Phase 0 contract freeze. Version 0.1, August 1 2026.

The state machine **is** the orchestration. Each agent is a worker authorised to perform specific transitions and nothing else; every transition emits a ledger entry and, where a follow-on agent is required, enqueues its job **in the same transaction**.

If a transition is not in this table it is illegal. The orchestrator rejects unknown transitions rather than inferring intent.

```
REGISTERED ──▶ AT_RISK ──▶ AFFECTED ──▶ VERIFIED ──▶ SETTLED
     │             │           ▲                        
     └─────────────┴───────────┘                        
       (unregistered reporters enter directly at AFFECTED)
```

## Storm File transitions

| # | From | To | Trigger | Performed by | Gate | Emits |
|---|---|---|---|---|---|---|
| T1 | — | `REGISTERED` | Registration conversation completes with consent (REG-01/03) | Intake Agent | none | `household.registered` |
| T2 | `REGISTERED` | `AT_RISK` | 64kt probability at the household crosses the configured threshold (IMP-03) | Risk Mapper | none | `household.at_risk` |
| T3 | `AT_RISK` | `REGISTERED` | Posture falls to QUIET or the household leaves the projected impact zone | Forecast Sentinel | none | `household.stood_down` |
| T4 | `REGISTERED` / `AT_RISK` | `AFFECTED` | A claim is filed for this household (INT-01/02) | Intake Agent | none | `claim.created` |
| T5 | — | `AFFECTED` | Claim filed by an unregistered reporter; a thin Storm File is created inline (INT-01) | Intake Agent | none | `household.registered`, `claim.created` |
| T6 | `AFFECTED` | `VERIFIED` | Verification confidence ≥ 0.85 (VER-02) | Verification Agent | none — confidence-gated | `claim.verified` |
| T7 | `AFFECTED` | `VERIFIED` | Review clerk approves from the evidence bundle (VER-03) | Review Clerk | human decision, **not** a money gate | `claim.verified` |
| T8 | `VERIFIED` | `SETTLED` | A disbursement or delivery for this household is confirmed (PAY-04) | Ledger Agent | **G3 already signed** — plus the database invariant | `household.settled` |

**No other Storm File transition is legal.** In particular: `AFFECTED → SETTLED` (skips verification), `REGISTERED → VERIFIED` (verifies a claim that does not exist), and any transition *out of* `SETTLED` are all rejected.

`T8` is guarded twice on purpose — once by the orchestrator and once by the `storm_file_settled_guard` trigger in `schema.sql`. The trigger is the one that matters, because it survives somebody forgetting the orchestrator.

## Claim status transitions

A Storm File is long-lived; a claim is per-event. They move together but are not the same object — a household can have a rejected claim and still be `AT_RISK` for the next storm.

| # | From | To | Trigger | Performed by | Gate |
|---|---|---|---|---|---|
| C1 | — | `FILED` | Intake completes, or 3 unanswered follow-ups force a partial (INT-03) | Intake Agent | none |
| C2 | `FILED` | `VERIFIED` | Confidence ≥ 0.85, or clerk approval | Verification Agent / Review Clerk | none |
| C3 | `FILED` | `REJECTED` | Clerk rejects with a recorded reason (VER-03) | Review Clerk | none |
| C4 | `FILED` | `WITHDRAWN` | Household withdraws over WhatsApp | Intake Agent | none |
| C5 | `REJECTED` | `FILED` | Household replies APPEAL; one appeal per claim (VER-06) | Intake Agent | none |
| C6 | `VERIFIED` | `SETTLED` | First confirmed disbursement or delivery | Ledger Agent | G3 already signed |

**T2R is measured `C1 → C6`** — `claim.filed_at` to `claim.settled_at`. The clock starts when the household speaks, not when we finish verifying (PRD §3).

## Human gates

Three gates, one `approval` table, role-checked. ADM-02 requires re-authentication at the moment of signing; an approval row without `reauth_at` is invalid.

| Gate | What it authorises | Role | Blocks |
|---|---|---|---|
| **G1** | Alert cascade may send (ALT-01) | Director | Any outbound alert to households |
| **G2** | Allocation plan may execute (LGX-02) | Director | Stock decrement, run sheet issue |
| **G3** | Disbursement batch may execute (PAY-01) | Finance Officer | Any `disbursement` row existing at all |

G3 is enforced by `disbursement.approval_id NOT NULL` — a disbursement is literally unwritable before a Finance Officer signs. That is the "no code path moves money without a signature" rule expressed where it cannot be forgotten.

**Agents may never perform a gated action.** They propose: an alert cascade draft, an allocation plan, a batch. The transition belongs to the human.

## Agent authority

| Agent | May perform | Autonomy |
|---|---|---|
| Forecast Sentinel | T3 | Autonomous — no harm possible |
| Risk Mapper | T2 | Autonomous |
| Alert Agent | *none* | Propose only → G1 |
| Intake Agent | T1, T4, T5, C1, C4, C5 | Autonomous |
| Verification Agent | T6, C2 | Confidence-gated; below 0.85 routes to a human |
| Triage Agent | *none* — annotates severity and rank | Autonomous |
| Logistics Agent | *none* | Propose only → G2 |
| Ledger Agent | T8, C6 | Autonomous, but only *after* G3 |

The Triage and Alert agents hold no transition authority at all. That is deliberate: an agent that cannot move a file cannot lose one.

## Safety-of-life bypass

A claim flagged `sol` (INT-04) skips every queue, pages the on-duty human immediately, and rides the job queue at `priority = 100`. It does **not** skip verification or any gate — it changes ordering, never authority. Build and test this path first, not last.

## Invariants

1. Every transition writes exactly one `ledger_entry`, in the same transaction as the state change.
2. Every transition that has a follow-on enqueues its `agent_job` in that same transaction. This is why the queue is Postgres and not Redis (PRD §11.6) — a file cannot change state while its next job is silently lost.
3. No agent writes to `allocation`, `disbursement`, or `approval`. Agents propose; the orchestrator writes; humans sign.
4. Every agent verdict is stored raw, including ones a human later overrides. Overrides are new `verification` rows linked by `overrides_id`, never edits (VER-07).
5. `verify_chain()` passes at all times.
