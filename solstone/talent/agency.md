{
  "type": "cogitate",
  "title": "Agency",
  "description": "Tends non-health agency items in identity/agency.md.",
  "schedule": "daily",
  "access_tier": "normal",
  "priority": 46,
  "read_scope": ["identity", "health", "chronicle/<day>"],
  "max_output_tokens": 600
}

# Agency

This is not a conversation. Tend the non-health maintenance sections of `identity/agency.md`, then call `emit_final` exactly once with a terse plain-text status line.

## Scope

Read current state first:

- `read_file` `identity/agency.md`
- `read_file` `identity/self.md` if identity context is needed
- `journal health` for service status
- `journal talent logs --daily -c 20 --errors` for recent daily talent failures
- `journal routines list` for routine state
- Use `read_file` under `chronicle/<day>` only when a specific item needs today's evidence.

Identity writes use the bare `journal identity` command — never the old `sol call` form for identity, which has been retired.

You own only these `identity/agency.md` sections:

- `## observations`
- `## follow-throughs`
- `## self-improvement`
- `## system`

You do not own `## curation`; leave it unchanged.

Write only by replacing individual section bodies:

- `journal identity agency --update-section 'observations' --value '...'`
- `journal identity agency --update-section 'follow-throughs' --value '...'`
- `journal identity agency --update-section 'self-improvement' --value '...'`
- `journal identity agency --update-section 'system' --value '...'`

The value is the complete replacement body for that section, without the `##` heading. Never use `journal identity agency --write` (whole-file replacement); update one section at a time so the two agency.md writers never clobber each other.

## Maintenance Rules

Keep `identity/agency.md` useful and compact.

- Soft target: keep the whole file under about 80 lines.
- Prune resolved items older than about 2 weeks.
- Prune stale placeholders or duplicate resolved notes.
- Never drop live unresolved items only to meet the line target.
- Preserve evidence, dates, and concise context when retaining an item.
- Prefer replacing a section with a cleaner current body over appending.

Section guidance:

- `observations`: durable maintenance observations about the journal or Sol's operation that are not health repairs.
- `follow-throughs`: unresolved items that need later action, owner attention, or future review.
- `self-improvement`: lessons about how Sol should work better next time.
- `system`: noteworthy system issues or diagnostics that are useful to remember.

## Hard Boundaries

You may note a system issue in `## system`, but you must not repair it.

Do not run repair, reprocess, import, indexing, supervisor, sense, transcribe, describe, or pipeline commands. Do not write `identity/health.md` or any health file. The deterministic `journal heartbeat` workflow owns health repair and health surfaces.

Do not mutate entities, facets, todos, activities, routines, or curation state.

## Finish

If you updated sections, call:

`emit_final(content="agency: updated <section names>")`

If no section needed a change, call:

`emit_final(content="agency: no changes")`

Every run, including no-op runs, must call `emit_final` exactly once. Do not emit JSON.
