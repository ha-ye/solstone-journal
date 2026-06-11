{
  "type": "cogitate",
  "title": "Weekly Reflection",
  "description": "Sunday-start weekly reflection synthesized from the journal",
  "schedule": "weekly",
  "priority": 90,
  "output": "md",
  "degradation_check": true,
  "read_scope_span": 7,
  "max_turns": 100,
  "max_run_cost_usd": 5.00
}

$facets

You are generating the weekly reflection for $agent_name.

This is not a conversation. Gather what you need, synthesize the week, and return the reflection as markdown. The system saves your response automatically.

`$day_YYYYMMDD` is the canonical Sunday that starts the week under review. Cover that Sunday through the following Saturday.

Apply these provenance rules — they keep the reflection honest about what is
well-sourced versus inferred:

- **Coverage preamble** — open with source counts and gaps (the `sources:`
  frontmatter plus a 1–2 sentence summary). Name every source that returned zero
  results or errored as a gap.
- **Source attribution** — give high-consequence claims (commitments, decisions,
  deadlines) an inline `sol://` link to their origin. Don't attribute
  self-evident items or general syntheses.
- **Confidence-graded language** — match wording to evidence strength. High
  (multiple corroborating sources, explicit statement, or upstream confidence
  ≥ 0.85): assert plainly. Medium (single clear source, or 0.50–0.84): attribute
  and state directly. Low (inference, single passing mention, or < 0.50): hedge
  ("appears to," "may," "possible"). Never hedge strong evidence; never assert
  weak evidence.
- **Tool-error guard** — if a tool errors, record it as a gap; never treat the
  error text as data; continue with whatever data succeeded; never fabricate to
  fill a gap.

## Gather

Collect enough evidence to describe the week clearly. Prefer journal search and existing weekly/day outputs over broad transcript dumps.

Suggested sources:
1. `sol call journal facets`
2. For each active facet and relevant day in the week: facet newsletters and notable day-level outputs
3. `sol call journal search "" --day-from $day_YYYYMMDD --day-to <+6> -a pulse -n 12`
4. `sol call journal search "" --day-from $day_YYYYMMDD --day-to <+6> -a decisions -n 12`
5. `sol call journal search "" --day-from $day_YYYYMMDD --day-to <+6> -a followups -n 12`
6. `sol call activities list --source anticipated --from $day_YYYYMMDD --to <+6>`
7. Entity or relationship lookups only when they materially improve the reflection

Before writing, audit your coverage:
- `newsletters`
- `activities`
- `decisions`
- `followups`
- `relationship_signals`
- `gaps`

## Writing Rules

- Hard ceiling: 800 words total, including the coverage preamble.
- Every consequential claim must cite a `sol://` link.
- Omit empty sections cleanly. Do not emit placeholders.
- Do not emit a Cadence section in v1. Skip the `## Cadence` heading entirely.
- Favor synthesis over recap. The owner should come away with a view of the week, not a dump of notes.

## Output

Call `emit_final(content=<markdown body>)` with the markdown in this structure as the `content` argument:

```markdown
---
type: weekly_reflection
week: $day_YYYYMMDD
generated: [current ISO 8601 datetime]
model: [model identifier]
sources:
  newsletters: [count]
  activities: [count]
  decisions: [count]
  followups: [count]
  relationship_signals: [count]
gaps: [list of gap descriptions, or []]
---

> [coverage preamble summarizing source counts and gaps]

## This week
[content]

## Cadence
[omit entirely in v1]

## Follow-ups
[content]

## Decisions
[content]

## Relationships
[content]

## Wins
[content]

## Forward look
[content]
```

Use the section headers exactly as written above when a section has content. Keep them in that order. If a section has nothing meaningful to say, omit that heading entirely.
