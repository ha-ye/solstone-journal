---
name: speakers
description: >
  Manage speaker voiceprints in observed media. Detect owner voice, bootstrap
  voiceprints, attribute segments, merge names. TRIGGER: speaker, voice, who
  was talking, identify speaker, voiceprint, tag-owner, build-from-tags,
  owner-ready, sol call speakers
  detect/confirm-owner/identify/merge-names/bootstrap/backfill.
---

# Speakers CLI Skill

Manage speaker voiceprints and owner identification in observed media. Invoke via Bash: `sol call speakers <command> [args...]`.

**Writer polarity**: `bootstrap`, `resolve-names`, `attribute-segment`, `backfill`, `backfill-last-seen`, `seed-from-imports`, and `wipe` preview by default. Pass `--commit` to persist. Without `--commit` they print a report and return — no data is modified. All other write verbs persist immediately: `tag-owner`, `build-from-tags`, `confirm-owner`, `reject-owner`, `identify`, `merge-names`, and `link-import`. `detect` also mutates persisted owner-candidate state, including failure paths.

Common pattern:

```bash
sol call speakers <command> [args...]
```

Use this skill for usage and when-to-reach-for guidance; for exhaustive flags run `sol call speakers <command> --help`.

**Typical workflow**: status → owner-ready/detect → confirm-owner or manual bootstrap → suggest → identify/merge-names

## status

```bash
sol call speakers status [SECTION]
```

Speaker ID subsystem dashboard (embeddings, owner, speakers, clusters, imports, attribution). Returns JSON.

- `SECTION`: optional section name to filter (e.g., `owner`).

Behavior notes:

- Without a section, returns the full dashboard.
- Use `sol call speakers status owner` to check just the owner centroid state.

Examples:

```bash
sol call speakers status
sol call speakers status owner
```

## suggest

```bash
sol call speakers suggest [--limit N] [--json]
```

Actionable curation opportunities: unknown recurring voices, name variants, low-confidence segments. Prints a conversational summary by default; use `--json` for the raw items.

- `--limit`: max suggestions to return.

Behavior notes:

- Run after think processing completes, or when the owner is engaging with transcripts or observed media.
- Don't check on every conversation — speaker state changes slowly.
- Surface suggestions one at a time conversationally — don't stack them.

Example:

```bash
sol call speakers suggest --limit 5
```

## detect

```bash
sol call speakers detect
```

Run owner voice candidate detection. Returns the candidate plus sample segments.

Behavior notes:

- Reach for `detect` when the owner asks about speakers/voices, when owner bootstrap blocks something they want, or when explicitly refreshing candidate-pool detection.
- It always emits JSON and has no `--json` flag.
- It writes owner-candidate or diagnostic state; if it returns `low_quality` or `no_cluster`, use that payload for manual-tag guidance.
- Detection expands the existing speaker candidate pool; it does not scan and cluster the full journal.

Example:

```bash
sol call speakers detect
```

## confirm-owner

```bash
sol call speakers confirm-owner [--backfill/--no-backfill] [--json]
```

Save the detected owner centroid after the owner confirms. Persists immediately. By default, runs attribution backfill across all existing segments immediately after saving.

- `--backfill/--no-backfill`: default `--backfill`. Pass `--no-backfill` to defer the bulk pass.
- `--json`: emit full result as JSON.

Behavior notes:

- Only run after presenting the candidate to the owner and getting explicit confirmation.
- The default backfill run can process a lot of segments; if that matters (battery, time), pass `--no-backfill` and run `backfill` later.

Example:

```bash
sol call speakers confirm-owner
sol call speakers confirm-owner --no-backfill --json
```

## reject-owner

```bash
sol call speakers reject-owner
```

Discard the candidate if the owner says "that's not me." Persists immediately: one call, no preview, deletes the candidate snapshot and records a 14-day advisory cooldown.

Behavior notes:

- Confirm with the owner before running `reject-owner`.
- It always emits JSON and has no `--json` flag.
- Honor the cooldown: do not re-run `detect` and do not re-ask during it. `owner-ready` reports `{reason: "cooldown", days_remaining}` with `ready:false`; `tag-owner` and `build-from-tags` remain available if the owner chooses manual bootstrap.

## owner-ready

```bash
sol call speakers owner-ready
```

Report whether a persisted owner voice candidate should be surfaced right now. `owner-ready` runs no detection and always emits JSON. The thin `no_candidate` response is just `{ready:false, reason:"no_candidate"}`; cooldown responses include `{reason:"cooldown", days_remaining}`.

Example:

```bash
sol call speakers owner-ready
```

## day-segments

```bash
sol call speakers day-segments <day> [--limit N] [--json]
```

List speaker-review segments for a day. Use `day-segments` while building manual owner tags to find segments and audio sources worth reviewing.

Example:

```bash
sol call speakers day-segments 20260304 --limit 10
```

## sentences

```bash
sol call speakers sentences <day> <stream> <segment> <source> [--json]
```

List sentence ids for one segment/source. Use `sentences` before tagging: `*` rows have embeddings and are taggable; `-` rows do not and are not taggable.

Example:

```bash
sol call speakers sentences 20260304 default 090000_300 audio
```

## tag-owner

```bash
sol call speakers tag-owner <day> <stream> <segment> <source> <sentence-id> [--json]
```

Tag one embedded sentence as the owner's voice. `tag-owner` persists immediately; re-tagging an already assigned owner row returns `already_assigned`.

Example:

```bash
sol call speakers tag-owner 20260304 default 090000_300 audio 3
```

## build-from-tags

```bash
sol call speakers build-from-tags [--json]
```

Build the owner centroid from validated manual owner tags. `build-from-tags` persists immediately when quality gates pass. Thirty tags makes the build attemptable, not guaranteed: usable embedding count, median duration, and intra-cohesion still apply, and the command can return `low_quality` with guidance on what's short.

Example:

```bash
sol call speakers build-from-tags --json
```

## identify

```bash
sol call speakers identify <cluster_id> <name> [--entity-id ID]
```

Name an unknown speaker cluster after the owner provides the name. `identify` persists immediately, always emits JSON, and has no `--json` flag.

- `cluster_id`: integer cluster identifier from `suggest` or `discover` output (positional argument).
- `name`: speaker name to assign (positional argument).
- `--entity-id`: optional entity ID to link the speaker to an existing entity.

Example:

```bash
sol call speakers identify 42 "Alicia Chen"
sol call speakers identify 42 "Alicia Chen" --entity-id alicia_chen
```

## merge-names

```bash
sol call speakers merge-names <alias> <canonical>
```

Merge a name variant into the canonical entity.

- `alias`: the variant name to merge away (positional argument).
- `canonical`: the canonical name to keep (positional argument).

Behavior notes:

- Use when `suggest` surfaces a name variant (e.g., "Mitch" and "Mitch Baumgartner" sound identical).
- Ask the owner for confirmation before merging.

Example:

```bash
sol call speakers merge-names "Mitch" "Mitch Baumgartner"
```

## Library maintenance

The following commands maintain the voiceprint library over time. For preview-by-default writers, use `--commit` to persist; see the writer-polarity rule at the top.

### bootstrap

```bash
sol call speakers bootstrap [--commit] [--json]
```

Scan the full journal for single-speaker segments and save voiceprints for every non-owner speaker in those segments. Uses the owner centroid for subtraction.

### resolve-names

```bash
sol call speakers resolve-names [--commit] [--json]
```

Compare voiceprint centroids across all entities. Pairs with cosine similarity > 0.90 are flagged as the same person; unambiguous variants (short name is first word of full name) are auto-merged by adding the short name as an aka on the canonical entity. Ambiguous matches are reported but not auto-merged.

### attribute-segment

```bash
sol call speakers attribute-segment DAY STREAM SEGMENT [--commit] [--save/--no-save] [--accumulate/--no-accumulate] [--json]
```

Run speaker attribution (Layers 1-3) on a single segment.

- `--save/--no-save`: default `--save`. Write `speaker_labels.json`.
- `--accumulate/--no-accumulate`: default `--accumulate`. Run voiceprint accumulation.
- `--save` / `--accumulate` only take effect when `--commit` is passed.

### backfill

```bash
sol call speakers backfill [--commit] [--json]
```

Run attribution across every segment in the journal. Skips segments that already have labels.

### backfill-last-seen

```bash
sol call speakers backfill-last-seen [--commit] [--json]
```

Backfill `last_seen_ts` on existing voiceprint metadata rows. `backfill-last-seen` previews by default; pass `--commit` to write.

### discover

```bash
sol call speakers discover [--json]
```

Find recurring unknown speakers across the journal. `suggest` is the curated surface; `discover` is the raw list — reach for it when debugging or doing manual triage.

### link-import

```bash
sol call speakers link-import NAME --entity-id ID
```

Link an imported-media participant name to a journal entity id. Persists immediately.

### seed-from-imports

```bash
sol call speakers seed-from-imports [--commit] [--json]
```

Bootstrap voiceprints from imported-media tracks where the participant roster is known.

### wipe

```bash
sol call speakers wipe [--commit] [--json]
```

Preview or remove legacy speaker artifacts. `wipe` previews by default. With `--commit`, it permanently deletes any `.npz` directly under segment directories (`chronicle/*/*/*/*.npz`), segment `speaker_labels.json` and `speaker_corrections.json` under `agents/` and `talents/`, per-entity `voiceprints.npz`, per-entity `owner_centroid.npz`, and `awareness/owner_candidate.npz`. Require explicit owner confirmation before `--commit`.

## Gotchas

- **Writer polarity lives at the top.** A successful-looking report from a preview-by-default writer is not a successful write. When testing those writers, run without `--commit` first to see the plan; then re-run with `--commit` to persist.
- **`confirm-owner` backfills by default.** It runs attribution across the entire journal immediately after saving the centroid. Pass `--no-backfill` if you want to defer.
- **Owner rejection cools down for 14 days.** `reject-owner` records the rejection; `owner-ready` reports cooldown. Honor it as an agent rule: don't re-run detection or re-ask until it clears.
- **`discover` is the raw feed; `suggest` is the curated one.** Don't surface `discover` output to the owner unfiltered — use `suggest`.

## Owner Detection

Cheap checks tell you the state; `detect` and `build-from-tags` tell you what to do next. Check `speakers owner-ready` or `speakers status owner` first:

- If `owner-ready` reports a candidate, present it to the owner and then run `speakers confirm-owner` or `speakers reject-owner` based on their answer.
- `ready:false` does not always mean no candidate. If the reason is `single_stream`, be conservative, not dead-ended: `candidate_available:true` and `next_step:"confirm_candidate"` mean the candidate exists, but was heard on only one device or stream.
- For `single_stream`, don't proactively pitch one-device evidence; when the owner asks about their voice or speaker ID, the candidate exists and can be confirmed.
- If `owner-ready` reports cooldown, honor it: don't re-run `detect` and don't re-ask during the cooldown.
- If there is no candidate and the owner asks about speakers/voices, or owner bootstrap blocks something they want, run `speakers detect` to get the real candidate or guidance payload. Don't wait solely because `owner-ready` says `no_candidate`.
- `speakers status owner` is thin. In `no_cluster`, it only carries `segments_checked` and `attempted_at`; use `detect` or `build-from-tags` when you need `manual_tags_count`, `can_build_from_tags`, `next_step`, and `guidance`.

When you have a candidate, present it naturally: "I've been listening to your journal across your different devices and I think I can recognize your voice. Here are a few moments — does this sound right?" Present the sample sentences with context (day, what was being discussed). Don't play audio — show text and context.

- If the owner confirms: run `speakers confirm-owner` (backfill runs automatically). Then tell them: "Great — now I can start identifying other voices in your observed media too."
- If the owner rejects: run `speakers reject-owner`. A 14-day cooldown starts; don't re-ask until `owner-ready` clears it.

### Manual owner bootstrap

When automatic detection cannot produce a usable candidate, or when the owner wants to help directly, frame manual bootstrap as a real session, not a quick aside: "I can help teach sol your voice from moments already in your journal. It takes about 30 owner-voice tags. Want to do that now?"

Run the loop like this:

1. Check state with `speakers owner-ready` or `speakers status owner`. Run `speakers detect` to actually run detection and get guidance.
2. Walk review material with `speakers day-segments <day> [--limit N]`, then `speakers sentences <day> <stream> <segment> <source>`.
3. Tag only `*`-marked rows with `speakers tag-owner <day> <stream> <segment> <source> <sentence-id>`. `-` rows have no embedding and are not taggable.
4. Repeat until there are around 30 validated owner tags. This usually means around 30 `tag-owner` calls, each sourced by walking `day-segments` and `sentences`.
5. Run `speakers build-from-tags`. Thirty tags makes the build attemptable, not guaranteed; if it returns `low_quality`, follow its guidance on what's short.
6. After a candidate is ready, confirm or reject with the owner.

## Speaker Curation

Run `speakers suggest` after think processing completes, or when the owner is engaging with transcripts or observed media. Surface suggestions conversationally based on type:

- **Unknown recurring voice:** "I keep hearing a voice in your [day/context] observed media. They said things like '[sample text]'. Do you know who that is?" If the owner names them, run `speakers identify <cluster_id> <name>`.
- **Name variant:** "I noticed 'Mitch' and 'Mitch Baumgartner' sound identical in your observed media. Should I merge them?" If yes, run `speakers merge-names <alias> <canonical>`.
- **Low confidence review:** "There are a few speakers in this conversation I'm not sure about. Want to take a quick look?"

**Don't stack suggestions.** Surface one at a time. Wait for the owner to respond before presenting another. Speaker curation should feel like a natural aside, not a checklist.

## When NOT to Act

- Don't proactively surface speaker ID during unrelated conversations. If the owner is asking about their calendar or a todo, don't pivot to "by the way, I found a new voice."
- Don't surface low-confidence suggestions. If a cluster has only a few embeddings, wait for it to grow.
- Don't re-ask about a rejected owner candidate while `owner-ready` reports cooldown.
