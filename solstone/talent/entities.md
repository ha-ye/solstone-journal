{
  "type": "generate",

  "title": "Entity Extraction",
  "description": "Extracts people, companies, projects, and tools from segment content",
  "color": "#2e7d32",
  "schedule": "segment",
  "priority": 10,
  "hook": {"post": "entities"},
  "thinking_budget": 4096,
  "max_output_tokens": 1024,
  "output": "json",
  "schema": "entities.schema.json",
  "load": {"transcripts": true, "percepts": true, "talents": false}

}

$segment_preamble

Extract named entities and descriptions from the given segment transcription document.

Focus on only these four types of entities:
- Person: individual people by name
- Company: businesses and organizations
- Project: named projects, products, or codebases
- Tool: software applications and services

Skip entities that don't fit one of these four types.
Skip any url, domain, filename, or path.

## Name Resolution

- Prefer full names over nicknames or abbreviations when context makes identity clear (e.g., "Dens" → "Dennis Crowley" if the surrounding conversation confirms identity).
- Use the first full name encountered for people (e.g., if "John B." and "John Borthwick" appear, use "John Borthwick").
- Only extract first-name-only references when identity is unambiguous (one "Sarah" in the conversation). Skip ambiguous first-name references rather than guessing.
- Use the official or most common name for companies (e.g., "MS", "Microsoft", "MSFT" → "Microsoft").

## What to return

Return a single JSON object: `{"entities": [ ... ]}`. Each entry has exactly three fields: `type` (one of Person, Company, Project, Tool), `name`, and `description`. If no entities qualify, return `{"entities": []}`.

Example:

{
  "entities": [
    {
      "type": "Person",
      "name": "Alice Smith",
      "description": "Mentioned in discussion about the project timeline"
    },
    {
      "type": "Tool",
      "name": "Grafana",
      "description": "Referenced for monitoring metrics dashboards"
    }
  ]
}
