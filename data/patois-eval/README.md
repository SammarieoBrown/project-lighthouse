# Patois eval set (NFR-G-03)

**This directory is the ruler. The measurement is not here yet, and must not be invented.**

The set is roughly 100 held-out utterances from Jamaican speakers, each with a
hand-written reference transcript and hand-written reference fields. Collecting
it depends on other people saying words into a phone, which is why it has been
the oldest open item in the project: it cannot be written, only gathered.

Nothing in this repository generates the audio or the labels. A synthetic eval
set scores well and means nothing, and the entire point of NFR-G-03 is to find
out whether the transcription is good enough to trust.

## Format

One JSON object per line in `manifest.jsonl`:

```json
{"utterance_id": "se-001", "audio_path": "audio/se-001.ogg", "reference_transcript": "Mi roof gone an mi need tarpaulin", "reference_damage_type": "roof_damage", "reference_needs": ["tarpaulin"]}
```

- `reference_transcript` — what the speaker actually said, written by a human who speaks Patois.
- `reference_damage_type` — one of the extractor's canonical types, or `null` if the utterance names none.
- `reference_needs` — the canonical needs actually stated. An empty list is a real label.

Keep the audio out of git if it carries a real voice. **Nothing in this set may
be a real household's claim** — the synthetic-only rule covers eval data too,
so these are volunteers reading scenarios, not people who lost a roof.

## Running it

`app/patois_eval.py` scores a transcriber against the set and reports word
error rate and per-field extraction accuracy **separately**, because Phase 2
makes the fine-tune decision conditional on which half is the bottleneck. A
single blended score cannot answer that question, so the scorer does not
produce one.

With an empty manifest it reports no cases and says so. That is the honest
reading and it is what it will report until the recordings exist.
