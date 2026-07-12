# Synthetic Oura Fixture Bundle

Synthetic Oura API v2 usercollection page documents for
`solstone/think/importers/oura.py`. Field names mirror the real API shape
(`{"data": [...], "next_token": ...}` pages; `id`, `day`, `score`,
`contributors`, stage durations in seconds) but every value is invented.
This directory intentionally contains no real account, device, or health
data, and never may.

Files: one `<endpoint>.json` per supported endpoint — `daily_sleep`,
`daily_readiness`, `daily_resilience`, `daily_stress`, `daily_spo2`,
`sleep`, plus the AH-mirror overlap endpoints imported per decision
O-5C: `daily_activity` (document-shaped) and `heartrate` (a time-series
page whose rows carry `timestamp`/`bpm`/`source` and no document `id` or
`day` — day attribution comes verbatim from the timestamp's date part),
plus the 2026-07-07 additions: `daily_cardiovascular_age`
(document-shaped, per openapi-1.35) and `blood_glucose` (a time-series
page whose PINNED-ASSUMPTION rows carry `timestamp`/`glucose` in UTC —
the endpoint is absent from the published spec **and is partner-gated**:
Oura's developer portal exposes no `metabolic` scope to standard apps,
so `blood_glucose` sits in `_PARTNER_GATED_ENDPOINTS` and the sync
engine never polls it; the fixture keeps the normalization machinery
tested for a future partner grant or file import).

The 2026-07-07 granted-scope expansion adds four more endpoint files,
all shapes verified against openapi-1.35 **and** live probes (workout /
session / enhanced_tag returned real rows; vO2_max returned an empty
page, so its rows here follow the documented `PublicVO2Max` shape):

- `workout.json` — document-shaped `{id, activity, day, start_datetime,
  end_datetime, intensity, source}` + nullable `calories` (kcal),
  `distance` (m), `label`. Datetimes are wearer-local offsets (never
  UTC-Z); the second row starts at 23:12 local so its UTC instant
  crosses midnight — the journal day must stay Oura's `day` verbatim.
- `session.json` — `{id, day, start_datetime, end_datetime, type}` +
  nullable `mood` and `heart_rate`/`heart_rate_variability`/
  `motion_count` sample blocks (`{interval, items, timestamp}`). The
  sample blocks stay in raw pages only — normalized metadata carries
  just `type`/`mood`.
- `enhanced_tag.json` — the ONLY document endpoint with no `day` field:
  `{id, start_time, start_day}` required + nullable `tag_type_code`,
  `end_time`, `end_day`, `comment`, `custom_name`. Journal day is
  `start_day` verbatim; the second row spans into the next day.
- `vO2_max.json` — `{id, day, timestamp, vo2_max}` (route casing is
  exactly `vO2_max`; lowercase 404s live).

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
