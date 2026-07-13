# Health Imports

This document is the source-truth planning boundary for Apple Health, Oura, and glucose data imports into Solstone.

## Current Scope

The current Apple Health importer supports a gated synthetic/test-week save path:

- Preview Apple Health `export.xml` data from a directory or zip.
- Filter previews and save runs with `--date-from YYYY-MM-DD` and `--date-to YYYY-MM-DD`.
- Require the health pre-save gate before any non-dry-run Apple Health write.
- Apply the approved raw-retention decision before installing raw source material.
- Write normalized monthly JSONL under `imports/<id>/normalized/`.
- Keep importer-owned record dedupe in `imports/health-dedupe.sqlite`.
- Optionally write small factual day-summary transcript files with `--with-day-summaries`.

The live journal and real Apple Health export remain outside automated tests. Use synthetic fixtures and temp/sandbox journals only until the owner creates the approval artifact and runs the first live test-week command.

## Apple Health Local Save Path

Apple Health has a concrete importer save path for orchestrated use after privacy preflight. The importer writes only under the provided `journal_root`: approved raw source material under `imports/<id>/raw/`, normalized monthly JSONL under `imports/<id>/normalized/`, importer-owned dedupe rows in `imports/health-dedupe.sqlite`, and optional factual day-summary transcript files under `chronicle/YYYYMMDD/import.apple_health/000000_300/`.

Dense normalized JSONL shards are not returned in `ImportResult.files_created`; only optional day-summary transcript files are returned there so indexers do not ingest per-sample health rows.

Raw retention is enforced from the validated gate decision. `discard` writes no `raw/` directory and normalized rows carry no `raw_ref`. `retain_parsed` installs only `raw/export.xml` for Apple Health, whether the input was a zip or an export directory. `retain_complete` is the only Apple Health branch that copies the original zip or full export tree. Oura API sync accepts `discard` and `retain_parsed`: parsed retention keeps the raw API page JSONL files, while discard writes normalized shards, manifests, dedupe rows, fetch windows, and cursor state without raw page files or raw refs.

All files written under `imports/` by the shared importer writers are installed as `0600`. Import-owned directories under `imports/` are created or repaired as `0700`. The approval-artifact directory `imports/_approvals/` remains manually owner-managed and is not created or repaired by the read-only gate.

## Source Strategy

Use Apple Health as the broad local bus:

- workouts
- steps
- heart-rate samples
- sleep records
- glucose values written by Stelo through HealthKit
- other HealthKit quantities and categories the user chooses to export

Use the Oura API for Oura-native semantics that Apple Health does not preserve well:

- readiness and resilience scores
- detailed sleep contributors
- tag and session metadata
- ring battery and device metadata when useful

Use a Dexcom/Stelo CSV fixture only as a synthetic glucose shape reference until a real export or API path is explicitly chosen.

## Date Attribution

Apple Health records are assigned to the local calendar day encoded in each record's own timestamp string, including its written timezone offset. The importer does not convert all records into the Mac's current timezone before windowing or grouping. A record with `startDate="2026-01-02 22:30:00 -0700"` belongs to `20260102`.

## Import Streams

`health_schema.HEALTH_CARD_STREAM_BY_FAMILY` is the single declaring registry
for health card streams; code resolves stream names from it rather than
hardcoding them. Registering a family's card stream there excludes it from
sense/think/entities before any writer exists, protecting derived summaries
ahead of the code that writes them.

Today `apple_health` declares `import.apple_health` (writer shipped), `oura_api`
declares `import.oura` (registered and excluded; no writer yet), and
`dexcom_clarity` declares no card stream. A new health source that writes a day
card needs exactly one registry edit; privacy preflight and save-mode tests still
gate shipping the writer.

## Dedupe Boundary

Health dedupe state belongs under:

`imports/health-dedupe.sqlite`

The dedupe database is importer-owned state. It must not live in entities, facets, observations, activities, indexer state, or app config.

Dedupe keys must include the source family so Apple Health, Oura, and Dexcom records do not collide. When a source supplies a stable record identifier, use that identifier. When it does not, use source family, record type, start/end time, source name, and a stable value hash.

Do not collapse cross-source records during import. Preserve source attribution and reconcile later in query, review, or summary layers.

## Apple Health Date Attribution

Apple Health Phase 1 attributes records and workouts to the local calendar day of their `startDate`. Date windows are inclusive and filter on that attributed start day. This means a sleep record that starts before midnight and ends the next morning belongs to the start day for preview counts, normalized monthly shards, dedupe rows, and optional day-summary transcript placement.

## Privacy Checklist

Before any live-journal save-mode health import:

- Require explicit user confirmation that the export contains sensitive health data.
- Print or display the target journal path before writing.
- Choose a closed raw-retention decision:
  - `discard`: Apple Health writes no `raw/`; Oura writes no raw API page JSONL; normalized rows and newly inserted dedupe rows carry no `raw_ref`.
  - `retain_parsed`: Apple Health stores only `imports/<id>/raw/export.xml` for either zip or directory inputs; Oura stores parsed raw API page JSONL under `imports/<id>/raw/oura/`.
  - `retain_complete`: Apple Health may copy the original zip or full export tree; Oura rejects this value as source-incompatible.
- Set `raw_retention.unparsed_sensitive_modalities_acknowledged: true` when, and only when, `retain_complete` is chosen.
- Confirm whether replicated devices or backups are allowed to carry raw health data.
- Never send raw health data, tokens, service-account JSON, or export files through Oracle, Claude, or other remote review prompts.
- Never commit real health fixtures.
- Keep summaries factual and avoid medical interpretation.

The required approval artifact lives at `imports/_approvals/health_import_preflight.json` in the target journal. It must match `solstone.health_import_preflight.checklist.v3`, bind an absolute `journal_root`, contain a closed `raw_retention.decision` of `discard`, `retain_parsed`, or `retain_complete`, and contain a decision for each replication destination: `time_machine`, `icloud`, `solbase`, `hosted_backup`, and `other`. `retain_complete` also requires `raw_retention.unparsed_sensitive_modalities_acknowledged: true`.

The Oura sync approval artifact lives at `imports/_approvals/oura_sync_preflight.json`. It must match `solstone.oura_sync_preflight.checklist.v2`, use the same raw-retention enum (`discard` or `retain_parsed` only), bind an absolute `journal_root`, and include the replication decisions. Scheduled sync consent additionally requires `scheduled_sync.approved: true`, a cadence, and a timezone-aware ISO-8601 `scheduled_sync.valid_until`; expired or malformed standing consent fails closed before any network or journal write.

Owner remediation after this hardening lands is manual by design: update both approval artifacts to checklist v3/v2, migrate old raw-retention strings to the enum (`retain_compressed_zip` -> `retain_complete` only if the owner accepts complete Apple Health raw retention; `retain_raw_pages` -> `retain_parsed` for Oura), add `unparsed_sensitive_modalities_acknowledged: true` only for `retain_complete`, and add/refresh scheduled `valid_until` for Oura if unattended sync should continue.

The Oura connect flow now asks future authorization requests for nine scopes: `daily`, `heartrate`, `workout`, `tag`, `session`, `spo2`, `stress`, `heart_health`, and `metabolic`. Removing `email` and `personal` changes only what future authorization requests ask for. It does not retroactively revoke scopes already granted on an already-issued token. Narrowing an existing token's granted scopes requires owner-present re-consent and/or revoking the old token; this code change does not perform that operator action.

## Current Deferred Work

Shipped on this branch:

- Oura OAuth via `journal importer --connect oura`.
- Oura token storage and refresh in journal config through `oura_auth.py`.
- Oura API sync that writes health bundles and sync cursor state.
- Oura save-mode sync locking, scheduled-consent expiry, and egress/scope guardrails.

Still deferred:

- Oura webhooks.
- Oura file-import save path; `OuraImporter.process(...)` gates save mode and then raises `NotImplementedError`.
- Health Auto Export or custom HealthKit ingest endpoints.
- Any LAN, public, or phone-to-Mac health ingest service.
- Entity, facet, observation, activity, or indexer writes.
- Medical advice, recommendations, or anomaly interpretation.

## Health Import Verification

Run these before treating health import changes as complete:

- `make test-only TEST=tests/test_health_dedupe.py`
- `make test-only TEST=tests/test_apple_health_importer.py`
- `make check-layer-hygiene`
- `make check-journal-io-access`
- `make check-journal-io-mechanic`

Run `make ci` before committing or handing this to a release branch.
