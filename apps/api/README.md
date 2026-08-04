# api

Python modular monolith (FastAPI). Two entrypoints: `web` (API + webhooks) and `worker` (agents + ingestion). Scaffolded in Phase 0. See `docs/engineering/lighthouse-build-spec.md`.

The authenticated allocation, Finance settlement, and public-ledger steps are in
[`act3-approval-runbook.md`](../../docs/engineering/act3-approval-runbook.md).

## WhatsApp media intake

The webhook never downloads media. It files a partial claim and queues an
`intake_media_enrichment` job. The worker then:

1. downloads the exact account/message-bound Twilio Media resource with a
   restricted API key (Account SID/AuthToken is rejected in production),
2. follows only Twilio's one allowlisted secure-media redirect without
   forwarding credentials,
3. enforces the declared MIME type, file signature, deadline, and content-aware
   byte limit (1 MiB for voice notes; 16 MiB for images),
4. writes a content-addressed object to private R2 and records its SHA-256
   (plus a real perceptual hash for decodable photos),
5. transcribes audio with the explicitly configured provider, preserves Patois
   wording, extracts only stated damage/needs, and only then queues verification.

The provider URL and transcript are private. Worker errors and logs contain
only safe error classes, never media URLs, provider bodies, or speech text.
All provider boundaries have injected network-free test doubles.
