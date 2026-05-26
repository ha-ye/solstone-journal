# Lode 3 Chat Liveness And Retry Design

## 1. Constants

The owner-facing chat liveness and retry copy lives in `solstone/apps/chat/copy.py`.
`solstone/convey/static/chat_copy.js` mirrors these bytes on `window.solChatCopy`.

- `CHAT_LIVENESS_THINKING = "Sol is thinking…"`
- `CHAT_ERROR_RETRY_LABEL = "Try again"`
- `CHAT_ERROR_RETRY_ARIA_FORMAT = "Try again — re-send: {excerpt}"`
- `CHAT_LIVENESS_TASK_FORMAT = "{label} {task}"`

`CHAT_LIVENESS_TASK_FORMAT` composes with `talent_label_for(target, "running")`.
The running label already carries the trailing ellipsis. Example:
`Looking in your journal… Retrieve the history for Adrian Kunzle and Bill Putsis from this week`.

If `talent_label_for(name, "running")` raises `ValueError`, leave the placeholder
at phase-1 text. Do not add a fallback constant and do not log.

## 2. Excerpt Helper

Locality decision: put the Python helper in `solstone/apps/chat/copy.py` as
`chat_error_retry_excerpt(text: str) -> str`. The 60-character ceiling and
truncation glyph are owner-facing copy contract details, so they belong with the
copy constants rather than private route code.

Algorithm:

- Convert missing input to `""`; owner messages should already be non-empty.
- Return the original text unchanged when `len(text) <= 60`.
- Otherwise return `text[:60] + "…"`.
- The ellipsis does not count toward the 60 source-character ceiling; truncated
  excerpts are at most 61 rendered code points.

Mirror the same algorithm in `chat_copy.js`, exported as
`window.solChatCopy.chatErrorRetryExcerpt`. Add parity coverage in
`chat-bar-copy.html` for a short string, an exactly-60-character string, and a
61-plus-character string.

## 3. SSR Flow

`solstone/apps/chat/routes.py` adds `_build_chat_error_retry_texts(events)`.
It walks events once and maintains a FIFO `deque[str]` named
`pending_owner_texts`.

- On `owner_message`, append `event.get("text", "")`.
- On `sol_message`, pop one pending owner text if present.
- On `chat_error`, pop one pending owner text if present and record it by event
  index.

The route shallow-copies events and adds `retry_text` directly to each
`chat_error` dict that has a mapped original message. `_chat_event.html` then
reads `ev.retry_text`. This keeps the template diff smaller than introducing a
macro argument from the loop.

`_chat_event.html` extends the existing `chat_error` branch. The retry button
renders inside `.chat-error-block` after the detail block, when `ev.retry_text`
exists:

- class: `chat-error-retry`
- `data-retry-text`: the original message text
- `aria-label`: `CHAT_ERROR_RETRY_ARIA_FORMAT.format(excerpt=chat_error_retry_excerpt(ev.retry_text))`
- visible label: `CHAT_ERROR_RETRY_LABEL`

No event schema changes are made to `owner_message` or `chat_error`.

## 4. Transcript Live Flow

`solstone/apps/chat/workspace.html` adds a closure-scoped
`pendingPlaceholders` FIFO array. Entries are `{ element, ownerText, ts }`.

Phase-1:

- In `appendEventFromLive`, after rendering and appending an `owner_message`,
  append a second `<li>` placeholder.
- The placeholder text is `window.solChatCopy.CHAT_LIVENESS_THINKING`.
- Remove `.chat-empty` through the existing owner-message append first; the
  placeholder append is a no-op for empty-state cleanup.

DOM shape:

- `<li class="chat-event chat-event--sol chat-event--placeholder" data-kind="sol_placeholder" data-ts="...">`
- child article: `chat-bubble chat-bubble--sol chat-bubble--placeholder`
- text node inside `.chat-bubble-text`

Amend transcript queries that should ignore placeholders:

- `decorateBubbles`: select `.chat-event:not([data-kind="sol_placeholder"])`.
- `insertTimeSeparators`: same filter.
- `renderEventItem` event-id count: same filter.

This preserves event IDs and prevents `decorateBubbles` from stripping the
placeholder classes.

Phase-2:

- On `talent_spawned`, peek the head of `pendingPlaceholders`.
- If `event.task.trim()` is empty, keep phase-1 text.
- Otherwise call `window.solChatCopy.talentLabel(event.name, "running")`.
- If that throws, keep phase-1 text.
- If it succeeds, set placeholder text to
  `CHAT_LIVENESS_TASK_FORMAT.replace("{label}", label).replace("{task}", task)`.
- Do not pop on `talent_spawned`.
- Multiple `talent_spawned` events update the same head placeholder in place.
- `talent_finished` does not remove the placeholder.

Terminal:

- On `sol_message` or `chat_error`, shift the head entry from
  `pendingPlaceholders`.
- Remove the placeholder element before appending the real sol bubble or error
  block.
- For live `chat_error`, pass the shifted `ownerText` into `renderEventItem` so
  `renderEventBody` can render the retry button with the same attributes as SSR.
- A re-error after retry naturally renders a new button because retry posts a
  new owner message, creating a new pending entry, and the new `chat_error` is
  rendered through the same live path.

Retry handler:

- Add one delegated `click` listener on `transcript` for `[data-retry-text]`.
- It handles SSR and live buttons.
- It posts to `/api/chat` with the original message text and the same payload
  shape as normal chat: `message`, `app`, `path`, `facet`.
- No new endpoint and no payload variant.
- The handler should disable only the clicked button during the request to avoid
  double submits. Chat-bar retry UI is out of scope.

## 5. Chat-Bar Live Flow

`solstone/convey/templates/app.html` adds a closure-scoped
`pendingPlaceholders` FIFO array. Entries are `{ element: null, ownerText, ts }`.

Phase-1:

- In `handleChatEvent`, use the `owner_message` branch, not `handleSubmit`.
- Push `{ element: null, ownerText: msg.text || "", ts: msg.ts || Date.now() }`.
- Call `setPendingState(false)` as today.
- If `solRequestState` is not set, call `setStatus(CHAT_LIVENESS_THINKING, CHAT_LIVENESS_THINKING)`.
- This makes the status appear regardless of which page submitted the message.

Phase-2:

- In the existing `talent_spawned` branch, keep `upsertTalent(...)` intact.
- Add only the new liveness status update.
- If `solRequestState` is set, skip the new `setStatus` call but still update
  the talent tray.
- Peek the head pending entry; do not pop.
- Empty/whitespace task keeps phase-1 text.
- Unknown talent target keeps phase-1 text.
- Successful composition uses `CHAT_LIVENESS_TASK_FORMAT`.

Terminal:

- In the existing `sol_message` branch, shift the head pending entry and let the
  existing `setStatus(msg.text, ...)` overwrite the placeholder.
- In the existing `chat_error` branch, shift the head pending entry and let the
  existing error `setStatus(...)` overwrite the placeholder.
- No chat-bar retry button is rendered.

## 6. CSS

Add the visual treatment in `solstone/convey/static/app.css` next to the existing
`.chat-bubble*` rules, not in the small `workspace.html` style block.

Rule:

- `.chat-bubble--placeholder { opacity: 0.65; font-style: italic; }`

No animation and no extra ellipsis.

## 7. Test Plan By Acceptance Criterion

AC1 constants: extend `tests/test_chat_copy.py::test_js_parity` to assert Python
constants are mirrored in `chat_copy.js`, and add direct assertions for the exact
bytes in `solstone/apps/chat/copy.py`.

AC2 excerpt helper: add `tests/test_chat_copy.py::test_chat_error_retry_excerpt`
for short, exactly-60, and longer-than-60 input. Assert the truncated case is
first 60 source characters plus U+2026.

AC3 JS excerpt parity: extend `solstone/convey/static/tests/chat-bar-copy.html`
with the same three excerpt fixtures and assert `window.solChatCopy` returns the
same visible strings. `tests/verify_browser.py` already runs this page.

AC4 SSR backfill: add
`solstone/apps/chat/tests/test_routes.py::test_chat_error_retry_backfills_owner_text`
with `owner_message -> chat_error`; assert the button exists, carries escaped
`data-retry-text`, and visible label comes from chat copy.

AC5 SSR detail order: extend that route test with `detail`; assert detail markup
appears before `chat-error-retry` in the rendered HTML.

AC6 SSR FIFO: add
`test_chat_error_retry_backfill_uses_fifo_when_messages_queue` with two owner
messages before one error; assert the first error maps to the first owner text.

AC7 SSR no schema change: add a focused assertion in the same test file that
stored `owner_message` and `chat_error` events do not need `retry_text` on disk;
the rendered copy comes from route backfill only.

AC8 live phase-1 placeholder: extend
`solstone/apps/chat/tests/test_origin_tag_live.py` or add
`test_liveness_live.py::test_live_script_creates_phase_one_placeholder`; assert
the page HTML includes `pendingPlaceholders`, `CHAT_LIVENESS_THINKING`, and the
placeholder DOM builder.

AC9 live placeholder query safety: add
`test_liveness_live.py::test_placeholder_is_excluded_from_event_bookkeeping`;
assert the script contains `.chat-event:not([data-kind="sol_placeholder"])` in
`decorateBubbles`, `insertTimeSeparators`, and event-id counting.

AC10 live phase-2 known talent: add
`test_liveness_live.py::test_live_script_updates_placeholder_on_talent_spawned`;
assert the script calls `window.solChatCopy.talentLabel(..., "running")` and
uses `CHAT_LIVENESS_TASK_FORMAT`.

AC11 live phase-2 fallbacks: add
`test_liveness_live.py::test_live_script_keeps_phase_one_for_unknown_or_empty_task`;
assert the script catches `talentLabel` errors and checks `task.trim()`.

AC12 terminal removal and re-error: add
`test_liveness_live.py::test_live_script_removes_placeholder_on_sol_message_or_chat_error`;
assert both terminal branches shift pending state and remove the element before
normal append. This also covers re-error because each `chat_error` render path
builds its own retry button.

AC13 delegated retry handler: add
`test_liveness_live.py::test_live_script_delegates_retry_clicks`; assert there
is one transcript listener for `[data-retry-text]`, it posts `/api/chat`, and it
uses `message`, `app`, `path`, and `facet`.

AC14 live retry button rendering: extend the live script test to assert
`renderEventBody` creates `.chat-error-retry`, sets `data-retry-text`, sets
`aria-label` with `CHAT_ERROR_RETRY_ARIA_FORMAT`, and uses
`CHAT_ERROR_RETRY_LABEL`.

AC15 CSS: add a lightweight assertion in an existing CSS/static test or
`tests/test_chat_copy.py::test_chat_placeholder_css_present` reading
`solstone/convey/static/app.css`; assert `.chat-bubble--placeholder`, opacity
`0.65`, and `font-style: italic`.

AC16 chat-bar phase-1: add
`solstone/apps/chat/tests/test_liveness_chat_bar.py::test_chat_bar_sets_phase_one_from_owner_message`;
assert rendered `app.html` script pushes pending state in the `owner_message`
branch and calls `setStatus` with `CHAT_LIVENESS_THINKING`.

AC17 chat-bar phase-2 and precedence: add
`test_chat_bar_sets_phase_two_from_talent_spawned_without_blocking_talent_tray`;
assert `upsertTalent` remains, the new `setStatus` is guarded by
`!solRequestState`, and composition uses the shared format.

AC18 chat-bar terminal/no retry: add
`test_chat_bar_terminal_overwrites_liveness_without_retry_button`; assert
`sol_message` and `chat_error` branches shift pending state and keep existing
`setStatus` behavior, and no chat-bar retry control is introduced.

## 8. Decisions Table

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Excerpt helper lives in `apps/chat/copy.py`. | The limit and ellipsis are copy contract, not route mechanics. |
| 2 | JS uses the exact 60-source-plus-ellipsis algorithm. | Matches Python and makes the spec's ceiling unambiguous. |
| 3 | Routes shallow-copy events and add `ev.retry_text`. | Smaller template diff than macro argument plumbing. |
| 4 | Retry click uses delegated listener on `transcript`. | Handles SSR and live buttons through one path. |
| 5 | Re-error gets a new button through normal live rendering. | Retry creates a fresh owner turn and the next error follows existing render flow. |
| 6 | Chat-bar phase-1 triggers on live `owner_message`. | Works regardless of submit origin/page. |
| 7 | Chat-bar phase-2 is added inside `talent_spawned`. | Keeps talent tray behavior while respecting sol-request status precedence. |
| 8 | Chat-bar terminal uses existing `sol_message`/`chat_error` status writes. | Existing terminal text naturally overwrites liveness. |
| 9 | Empty `task` keeps phase-1 text. | Avoids awkward composed labels with missing task text. |
| 10 | Placeholder follows owner bubble and no-ops empty cleanup. | Existing owner append already removes `.chat-empty`; ordering remains simple. |

## 9. Risks And Surprises

- Current chat copy has only `exec` and `reflection` running labels, not four
  labels. Unknown targets must stay phase-1.
- Existing running labels already include U+2026, so composition must not add a
  second ellipsis.
- `CONVEY_ACTION_TRY_AGAIN` exists in Python convey copy, but the retry label is
  intentionally local to chat copy and should not use convey copy.
- `chat_error` has no owner text and `owner_message` has no `use_id`; SSR retry
  text is inferred by FIFO, not by a stored schema field.
- The FIFO SSR reducer intentionally consumes on `sol_message` as well as
  `chat_error`, matching the placeholder lifecycle. Later multi-hop errors after
  a sol reply will not have retry text unless the event schema changes later.
- Any placeholder included in `.chat-event` queries will skew live event IDs and
  be restyled by `decorateBubbles`; all bookkeeping queries must explicitly
  exclude `data-kind="sol_placeholder"`.
