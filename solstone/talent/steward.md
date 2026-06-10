{
  "type": "generate",

  "title": "Steward",
  "description": "Writes the owner-facing health summary (headline, sentence, suggested action) from the deterministic health surface.",
  "schedule": "daily",
  "priority": 45,
  "tier": 3,
  "hook": {"pre": "steward", "post": "steward"},
  "output": "json",
  "schema": "steward.schema.json",
  "thinking_budget": 1024,
  "max_output_tokens": 400,
  "load": {"transcripts": false, "percepts": false, "talents": false}
}

# Steward — health summary

Write a short, human-friendly summary of Sol's current health for the owner's home screen. The health state below is already computed deterministically — use it as ground truth; do not recompute it or invent problems it doesn't show. Your previous summary is included so you can keep continuity run-to-run.

## Today's health state

$health_state

## Your previous summary

$previous_summary

## Write

Return a JSON object with exactly these keys:

- `headline` — 2–5 words, plain language (e.g. "All clear", "Pipeline gap", "Repairs failing").
- `summary_sentence` — one plain sentence an owner can read at a glance. Lean on the previous summary for continuity where it helps ("still clear", "now resolved", "new since yesterday"); otherwise just describe the current state plainly.
- `suggested_action` — exactly one of:
  - `none` — nothing for the owner to do (use when the state is clear/healthy).
  - `reprocess_stale` — stale segment repairs have failed or escalated and the owner may want to retry processing.
  - `open_health_detail` — there is an issue worth viewing on the health page, but no specific retry applies.

Output only the JSON object.
