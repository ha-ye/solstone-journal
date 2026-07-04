# Health Imports Phase 0

This document is the source-truth planning boundary for Apple Health, Oura, and glucose data imports into Solstone.

## Phase 0 Scope

Phase 0 is repository setup only:

- Define the health import schema and dedupe substrate.
- Add synthetic fixtures for Apple Health, Oura, and Dexcom/Stelo-style glucose exports.
- Add an Apple Health detector and preview parser that can read synthetic export directories and zip files.
- Keep Apple Health save-mode blocked and keep the importer out of `FILE_IMPORTER_REGISTRY`.
- Keep all fixtures synthetic. Do not commit real Apple Health, Oura, Stelo, Dexcom, location, or token data.

Phase 0 must not import health data into the live journal.

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

## Import Streams

The expected first stream name is `import.apple_health`.

Later source-specific streams can be added only after privacy preflight and save-mode tests exist:

- `import.oura`
- `import.dexcom_clarity`
- `import.health_auto_export`

## Dedupe Boundary

Health dedupe state belongs under:

`imports/health-dedupe.sqlite`

The dedupe database is importer-owned state. It must not live in entities, facets, observations, activities, indexer state, or app config.

Dedupe keys must include the source family so Apple Health, Oura, and Dexcom records do not collide. When a source supplies a stable record identifier, use that identifier. When it does not, use source family, record type, start/end time, source name, and a stable value hash.

Do not collapse cross-source records during import. Preserve source attribution and reconcile later in query, review, or summary layers.

## Privacy Checklist

Before any save-mode health import is registered or exposed:

- Require explicit user confirmation that the export contains sensitive health data.
- Print or display the target journal path before writing.
- Confirm whether raw export files should be retained, copied, or discarded.
- Confirm whether replicated devices or backups are allowed to carry raw health data.
- Never send raw health data, tokens, service-account JSON, or export files through Oracle, Claude, or other remote review prompts.
- Never commit real health fixtures.
- Keep summaries factual and avoid medical interpretation.

## Deferred Work

The following are intentionally out of scope for Phase 0:

- Oura OAuth, token storage, API sync, or webhooks.
- Health Auto Export or custom HealthKit ingest endpoints.
- Any LAN, public, or phone-to-Mac health ingest service.
- Apple Health save-mode import registration.
- Entity, facet, observation, activity, or indexer writes.
- Medical advice, recommendations, or anomaly interpretation.

## Phase 0 Verification

Run these before treating Phase 0 as complete:

- `make test-only TEST=tests/test_health_dedupe.py`
- `make test-only TEST=tests/test_apple_health_importer.py`
- `make check-layer-hygiene`
- `make check-journal-io-access`
- `make check-journal-io-mechanic`

Run `make ci` before committing or handing this to a release branch.
