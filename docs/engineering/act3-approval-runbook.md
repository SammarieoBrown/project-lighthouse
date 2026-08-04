# Act 3 allocation and simulated settlement

Act 3 now has two separate human gates and three public money states:

1. a Director signs one fixed `JMD 45,000.00` allocation;
2. a Finance Officer signs a batch that binds exactly that allocation;
3. an explicitly enabled demo executor records execution and a simulated
   provider-style confirmation, then moves the Claim and StormFile to
   `SETTLED` in the same transaction.

There is **no real payment provider in this release**. The simulator never
contacts a bank, mobile-money service, voucher issuer, or other payment rail.
Its API and ledger provenance is `SIMULATED_DEMO`; every confirmation says
`no_real_money_moved: true`. A real provider adapter remains an external
integration and must not reuse the simulator's provenance.

## Execution is fail-closed

The default is:

```dotenv
DISBURSEMENT_EXECUTOR_MODE=disabled
```

With that value, batch signing still works, but execution returns `503` and the
disbursement remains `PENDING`. The Render demo Blueprint opts in explicitly:

```dotenv
DISBURSEMENT_EXECUTOR_MODE=simulated
```

Do not interpret that setting as a payment-provider configuration. It enables
only `LIGHTHOUSE_DEMO_EXECUTOR_V1`.

## Issue five-minute human credentials

An `app_user` must already exist and be active. Allocation signing requires a
`DIRECTOR`; batch signing and execution require a `FINANCE_OFFICER`.

Set each user's password once through the no-echo terminal prompt:

```bash
cd apps/api
uv run python -m app.approval_credentials set-password --email director@example.org
uv run python -m app.approval_credentials set-password --email finance@example.org
```

Issue a credential immediately before each human decision:

```bash
cd apps/api
uv run python -m app.approval_credentials issue --email director@example.org
uv run python -m app.approval_credentials issue --email finance@example.org
```

The bearer value is shown once on the controlling terminal. It is never written
to stdout, logs, or Postgres; only its SHA-256 digest is stored. Keep it in UI
memory, discard it on page exit, and reauthenticate after five minutes.

## Gate 1 — Director allocation

```http
POST /v1/claims/{claim_id}/allocations/approve
Authorization: Bearer <Director credential>
Idempotency-Key: <fresh UUID>
Content-Type: application/json

{
  "resource": "CASH",
  "amount": "45000.00",
  "currency": "JMD",
  "payer_route": "GOV_RELIEF",
  "note": "optional internal note"
}
```

The Claim and its StormFile must be `VERIFIED`. The allocation binds the latest
eligible immutable Verification and its database-generated snapshot hash. The
signature, plan, allocation, and `allocation.approved` ledger receipt commit
atomically. The truthful money state is still `NOT_INITIATED`.

## Finance queue

```http
GET /v1/settlements?limit=100
Authorization: Bearer <Finance Officer credential>
```

This protected response exposes only the redacted operational identity needed
to act. Its state is one of:

- `AWAITING_FINANCE_SIGNATURE`
- `SIGNED_PENDING_SIMULATED_EXECUTION`
- `SIMULATED_EXECUTING`
- `SIMULATED_CONFIRMED`
- `SIMULATED_FAILED`

It also says whether demo execution is enabled. A Finance Officer should not
infer availability from a signed batch.

## Gate 2 — Finance batch signature

```http
POST /v1/allocations/{allocation_id}/disbursements/sign
Authorization: Bearer <Finance Officer credential>
Idempotency-Key: <fresh UUID>
Content-Type: application/json

{
  "channel": "BANK",
  "executor_provenance": "SIMULATED_DEMO",
  "note": "Finance review completed"
}
```

`BANK`, `MOBILE_MONEY`, and `VOUCHER` are accepted for this cash grant; `GOODS`
is not. The response creates one immutable signed batch and one `PENDING`
disbursement. It returns `money_movement: NOT_INITIATED` and
`no_real_money_moved: true`.

Postgres enforces all of the following, independently of the API:

- the signer is an active Finance Officer with recent reauthentication;
- the Approval signs that exact batch ID;
- one batch binds one allocation and one disbursement;
- channel, total, approval, allocation, and executor provenance cannot mutate;
- the batch and disbursement carry database-generated SHA-256 snapshot hashes;
- exactly one `disbursement.batch_signed` receipt exists before commit.

## Explicit demo execution and confirmation

```http
POST /v1/disbursements/{disbursement_id}/execute
Authorization: Bearer <Finance Officer credential>
Idempotency-Key: <fresh UUID>
Content-Type: application/json

{
  "executor_provenance": "SIMULATED_DEMO",
  "acknowledge_no_real_money": true
}
```

The acknowledgement is a required literal `true`. When simulation is enabled,
the service records these steps atomically:

1. `PENDING -> EXECUTING` under the Finance Officer's idempotent intent;
2. an immutable `disbursement.executed` receipt labelled
   `SIMULATION_EXECUTED_NO_REAL_FUNDS`;
3. a local demo confirmation reference and receipt hash;
4. `EXECUTING -> CONFIRMED` only after that confirmation exists;
5. an immutable `disbursement.confirmed` receipt labelled
   `SIMULATED_CONFIRMATION_RECORDED_NO_REAL_FUNDS`;
6. Claim C6 and StormFile T8 transitions to `SETTLED` through the existing state
   machine.

If any database guard, receipt append, confirmation, or state transition fails,
the transaction rolls back. It cannot leave a committed `CONFIRMED` row without
both execution and confirmation receipts.

## Idempotency and lost responses

Keep the same key while retrying the exact same intent:

- the first batch signature returns `201`; an exact retry returns `200` and the
  original records;
- execution returns `200`; an exact retry returns the original confirmation;
- changing the body under the same key returns `409`;
- changing the key after that batch/disbursement already advanced returns
  `409` rather than creating a duplicate;
- one Finance Officer cannot reuse an execution key for another disbursement.

Do not generate a new key merely because the client lost the response. Refresh
the protected settlement queue if the original key is unavailable.

## Public ledger and aggregate

```http
GET /v1/public/ledger?latest=true&limit=50
```

The public stream distinguishes exactly these publishable actions:

- `allocation.approved` — no movement initiated;
- `disbursement.executed` — simulation ran, no real funds;
- `disbursement.confirmed` — simulated confirmation recorded, no real funds.

`disbursement.batch_signed` remains an internal audit receipt. Public entries
contain UTC dates, hashes, the fixed relief shape, channel, and simulated
provenance. They never contain claim/allocation/disbursement IDs, claim
references, signer IDs, provider confirmation references, parish/community,
need/damage category, transcript/media data, or exact timestamps.

The `aggregate` is derived only from immutable, hash-verified
`disbursement.confirmed` ledger receipts. It groups count and JMD amount by
channel with scope `CONFIRMED_SIMULATED_RELIEF_ONLY`; it never groups on a
household dimension. Both the full-chain proof and aggregate cache are keyed by
the immutable ledger head and fail closed on invalid receipts.

## Recovery and escalation

- `503 execution disabled`: keep the signed row pending; do not alter it in SQL.
- lost HTTP response: retry with the same Idempotency-Key.
- `409`: refresh the queue and compare the durable state before retrying.
- ledger integrity `503`: stop publication and settlement; investigate instead
  of regenerating hashes or deleting rows.
- migration `0008` refuses any legacy batch/disbursement rows. That is
  deliberate: old confirmation claims cannot be truthfully backfilled. Audit
  and reconcile them explicitly before retrying the migration.

Replacing the simulator requires a separate reviewed provider adapter,
authenticated callbacks/confirmation verification, provider-secret handling,
failure/retry reconciliation, and a new provenance value. Until then, no API or
screen may say that real money moved.
