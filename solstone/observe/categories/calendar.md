{

  "description": "Calendar and scheduling interfaces: day/week/month views, agenda lists, event details, event creation forms, booking pages, availability grids, and RSVP/scheduling workflows",
  "output": "markdown",
  "extraction": "Extract when the visible date range, event detail, availability grid, booking page, or scheduling workflow changes",
  "importance": "high"

}

# Calendar Text Extraction

Extract text from this calendar or scheduling screenshot.

## Header

`# [Calendar/App - View or Date Range]`

## Content Focus

Extract the scheduling information that is visible:

- **Calendar views**: Preserve date range, visible days, event titles, times, locations, calendars/colors if meaningful, and attendee/status hints.
- **Event detail/edit forms**: Include title, start/end time, date, location, conferencing link/platform, guests/attendees, description, recurrence, reminders, RSVP/status, and calendar name when visible.
- **Availability/booking pages**: Include host/service name, timezone, available slots, selected slot, duration, location/meeting method, form fields, and booking state.
- **Scheduling assistants**: Preserve participant names, availability blocks, conflicts, proposed times, and selected time.

## Quality

- Preserve chronological order.
- Keep timezones and recurrence details when visible.
- Mark unclear text with `[unclear]`.
- Mark cut-off text with `...`.
- Skip unrelated app chrome unless it identifies the calendar account, date range, or scheduling state.

Return ONLY the formatted markdown.
