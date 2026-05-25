# L8 Push Portal Dispatch Design

## 1. Auth Resolution

The original scope said the portal dispatch token should live in the JSON body with no `Authorization` header. That does not match the shipped worker. Production requires `Authorization: Bearer <dispatch_token>` for `POST /push/dispatch`; `account/src/push.js:226-233` reads the header, extracts the bearer token, resolves it with `resolveDispatchToken()`, and returns `401 invalid_token` when resolution fails.

L8 follows the shipped worker contract. The request body contains only the dispatch payload fields used by the worker: `summary`, `category`, and `request_id`. The token is sent only in the header and must never be logged.

## 2. Transport And Location

Use `urllib.request` for the portal call. This matches the only existing think-layer portal HTTP client, `solstone/think/services/portal_client.py`, and keeps one portal HTTP style in the think layer. `httpx` remains confined to `solstone/think/push/dispatch.py`, where it is used for APNs-specific async HTTP/2 delivery.

Add a new module:

`solstone/think/push/portal_dispatch.py`

Do not extend `solstone/think/push/dispatch.py`; that module is explicitly APNs transport, and its public surface is APNs categories, APNs payload builders, collapse-id builders, `send`, and `send_many`.

Final helper signature:

```python
def dispatch_via_portal(*, request_id: str, summary: str, category: str) -> dict | None
```

Timeout is 10 seconds. Do not reuse `POLL_TIMEOUT_SECONDS = 35`; that value is for scout enable long-polling, while push dispatch runs on the hot path.

## 3. Trigger Restructure

Prep found that `is_configured()` and `_eligible_devices()` currently run before `request_id`, `summary`, and `category` are extracted. For scout-only owners, those guards would return before a portal dispatch could run.

Resolution: extract the message fields immediately after the tract/event filter, then try scout portal routing before local APNs checks.

Trigger flow:

```text
1. Extract request_id, summary, category from message
2. Bail if summary is empty/missing
3. scout = scout_provenance()
   if scout and scout.get("dispatch_token"):
       result = dispatch_via_portal(request_id=..., summary=..., category=...)
       if result is not None:
           append_nudge_log(outcome="dispatched", via="portal", ...)
           return
4. Fall through to existing flow:
   if not is_configured(): return
   devices = _eligible_devices()
   if not devices: return
   try: send_many(...); outcome = "dispatched"; via = "local"
   except: outcome = "error"; via = "local"
   append_nudge_log(...)
```

The nudge log entry should include `via: "portal" | "local"` as a small diagnostic win. The no-scout baseline remains the same path as today when `scout_provenance()` returns `None` or returns a dict without `dispatch_token`, except for the added `via: "local"` field.

Implementation note from prep: the existing `test_handle_sol_chat_request_dispatches_and_logs` currently asserts full nudge-row equality, so its expected row must add `via: "local"`. The runtime path stays unchanged, but that assertion will not pass byte-for-byte once the diagnostic field is added.

## 4. Stale Scout Dict Caveat

`scout_provenance()` returns the `services.scout` dict when present; it does not call `is_scout_enabled()` or require `GOOGLE_API_KEY`. A stale config block can therefore exist even if scout is not fully enabled.

Gate portal routing on `scout.get("dispatch_token")` truthiness, not only on `scout` being non-None. Missing or empty `dispatch_token` means "treat as no scout" and fall through to local APNs.

## 5. Error Policy

`dispatch_via_portal()` handles portal dispatch as an opportunistic route. Failure never prevents the existing local APNs path from running.

- 2xx response: return parsed JSON dict, or `{"ok": True}` when the response body is empty.
- 4xx response: log warning `portal dispatch rejected`, return `None`.
- 5xx response: log warning `portal dispatch server error`, return `None`.
- `URLError`, `socket.timeout`, `TimeoutError`, or any unexpected exception: log warning `portal dispatch transport failure`, return `None`.
- No retries.
- Silent fall-through to local APNs.
- Log only status code and `request_id`.
- Never log `dispatch_token`, the `Authorization` header value, or response bodies that might contain secrets.

## 6. Test Plan

Tests live in `tests/test_push_triggers.py`; extending the existing module is enough.

- `test_dispatch_via_portal_*` - happy path, 4xx, 5xx, timeout.
- `test_handle_sol_chat_request_routes_via_portal_when_scout_enabled` - scout patched present + portal mock success -> `send_many` NOT called, nudge log outcome=dispatched + via=portal.
- `test_handle_sol_chat_request_falls_back_to_local_when_portal_fails` - scout present + portal returns None -> `send_many` IS called, via=local.
- `test_handle_sol_chat_request_falls_back_to_local_when_scout_missing_token` - scout dict present but no `dispatch_token` -> goes to local.
- `test_handle_sol_chat_request_no_scout_unchanged` - `scout_provenance` returns None -> `send_many` IS called, via=local.
- `test_dispatch_via_portal_does_not_log_token_plaintext` - caplog audit.
- `test_dispatch_via_portal_module_has_no_brand_canon_violations` - string-grep audit on the new file's text.

The helper assumes it was called intentionally. Scout/no-scout gating belongs in `handle_sol_chat_request()`, so there is no no-scout helper test.

## 7. Brand Canon

Avoid the banned owner-facing auth phrases in the new push module: `sign in`, `your account`, `linked`, `authenticate`, `log in`, and `login`. The intended log strings are:

- `portal dispatch rejected`
- `portal dispatch server error`
- `portal dispatch transport failure`

These avoid the banned set. Add a text audit for the new module so future edits do not introduce those strings.

## 8. Idempotence

No solstone-local dedup is added. Idempotence inherits from the L4 worker, which dedups by `request_id`.

The local trigger remains append-only for nudge log diagnostics. Portal success logs one `sol_chat_request_push` row with `via: "portal"` and returns. Portal failure falls through and logs the local result.

## 9. Deferred Follow-Ups

- L8.1: route `handle_chat_lifecycle` silent pushes through worker `/push/dedup`. The original spec grouped this with L8, but the current task scope defers it.
- Summary byte-length skew: local validation uses character count, while the worker uses UTF-8 byte count. Multi-byte summaries up to 80 characters can be accepted locally but rejected by the worker with `400`. MVP behavior is covered by the error policy: portal failure falls through to local APNs.
- Stale scout-dict cleanup: `scout_provenance()` does not gate on `is_scout_enabled()`. This dispatch path handles that defensively through the `dispatch_token` check; broader cleanup can be a separate decision.
