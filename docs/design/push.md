# Push Registry And Relay

## Summary

The journal's push role is deliberately narrow:

- Keep a device registry keyed by each paired device's link fingerprint.
- Provide a test-push endpoint that goes through the hosted relay.
- Relay sol-initiated chat push events to the hosted service through
  `portal_dispatch`.

The journal does not contact Apple. Device delivery, relay-to-service token
plumbing, and platform-specific delivery details live outside this repository.

## Module Layout

| Path | Role |
|---|---|
| `solstone/think/push/devices.py` | Sole writer for `journal/config/push_devices.json`. Stores one row per link fingerprint. Re-registering a fingerprint replaces that row, and registering a token under another fingerprint drops the older holder so one token maps to exactly one device. |
| `solstone/convey/push.py` | Root Flask blueprint for `/api/push/register`, `/api/push/status`, and `/api/push/test`. Registration and deregistration source the fingerprint from `g.identity.fingerprint`, never from request JSON. |
| `solstone/think/push/triggers.py` | Relay-only callosum handlers for direct chat requests and chat lifecycle events, plus the nudge-log writer used by sol-initiated chat accounting. |
| `solstone/think/push/runtime.py` | Runtime singleton that starts a callosum listener and routes each message through the two push trigger handlers. |
| `solstone/think/push/portal_dispatch.py` | HTTP relay client for the hosted `/push/dispatch` and `/push/dedup` endpoints. |

`push_devices.json` stores:

```json
{
  "devices": [
    {
      "fingerprint": "sha256:...",
      "token": "...",
      "bundle_id": "org.solpbc.solstone-swift",
      "environment": "development",
      "platform": "ios",
      "registered_at": 1770000000
    }
  ]
}
```

No device public key is stored today. Per-device body encryption is a future
arc and will require an explicit `device_pubkey` field or equivalent schema.

## Endpoint Shapes

All `/api/push/*` routes inherit the normal Convey auth gate.

### `POST /api/push/register`

Request body:

```json
{
  "device_token": "...",
  "bundle_id": "org.solpbc.solstone-swift",
  "environment": "development",
  "platform": "ios"
}
```

The handler requires `g.identity.fingerprint`. Requests with no fingerprint are
rejected with `push_request_invalid` and detail
`push registration requires a paired device`.

On success, `devices.register_device(...)` upserts by fingerprint and returns:

```json
{"registered": true, "device_count": 1}
```

### `DELETE /api/push/register`

No request body is needed. The handler requires `g.identity.fingerprint` and
removes that fingerprint's row:

```json
{"removed": true, "device_count": 0}
```

### `GET /api/push/status`

Response:

```json
{
  "device_count": 1,
  "relay_available": true,
  "devices": [
    {
      "token_suffix": "...abcd",
      "bundle_id": "org.solpbc.solstone-swift",
      "environment": "development",
      "platform": "ios",
      "registered_at": "2026-05-20T00:00:00Z"
    }
  ]
}
```

`relay_available` is true when an approved scout dispatch token is present.
Fingerprints and full tokens are not exposed.

### `POST /api/push/test`

Optional request body:

```json
{"body": "This is a test notification."}
```

The handler requires an approved dispatch token. It creates a
`push-test-<hex>` request id and calls `dispatch_via_portal(...)` with the test
summary and sol-chat-request category. If no dispatch token is present, the
route returns `503 feature_unavailable` with detail `push relay unavailable`.
If the relay call fails, it returns `503 feature_unavailable` with detail
`push relay dispatch failed`.

Success response:

```json
{"dispatched": true, "request_id": "push-test-abc123def456"}
```

## Relay Triggers

`runtime._on_callosum_message(...)` calls:

1. The direct chat request trigger handler.
2. `triggers.handle_chat_lifecycle(message)`

The direct chat request handler listens for:

- `tract == "chat"`
- `event == KIND_SOL_CHAT_REQUEST`
- non-empty `request_id`

With an approved dispatch token, it calls `dispatch_via_portal(request_id,
summary, category)`.

`handle_chat_lifecycle` listens for:

- `tract == "chat"`
- `event in {KIND_OWNER_CHAT_OPEN, KIND_OWNER_CHAT_DISMISSED}`
- non-empty `request_id`

With an approved dispatch token, it calls
`dispatch_dedup_via_portal(request_id, action=event)`.

## Nudge Log

`triggers.py` remains the writer for `journal/push/nudge_log.jsonl`. Rows are
append-only JSON objects.

Successful relay row:

```json
{
  "ts": 1770000000,
  "kind": "<chat-request push kind>",
  "dedupe_key": "req-1",
  "category": "notice",
  "outcome": "dispatched",
  "via": "portal"
}
```

No dispatch token row:

```json
{
  "ts": 1770000000,
  "kind": "<chat-request push kind>",
  "dedupe_key": "req-1",
  "category": "notice",
  "outcome": "skipped",
  "reason": "no_dispatch_token"
}
```

Relay unavailable row:

```json
{
  "ts": 1770000000,
  "kind": "<chat-request push kind>",
  "dedupe_key": "req-1",
  "category": "notice",
  "outcome": "skipped",
  "reason": "portal_unavailable"
}
```

Lifecycle rows use `kind == "sol_chat_lifecycle_push"` and store the lifecycle
event name in `category`.

## Domain Ownership

Per AGENTS.md L2, `solstone/think/push/devices.py` is the sole writer for
`journal/config/push_devices.json`. `solstone/think/push/triggers.py` is the
sole writer for `journal/push/nudge_log.jsonl`.

`solstone/convey/push.py` validates HTTP input and delegates mutations to
`devices.py`. It must not write journal files directly.

## Out Of Scope

- Relay-to-service token plumbing beyond the approved dispatch token check.
- Per-device body encryption.
- A `device_pubkey` column or migration.
- Delivery-provider behavior owned by the hosted relay.
