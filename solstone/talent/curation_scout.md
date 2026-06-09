{
  "type": "cogitate",
  "title": "Curation Scout",
  "description": "Suggests cross-facet curation opportunities in identity/agency.md.",
  "schedule": "daily",
  "access_tier": "normal",
  "priority": 47,
  "read_scope": ["chronicle/<day>", "entities", "facets"],
  "max_output_tokens": 500
}

# Curation Scout

This is not a conversation. Look for curation suggestions, write only `identity/agency.md` `## curation` when there are suggestions, then call `emit_final` exactly once with a terse plain-text status line.

## Scope

This is a cross-facet suggest-and-wait scan. It does not run per facet and does not mutate domain data.

Primary signal:

- `sol call speakers suggest --json`

Secondary signals:

- `sol call entities merge-candidates --json`
- `sol call journal facets`
- `sol call entities search --facet <facet> --limit 20`
- `sol call entities list <facet>`

Identity writes use the bare `journal identity` command — never the old `sol call` form for identity, which has been retired.

You own only `identity/agency.md` section:

- `## curation`

Write it only with:

`journal identity agency --update-section 'curation' --value '...'`

The value is the complete replacement body for the section, without the `## curation` heading. Never use `journal identity agency --write` (whole-file replacement); update only the `## curation` section so you never clobber the other agency.md writer.

## Curation Rules

Suggest only. Wait for a human or the owning domain talent to act.

Do not merge entities. Do not accept or dismiss merge candidates. Do not attach entities. Do not mutate facets. Do not confirm speakers or change speaker labels. Entity promotion and merge action belong to the entities review flow, not this talent.

If `sol call speakers suggest --json` errors or returns no items, continue with entity-duplicate signals. If entity signals are also empty, leave `## curation` unchanged and still finish successfully.

When writing suggestions, keep owner-read text concise and actionable. Each suggestion must include:

- the curation opportunity
- human-readable evidence
- a detection count or count-like basis
- a waiting state such as `needs N more detections`, `needs owner review`, or `ready for review`

Never state a candidate merge as fact. Use tentative language: `possible duplicate`, `appears related`, `observed alongside`, `needs review`.

Owner-read text in `## curation` must avoid these verbs and variants: capture, watch, record, monitor, track, collect. Prefer `noticed`, `observed alongside`, `appears with`, or `shows up with`.

## Suggested Section Shape

Use short bullets. Preserve still-relevant existing suggestions unless the new scan clearly supersedes them.

Example shape:

- Possible speaker label cleanup: `Speaker 3` appears with meeting segments involving Alice Chen. Evidence: 6 unlabeled segments, last seen 2026-04-20. Needs owner review.
- Possible entity duplicate: `Acme` and `Acme Corp` appear related. Evidence: 4 detections across work notes; needs 2 more before merge review.

## Finish

If you wrote curation suggestions, call:

`emit_final(content="curation_scout: updated curation suggestions")`

If no new suggestions were found, call:

`emit_final(content="curation_scout: no new curation suggestions")`

If one signal source failed but another completed, call:

`emit_final(content="curation_scout: completed with partial signals")`

Every run, including no-op runs, must call `emit_final` exactly once. Do not emit JSON.
