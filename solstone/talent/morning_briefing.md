{
  "type": "generate",

  "title": "Morning Briefing",
  "description": "Synthesizes all daily agent outputs into a structured five-section morning briefing",
  "color": "#1565c0",
  "schedule": "daily",
  "priority": 50,
  "output": "md",
  "degradation_check": true,
  "hook": {"pre": "morning_briefing"}
}

You are generating the morning briefing for $agent_name: a structured daily briefing that synthesizes agent outputs, calendar, follow-ups, and current context into an actionable start-of-day view.

The source packet below is complete. Do not invent data outside the packet. When a source is missing or empty, preserve that as a visible gap instead of treating it as a clean day.

## Output Contract

Return only the complete briefing markdown in this exact outer shape:

```
---
type: morning_briefing
date: $day_YYYYMMDD
generated: $generated
model: $model
sources:
$source_counts
gaps: $source_gaps
---

$coverage_preamble

## Your Day
[today's prioritized agenda]

## Yesterday
[what happened yesterday]

## Needs Attention
[ranked actions and pipeline gaps]

## Forward Look
[next seven days]

## Reading
[facet newsletter links]
```

Omit any section that has no content. Keep the YAML frontmatter, `sources`, `gaps`, and coverage preamble exactly as injected above.

## Source Packet

### Active Facets

$active_facets

### Facet Newsletters

$facet_newsletters

### Anticipated Activities Today

$anticipated_today

### Anticipated Activities Next 7 Days

$anticipated_forward

### Pulse Surface

$pulse_surface

### Partner Surface

$partner_surface

### Steward Health Surface

$health_surface

### Follow-Ups

$followups

### Decisions

$decisions

## Synthesis Rules

**Source attribution.** Attribute high-consequence factual claims to their source using inline parenthetical links with `sol://` URIs when a source URI is present in the packet. Not every claim needs attribution; anticipated activities are schedule-derived and the Reading section is inherently attributed.

**Your Day** - What's ahead today. Lead with anticipated activities in chronological order. For each meeting, include who's attending and source-backed context when available. If no anticipated activities exist, lead with the highest-priority follow-ups or pulse needs.

**Yesterday** - What happened. Draw from facet newsletters, pulse, and decisions. Highlight accomplishments, consequential decisions, and notable interactions. Keep to 3-5 bullets max. Only include if facet newsletters or decisions have content for the analysis day.

**Needs Attention** - Ranked action list. Start with steward health pipeline gaps when the health surface contains needs-attention items. Then include overdue commitments, missed follow-ups, pending follow-ups, and important pulse needs without calendar time blocked. Do not include pipeline gaps when the steward health surface has no needs-attention bullets.

**Forward Look** - What's coming. Draw from anticipated activity records and upcoming scheduled items in the next seven days. Note preparation needed for upcoming meetings or deadlines.

**Reading** - Links to full facet newsletters for deeper context. List each active facet that has a newsletter for the analysis day, with a brief one-line description of what it covers.

## Evidence Strength

Grade highlights and action items by evidence strength. High confidence means corroborated by multiple sources, a confirmed scheduled item, an explicit commitment with a date, or an overdue follow-up. Medium confidence means a clear single-source item or schedule-derived item with a clear basis. Low confidence means ambiguous, speculative, or pattern-based evidence. Hedge low-confidence items, but never hedge confirmed scheduled items, explicit deadlines, or commitments with clear dates.
