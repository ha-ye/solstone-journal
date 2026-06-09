---
name: support
description: >
  File support tickets, search the KB, submit feedback to sol pbc. Manage open
  tickets, attach files, check announcements, run diagnostics. TRIGGER: file
  bug, request feature, submit feedback, search KB, announcements, tickets,
  sol call support create/search/list/reply/diagnose.
---

# sol support

File tickets, search the knowledge base, and submit feedback. Invoke via Bash: `sol call support <command> [flags]`.

## Before You Start

1. **Read the TOS first.** The TOS is cached locally at `<journal_root>/apps/support/portal/tos.txt` after first registration (the actual path resolves via the journal's app storage). If it doesn't exist yet, run `sol call support register` to fetch and cache it.

2. **Always search the KB before filing a ticket.** Run `sol call support search "your question"` first. Many common issues are already documented. Only file a ticket if the KB doesn't answer the question.

3. **Diagnostics are auto-populated.** When creating a ticket, `sol call support create` automatically collects system info (version, OS, services, recent errors). You don't need to gather this manually.

4. **Outbound operations are runtime-gated.** If this run carries owner send-approval, you may draft and submit in one pass. If it does not, draft the message, show the owner exactly what would be sent, and stop — the runtime will refuse the send.

## Subcommands

### Registration

```bash
sol call support register
```

Register (or re-register) with the support portal. Generates an RSA-4096 keypair on first use, signs the TOS, and creates an account. Run this if you get auth errors.

### Knowledge Base

```bash
# Search articles
sol call support search "transcription errors"

# Read a specific article
sol call support article getting-started
```

Always search before filing a ticket. Present matching articles to the owner.

### Filing a Ticket

```bash
sol call support create \
  --subject "Transcription fails on long recordings" \
  --description "Recordings over 2 hours consistently fail with timeout errors. Started after updating to v2.1." \
  --severity medium \
  --category bug \
  --submit
```

The `create` command is a dry run by default: it prints the would-be payload and sends nothing. Pass `--submit` to actually file the ticket.

The submitted `create` command implements a KB-first flow:
1. Searches KB for related articles
2. Shows matches (owner can read them and cancel if resolved)
3. Collects diagnostics automatically
4. Shows the full ticket draft for review
5. Submits only when the runtime allows this run to send

**Flags:**
- `--subject` / `-s` — Ticket subject (required)
- `--description` / `-d` — Detailed description (required)
- `--product` / `-p` — Product name (default: solstone)
- `--severity` — low, medium, high, critical (default: medium)
- `--category` — bug, feature, question, account
- `--skip-kb` — Skip KB search (not recommended)
- `--submit` — Actually file the ticket; without it, print a safe dry-run preview
- `--yes` / `-y` — Keep the subprocess non-interactive. This is not the consent gate; the runtime decides whether a send is permitted.
- `--anonymous` — Strip installation identifiers

### Ticket Management

```bash
# List open tickets
sol call support list

# List all tickets (including resolved)
sol call support list --status resolved

# View a ticket with thread (includes attachment metadata)
sol call support show 42

# Reply to a ticket
sol call support reply 42 --body "Here's the additional info you requested..."

# JSON output for any command
sol call support list --json
sol call support show 42 --json
```

### Attachments

```bash
# Attach a screenshot to ticket #42
sol call support attach 42 ~/screenshot.png

# Attach multiple files
sol call support attach 42 screenshot.png error-log.txt

# Non-interactive upload; runtime still decides whether send is permitted
sol call support attach 42 screenshot.png --yes
```

Upload files to an existing ticket. Show each file name and size to the owner before upload. Attachments are a follow-up action — create the ticket first, then attach files.

**Limits:** max 10 MB per file.

**Supported formats:** `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.svg`, `.pdf`, `.txt`, `.csv`, `.html`, `.md`, `.xml`, `.json`

When an owner reports a visual bug (UI glitch, rendering issue), proactively suggest attaching a screenshot.

### Feedback

```bash
sol call support feedback --body "The entity search is great but I wish it could filter by date range" --submit
```

Lower friction than a full ticket. Feedback is dry run by default: it prints the would-be payload and sends nothing. Pass `--submit` to actually send. Feedback is submitted as a ticket with category "feedback". Supports `--anonymous` flag.

### Announcements

```bash
sol call support announcements
```

Check for product updates, known issues, and maintenance notices.

### Local Diagnostics

```bash
sol call support diagnose
sol call support diagnose --json
```

Reflects the journal host (read-only — no support ticket is sent). Shows:
- solstone version
- OS/platform info
- Active services and their status
- Recent errors from service logs
- Configuration (secrets stripped)

## Good Ticket Descriptions

A good ticket includes:
- **What happened** — specific behavior observed
- **What was expected** — what should have happened
- **Steps to reproduce** — how to trigger the issue
- **Context** — when it started, how often, any recent changes

The diagnostic collector auto-populates version, OS, and service status. You don't need to include these in the description.

## Examples

```bash
# Owner reports a bug — full flow
sol call support search "calendar sync"          # check KB first
sol call support create \
  --subject "Calendar events not syncing" \
  --description "Google Calendar events imported yesterday aren't showing up in the calendar app. Tried re-importing but same result." \
  --category bug \
  --severity medium \
  --submit

# Attach a screenshot to the ticket
sol call support attach 15 ~/screenshot.png

# Owner wants to give feedback
sol call support feedback \
  --body "Love the entity detection but it sometimes misidentifies project names as people" \
  --submit

# Check for responses on open tickets
sol call support list
sol call support show 15

# Quick system health check
sol call support diagnose
```

Running `create` or `feedback` without `--submit` produces a safe dry-run preview.

## Outcome Reporting

- **Success:** The request was filed or sent and a ticket id or confirmation came back.
- **Gate denial:** The runtime refused the send because this run carries no per-send owner approval. Nothing left the machine. Tell the owner to ask again from the live chat where they are present so the send carries approval.
- **Send failure:** The runtime allowed the send, then the portal or network errored. The send was attempted and failed.

## Gotchas

- **`create`/`feedback` are dry-run by default.** Re-run with `--submit` to actually send. The `DRY RUN` banner in stdout is the signal that nothing was sent; exit code is 0 in both cases.
- **KB-first is automatic on `create`.** The `create` command always searches the KB and shows matches for owner review before filing. Pass `--skip-kb` only if the issue is clearly unique — it's there for edge cases, not as a speed-up.
- **`--product` defaults to solstone.** Support handles other sol pbc products too. Confirm with the owner before filing a non-solstone ticket; don't assume the default.
- **Diagnostics can leak configuration.** The auto-collector strips secrets, but the full diagnostic payload must still be shown to the owner.
- **Attachments follow ticket creation.** Create first, then `attach` — attachments can't be included in the initial create call.
