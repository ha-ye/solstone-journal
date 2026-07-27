# Native `sol` Client Prep Findings

## 1. Reviewer Frozen-Input Availability (P0)

### Decision Verdict

| Frozen input | Required pin / shape | Search result | Verdict |
|---|---:|---|---|
| `sol-call-grammar-v1` grammar oracle | 174 entries; canonical length 113,106 bytes; SHA-256 prefix `c61078f0...dd117` | No artifact, ref, blob, tag, note, stash, branch, working-tree file, or local agent-tooling state file found by the exact searches below. | **OPEN BLOCKER: missing.** |
| 20-file Python-oracle tree digest | SHA-256 `1d14f01a819f2f44bfe229603aa38861cda3460ff1ca66b9593a33b6172a772d` | No explicit manifest found. Plausible 20-file sets did not reproduce the pin. | **OPEN BLOCKER: unreproducible.** |
| Parity corpus | argv/help/stdout/stderr/exit/request-shape vectors plus permitted normalizations | No target corpus found. Existing parity material is unrelated ad hoc test/fixture material, not the requested fixed reviewer corpus. | **OPEN BLOCKER: missing.** |

### Search Evidence

| Scope | Commands / recipe | Result |
|---|---|---|
| Git refs | Ref, tag, note, stash, and log searches across all refs for the grammar, parity, oracle, corpus, and native-sol needles, including name-status history searches. | No target artifact hits. `git notes list` and `git stash list` were empty. |
| Exact all-ref greps | `git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/stash refs/notes | xargs ... git grep -n -I -F <needle>` for `sol-call-grammar-v1`, `c61078f0`, full `1d14...a772d`, and `request-shape` | No output. |
| Named prep branches | Checked whether two named prep branches were already ancestors of main, then inspected each branch for commits not on main. | Both branches are ancestors of `main`; no unique commits. |
| Working tree | Tracked-file listing filtered by the same needles, plus recursive content and filename searches of the working tree excluding `.git`, `.venv`, and `core/target`. | Only unrelated files such as existing markdown parity fixtures and normal contract/openapi files. |
| Local agent-tooling state | Recursive content and filename searches of the local agent-tooling state directory for the same needles. | No output. |
| Unreachable Git objects | `git fsck --full --no-reflogs --unreachable`, then exact grep across unreachable blobs for the same needles | No target hits. |

### 20-File Digest Reconstruction

| Attempt | Manifest / recipe | SHA-256 result |
|---|---|---:|
| Initial plausible 20-file set | `printf '%s\n' <paths> | sort > /tmp/native_sol_candidate_paths.txt`; `xargs git ls-tree HEAD -- < /tmp/native_sol_candidate_paths.txt | sort | sha256sum` | `36c2cb291fc62a6da917ca3d17d53eddde2683492e1451d0deb025a86ae82e63` |
| Serialization variants on initial set | sorted raw `git ls-tree`; sorted-by-path `git ls-tree`; `path sha`; `sha path`; `sha` only; path only | None matched; examples included `43f82dc6...`, `e48ab2fa...`, `1a9a52c...`. |
| Brute force | Fixed likely base: `solstone/think/convey_client.py`, `solstone/think/utils.py`, `solstone/think/service.py`, `solstone/apps/activities/call.py`, `solstone/apps/support/call.py`, `solstone/think/tools/health.py`, `solstone/think/pipeline_health.py`, `solstone/think/chat_cli.py`, `solstone/think/call.py`; chose 11 from 18 plausible production dependencies; checked 31,824 combinations. | No match in raw sorted `git ls-tree` format. Closest arbitrary prefix: `1d14714d5545f5e9f1817fe5ecf0be17df0f16e3172d201a886205bd5bea5f28`. |
| Brute-force serialization variants | Same 31,824 combinations across six recipes: raw sorted `git ls-tree`, path-sorted `git ls-tree`, `path sha`, `sha path`, sha-only, path-only. | No match. |

**Decision question:** The reviewer's frozen inputs are **not present** and the 20-file digest is **not reproducible to pin** from plausible current-tree candidates. Because the oracle canonical serialization is nowhere specified and the digest cannot be reproduced, this is an **OPEN BLOCKER** for senior escalation.

## 2. Grammar-Oracle Format Reconnaissance (P1)

### Current CLI Discovery Shape

| Fact | Evidence |
|---|---|
| `sol call` mounts `solstone/apps/*/call.py` modules that export a Typer `app`; app directory names become subcommands. | `solstone/think/call.py:33`, `solstone/think/call.py:53`, `solstone/think/call.py:60`, `solstone/think/call.py:62`, `solstone/think/call.py:73` |
| The only app-name override is `network` -> `link`. | `solstone/think/call.py:22`, `solstone/think/call.py:24`, `solstone/think/call.py:73` |
| Built-in `sol call` Typer apps are mounted for `health`, `journal`, `ledger`, and `profile`; moved stubs are mounted for `navigate` and `identity`. | `solstone/think/call.py:89`, `solstone/think/call.py:120`, `solstone/think/call.py:125` |
| Moved stubs accept extra args/options, print `Moved to \`journal {cmd}\` — run that instead.` to stderr, and exit 2. | `solstone/think/call.py:96`, `solstone/think/call.py:102`, `solstone/think/call.py:114`, `solstone/think/call.py:115` |
| Nearest precedent extracts Typer command names only using `typer.main.get_command(app)`. | `solstone/think/skills_build.py:156`, `solstone/think/skills_build.py:159`, `solstone/think/skills_build.py:160` |
| Top-level `sol chat` is outside `sol call`; it is registered as an access command. | `solstone/think/sol_cli.py:200`, `solstone/think/sol_cli.py:204`; argparse grammar is in `solstone/think/chat_cli.py:244`, `solstone/think/chat_cli.py:252` |

### Full `sol call` Surface Today

Live Typer introspection of `solstone.think.call:call_app` returns **172 leaves**. The grouped leaves:

| App | Leaves |
|---|---|
| `activities` | `create`, `get`, `list`, `mute`, `unmute`, `update` |
| `awareness` | `imports`, `log`, `log-read`, `status` |
| `body` | `day`, `status`, `window` |
| `chat` | `start` |
| `entities` | `accept-merge-candidate`, `aka`, `ambiguities`, `attach`, `detect`, `dismiss-merge-candidate`, `entity-history`, `history`, `list`, `merge`, `merge-candidates`, `move`, `network`, `observations`, `observe`, `overview`, `record-merge-candidate`, `resolve-ambiguity`, `restore-version`, `search`, `undo-merge`, `update` |
| `facets` | `accept`, `dismiss`, `list-candidates` |
| `health` | `for-range`, `full`, `pipeline`, `summary` |
| `identity` | moved stub |
| `import` | `list-staged`, `resolve-config`, `resolve-config-all`, `resolve-entity`, `resolve-staged-facet` |
| `journal` | `agents`, `export`, `facet create`, `facet delete`, `facet doctor`, `facet merge`, `facet mute`, `facet rename`, `facet show`, `facet unmute`, `facet update`, `facets`, `import`, `imports`, `merge`, `news`, `read`, `retention config`, `retention purge`, `search`, `storage-summary` |
| `ledger` | `close`, `decisions`, `get`, `list` |
| `link` | `authorized-clients`, `list`, `observer-pause`, `pair`, `private-link disable`, `private-link setup`, `private-link status`, `status`, `unpair` |
| `navigate` | moved stub |
| `profile` | `brief`, `cadence`, `full`, `list-active` |
| `settings` | `convey status`, `identity set`, `identity show`, `keys clear`, `keys set`, `keys show`, `keys validate`, `observer set`, `observer show`, `processing set`, `processing show`, `show`, `transcribe set-backend`, `transcribe show` |
| `sol` | `reset`, `set-name`, `set-owner`, `sol-init` |
| `speakers` | `attribute-segment`, `backfill`, `backfill-last-seen`, `bootstrap`, `build-from-tags`, `confirm-owner`, `correct`, `day-segments`, `detect`, `discover`, `dismiss-cluster`, `dismissals`, `identify`, `identify-operation`, `identify-operations`, `identify-undo`, `keep-separate-list`, `link-import`, `merge-names`, `owner-ready`, `presence`, `propagate-correction`, `rebuild-owner`, `reject-owner`, `resolve-names`, `seed-from-imports`, `sentences`, `status`, `suggest`, `tag-owner`, `wipe` |
| `support` | `announcements`, `article`, `attach`, `create`, `diagnose`, `feedback`, `list`, `register`, `reply`, `search`, `show` |
| `thinking` | `clear-local-endpoint`, `keys clear`, `keys set`, `keys show`, `keys validate`, `local availability`, `local bootstrap`, `local bootstrap-status`, `local models`, `local readiness`, `local status`, `providers set-active`, `providers show`, `scout check`, `scout disable`, `scout enable`, `scout refresh`, `scout status`, `set-local-endpoint` |
| `transcripts` | `read`, `scan`, `segments`, `speakers`, `stats` |

**174 decomposition:** 174 is not current `sol call` leaves alone. Plausible decompositions are `172` current `sol call` leaves + top-level `sol chat` + one root/help/callback grammar entry, or `172` current leaves + two moved-stub special entries. The frozen format is missing, so this cannot be decided.

### Lead Inventory Confirmation

| Slice | Expected verbs | Actual source |
|---|---|---|
| activities (6) | `list`, `get`, `create`, `update`, `mute`, `unmute` | `solstone/apps/activities/call.py:240`, `solstone/apps/activities/call.py:332`, `solstone/apps/activities/call.py:371`, `solstone/apps/activities/call.py:510`, `solstone/apps/activities/call.py:627`, `solstone/apps/activities/call.py:657` |
| support (11) | `register`, `search`, `article`, `create`, `list`, `show`, `reply`, `attach`, `feedback`, `announcements`, `diagnose` | `solstone/apps/support/call.py:175`, `solstone/apps/support/call.py:185`, `solstone/apps/support/call.py:205`, `solstone/apps/support/call.py:223`, `solstone/apps/support/call.py:343`, `solstone/apps/support/call.py:370`, `solstone/apps/support/call.py:412`, `solstone/apps/support/call.py:460`, `solstone/apps/support/call.py:552`, `solstone/apps/support/call.py:607`, `solstone/apps/support/call.py:632` |
| health (4) | `summary`, `full`, `for-range`, `pipeline` | `solstone/think/tools/health.py:128`, `solstone/think/tools/health.py:146`, `solstone/think/tools/health.py:164`, `solstone/think/tools/health.py:185` |
| moved stubs (2) | `identity`, `navigate` | `solstone/think/call.py:123`, `solstone/think/call.py:125` |
| top-level chat | `sol chat` | `solstone/think/sol_cli.py:204`, `solstone/think/chat_cli.py:244` |

**Delta:** the lead inventory contains 21 `sol call` leaves, but only 20 are HTTP leaves: `health pipeline` is a local wrapper around `solstone.think.pipeline_health.summarize_pipeline_day`, not a Convey HTTP request. Evidence: `solstone/think/tools/health.py:198`, `solstone/think/tools/health.py:211`; function owner `solstone/think/pipeline_health.py:255`.

## 3. Leaf -> Route -> Reason-Code Table (P2)

### Shared Activity Grammar

| Mechanism | Exact behavior |
|---|---|
| Env defaults | `SOL_DAY` and `SOL_FACET` are read by `_get_sol_day()` / `_get_sol_facet()`. Required resolvers print `Error: day is required (pass as argument or set SOL_DAY).` or `Error: facet is required (pass as argument or set SOL_FACET).` and exit 1. `list` defaults day to `SOL_DAY` or today's `YYYYMMDD`. `solstone/apps/activities/call.py:30`, `solstone/apps/activities/call.py:35`, `solstone/apps/activities/call.py:38`, `solstone/apps/activities/call.py:45`, `solstone/apps/activities/call.py:48`, `solstone/apps/activities/call.py:54`, `solstone/apps/activities/call.py:57`, `solstone/apps/activities/call.py:66` |
| Stdin JSON mode | Empty/non-object stdin prints `Error: expected JSON object on stdin.`; invalid JSON prints `Error: invalid JSON on stdin: {exc}`; exit 1. `update` may pass `allow_empty=True`. `solstone/apps/activities/call.py:81`, `solstone/apps/activities/call.py:94`, `solstone/apps/activities/call.py:97` |
| Participation validation | `participation` must be a list of objects; each entry requires non-empty string `name`, role `attendee|mentioned`, source `voice|speaker_label|transcript|screen|other`, numeric non-bool `confidence`, string `context`; `entity_id` is dropped before send. `solstone/apps/activities/call.py:132`, `solstone/apps/activities/call.py:193` |
| Rendering | `--json` prints `record` payload via `json.dumps(..., indent=2, ensure_ascii=False)`; human mode prints server-provided markdown. `solstone/apps/activities/call.py:102`, `solstone/apps/activities/call.py:236`, `solstone/apps/activities/call.py:324`, `solstone/apps/activities/call.py:329` |
| Routes | Flask blueprint prefix is `/app/activities`; CLI routes live under `/app/activities/api/day/<day>/...`. `solstone/apps/activities/routes.py:56`, `solstone/apps/activities/routes.py:181`, `solstone/apps/activities/routes.py:206`, `solstone/apps/activities/routes.py:291`, `solstone/apps/activities/routes.py:353`, `solstone/apps/activities/routes.py:359` |

### Activities

| Leaf | Argv grammar | Request | Reason-code / stderr / exit | Render |
|---|---|---|---|---|
| `activities list` | Options `--day/-d`, `--from`, `--to`, `--facet/-f`, `--activity/-a`, `--entity`, `--source`, `--all`, `--json`. `--day` conflicts with range; `--source` must be `anticipated|cogitate|user`. | For each resolved day: `GET /app/activities/api/day/{day}/records` with params `include_hidden=1|0` and optional `facet`; `urlencode(..., doseq=True)` is available in shared client. `solstone/apps/activities/call.py:240`, `solstone/apps/activities/call.py:318`; client encoding `solstone/think/convey_client.py:129`, `solstone/think/convey_client.py:132`. | Local validation exits 1 with exact errors at `solstone/apps/activities/call.py:290` and `solstone/apps/activities/call.py:304`; route emits no activity-specific errors for this leaf. | JSON: list of records; human: `No activities found.` or markdown joined by blank lines. `solstone/apps/activities/call.py:324`, `solstone/apps/activities/call.py:329` |
| `activities get SPAN_ID` | Positional `span_id`; options `--facet/-f`, `--day/-d`, `--json`; day/facet required via args or env. | `GET /app/activities/api/day/{day}/record/{span_id}` params `facet`. `solstone/apps/activities/call.py:332`, `solstone/apps/activities/call.py:358` | `activity_not_found` -> `activity not found: {span_id}`, exit 1. Route returns `ACTIVITY_NOT_FOUND` when missing. `solstone/apps/activities/call.py:359`, `solstone/apps/activities/routes.py:201`, `solstone/convey/reasons.py:235` | JSON record or markdown. `solstone/apps/activities/call.py:365`, `solstone/apps/activities/call.py:368` |
| `activities create` | Options `--facet/-f`, `--day/-d`, `--since-segment`, `--source=user`, `--title`, `--activity`, `--description`, `--details`, `--json`. Argv mode requires `--title` and `--activity`; otherwise stdin JSON requires `title` and `activity` and may include `description`, `details`, `participation`. | `POST /app/activities/api/day/{day}/records` params `facet`; JSON includes `title`, `activity`, `source`, optional `description`, `details`, `participation`, `since_segment`. `solstone/apps/activities/call.py:371`, `solstone/apps/activities/call.py:488`; route validates body and writes record. `solstone/apps/activities/routes.py:206`, `solstone/apps/activities/routes.py:288` | Local errors: bad source, bad segment, missing title/activity. Server mappings: `activity_not_found` -> `Error: unknown activity for facet '{facet}': {activity}`; `activity_already_exists` -> `Error: activity already exists: {detail}`; `activity_invalid` -> `Error: {detail}`; all exit 1. Route also may emit `activities_busy`. `solstone/apps/activities/call.py:419`, `solstone/apps/activities/call.py:431`, `solstone/apps/activities/call.py:490`, `solstone/apps/activities/routes.py:216`, `solstone/apps/activities/routes.py:277`, `solstone/apps/activities/routes.py:280`; reasons `solstone/convey/reasons.py:230`, `solstone/convey/reasons.py:240`, `solstone/convey/reasons.py:450` | JSON record or markdown. `solstone/apps/activities/call.py:504`, `solstone/apps/activities/call.py:507` |
| `activities update SPAN_ID` | Positional `span_id`; options `--facet/-f`, `--day/-d`, `--note`, `--title`, `--description`, `--details`, `--json`. If no payload flags, stdin JSON may contain only `title`, `description`, `details`. Empty patch is rejected. | `POST /app/activities/api/day/{day}/record/{span_id}/update` params `facet`; JSON `{"patch": patch, "note": note_text}`; default note is `updated fields: {sorted fields}`. `solstone/apps/activities/call.py:510`, `solstone/apps/activities/call.py:584` | Local extra fields -> `Error: disallowed update fields: {extra}`; empty patch -> `Error: update payload must include at least one mutable field.`; `activity_not_found` -> `activity not found: {span_id}`; route may emit `activities_busy`. `solstone/apps/activities/call.py:566`, `solstone/apps/activities/call.py:575`, `solstone/apps/activities/call.py:586`, `solstone/apps/activities/routes.py:291`, `solstone/apps/activities/routes.py:313`, `solstone/apps/activities/routes.py:315` | JSON record or markdown. `solstone/apps/activities/call.py:591`, `solstone/apps/activities/call.py:594` |
| `activities mute SPAN_ID` | Positional `span_id`; options `--facet/-f`, `--day/-d`, `--reason`, `--json`. | `POST /app/activities/api/day/{day}/record/{span_id}/mute` params `facet`; JSON `{"reason": reason}`. `solstone/apps/activities/call.py:627`, `solstone/apps/activities/call.py:613`, `solstone/apps/activities/routes.py:353` | `activity_not_found` -> `activity not found: {span_id}`; route may emit `activities_busy`. `solstone/apps/activities/call.py:616`, `solstone/apps/activities/routes.py:340`, `solstone/apps/activities/routes.py:342` | JSON record or markdown. `solstone/apps/activities/call.py:621`, `solstone/apps/activities/call.py:624` |
| `activities unmute SPAN_ID` | Positional `span_id`; options `--facet/-f`, `--day/-d`, `--reason`, `--json`. | `POST /app/activities/api/day/{day}/record/{span_id}/unmute` params `facet`; JSON `{"reason": reason}`. `solstone/apps/activities/call.py:657`, `solstone/apps/activities/call.py:613`, `solstone/apps/activities/routes.py:359` | Same as `mute`. `solstone/apps/activities/call.py:616`, `solstone/apps/activities/routes.py:340`, `solstone/apps/activities/routes.py:342` | JSON record or markdown. `solstone/apps/activities/call.py:621`, `solstone/apps/activities/call.py:624` |

### Support

Shared support behavior: support CLI uses `ConveyClient(require_service=False)`; `_support_cli` maps `ConveyUnreachableError` to two-line support fallback and exit 1, and maps other `ConveyClientError` to `err.error` and exit 1. `_check_enabled` calls `GET /app/support/api/config` and exits 1 with `Support agent is disabled in settings.` when disabled. Evidence: `solstone/apps/support/call.py:39`, `solstone/apps/support/call.py:51`, `solstone/apps/support/call.py:61`, `solstone/apps/support/call.py:71`, `solstone/apps/support/call.py:75`.

| Leaf | Argv grammar | Request | Reason-code / stderr / exit | Render / consent |
|---|---|---|---|---|
| `support register` | No args/options. | `_check_enabled`; `POST /app/support/api/register`. `solstone/apps/support/call.py:175`, `solstone/apps/support/call.py:182`; route `solstone/apps/support/routes.py:168` | Routes: `feature_unavailable`, `support_portal_failed`; shared CLI prints `err.error`. `solstone/apps/support/routes.py:171`, `solstone/apps/support/routes.py:179`; reasons `solstone/convey/reasons.py:290`, `solstone/convey/reasons.py:315` | `Registered as: {handle or ?}`. `solstone/apps/support/call.py:182` |
| `support search QUERY` | Positional `query`. | `_check_enabled`; `GET /app/support/api/articles` params `q=query`. `solstone/apps/support/call.py:185`, `solstone/apps/support/call.py:193`; route `solstone/apps/support/routes.py:370` | Routes: `feature_unavailable`, `support_portal_failed`; shared CLI prints `err.error`. `solstone/apps/support/routes.py:373`, `solstone/apps/support/routes.py:383` | `No articles found.` or `  [{slug}] {title}` plus count/instruction. `solstone/apps/support/call.py:194`, `solstone/apps/support/call.py:201` |
| `support article SLUG` | Positional `slug`; option `--json`. | `_check_enabled`; `GET /app/support/api/articles/{slug}`. `solstone/apps/support/call.py:205`, `solstone/apps/support/call.py:214`; route `solstone/apps/support/routes.py:386` | Routes: `feature_unavailable`, `support_portal_failed`; shared CLI prints `err.error`. `solstone/apps/support/routes.py:389`, `solstone/apps/support/routes.py:397` | JSON raw, or `# {title}` then content / `(no content)`. `solstone/apps/support/call.py:216`, `solstone/apps/support/call.py:220` |
| `support create` | Required `--subject/-s`, `--description/-d`; options `--product/-p=solstone`, `--severity=medium`, `--category`, `--skip-kb`, `--submit` default false, `--yes/-y`, `--anonymous`. | Dry run: `GET /app/support/api/diagnostics`, then dormant `POST /app/support/api/draft`. Submit: optional `GET /app/support/api/articles?q=subject`, `GET /app/support/api/diagnostics`, then `POST /app/support/api/tickets` JSON `{subject, description, product, severity, category, user_context, auto_context:false, anonymous}`. `solstone/apps/support/call.py:223`, `solstone/apps/support/call.py:257`, `solstone/apps/support/call.py:267`, `solstone/apps/support/call.py:287`, `solstone/apps/support/call.py:326`; routes `solstone/apps/support/routes.py:64`, `solstone/apps/support/routes.py:219` | Routes: draft `feature_unavailable`, `missing_required_field`, `invalid_request_value`; ticket `feature_unavailable`, `missing_required_field`, `support_portal_failed`. Shared CLI prints `err.error`. `solstone/apps/support/routes.py:72`, `solstone/apps/support/routes.py:143`, `solstone/apps/support/routes.py:149`, `solstone/apps/support/routes.py:222`, `solstone/apps/support/routes.py:230`, `solstone/apps/support/routes.py:251` | Dry run prints exact dry-run preview and captures draft. Submit path KB-confirm prompt `Still want to file a ticket?`; draft confirm `Submit this ticket?`; cancellation text as coded. `solstone/apps/support/call.py:92`, `solstone/apps/support/call.py:106`, `solstone/apps/support/call.py:299`, `solstone/apps/support/call.py:320`, `solstone/apps/support/call.py:322`, `solstone/apps/support/call.py:340` |
| `support list` | Options `--status`, `--json`. | `_check_enabled`; `GET /app/support/api/tickets` with optional `status`. `solstone/apps/support/call.py:343`, `solstone/apps/support/call.py:353`; route `solstone/apps/support/routes.py:188` | Routes: `feature_unavailable`, `support_portal_failed`; shared CLI prints `err.error`. `solstone/apps/support/routes.py:191`, `solstone/apps/support/routes.py:201` | JSON raw, `No tickets found.`, or formatted ticket rows and count. `solstone/apps/support/call.py:354`, `solstone/apps/support/call.py:367` |
| `support show TICKET_ID` | Positional int `ticket_id`; option `--json`. | `_check_enabled`; `GET /app/support/api/tickets/{ticket_id}`. `solstone/apps/support/call.py:370`, `solstone/apps/support/call.py:379`; route `solstone/apps/support/routes.py:204` | Routes: `feature_unavailable`, `support_portal_failed`; shared CLI prints `err.error`. `solstone/apps/support/routes.py:207`, `solstone/apps/support/routes.py:216` | JSON raw or ticket markdown with messages and attachment line `📎 {filename} ({size})`. `solstone/apps/support/call.py:381`, `solstone/apps/support/call.py:409` |
| `support reply TICKET_ID` | Positional int `ticket_id`; required `--body/-b`; option `--submit/--no-submit` default true; `--yes/-y`. | Dry run: dormant `POST /app/support/api/draft` JSON `{verb:"reply", payload:{ticket_id, content}}`. Submit: `POST /app/support/api/tickets/{ticket_id}/reply` JSON `{"content": body}`. `solstone/apps/support/call.py:412`, `solstone/apps/support/call.py:438`, `solstone/apps/support/call.py:452`; route `solstone/apps/support/routes.py:254` | Routes: `feature_unavailable`, `missing_required_field`, `support_portal_failed`; shared CLI prints `err.error`. `solstone/apps/support/routes.py:257`, `solstone/apps/support/routes.py:263`, `solstone/apps/support/routes.py:271` | Dry run text `DRY RUN — nothing was sent. Re-run with --submit to actually send this.`; submit confirm `Send this reply?`; false -> `Cancelled.`; success -> `Reply sent to ticket #{ticket_id}.` `solstone/apps/support/call.py:435`, `solstone/apps/support/call.py:448`, `solstone/apps/support/call.py:449`, `solstone/apps/support/call.py:457` |
| `support attach TICKET_ID FILE...` | Positional int `ticket_id`; one or more file paths; option `--submit/--no-submit` default true; `--yes/-y`. | Dry run: exactly one file, multipart `POST /app/support/api/draft` with `file` field and form `verb=attach`, `ticket_id`. Submit: multipart `POST /app/support/api/tickets/{ticket_id}/attachments` for each file. `solstone/apps/support/call.py:460`, `solstone/apps/support/call.py:491`, `solstone/apps/support/call.py:540`; routes `solstone/apps/support/routes.py:64`, `solstone/apps/support/routes.py:277` | Local dry-run multi-file error `Attach one file at a time when preparing a draft for review.`; missing local file `Error: file not found: {f}`. Routes: `feature_unavailable`, `missing_required_field`, `invalid_request_value`, `support_portal_failed`; per-file submit failures print `Skipped {name}: {err.error}` and continue. `solstone/apps/support/call.py:480`, `solstone/apps/support/call.py:518`, `solstone/apps/support/call.py:547`; route validation `solstone/apps/support/routes.py:75`, `solstone/apps/support/routes.py:89`, `solstone/apps/support/routes.py:98`, `solstone/apps/support/routes.py:283`, `solstone/apps/support/routes.py:298` | Dry run text `DRY RUN — nothing was sent. Re-run without --no-submit to upload this.`; submit review `--- Attachment Review (ticket #{id}) ---`; prompt `Upload these files?`; false -> `Cancelled — nothing was sent.`; success `Attached: {name} (id: {id or ?})`. `solstone/apps/support/call.py:510`, `solstone/apps/support/call.py:522`, `solstone/apps/support/call.py:535`, `solstone/apps/support/call.py:537`, `solstone/apps/support/call.py:549` |
| `support feedback` | Required `--body/-b`; options `--product/-p=solstone`, `--anonymous`, `--submit` default false, `--yes/-y`. | Dry run: `GET /app/support/api/diagnostics`, then `POST /app/support/api/draft` with `{body, product, anonymous}`. Submit: `POST /app/support/api/feedback` JSON `{body, product, anonymous}`. `solstone/apps/support/call.py:552`, `solstone/apps/support/call.py:574`, `solstone/apps/support/call.py:584`, `solstone/apps/support/call.py:599`; route `solstone/apps/support/routes.py:334` | Routes: `feature_unavailable`, `missing_required_field`, `support_portal_failed`; shared CLI prints `err.error`. `solstone/apps/support/routes.py:337`, `solstone/apps/support/routes.py:343`, `solstone/apps/support/routes.py:364` | Dry run uses `FEEDBACK_SUBJECT="feedback"`, severity `low`, category `feedback`; submit prompt `Submit this feedback{anon_note}?`; false -> `Cancelled.`; success `Feedback submitted: #{id or ?}`. `solstone/apps/support/copy.py:6`, `solstone/apps/support/call.py:572`, `solstone/apps/support/call.py:595`, `solstone/apps/support/call.py:596`, `solstone/apps/support/call.py:604` |
| `support announcements` | Option `--json`. | `_check_enabled`; `GET /app/support/api/announcements`. `solstone/apps/support/call.py:607`, `solstone/apps/support/call.py:615`; route `solstone/apps/support/routes.py:400` | Routes: `feature_unavailable`, `support_portal_failed`; shared CLI prints `err.error`. `solstone/apps/support/routes.py:403`, `solstone/apps/support/routes.py:412` | JSON raw; `No active announcements.`; otherwise icon/title/content excerpt and count. `solstone/apps/support/call.py:616`, `solstone/apps/support/call.py:629` |
| `support diagnose` | Option `--json`. No `_support_cli`, no `_check_enabled`. | `GET /app/support/api/diagnostics`; on unreachable, local fallback calls `_local_build_identity()`. `solstone/apps/support/call.py:632`, `solstone/apps/support/call.py:639`, `solstone/apps/support/call.py:640`; route `solstone/apps/support/routes.py:418` | Unreachable prints local diagnostics, then exact support fallback and exits 1. Other client errors print `err.error`, exit 1. `solstone/apps/support/call.py:641`, `solstone/apps/support/call.py:654`, `solstone/apps/support/call.py:656` | JSON raw or human diagnostics: version/platform/python/services/brain/recent errors. `solstone/apps/support/call.py:660`, `solstone/apps/support/call.py:698` |

### Health

| Leaf | Argv grammar | Request | Reason-code / stderr / exit | Render |
|---|---|---|---|---|
| `health summary` | Options `--day`, `--json`. | `GET /api/health/summary` params optional `day`. `solstone/think/tools/health.py:128`, `solstone/think/tools/health.py:137`; route `solstone/convey/health.py:27` | `_handle_health_error` prints `err.detail or err.error`, exit 1. Route maps `ValueError` -> `invalid_request_value` detail `str(exc)`, broad failure -> `health_report_failed` detail `health report unavailable`. `solstone/think/tools/health.py:29`, `solstone/convey/health.py:31`, `solstone/convey/health.py:35`; reasons `solstone/convey/reasons.py:65`, `solstone/convey/reasons.py:282` | JSON indent 2 or `_render_summary`. `solstone/think/tools/health.py:140`, `solstone/think/tools/health.py:143` |
| `health full` | Options `--day`, `--json`. | `GET /api/health/full` params optional `day`. `solstone/think/tools/health.py:146`, `solstone/think/tools/health.py:155`; route `solstone/convey/health.py:39` | Same as `summary`. `solstone/think/tools/health.py:156`, `solstone/convey/health.py:43`, `solstone/convey/health.py:47` | JSON indent 2 or `_render_full`. `solstone/think/tools/health.py:158`, `solstone/think/tools/health.py:161` |
| `health for-range` | Options `--day-from`, `--day-to`, `--json`. | `GET /api/health/range` params optional `day_from`, `day_to`. `solstone/think/tools/health.py:164`, `solstone/think/tools/health.py:176`; route `solstone/convey/health.py:51` | Same as `summary`. `solstone/think/tools/health.py:177`, `solstone/convey/health.py:58`, `solstone/convey/health.py:62` | JSON indent 2 or `_render_full`. `solstone/think/tools/health.py:179`, `solstone/think/tools/health.py:182` |
| `health pipeline` | Options `--day`, `--yesterday`; mutually exclusive. | **No HTTP.** Calls `summarize_pipeline_day(target)` and prints JSON. `solstone/think/tools/health.py:185`, `solstone/think/tools/health.py:198`, `solstone/think/tools/health.py:211` | Local conflict prints `--day and --yesterday are mutually exclusive`, exit 1. `solstone/think/tools/health.py:200`, `solstone/think/tools/health.py:202` | Always JSON `json.dumps(summary, indent=2, sort_keys=False)`. `solstone/think/tools/health.py:212` |

### Moved Stubs and Top-Level Chat

| Leaf | Grammar | Request | Exit/render |
|---|---|---|---|
| `sol call navigate ...` | Stub sub-app ignores unknown options and allows extra args. | No HTTP. | stderr `Moved to \`journal navigate\` — run that instead.`, exit 2. `solstone/think/call.py:96`, `solstone/think/call.py:115`, mounted at `solstone/think/call.py:123` |
| `sol call identity ...` | Same stub mechanics. | No HTTP. | stderr `Moved to \`journal identity\` — run that instead.`, exit 2. `solstone/think/call.py:96`, `solstone/think/call.py:115`, mounted at `solstone/think/call.py:125` |
| `sol chat MESSAGE... [--facet FACET] [-v/--verbose] [-d/--debug]` | Argparse positional `message` `nargs="*"`; option `--facet`; `setup_cli` adds shared flags. No message prints help and returns. | Opens `GET /sse/events` stream; posts `POST /api/chat` JSON `{"message": message}` plus `facet` only if truthy; degraded poll `GET /api/chat/session`. `solstone/think/chat_cli.py:95`, `solstone/think/chat_cli.py:117`, `solstone/think/chat_cli.py:155`, `solstone/think/chat_cli.py:250`, `solstone/think/chat_cli.py:252` | Detailed in section 4. |

## 4. Edge-Case Catalog: Chat State Machine + Support Diagnose/Consent (P3)

### Chat Degraded-Progress State Machine

| Area | Exact behavior |
|---|---|
| Constants | `POST_TIMEOUT_SECONDS=10`, `POLL_SECONDS=2`, `IDLE_CEILING_SECONDS=240`; messages: service down, queued, live-progress-unavailable, lost-contact, empty-answer, malformed chat response, composing. `solstone/think/chat_cli.py:30`, `solstone/think/chat_cli.py:44` |
| Client / post contract | `_TimeoutSession` sets request timeout default to 10 seconds; `_post_chat` sends `POST /api/chat` and requires dict response with non-empty `use_id`; malformed 2xx/non-dict/missing use_id -> `I couldn't read the chat response.` ValueError. `solstone/think/chat_cli.py:47`, `solstone/think/chat_cli.py:130` |
| SSE open | `_open_sse` does `requests.get(base_url + "/sse/events", stream=True, timeout=(10, None))`; request exceptions return `None`; content iterator uses `chunk_size=None`. `solstone/think/chat_cli.py:95`, `solstone/think/chat_cli.py:105` |
| SSE frame parser | UTF-8 decode with replacement; CR stripped; comment lines ignored; blank line flushes accumulated `data:` lines joined by `\n`; invalid JSON and non-dict frames are ignored. `solstone/think/chat_cli.py:61`, `solstone/think/chat_cli.py:93` |
| Progress rendering | Non-verbose cortex `start`/`thinking` -> `CHAT_LIVENESS_THINKING` (`sol is thinking…`); `tool_start` -> `· {tool}`. Verbose cortex `start` -> `Provider: {provider}; model: {model}`, `thinking` -> `Thinking: {summary}`, `tool_end` -> `· {tool} done`. Chat `sol_message` with valid target uses `talent_label_for(target, "running") + task suffix`; `talent_finished` -> `Composing your answer…`. `solstone/think/chat_cli.py:192`, `solstone/think/chat_cli.py:227`; labels in `solstone/apps/chat/copy.py:8`, `solstone/apps/chat/copy.py:26`, `solstone/apps/chat/copy.py:88` |
| Deduped progress | `emit_progress` suppresses consecutive duplicate progress lines via `state["last_progress"]`. `solstone/think/chat_cli.py:281`, `solstone/think/chat_cli.py:290` |
| Terminal correlation | Cortex terminal events count only when `tract=="cortex"`, `chat_proxy is True`, and event `use_id` equals the posted logical use id. `finish` stores `result`; `error` stores reason/provider/detail. `solstone/think/chat_cli.py:303`, `solstone/think/chat_cli.py:329` |
| Fold-terminal message | A chat `sol_message` is terminal when `requested_target is None` and `origin.logical_use_id == use_id`; `_session_terminal` applies the same rule and rejects latest messages with non-null `requested_target`. `solstone/think/chat_cli.py:148`, `solstone/think/chat_cli.py:168`, `solstone/think/chat_cli.py:342`, `solstone/think/chat_cli.py:355` |
| Queued progress | If POST response has truthy `queued`, stderr emits `Sol is busy right now — your message is queued.` once. `solstone/think/chat_cli.py:37`, `solstone/think/chat_cli.py:378`, `solstone/think/chat_cli.py:379` |
| Degraded poll recovery | Reader thread sets `sse_ended` if it exits before terminal. Main loop polls every 2s; if SSE ended or idle >= 240s, it calls `GET /api/chat/session`. If terminal is recovered, stderr prints `Live progress was unavailable.` and exits through normal finish. `solstone/think/chat_cli.py:381`, `solstone/think/chat_cli.py:405` |
| Lost contact | If terminal is absent or explicit `lost_contact`, stderr prints `sol: Lost contact with Sol before it finished — check 'journal doctor'.` and exits 1. `solstone/think/chat_cli.py:414`, `solstone/think/chat_cli.py:416` |
| Empty answer | Terminal `finish` with blank/whitespace result prints `sol: Sol returned an empty answer.` and exits 1. `solstone/think/chat_cli.py:418`, `solstone/think/chat_cli.py:423` |
| Provider-readiness terminal error | Terminal `error` passes reason/provider through `solstone.convey.provider_readiness.chat_view`; stderr is `sol: {message}`, exit 1. `solstone/think/chat_cli.py:230`, `solstone/think/chat_cli.py:237`, `solstone/think/chat_cli.py:426`, `solstone/think/chat_cli.py:431` |
| Post-time failures | POST unreachable -> `sol: solstone isn't running. Start it with 'journal up' and retry.`, exit 1; other `ConveyClientError` -> `sol: {err.error}` and optional `sol: {detail}`, exit 1; malformed -> `sol: I couldn't read the chat response.`, exit 1. `solstone/think/chat_cli.py:133`, `solstone/think/chat_cli.py:138`, `solstone/think/chat_cli.py:363`, `solstone/think/chat_cli.py:373` |
| KeyboardInterrupt | Prints `\nInterrupted.` to stderr and exits 1. `solstone/think/chat_cli.py:407`, `solstone/think/chat_cli.py:409` |
| Contract fragments already exist | `chat.postMessage` covers `POST /api/chat`; `chat.session` covers `GET /api/chat/session`; root SSE covers `GET /sse/events`. `solstone/convey/chat_contract.py:29`, `solstone/convey/chat_contract.py:93`, `solstone/convey/root_contract.py:35`, `solstone/convey/root_contract.py:45` |

### Support Diagnose and Consent

| Area | Exact behavior |
|---|---|
| Local build fallback seam | `support diagnose` catches `ConveyUnreachableError`, calls `_local_build_identity()`, prints JSON or human local diagnostics, emits support fallback notice, exits 1. `_local_build_identity()` reads package version, then runs `git rev-parse --short HEAD` with `cwd=Path(__file__).parents[2]`, `capture_output=True`, `text=True`, `timeout=5`; failure returns `revision=None`. `solstone/apps/support/call.py:139`, `solstone/apps/support/call.py:167`, `solstone/apps/support/call.py:632`, `solstone/apps/support/call.py:655` |
| Required fallback string | Unreachable support prints exactly `I couldn't reach support because solstone isn't reachable right now.` and `To file a support ticket, visit https://support.solstone.app`. `solstone/apps/support/call.py:43`, `solstone/apps/support/call.py:49` |
| Portal default/env | External portal default is `https://support.solstone.app`; `SOLSTONE_SUPPORT_URL` overrides, then journal config `support.portal_url`, then default. `solstone/apps/support/portal.py:37`, `solstone/apps/support/portal.py:667`, `solstone/apps/support/portal.py:685` |
| Dry-run preview | `_print_dry_run_preview` prints `DRY RUN — nothing was sent. Re-run with --submit to actually file this.`, build identity, `--- Would send ---`, payload fields, JSON `user_context`, `Would POST to: {portal_url}`, and `--- End dry run ---`. `solstone/apps/support/call.py:79`, `solstone/apps/support/call.py:106` |
| Dormant draft-capture route | Dry-run/no-network capture posts exact submit-path payload to `/app/support/api/draft`; route comment says it emits backend-only `support_draft` and sends nothing to support. CLI failure is non-fatal and prints `(Draft not captured — solstone wasn't reachable to save it for review.)`. `solstone/apps/support/call.py:109`, `solstone/apps/support/call.py:136`, `solstone/apps/support/routes.py:64`, `solstone/apps/support/routes.py:71` |
| Create consent | `create` defaults to dry run. Submit path optionally does KB search, prompts `Still want to file a ticket?`, presents `--- Ticket Draft ---`, prompts `Submit this ticket?`, and false response prints `Cancelled — nothing was sent.` `solstone/apps/support/call.py:240`, `solstone/apps/support/call.py:299`, `solstone/apps/support/call.py:308`, `solstone/apps/support/call.py:323` |
| Reply consent | `reply` defaults to submit; `--no-submit` captures draft and prints `DRY RUN — nothing was sent. Re-run with --submit to actually send this.` Submit path prompts `Send this reply?`, false -> `Cancelled.` `solstone/apps/support/call.py:417`, `solstone/apps/support/call.py:435`, `solstone/apps/support/call.py:448`, `solstone/apps/support/call.py:450` |
| Attach consent | `attach` defaults to submit; `--no-submit` is single-file only and uploads to draft endpoint. Submit path validates all files, shows attachment review, prompts `Upload these files?`, false -> `Cancelled — nothing was sent.`, per-file upload failures are non-terminal. `solstone/apps/support/call.py:465`, `solstone/apps/support/call.py:480`, `solstone/apps/support/call.py:522`, `solstone/apps/support/call.py:549` |
| Feedback consent | `feedback` defaults to dry run; submit path prompts `Submit this feedback{anon_note}?`, false -> `Cancelled.`, success -> `Feedback submitted: #{id}`. `solstone/apps/support/call.py:558`, `solstone/apps/support/call.py:595`, `solstone/apps/support/call.py:604` |

## 5. Reused-Mechanism Map: Contract/OpenAPI, Staleness, Allowlist Idioms (P4)

### Contract / OpenAPI

| Mechanism | Current implementation |
|---|---|
| DSL | Dataclass specs define fields, parameters, requests, responses, operations. `solstone/convey/contract/spec.py:12`, `solstone/convey/contract/spec.py:64` |
| Fragment assembly | `FRAGMENT_MODULES` is an explicit hand-list: network, observer, home, push, chat, root, voice, import. Activities/support/health are not listed. `solstone/convey/contract/assemble.py:18`, `solstone/convey/contract/assemble.py:27` |
| Reason-code vocabulary | `all_reason_codes()` scans `solstone.convey.reasons` for `Reason` instances; the OpenAPI `Error.reason_code` enum uses it. `solstone/convey/contract/assemble.py:91`, `solstone/convey/contract/assemble.py:96`, `solstone/convey/contract/assemble.py:313`, `solstone/convey/contract/assemble.py:317` |
| Build document | `build_document()` imports each fragment module and assembles operations into OpenAPI paths. `solstone/convey/contract/assemble.py:329`, `solstone/convey/contract/assemble.py:376` |
| Generated artifact | `scripts/build_openapi_contract.py` renders `build_document()` to `docs/openapi/convey-clients.json` and generated Callosum registry docs; check mode compares and prints stale-path guidance. `scripts/build_openapi_contract.py:23`, `scripts/build_openapi_contract.py:32`, `scripts/build_openapi_contract.py:57`, `scripts/build_openapi_contract.py:108` |
| Breaking-change check | `scripts/check_openapi_contract.py` compares current vs committed using `classify_changes`; failures instruct to run `make openapi` and notify native-client owners. `scripts/check_openapi_contract.py:18`, `scripts/check_openapi_contract.py:33` |
| Diff classifier | Breaking classes include removed endpoints, removed/renamed operation ids, removed request fields, new required request fields, removed parameters, removed response fields, and removed reason codes/SSE reason codes. `solstone/convey/contract/diff.py:22`, `solstone/convey/contract/diff.py:58`, `solstone/convey/contract/diff.py:239`, `solstone/convey/contract/diff.py:269` |
| Error emitter | `error_response()` returns JSON with `error`, `reason_code`, `detail`, plus optional extras, using the reason's default status unless overridden. `solstone/convey/utils.py:289`, `solstone/convey/utils.py:319` |
| Already contracted chat/root | `chat.postMessage`, `chat.session`, and `callosum.rootEvents` already exist as fragments. `solstone/convey/chat_contract.py:31`, `solstone/convey/chat_contract.py:91`, `solstone/convey/root_contract.py:37` |

### Staleness Target Pairs

| Pair | Makefile evidence |
|---|---|
| OpenAPI | `openapi` -> `scripts/build_openapi_contract.py`; `check-openapi` -> `scripts/check_openapi_contract.py`, build `--check`, observer-client bundle check. `Makefile:718`, `Makefile:724` |
| Journal resolution vectors | `journal-resolution-vectors`; `check-journal-resolution-vectors --check`. `Makefile:729`, `Makefile:733` |
| Journal format contract | `contract`; `check-contract` runs `contract_cli check` and build `--check`. `Makefile:735`, `Makefile:740` |
| Core fixtures | `core-fixtures`; `check-core-fixtures --check`. `Makefile:742`, `Makefile:746` |
| Skills | `skills` builds/installs router skills; `check-skill-references` runs `sol skills build --check`. `Makefile:180`, `Makefile:183`, `Makefile:714`, `Makefile:716` |
| Install checks wiring | `install-checks` includes `check-call-http-only`, `check-cogitate-prompts`, skill refs, OpenAPI, contract, core fixtures, journal vectors, and Rust gates; no `.github/workflows` files exist in this worktree. `Makefile:479`, `Makefile:571` |

### Allowlist Self-Check Idiom

| Gate | Pattern |
|---|---|
| `check_call_http_only` | Scans direct `solstone/apps/*/call.py`; `ALLOWLIST={}` and excluded sets are empty; `evaluate()` unions live keys and allowlist keys. Count above allowed is `NEW violations`; count below allowed is `STALE allowlist entries`; pass prints `call-http-only: pass`. `scripts/check_call_http_only.py:129`, `scripts/check_call_http_only.py:141`, `scripts/check_call_http_only.py:158`, `scripts/check_call_http_only.py:172`, `scripts/check_call_http_only.py:303`, `scripts/check_call_http_only.py:341`, `scripts/check_call_http_only.py:381` |
| `check_cogitate_prompts` | Same allowlist-by-`(file, kind)` count shape for cogitate prompt command spans; `ALLOWLIST={}`; live/allowlist union detects over-count and stale entries; pass prints `cogitate-prompts: pass`. `scripts/check_cogitate_prompts.py:12`, `scripts/check_cogitate_prompts.py:16`, `scripts/check_cogitate_prompts.py:87`, `scripts/check_cogitate_prompts.py:318`, `scripts/check_cogitate_prompts.py:355`, `scripts/check_cogitate_prompts.py:387` |

## 6. Native Surfaces + Rust Constraints + Port-Reader Default Semantics (P5, P6)

### Native Rust Surface

| Area | Current state |
|---|---|
| Native root binary | `main()` collects args, delegates to `evaluate_args`, dispatches `--version`, `journal-path`, and `indexer`. Usage errors print `USAGE` to stderr and exit 64. `core/crates/solstone-core/src/main.rs:35`, `core/crates/solstone-core/src/main.rs:57`; `EXIT_USAGE=64` at `core/crates/solstone-core/src/main.rs:19` |
| CLI parser crate | `USAGE` names only `--version`, `journal-path`, and `indexer`; `Command` enum has `Version`, `JournalPath`, `Indexer`; parser rejects unknown forms with `UsageError`. `core/crates/solstone-core-cli/src/lib.rs:6`, `core/crates/solstone-core-cli/src/lib.rs:13`, `core/crates/solstone-core-cli/src/lib.rs:34`, `core/crates/solstone-core-cli/src/lib.rs:44` |
| Journal resolver | `Source` enum is `Env|Config|Source|Default`; `ResolvedJournal` carries `path` and `source`; `resolve_journal_path()` precedence is non-empty env, stripped non-empty config, checkout root `journal`, then home default. `core/crates/solstone-core-journal/src/lib.rs:14`, `core/crates/solstone-core-journal/src/lib.rs:37`, `core/crates/solstone-core-journal/src/lib.rs:70`, `core/crates/solstone-core-journal/src/lib.rs:104` |
| Config/home helpers | `read_config_journal()` reads `$HOME/.config/solstone/config.toml`, returns `Decode` on invalid UTF-8 and `None` on missing/invalid TOML/non-string; `discover_home()` uses `HOME` then passwd fallback. `core/crates/solstone-core-journal/src/lib.rs:106`, `core/crates/solstone-core-journal/src/lib.rs:122`, `core/crates/solstone-core-journal/src/lib.rs:124`, `core/crates/solstone-core-journal/src/lib.rs:135` |
| Existing tests | Native tests cover version/usage/journal path and indexer behavior. `core/crates/solstone-core/tests/version.rs:20`, `core/crates/solstone-core/tests/version.rs:39`, `core/crates/solstone-core/tests/version.rs:57`, `core/crates/solstone-core/tests/indexer.rs:54`, `core/crates/solstone-core/tests/indexer.rs:73` |
| Missing native transport pieces | No native HTTP client dependency or Convey-port reader exists today. `core/Cargo.toml` workspace dependencies list only path crates plus chrono/glob/md5/pulldown-cmark/rapidfuzz/rusqlite/serde_json/toml_edit/unicode-normalization/unidecode. `core/Cargo.toml:20`, `core/Cargo.toml:35` |

### Port Reader / Transport Contract

| Python contract | Exact semantics to preserve |
|---|---|
| Port file reader | `read_service_port(service)` reads `<journal>/health/{service}.port`, returns `int(text.strip())`, and returns `None` only for `FileNotFoundError` or `ValueError`. `solstone/think/utils.py:1214`, `solstone/think/utils.py:1227` |
| Default port | Installed service default is 5015; constant `DEFAULT_SERVICE_PORT = 5015`. `solstone/think/service.py:19`, `solstone/think/service.py:48` |
| Base URL | `resolve_base_url()` uses `read_service_port("convey") or DEFAULT_SERVICE_PORT` and returns `http://localhost:{port}`. Missing/empty/malformed port therefore falls back to 5015. `solstone/think/convey_client.py:82`, `solstone/think/convey_client.py:84` |
| Timeout policy | API timeout `connect=2, read=20, total=30`; upload timeout `connect=2, read=120, total=180`. `solstone/think/convey_client.py:36`, `solstone/think/convey_client.py:39` |
| Error strings | `MALFORMED_RESPONSE_MESSAGE = "I couldn't read the journal response."`; `SERVER_ERROR_MESSAGE = "The journal returned an unreadable error."`; `UNREACHABLE_MESSAGE = "I couldn't reach the journal over HTTP."`; `TIMEOUT_MESSAGE = "The journal didn't answer in time."` `solstone/think/convey_client.py:31`, `solstone/think/convey_client.py:34` |
| Error classes | `ConveyClientError`, `ConveyUnreachableError`, and `ConveyTimeoutError`; timeout reason code is `local_convey_timeout`. `solstone/think/convey_client.py:44`, `solstone/think/convey_client.py:79` |
| Decode rules | 2xx + parsed JSON returns parsed; 2xx malformed raises malformed response; non-2xx parsed JSON with `error`/`reason_code` raises `ConveyClientError(error, reason_code, detail, status, payload)`; other non-2xx raises server-error message. `solstone/think/convey_client.py:194`, `solstone/think/convey_client.py:222` |
| Query encoding | Request params are appended with `urlencode(params, doseq=True)`. `solstone/think/convey_client.py:129`, `solstone/think/convey_client.py:132` |

### Rust Constraints / Gates

| Constraint | Evidence |
|---|---|
| Workspace | Rust workspace resolver 3, package version 1.0.12, edition 2024, Rust version 1.95, `unsafe_code = "forbid"`. `core/Cargo.toml:1`, `core/Cargo.toml:18` |
| Toolchain | `rust-toolchain.toml` pins channel `1.97.1`, minimal profile, rustfmt/clippy, and musl/iOS/darwin targets. `rust-toolchain.toml:1`, `rust-toolchain.toml:10` |
| Dependency policy | `cargo-deny` license allowlist, wildcard deny, banned `pyo3`/`pyo3-ffi`/`cpython`, graph includes iOS, crates.io-only sources. `core/deny.toml:1`, `core/deny.toml:34` |
| Rust gates | `check-rust-fmt`, `check-rust-msrv`, `check-rust-clippy -D warnings`, `check-rust-test`, `check-rust-ios`, `check-rust-deny`, `audit`. `Makefile:146`, `Makefile:178`; `install-checks` wires them at `Makefile:555`, `Makefile:571` |
| iOS gate exclusions | `check-rust-ios` runs workspace lib check for `aarch64-apple-ios` and excludes `solstone-core` and `solstone-core-indexer-store`. `Makefile:163`, `Makefile:167` |

## 7. Test Baseline Results (H)

All requested gates were run on the untouched tree.

| Command | Result | One-line tail / note |
|---|---:|---|
| `make check-rust-fmt` | PASS | `make check-rust-fmt` exited 0 |
| `make check-rust-clippy` | PASS | `Finished \`dev\` profile [unoptimized + debuginfo] target(s) in 6.34s` |
| `make check-rust-test` | PASS | Doc-tests complete; unit output included `63 passed` and `17 passed`; exit 0. |
| `make check-rust-msrv` | PASS | `Finished \`dev\` profile [unoptimized + debuginfo] target(s) in 5.95s` |
| `make check-rust-deny` | PASS | `bans ok, licenses ok, sources ok`; emitted existing unmatched-license allowance warnings for `BSD-2-Clause`, `ISC`, `Unicode-3.0`, `Unicode-DFS-2016`. |
| `make check-openapi` | PASS | `observer-client-contract: pass for docs/openapi/observer-client-contract` |
| `make check-contract` | PASS | `.venv/bin/python -m solstone.think.contract_cli build --check`; exit 0. |

## 8. Open Design Questions

| Question | Why it remains open |
|---|---|
| What is the canonical serialization format for `sol-call-grammar-v1`? | The pinned grammar artifact and parity corpus were not found, and no format spec was found in refs, worktree, unreachable blobs, or the local agent-tooling state directory. |
| What exact 20 Python files define the frozen oracle digest? | The explicit manifest is not present. Plausible manifests and serializations did not reproduce `1d14f01a819f2f44bfe229603aa38861cda3460ff1ca66b9593a33b6172a772d`. |
| Is `health pipeline` in or out of the native HTTP-client lead slice? | It is listed in the lead inventory, but current Python is a local non-HTTP wrapper around `pipeline_health`; only 20 of the 21 lead `sol call` leaves emit HTTP. |
| Should the native client use the existing OpenAPI DSL or a new grammar fixture as the source of truth? | Contract infrastructure exists and already covers chat/root, but activities/support/health fragments do not exist yet; the reviewer's grammar oracle format is missing. |
| Should native request-shape parity include support dry-run draft capture and local diagnostics fallback? | Current Python behavior sends dormant draft-capture requests during dry runs and locally spawns `git rev-parse` during unreachable `diagnose`; both are byte-visible behavior in the fixed slice. |
| Which Rust HTTP client is allowed? | No HTTP dependency exists today; deny policy is crates.io-only, license restricted, pyo3/cpython banned, and iOS is in the dependency graph. |
