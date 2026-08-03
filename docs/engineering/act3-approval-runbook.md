# Act 3 allocation approval

This slice records a Director-approved allocation and one append-only ledger
entry. It does **not** create a disbursement batch, call a bank or payment
provider, or claim that funds moved. The resulting state is
`APPROVED_NOT_DISBURSED`; money movement is `NOT_INITIATED`.

## Issue a five-minute human credential

An `app_user` must already exist and be active. Allocation signing requires the
`DIRECTOR` role; protected claim reads may also allow `REVIEW_CLERK`.

Set that user's password once, interactively:

```bash
cd apps/api
uv run python -m app.approval_credentials set-password --email director@example.org
```

Issue a credential after re-entering the password:

```bash
cd apps/api
uv run python -m app.approval_credentials issue --email director@example.org
```

Passwords are accepted only through a no-echo prompt. The bearer value is
shown once on the controlling terminal, never written to stdout, application
logs, or Postgres. Postgres stores only its SHA-256 digest. The credential
expires five minutes after reauthentication; keep it in UI memory only and
discard it on expiry or page exit.

## Approve once

```http
POST /v1/claims/{claim_id}/allocations/approve
Authorization: Bearer <five-minute credential>
Idempotency-Key: <fresh client-generated UUID>
Content-Type: application/json

{
  "resource": "CASH",
  "amount": "45000.00",
  "currency": "JMD",
  "payer_route": "GOV_RELIEF",
  "note": "optional internal note"
}
```

This release accepts only the exact `CASH` / `JMD 45000.00` / `GOV_RELIEF`
shape above. The claim must be `VERIFIED`, its StormFile must be `VERIFIED` or
`SETTLED`, and its latest immutable Verification must satisfy the release
threshold or carry an active Review Clerk's approval. The allocation and
internal ledger receipt bind that Verification's database-generated snapshot
hash. A new request returns `201`; an exact retry with the same key returns the
original records with `200` and
`idempotent_replay: true`. Reusing the key for different content returns `409`.
The signature, plan, allocation, and ledger entry commit in one database
transaction and become immutable.

Public-safe records are available without authentication at:

```http
GET /v1/public/ledger?after_seq=0&limit=50
```

Use `latest=true&limit=N` to select the newest N approval receipts.

That endpoint exposes only allowlisted `allocation.approved` receipt facts. It
omits stable row/subject/claim/signer IDs, parish, need category, and exact
timestamps; each receipt carries a UTC date only. It reports cached full-chain
integrity separately and labels the immutable point-in-time movement state
`NOT_INITIATED_AT_APPROVAL`.
