# Arc A L10 Convey Scout Enable Design

Backend-only design for adding a Convey orchestrator and SSE channel that enables
the scout service through the existing services portal handoff.

No template or JavaScript changes are in scope for this lode.

## Proposed File Tree

- Add `solstone/think/services/portal_client.py`
- Update `solstone/think/services/cli.py`
- Add `solstone/convey/services_scout.py`
- Update `solstone/convey/__init__.py`
- Update `solstone/convey/root.py`
- Update `solstone/convey/tests/conftest.py`
- Add `solstone/convey/tests/test_services_scout.py`
- Update service tests that import or monkeypatch moved helpers:
  - `tests/services/test_cli_namespace.py`
  - `tests/services/test_constants.py`
  - `tests/services/test_brand_canon.py`

## D1 — Shared Helper Module

Decision: create `solstone/think/services/portal_client.py`.

Move from `solstone/think/services/cli.py`: `_mint_nonce`, `DEFAULT_PORTAL_URL`,
`POLL_TIMEOUT_SECONDS`, `DEFAULT_WAIT_SECONDS`, `_portal_base_url`,
`_request_headers`, `_poll_url`, `_browser_url`, `_is_timeout_error`,
`_handle_http_status`, `_read_handoff_payload`, `_package_version`, and the
single-attempt portion of `_poll_handoff`.

Expose public names without leading underscores:

- `mint_nonce`
- `portal_base_url`
- `browser_url`
- `poll_url`
- `request_headers(component: str)`
- `is_timeout_error`
- `read_handoff_payload`
- `poll_handoff_once`

`request_headers(component)` builds `User-Agent:
solstone-<component>/<version>`. The CLI calls it with `"cli"`; Convey calls it
with `"convey"`.

Use a public frozen outcome object named `PollOutcome`, with named constructors
or equivalent helpers for `success`, `continue`, `failed`, and `timeout`.
`poll_handoff_once` performs exactly one HTTP request and returns only success,
continue, or failed. Wall-clock timeout remains owned by callers, which may
construct or emit timeout outcomes themselves.

Rationale: a single-attempt helper gives the CLI and Convey independent loop
control. The CLI can preserve its current terminal behavior and exit-code
translation, while Convey can emit SSE progress between attempts and apply its
own transient-network policy.

Implementer does: extract the shared portal mechanics into
`portal_client.py`, update imports and monkeypatch targets directly, and remove
the private `_CliError` dependency from extracted code.

## D2 — Route Handler Home

Decision: add `solstone/convey/services_scout.py` as a sibling Convey module
with its own Flask blueprint.

The module owns the start route, SSE route, orchestrator thread, in-process
registry, cleanup sweep, and small route helpers. `root.py` stays focused on
auth/core init routes, with only its exempt-list updated for the new blueprint
endpoints.

Rationale: this feature is expected to add roughly 200-300 LOC and has a
background worker plus registry state. Keeping it out of `root.py` makes the
orchestrator easier to test and avoids burying service-specific state inside
core authentication/onboarding code.

Implementer does: create a dedicated blueprint in `services_scout.py`, register
it from `create_app`, and keep route names under the `/init/services/scout/*`
URL space.

## D3 — Registry Shape

Decision: use one module-level registry dictionary protected by one module-level
lock in `solstone/convey/services_scout.py`.

Entry fields:

- `nonce: str`
- `nonce_id: str`
- `start_time_monotonic: float`
- `event_queue: queue.Queue` of `(event_name, data)` tuples
- `browser_open_attempted: bool = False`
- `browser_open_succeeded: bool | None = None`
- `terminal_event: tuple[str, dict] | None = None`
- `cleanup_at_monotonic: float | None = None`

Module-level state:

- `_REGISTRY: dict[str, OrchestratorEntry]`, keyed by `nonce_id`
- `_REGISTRY_LOCK = threading.Lock()`

`nonce_id` is a separate opaque token generated with `secrets.token_urlsafe`,
not a truncation of the nonce. The portal nonce may be visible in `portal_url`,
but the SSE registry lookup should still use its own identifier.

Rationale: the lifecycle is process-local, short-lived, and only needs to
coordinate one active flow. A small locked registry is enough and avoids adding
cross-process persistence that the current setup flow does not require.

Implementer does: define the dataclass and registry in the new module, and keep
all registry reads/writes under `_REGISTRY_LOCK` except queue put/get operations.

## D4 — Single Active Orchestrator Per Process

Decision: `POST /init/services/scout/start` is idempotent while one non-terminal
entry remains inside the wall-clock budget.

Handler sequence:

1. Sweep expired registry entries.
2. If `is_scout_enabled()` is true, return status `409` with `{"error":
   "already_enabled"}`.
3. If `is_manual_key_present()` is true, return status `409` with `{"error":
   "manual_key_present"}`. There is no force option in the backend route.
4. Acquire `_REGISTRY_LOCK`.
5. If any entry has no `terminal_event` and is still inside
   `WALL_CLOCK_BUDGET_SECONDS`, return that entry with status `202`.
6. Otherwise mint a new nonce and nonce_id, create and store the entry, release
   the lock, spawn the orchestrator thread, and return status `202`.

The response body for both fresh and idempotent starts is the same:
`nonce_id`, `subscribe_url`, and `portal_url`.

Rationale: double-clicks or retried POSTs should not spawn duplicate browser
opens or polling threads. Returning `202` for both fresh and existing active
work keeps client behavior simple and makes browser-open call count stable in
tests.

Implementer does: make the POST route idempotent around active entries and
ensure the orchestrator thread is spawned only after the new entry is visible in
the registry.

## D5 — Portal URL Exposure

Decision: call `portal_base_url()` once in the start handler for each new entry,
and derive all related URLs from that same base and nonce.

The same `browser_url(base, nonce)` string is used for the POST response
`portal_url` and for the orchestrator's `webbrowser.open` call. The polling
worker uses `poll_url(base, nonce)` through `poll_handoff_once`.

Rationale: this makes the `SERVICES_PORTAL_URL` override consistent across
response, browser open, and polling. It also makes tests straightforward because
a single monkeypatch controls all three surfaces.

Implementer does: compute and store or pass the base/URL pair consistently, and
do not recompute the base separately inside the worker.

## D6 — Browser Open Inside Thread

Decision: the orchestrator thread attempts to open the browser as its first
action.

It sets `entry.browser_open_attempted = True`, then calls
`webbrowser.open(browser_url(base, nonce), new=2)`, recording
`browser_open_succeeded` as true or false. Exceptions from `webbrowser.open`
are caught and recorded as `False`; they do not stop polling.

The SSE `subscribed` event is emitted by the SSE generator, not the
orchestrator. It reads the current browser-open fields at subscription time, so
the values may still be `False` and `null` if the client subscribes before the
worker gets CPU.

Rationale: opening the browser from the worker keeps POST fast and makes
idempotent double-POST behavior deterministic. Treating browser-open failure as
non-terminal preserves the manual portal URL fallback already returned by POST.

Implementer does: import `webbrowser` in `services_scout.py`, not in the shared
portal client, and keep browser opening out of `cli.py` extraction except for
the existing CLI wrapper.

## D7 — Cleanup Ordering On Terminal

Decision: terminal entries stay in the registry for a short grace period.

When the orchestrator reaches a terminal state:

1. Set `entry.terminal_event` to the terminal event tuple.
2. Put the same tuple on `entry.event_queue`.
3. Set `entry.cleanup_at_monotonic` to now plus `GRACE_SECONDS`.
4. Return from the orchestrator.

Do not remove the registry entry immediately. At the top of every POST and GET
handler, call a sweep function that removes entries whose cleanup timestamp is
set and older than now. There is no separate sweeper thread.

SSE closure rule: the generator emits `subscribed` first. Then it drains queued
events. If it yields an event whose name is terminal (`scout-enabled`, `failed`,
or `timeout`), it closes the stream immediately after that yield. If a late
subscriber arrives while `terminal_event` is already set and the grace period
has not expired, it emits `subscribed`, then the stored terminal event, then
closes.

Rationale: this handles the race where the worker finishes just before or just
after the browser subscribes. Sweeping on handler entry avoids an extra
maintenance thread while keeping the registry bounded.

Implementer does: centralize terminal recording in one helper and use the same
terminal-name predicate in the worker and SSE generator tests.

## D8 — Convey Restart Recovery Test

Decision: add a test that bypasses the orchestrator and provisions scout state
directly.

The test calls `provision_scout_handoff()` with a valid payload, then posts to
`/init/services/scout/start`. The expected response is status `409` and body
`{"error": "already_enabled"}`.

Rationale: the registry is intentionally process-local, so recovery after a
restart is based on journal config state, not in-memory job state. This test
proves a completed handoff is recognized even if no orchestrator entry exists.

Implementer does: add this case to the new Convey scout route tests.

## D9 — Error Taxonomy Mapping

Decision: Convey maps terminal outcomes to named SSE events with JSON data.

Terminal SSE events:

- Successful `provision_scout_handoff`: event `scout-enabled`, data
  `{"account_id": payload["account_id"]}`.
- Malformed `200` payload from `read_handoff_payload`: event `failed`, data
  `{"reason": "unexpected_payload", "detail": "<message>"}`.
- `provision_scout_handoff` raises `ValueError`: event `failed`, data
  `{"reason": "unexpected_payload", "detail": "<message>"}`.
- `provision_scout_handoff` raises `JournalNotInitializedError`: event
  `failed`, data `{"reason": "journal_not_initialized", "detail": null}`.
- `provision_scout_handoff` raises any other exception: event `failed`, data
  `{"reason": "write_failed", "detail": "<str(e)>"}`.
- Portal status `410`: event `failed`, data
  `{"reason": "consent_link_expired", "detail": null}`.
- Portal status `400`: event `failed`, data
  `{"reason": "nonce_invalid", "detail": null}`.
- TLS verification failure: event `failed`, data
  `{"reason": "tls_verification_failed", "detail": "<msg>"}`.
- Wall-clock budget exceeded: event `timeout`, data
  `{"elapsed_ms": <int>}`.
- Unhandled orchestrator exception: event `failed`, data
  `{"reason": "internal_error", "detail": "<str(e)>"}`.

Network errors such as connection refused, DNS failure, or temporary portal
unreachability are transient for Convey. The worker retries until the
wall-clock budget expires, emitting only non-terminal `waiting` events with
`elapsed_ms` and no transient error detail. If the budget expires while the
portal remains unreachable, the terminal event is `timeout`, not
`failed/portal_unreachable`.

The CLI keeps its existing user-facing behavior: a `portal_unreachable` failed
outcome remains terminal for the CLI and maps to its current exit code and copy.

Rationale: the prep pass showed current CLI behavior fails immediately on
non-timeout network errors, but the Convey wizard benefits from tolerating
short transient network failures. Splitting caller policy around the same
single-attempt helper keeps both behaviors explicit.

Implementer does: keep reason strings canonical, never send Google keys or
dispatch tokens through SSE, and ensure transient network detail is not exposed
in waiting events.

## D10 — Importing `_is_setup_complete`

Decision: `services_scout.py` imports `_is_setup_complete` from
`solstone.convey.root`.

There is no circular import if the new blueprint is registered from
`create_app`, because `root.py` does not import `services_scout.py`. Reusing the
helper avoids a second implementation of setup-complete semantics.

Rationale: moving `_is_setup_complete` would broaden this lode and touch root
structure for little gain. Importing the private helper is acceptable here
because this is an internal sibling module in the same package.

Implementer does: import `_is_setup_complete` directly and use it only for
setup-state decisions needed by these routes.

## D11 — Blueprint Registration

Decision: `services_scout.py` defines its own blueprint named
`services_scout`, registered from `solstone/convey/__init__.py:create_app`.

Routes:

- `POST /init/services/scout/start`, endpoint `services_scout.start`
- `GET /init/services/scout/events/<nonce_id>`, endpoint
  `services_scout.events`

Update `root.require_login` exempt-list to include both endpoint names. The
`@bp.before_app_request` hook in `root.py` runs for all blueprints, so the
exempt-list continues to govern these routes.

Rationale: a separate blueprint matches Flask convention for a self-contained
feature and preserves route-name clarity in auth tests. Registering from
`create_app` is consistent with the other Convey sibling blueprints.

Implementer does: import the new blueprint in `create_app`, register it near the
other Convey-level blueprints, and add both endpoint names to the root exempt
set.

## D12 — Conftest Variant

Decision: add `convey_env_setup_pending` to
`solstone/convey/tests/conftest.py`.

It mirrors `convey_env` but writes a journal config without
`setup.completed_at`. It still sets `SOLSTONE_JOURNAL`, creates the app with the
temporary journal, and returns an object with `journal`, `client`, and `app`.

Existing tests keep using `convey_env`. New setup/wizard route tests use
`convey_env_setup_pending` to prove exempted init routes are reachable before
setup completion.

Rationale: this avoids weakening existing tests that assume completed setup and
keeps setup-pending behavior explicit at each test call site.

Implementer does: factor only if it stays small; otherwise duplicate the few
fixture lines to keep the variant readable.

## Implementation Sequence

1. Add `portal_client.py` and update `cli.py` to consume it while preserving CLI
   behavior and exit codes.
2. Update service tests that import or monkeypatch moved helpers.
3. Add `services_scout.py` with registry, start route, worker, and SSE route.
4. Register the blueprint in `create_app` and exempt its endpoints in
   `root.require_login`.
5. Add the setup-pending Convey fixture.
6. Add Convey scout route tests for start, idempotent double POST, SSE success,
   terminal replay during grace, timeout, direct-provision restart recovery, and
   representative failure mappings.
7. Run targeted service and Convey tests, then the broader relevant suite.

## Risks And Notes

- Current CLI behavior treats non-timeout portal network errors as terminal.
  Convey intentionally retries those as transient; tests should pin this
  difference so future maintainers do not accidentally collapse the policies.
- The registry is process-local. That is acceptable for this setup wizard flow,
  but completed state must always be read from journal config.
- Browser-open state is inherently racy relative to the SSE `subscribed` event.
  Tests should allow `browser_open_attempted` to be false/null at subscribe time
  unless they explicitly synchronize the worker.
- SSE tests must use `buffered=False` and close responses in `finally` blocks to
  avoid hanging generators.
- No template or JavaScript updates should be included in this lode.
