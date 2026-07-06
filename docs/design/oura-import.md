# Oura API Lane — Design

- **Date:** 2026-07-05 (overnight lane, owner-authorized)
- **Repo:** `/Users/jack/solpbc/solstone`, branch `health-imports-phase1`
- **Companion skeleton:** `solstone/think/importers/oura.py` + `tests/test_oura_importer.py` + synthetic fixtures under `tests/fixtures/importers/health/oura_synthetic/` (landed with this doc; see §9)
- **Hard rules honored:** no network code anywhere (a test greps the module for network-capable imports), no OAuth against real Oura, no live-journal writes, no credentials or token files, synthetic fixtures only. The first live OAuth authorization is **OWNER-PRESENT-ONLY** (§8, phase O2).
- **Copy canon:** §13 of the repo guide. Oura's numbers render as attributed facts — "Readiness 82 · Oura's score" — never our gloss, never medical interpretation.

---

## 1. Where this fits in the existing architecture

The health-import stack this lane extends (all shipped on `health-imports-phase1`):

| Piece | File | Role |
|---|---|---|
| Apple Health importer | `solstone/think/importers/apple_health.py` | Detect/preview/save `export.xml`; normalized monthly shards; optional day summaries |
| Shared health schema | `solstone/think/importers/health_schema.py` | Source families, friendly names, sleep-session math, dedupe-key functions |
| Dedupe store | `solstone/think/importers/health_dedupe.py` | `imports/health-dedupe.sqlite`, batch upserts keyed by `dedupe_key` |
| Pre-save gate | `solstone/think/importers/pre_save_gate.py` | Fail-closed approval gate for `SENSITIVE_IMPORTERS` save mode |
| File-importer registry | `solstone/think/importers/file_importer.py` | `FILE_IMPORTER_REGISTRY`, detect/preview/process protocol |
| Sync framework | `solstone/think/importers/sync.py` | `SyncableBackend` protocol, `SYNCABLE_REGISTRY`, cursor state at `imports/<backend>.json` |
| Body app | `solstone/apps/body/` | Read-only archive + day pages over normalized shards and the dedupe DB |

Oura joins as a **second health source family** using the same storage, dedupe, gate, and (later) sync machinery. Nothing about the Apple Health path changes.

---

## 2. (a) What the Oura API v2 adds beyond the Apple Health mirror

Jack's ring already reaches the journal indirectly: the Oura app mirrors some series into Apple Health, and those arrive with `source_family="apple_health"` and a ring `source_name`. What the mirror carries (empirically: sleep stages and some vitals) versus what it **cannot** carry (Oura's computed scores and contributors) is the reason this lane exists.

### Endpoints worth importing (Oura API v2, `https://api.ouraring.com/v2/usercollection/...`)

Endpoint names below are from model knowledge of the v2 API; **verify each against live docs at phase O2 when network work is authorized**. Confidence flags: ✅ high, ◑ moderate, ⚠ verify.

| Endpoint | Payload (key fields) | Beyond the AH mirror? | Confidence |
|---|---|---|---|
| `daily_sleep` | `score`, `contributors` (deep_sleep, efficiency, latency, rem_sleep, restfulness, timing, total_sleep), `day` | **Yes** — the sleep score and its contributor breakdown never cross into HealthKit | ✅ |
| `daily_readiness` | `score`, `contributors` (activity_balance, body_temperature, hrv_balance, previous_day_activity, previous_night, recovery_index, resting_heart_rate, sleep_balance), `temperature_deviation`, `temperature_trend_deviation` | **Yes** — readiness score and °C temperature deviation are Oura-only | ✅ |
| `daily_resilience` | `level` (limited/adequate/solid/strong/exceptional), `contributors` (sleep_recovery, daytime_recovery, stress) | **Yes** — resilience is Oura-only; endpoint added ~mid-2024 | ◑ (field names ⚠) |
| `daily_stress` | `stress_high` (s), `recovery_high` (s), `day_summary` (restored/normal/stressful) | **Yes** — daytime stress minutes are Oura-only | ◑ |
| `daily_spo2` | `spo2_percentage.average`, `breathing_disturbance_index` | **Yes** — nightly SpO2 average + BDI; AH mirror may carry raw SpO2 samples but not Oura's nightly average/BDI | ◑ |
| `sleep` | per-period: `bedtime_start/end`, `type` (long_sleep/late_nap/…), stage durations (`deep_sleep_duration`, `rem_sleep_duration`, `light_sleep_duration`, `awake_time`), `efficiency`, `latency`, `average_heart_rate`, `lowest_heart_rate`, `average_hrv`, `average_breath`, `sleep_phase_5_min` hypnogram (1=deep, 2=light, 3=REM, 4=awake) | **Partly** — stages also mirror into AH as `HKCategoryValueSleepAnalysis*` intervals, but Oura-native durations, efficiency/latency, per-period HRV/HR aggregates, and the 5-minute hypnogram string are richer and carry Oura's own period identity (`id`, `day` attribution) | ✅ |
| `heartrate`, `daily_activity`, `workout`, `session`, `enhanced_tag`, `sleep_time`, `ring_configuration`, `vO2max` / `daily_cardiovascular_age` | series + activity + tags + device | Mostly **duplicates the AH mirror** (HR, steps, workouts) or is metadata; excluded from the first import scope to avoid double-counting in presentation | ⚠ names |

Other API facts to verify live at O2: OAuth2 endpoints (`cloud.ouraring.com/oauth/authorize`, `api.ouraring.com/oauth/token` ⚠), scopes (`email personal daily heartrate workout tag session spo2` ⚠), rate limit (historically 5000 requests / 5 min ⚠), the no-auth sandbox (`/v2/sandbox/usercollection/*` ⚠), personal-access-token deprecation status ⚠, webhook subscription API ⚠.

**Skeleton scope (implemented):** `daily_sleep`, `daily_readiness` (+ split-out `temperature_deviation` rows), `daily_resilience`, `daily_stress`, `daily_spo2`, `sleep`. That is exactly the "scores + stages" slice the day pages need and the AH mirror can't provide.

### Do we still need the pending Oura export?

**Keep the request open, but nothing waits on it.**

- The API serves full account history for every endpoint above (paged by `start_date`/`end_date` + `next_token`), so backfill does not need the export.
- The export still earns its keep as: (1) an offline raw archive independent of API availability and dev-app approval; (2) possibly the only carrier of legacy/older-generation or full-resolution data the v2 API doesn't expose (⚠ unknown until inspected); (3) a zero-network import path that could run under today's gate before OAuth is ever authorized.
- When it arrives: inspect read-only; if its shape matches API documents, the existing parse layer covers it; if not, it gets its own parser under the **reserved** `SOURCE_OURA = "oura"` family. Records from export and API deliberately do **not** collapse at import (per `docs/health_imports.md` — cross-source reconciliation happens at query/presentation time, and document `id`s should match if the export carries them).

---

## 3. (b) Presentation — day pages, new card, overview, window API

All owner-facing strings follow §13: attributed facts, no surveillance verbs, no medical interpretation. The body app (`solstone/apps/body/`) is another agent's surface; this section is the spec it implements.

### Copy rules (the whole §13/no-interpretation contract in one table)

| Do | Don't |
|---|---|
| `Readiness 82 · Oura's score` | "You're well recovered" |
| `Sleep score 88 · Oura's score` | "Great sleep!" |
| `Resilience solid · Oura's level` | "Your resilience is strong — keep it up" |
| `Deep 1h 31m · REM 1h 48m · Light 4h 00m · Awake 32m — Oura's staging` | Any re-derived or re-weighted stage math presented as ours |
| `Temperature deviation +0.34 °C · Oura's measurement` | "Possible fever" / any clinical reading |
| `Day stress summary normal · Oura's label` | Color-coding stress as good/bad beyond quoting Oura's own label, attributed |

If Oura's own qualitative band is shown (e.g., "optimal"), it renders quoted and attributed ("Oura calls this optimal"), never as our judgment.

### Existing cards that absorb Oura data

- **Sleep card (day page).** `oura.sleep` rows join the day's sleep interval pool. The primary-source rule in `health_schema.pick_day_sleep` is unchanged — Oura becomes one more source in `intervals_by_source`, and when it wins coverage the card renders Oura's stage breakdown from period metadata (`deep/rem/light/awake` durations) instead of interval-derived math, labeled "— Oura's staging". The day's `oura.daily_sleep` score renders as one attributed line on the same card: `Sleep score 88 · Oura's score`. Oura's `day` attribution (night belongs to the day it ended) already matches the journal's cross-midnight canon, so no re-attribution.
- **Coverage families (`_FAMILY_RULES` in `body/routes.py`).** Fragment additions:
  - `Sleep` gains `("oura.daily_sleep", "oura.sleep")`
  - `Heart` gains `("oura.daily_spo2",)` (consistent with `OxygenSaturation` living in Heart)
  - New family **`Recovery`** (ordered after Glucose, before Activity in `_FAMILY_ORDER`) claims `("oura.daily_readiness", "oura.daily_resilience", "oura.daily_stress", "oura.temperature_deviation")`
- **Sources / audit surfaces.** Source label for `source_family="oura_api"` renders as "Oura API". The archive day-grid needs no change — Oura rows enter `health-dedupe.sqlite` and count per day like any other family.
- **Friendly names** already landed in `health_schema.FRIENDLY_TYPE_NAMES` (`oura.daily_readiness` → "Readiness", etc.), so any generic signal list renders cleanly today.

### New day-page card: "How recovered am I?"

Renders only when the day has at least one of readiness / resilience / stress / temperature-deviation / SpO2 rows (same "cards appear only with data" rule as the rest of the day page).

```
How recovered am I?
Readiness 82 · Oura's score
Resilience solid · Oura's level
Temperature deviation −0.21 °C · Oura's measurement
Nightly blood oxygen 97.4% · Oura's average (breathing disturbance index 3)
Daytime stress 2h 0m high · 5h 40m recovery · Oura's day summary: normal
▸ Oura's contributors  (disclosure: raw contributor numbers, verbatim, attributed)
```

The skeleton's `render_day_summary()` in `oura.py` is the copy reference implementation — the card and the (optional, later) `import.oura` day-summary transcript must agree with it line-for-line in register.

- **Day lede** gains at most one clause when present: `…, readiness 82 (Oura's score)`.
- **Day prompts** may gain: "What did my day look like around the readiness dip on {date}?" — phrased as a question about the journal, never advice.

### Overview / coverage additions

- Archive coverage chips pick up the `Recovery` family automatically once `_FAMILY_RULES` lands.
- The overview's sources snapshot lists "Oura API" with last-seen day (from dedupe rows), marking staleness with the existing `STALE_SOURCE_DAYS` rule — factual ("last brought in N days ago"), not alarming.

### Window API additions (`/api/window`)

Oura's daily documents are day-granularity; they don't belong inside intra-day windows as samples. Two additions:

1. `events`: `oura.sleep` periods are true intervals (`bedtime_start`/`bedtime_end`) — include them in the window's events list like workouts (they already fit `_row_interval`).
2. New `day_context` block: for each calendar day the window overlaps, the day's Oura score rows as attributed facts — `{"day": "20260102", "facts": ["Readiness 82 · Oura's score", ...]}`. Windows never interpolate a daily score across hours.

---

## 4. (c) Storage

Mirrors `apple_health` exactly; all writes live under `imports/**` plus (optionally, save phase) declared day-summary transcript files — L7-clean.

```
imports/<import_id>/
  raw/oura/<endpoint>/<NNNN>.json      # verbatim API page documents (save phase)
  normalized/<YYYY-MM>.jsonl           # monthly shards, schema solstone.health.oura.v1
  manifest.json                        # shared.write_manifest, source_type "oura"
  content_manifest.jsonl               # shared.write_content_manifest
imports/health-dedupe.sqlite           # shared dedupe DB (existing)
imports/oura.json                      # sync cursor (phase O3; never tokens)
chronicle/<day>/import.oura/000000_300/day_summary_transcript.md   # optional, save phase
```

**Normalized row** (implemented in the skeleton):

```json
{
  "schema": "solstone.health.oura.v1",
  "source_family": "oura_api",
  "kind": "daily_summary" | "sleep_period",
  "record_type": "oura.daily_readiness" | "oura.daily_sleep" | "oura.daily_resilience"
               | "oura.daily_stress" | "oura.daily_spo2" | "oura.temperature_deviation"
               | "oura.sleep",
  "dedupe_key": "sha256:…",
  "day": "20260102",
  "start_date": "...", "end_date": "... (sleep periods only)",
  "source_record_id": "<oura document id>",
  "value": 82, "unit": "score|degC|%|s|null",
  "metadata": { "contributors": {...}, "stage durations": "…", "…": "…" },
  "raw_ref": "imports/<id>/raw/oura#<endpoint>-<n>"
}
```

**Source family: `oura_api`** (new constant `SOURCE_OURA_API` in `health_schema.py`, added to `KNOWN_SOURCE_FAMILIES`). Three Oura-adjacent families now exist by design and never collapse at import: `apple_health` (mirror rows), `oura_api` (this lane), `oura` (reserved for the pending account export).

**Dedupe keys** go through `health_schema.health_record_dedupe_key`. Every Oura v2 document carries a stable `id`, so keys take the `source-id` path (`source_family` + `record_type` + `source_record_id`). Consequences, both wanted:

- Oura *revises* recent documents (scores settle for a day or two). Same `id` → same key → the dedupe upsert updates in place (`value_hash` records that the payload changed) instead of duplicating. Re-fetching a trailing window is idempotent (L9).
- The temperature-deviation row splits out of the readiness document with `source_record_id = "<readiness id>/temperature_deviation"`, keeping its identity distinct and stable.

---

## 5. (d) Sync design

**Backend.** `OuraSyncBackend` (skeleton class exists; `sync()` raises with a pointer here). Registered in `SYNCABLE_REGISTRY` **only at phase O3** — the skeleton deliberately leaves it unregistered so no runtime flow (CLI `--sync`, export tooling) can reach a half-built path; a test pins that until O3 flips both together.

**Cursor state** at `imports/oura.json` via `sync.load_sync_state`/`save_sync_state`:

```json
{
  "schema": "solstone.import_sync.oura.v1",
  "last_sync_at": "2026-07-10T06:00:00Z",
  "endpoints": { "daily_sleep": {"high_water_day": "2026-07-09"}, "…": {} },
  "backfill": { "complete": false, "oldest_fetched_day": "2026-03-01" },
  "last_result": { "pages": 4, "rows": 61, "inserted": 58, "updated": 3 }
}
```

Never tokens, never client credentials, never raw values in the cursor. Catalog (dry-run) sync writes **nothing**, including the cursor; the cursor advances only on gated save runs.

**Poll cadence.** The ring reaches Oura's cloud only when the phone app syncs, so aggressive polling buys nothing. Default: every 6 hours via the existing scheduler, plus manual `sol import --sync oura` (catalog by default, `--save` for the gated write path). Each save run re-fetches a trailing 7-day window to pick up Oura's document revisions — idempotent by document-id keys.

**Backfill.** The API serves full history: page each endpoint in 30-day `start_date`/`end_date` chunks, following `next_token`, walking back from today until pages come back empty (or from the `personal_info` registration date if exposed). Resumable via `backfill.oldest_fetched_day`; runs inside the same gate + rate-limit budget (limit figure ⚠ verify at O2). Backfill is just repeated save-mode sync — no special write path.

**OAuth (design only; phase O2, OWNER-PRESENT-ONLY).**

- Authorization-code flow; **PKCE preferred if Oura supports it** (⚠ unverified — Oura's documented flow historically uses a client secret; if PKCE isn't supported, the confidential-client secret lives behind the same token boundary below, and nothing else changes).
- Redirect: loopback `http://localhost:<ephemeral>/callback` on the journal host, opened in the owner's browser with the owner at the keyboard. No headless, no automated retry, no unattended re-auth ever; if tokens die, sync degrades to a factual "authorization needed" status until Jack runs the step again.
- **Token boundary: journal configuration, never the repo.** Client id, (secret if applicable), access + refresh tokens live in the journal's config domain under the reserved key `oura` (`OAUTH_CONFIG_KEY` in the skeleton), written exclusively through the config owner `solstone/think/journal_config.py` (L2). Never in this repository, never in env vars, never in logs, never in `imports/oura.json`, never through Oracle/Claude prompts. Refresh-token rotation writes through the same owner.
- **Dev-account cap noted:** Oura developer apps are limited to roughly 10 users before requiring Oura's partnership review — irrelevant for a single owner, but it means client credentials must never be shared or committed, and a future multi-owner story needs Oura's blessing first.
- Scopes: request the minimum for the import scope (daily + sleep + spo2-family; exact scope names ⚠ verify at O2).

---

## 6. (e) Gate

Landed in the skeleton:

- `SENSITIVE_IMPORTERS = frozenset({"apple_health", "oura"})` in `pre_save_gate.py`.
- Same approval artifact (`imports/_approvals/health_import_preflight.json`, same `APPROVAL_SCHEMA`/`CHECKLIST_VERSION`): `approved_importers` must include `"oura"`; all five replication-destination decisions and the raw-retention decision apply unchanged to Oura data.
- Same per-run `--confirm-health-save` requirement; `OuraImporter.process()` enforces the gate itself in save mode (defense in depth alongside the CLI's pre-`process` enforcement), **before** any parse or write, then stops at the phase-O1 seam.
- Tests prove: missing artifact blocks; artifact without `"oura"` in `approved_importers` blocks (`importer_not_approved`); missing per-run confirmation blocks; a fully approved run still writes nothing (seam); failure payloads leak no fixture paths or values.

Phase O3 extends the same gate to sync: any save-mode `sync()` calls `enforce_pre_save_gate("oura", dry_run=False, confirm_health_save=...)` before its first journal write, with the confirm flag passed explicitly from the CLI/scheduler invocation (scheduled runs may only be enabled after Jack records the approval artifact and opts the schedule in — a scheduled job never self-confirms implicitly; the opt-in is the standing confirmation, documented in the artifact's notes).

---

## 7. (f) Phased rollout

| Phase | Contents | Guardrail |
|---|---|---|
| **O0 — landed tonight** | Design doc; `oura.py` parse/normalize/dedupe skeleton; synthetic fixtures; `"oura"` in `SENSITIVE_IMPORTERS`; file-importer registry entry with only detect/preview/dry-run live; sync + OAuth seams raise `NotImplementedError` | No network code (test-enforced); no journal writes; no sync registry entry |
| **O1 — file-import save path (synthetic only)** | Raw install under `imports/<id>/raw/oura/`; normalized shards; dedupe upserts; optional `--with-day-summaries` writing `import.oura` transcripts from `render_day_summary`; L2 table + hygiene-script owner entries extended to `oura.py`; body app absorbs record types (family rules, sleep card, "How recovered am I?" card, window `day_context`) | Gate enforced; synthetic fixtures and temp journals only until Jack's separate live approval |
| **O2 — first OAuth. OWNER-PRESENT-ONLY.** | Register Oura dev app; verify PKCE vs confidential, scopes, rate limits, sandbox against live docs; interactive `sol import oura auth` with Jack at the keyboard; tokens land in journal config via `journal_config.py`; single `personal_info` verification call; nothing unattended | **Jack physically present for every step**; no credentials anywhere but journal config |
| **O3 — sync** | Real `sync()` (catalog default, gated save); `SYNCABLE_REGISTRY` entry + flip the phase-guard test; cursor state `imports/oura.json`; trailing-7-day revision window; 30-day backfill chunks; double-run idempotence verified | Gate before first write; catalog mode writes nothing |
| **O4 — steady state** | 6-hourly schedule (opt-in per §6); backfill completion; pending-export reconciliation when it arrives (read-only inspection first); webhooks study (deferred — needs a public endpoint) | Scheduled runs only after explicit opt-in recorded in the approval artifact |

---

## 8. Open questions for Jack (morning)

1. **PKCE vs client secret** — can't verify Oura's current OAuth support offline; decides whether a secret enters journal config at O2.
2. **Family naming** — keeping `oura_api` vs `oura` split assumes the account export may still arrive with a different shape. If you cancel the export request, we could collapse to one `oura` family before any live data exists (cheapest moment to rename).
3. **Day-summary transcripts** — should Oura write optional `import.oura` day summaries like Apple Health does (`--with-day-summaries`), or should day pages read normalized rows only? Skeleton renders the copy either way.
4. **`daily_activity` / `heartrate` endpoints** — excluded to avoid double-counting the AH mirror. Confirm, or pick a precedence rule for presentation.
5. **Canonical home for this doc** — copy into `docs/design/` (repo) as the durable reference?
6. **Oura sandbox API** — a no-auth synthetic endpoint (⚠ verify) could de-risk O3 before real OAuth; still network, so it needs your explicit go-ahead like any other network step.

---

## 9. What landed with this doc (phase O0 inventory)

- `solstone/think/importers/oura.py` — parse layer (`parse_oura_bundle`, `parse_endpoint_document`, `parse_oura_day`), normalizer (`normalize_bundle` → rows + `HealthDedupeRecord`s via `health_schema`), §13 copy reference (`render_day_summary`), `OuraImporter` (detect/preview/dry-run live; save gated then seamed), `OuraSyncBackend` + OAuth seams (all raise, pointing here). Zero network imports, test-enforced.
- `solstone/think/importers/health_schema.py` — `SOURCE_OURA_API`, `KNOWN_SOURCE_FAMILIES` entry, friendly names for the seven `oura.*` record types.
- `solstone/think/importers/pre_save_gate.py` — `"oura"` joins `SENSITIVE_IMPORTERS`.
- `solstone/think/importers/file_importer.py` — registry entry (preview/dry-run-only paths active).
- `tests/fixtures/importers/health/oura_synthetic/` — six endpoint documents, API-page-shaped, arithmetic-consistent, fully synthetic.
- `tests/test_oura_importer.py` — 30 tests: registration/gate membership, parse validation, normalization + dedupe-key stability and cross-family non-collision, JSONL round-trip, detect/preview/dry-run, gate enforcement (blocks before any write; approved runs still write nothing past the seam), §13 rendering, seam errors, no-network guard.
