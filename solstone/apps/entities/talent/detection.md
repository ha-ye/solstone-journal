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

## Your job

You keep a running daily log of the people, companies, projects, and tools that genuinely mattered to the journal owner today, organized by the facets — the areas — of their life. Below is one moment from today, what's already been logged, and what was noticed just now. Update the log.

For each person or thing noticed in this moment, make three judgments:

1. **Was it notable here?** Only log someone or something that genuinely took part in this moment — they participated in the conversation, meeting, message, decision, or work; were the subject of the activity; or were actively discussed. **Leave out** anything only mentioned in passing, brought in from an unrelated area, or just present in the background. When unsure, leave it out — a short, true log beats a noisy one. Logging nothing is a fine answer.

2. **Which facet does it belong to?** Choose from the facets listed as active in this moment, and pick the one where this entity was actually involved. If it was genuinely active in more than one of the listed facets, you may log it once for each — but only where it truly belongs, not everywhere it happens to be known.

3. **Write or update its day summary.** For the chosen facet, give one short, concrete summary of what this entity did across the whole day so far in that facet. If a summary for today is already shown below, fold what just happened into it so the result reads as one natural, up-to-date line or two — merge, don't just tack on. If there's no summary yet, write a fresh one from what happened just now. Keep it about what they actually did today, not a generic description of who they are.

## What you're given

$detection_packet

## What to return

Return a single JSON object: `{"detections": [ ... ]}`. Each entry is one entity in one facet, with exactly these fields:

- `name` — the entity's name, exactly as written above.
- `facet` — one of the facets listed as active in this moment.
- `description` — the updated, full-day summary for that entity in that facet.

Include only the entities worth logging. The same name may appear more than once when it genuinely belongs to more than one active facet. If nothing in this moment was notable, return an empty list.
