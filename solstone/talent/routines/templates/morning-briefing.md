{
  "name": "morning-briefing",
  "description": "Daily morning digest of calendar, follow-ups, priorities, and relationship context.",
  "default_cadence": "0 7 * * *",
  "default_timezone": "UTC",
  "default_facets": []
}

You are preparing a daily morning briefing for this routine run.

This is not a conversation. Gather the information, synthesize it, and write a concise markdown briefing for the routine output file.

## Gather

1. Call `sol call journal facets` to see the active facets if you need broader context.
2. Call `sol call activities list --source anticipated --day $day_YYYYMMDD` to review today's scheduled items and participants.
3. Call `journal identity pulse` to capture current narrative, priorities, and needs-you items.
4. Call `sol call journal search "" -a followups -n 10` to find recent follow-up items.
5. For each person on today's calendar, call `sol call entities intelligence PERSON --brief`.
6. If a facet needs more detail, call `sol call journal news FACET --day $day_YYYYMMDD`.

## Synthesize

- Lead with today's calendar in chronological order.
- For each meeting, include attendees and one line of relationship or project context from entity intelligence.
- Surface the most important follow-ups and pulse items that should shape the day.
- Highlight any follow-ups or pulse items that need attention today.
- If there are no meetings, lead with the highest-priority actionable work.

## Write

Write a markdown briefing with short sections such as:

- `## Today`
- `## Needs Attention`
- `## People Context`
- `## Optional Reading`

Use bullets, not long paragraphs.
Omit empty sections entirely.
Keep the briefing scannable and practical.
Do not include greetings, sign-offs, or commentary about your process.
