# Health Imports

This document is the source-truth planning boundary for Apple Health, Oura, and glucose data imports into Solstone.

## Current Scope

The current Apple Health importer supports a gated synthetic/test-week save path:

- Preview Apple Health `export.xml` data from a directory or zip.
- Filter previews and save runs with `--date-from YYYY-MM-DD` and `--date-to YYYY-MM-DD`.
- Require the health pre-save gate before any non-dry-run Apple Health write.
- Install the source export under `imports/<id>/raw/`.
- Write normalized monthly JSONL under `imports/<id>/normalized/`.
- Keep importer-owned record dedupe in `imports/health-dedupe.sqlite`.
- Optionally write small factual day-summary transcript files with `--with-day-summaries`.

The live journal and real Apple Health export remain outside automated tests. Use synthetic fixtures and temp/sandbox journals only until the owner creates the approval artifact and runs the first live test-week command.

## Apple Health Local Save Path

Apple Health has a concrete importer save path for orchestrated use after privacy preflight. The importer writes only under the provided `journal_root`: raw source material under `imports/<id>/raw/`, normalized monthly JSONL under `imports/<id>/normalized/`, importer-owned dedupe rows in `imports/health-dedupe.sqlite`, and optional factual day-summary transcript files under `chronicle/YYYYMMDD/import.apple_health/000000_300/`.

Dense normalized JSONL shards are not returned in `ImportResult.files_created`; only optional day-summary transcript files are returned there so indexers do not ingest per-sample health rows.

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

Health-owned day-summary stream identities live in `HEALTH_IMPORTER_REGISTRY` in `health_schema.py`. The Apple Health summary stream name is `import.apple_health`.

The Oura stream identity `import.oura` is reserved there for exclusion/ownership bookkeeping only; no Oura day-summary writer exists. Later source-specific streams can be added only after privacy preflight and save-mode tests exist:

- `import.oura`
- `import.dexcom_clarity`
- `import.health_auto_export`

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
- Confirm whether raw export files should be discarded, parsed-only retained, or completely retained.
- Confirm whether replicated devices or backups are allowed to carry raw health data.
- Never send raw health data, tokens, service-account JSON, or export files through Oracle, Claude, or other remote review prompts.
- Never commit real health fixtures.
- Keep summaries factual and avoid medical interpretation.

The required approval artifact lives at `imports/_approvals/health_import_preflight.json` in the target journal. It must match `solstone.health_import_preflight.checklist.v3`, bind an absolute `journal_root`, contain a closed `raw_retention.decision` of `discard`, `retain_parsed`, or `retain_complete`, and contain a decision for each replication destination: `time_machine`, `icloud`, `solbase`, `hosted_backup`, and `other`. `retain_complete` also requires `raw_retention.unparsed_sensitive_modalities_acknowledged: true`.

## Current Deferred Work

Shipped on this branch:

- Oura OAuth via `journal importer --connect oura`.
- Oura token storage and refresh in journal config through `oura_auth.py`.
- Oura API sync that writes health bundles and sync cursor state.

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
