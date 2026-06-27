{
  "type": "generate",
  "title": "Entity Detection",
  "description": "Per-segment, per-entity facet-relevance judgment feeding the living detection substrate",
  "color": "#00695c",
  "schedule": "segment",
  "priority": 15,
  "provider": "google",
  "thinking_budget": 2048,
  "max_output_tokens": 1024,
  "output": "json",
  "schema": "detection.schema.json",
  "hook": {"pre": "entities:detection", "post": "entities:detection"},
  "load": {"transcripts": false, "percepts": false, "talents": false}
}

## Core Mission

Make per-ENTITY facet-relevance judgments for THIS ~5-minute segment's candidates. Return exactly one JSON object matching the schema:

`{"detections": [...]}`

Each row judges whether a candidate actively participated in one of THIS segment's facets. Zero detections is valid and preferred over cross-facet contamination.

## ⚠️ CRITICAL FACET SCOPING RULE

**ONLY detect entities that were ACTIVELY INVOLVED in THIS segment's facet activity.**

❌ DO NOT DETECT if:
- Entity is mentioned in passing from another facet's context
- Entity appears in context but is not tied to this segment's facet work
- Person/org from Facet A is merely referenced while working in Facet B
- Transcript mentions "then I called my friend Sarah" but Sarah is not relevant to this segment's shown facets

✅ DETECT if:
- Entity participated in this segment's meetings/events/communications
- Entity is the subject of work/activities within one shown facet
- Entity appears in facet-tagged segment activity or insights for this facet
- Entity had direct involvement in this segment's facet activity

**When in doubt: If the entity was not actively participating in THIS segment's shown facet activity, skip it.**

**If the segment was quiet or only background mentions appeared, 0 detections is perfectly acceptable and preferred over cross-contamination.**

## Entity Priority Guidelines

1. **High Priority - People and Contacts** (capture all active involvement)
   - Detect people who participated in conversations, meetings, messages, reviews, decisions, or collaboration in this segment.
   - Include brief but active participation.
   - Type: Person.

2. **Medium Priority - Companies and Projects** (selective)
   - Companies: Detect only significant business relationships, clients, vendors, partners, or organizations actively discussed or acted on.
   - Projects: Detect only when clearly central to this segment's work, planning, review, or decision.
   - Skip passing mentions and tangential references.
   - Types: Company or Project.

3. **Low Priority - Tools and Resources** (rare)
   - Detect only when the tool is the subject of discussion, evaluation, migration, debugging, or learning.
   - Skip tools merely used in the background.
   - Type: Tool.

## Per-Candidate Decision

For each candidate in the packet:

- Decide whether it ACTIVELY participated in one of THIS segment's shown facets.
- If yes, set `detect: true`.
- Pick EXACTLY ONE `facet`, only from the facets shown in the packet.
- Default to the dominant `level: high` facet when the entity's involvement spans multiple shown facets.
- Write `contribution` as one concrete sentence describing what happened with this entity in THIS segment within THAT facet.
- Make `contribution` day-specific and concrete, not a generic bio.
- If the entity was only mentioned, background, cross-facet, generic, or uncertain, set `detect: false`; `contribution` may be an empty string.

## Source Packet

$detection_packet

## Output Rules

- Output only JSON matching the schema.
- Use `facet` only from the packet's active segment facets.
- Use only these types: Person, Company, Project, Tool.
- Do not invent entities beyond the candidates.
- Do not detect entities just because they are attached, familiar, or historically important.
