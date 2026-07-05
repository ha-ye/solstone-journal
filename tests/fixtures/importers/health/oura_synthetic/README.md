# Synthetic Oura Fixture Bundle

Synthetic Oura API v2 usercollection page documents for
`solstone/think/importers/oura.py`. Field names mirror the real API shape
(`{"data": [...], "next_token": ...}` pages; `id`, `day`, `score`,
`contributors`, stage durations in seconds) but every value is invented.
This directory intentionally contains no real account, device, or health
data, and never may.

Files: one `<endpoint>.json` per supported endpoint — `daily_sleep`,
`daily_readiness`, `daily_resilience`, `daily_stress`, `daily_spo2`,
`sleep`.

## revisions/

Re-issued page documents for the revision/upsert de-risk tests in
`tests/test_oura_importer.py`. Oura corrects recent documents in place:
a later fetch of the same endpoint returns the same document `id` with a
changed payload (scores settle for a day or two after the night). The
dedupe design requires the same `id` to keep the same dedupe key so the
revision updates rather than duplicates (`oura_design_20260705.md` §4c).

`revisions/daily_readiness.json` re-issues the base
`daily_readiness.json` page:

- `synthetic-readiness-2026-01-02` — the revised document: corrected
  score (82 → 79), corrected temperature deviation (-0.21 → -0.05),
  corrected trend deviation, one changed contributor (`hrv_balance`
  79 → 81), and a drifted `timestamp` (Oura may restamp on re-issue).
  Same document id, so every derived row's dedupe key must not change.
- `synthetic-readiness-2026-01-03` — a byte-identical re-issue: an
  unchanged document inside a re-fetched trailing window must upsert as
  a pure duplicate.

`parse_oura_bundle` never descends into subdirectories, so this
directory is invisible to detect/preview over the base bundle.
