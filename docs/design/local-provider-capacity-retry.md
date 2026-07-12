# Local Provider Capacity Retry

## Scope

This note covers the bundled local generate path only. It does not change
llama-server launch flags, tiers, context sizing, cloud-provider
classification, BYO endpoint behavior, cogitate, grace windows, backoff, or
multi-retry policy.

Ground truth from llama.cpp `b9291`:

- Genuine prompt overflow is HTTP 400 with
  `error.type == "exceed_context_size_error"`. The pinned server also includes
  numeric `error.n_prompt_tokens` / `error.n_ctx`, but classification does not
  depend on them.
- Post-admission unified-KV exhaustion is HTTP 500 with
  `error.type == "server_error"` and message
  `Context size has been exceeded.`, with no numeric context fields.
- `local_budget.fit_contents()` makes admitted bundled requests fit the
  advertised window before send, so a generic context-shaped error is capacity
  pressure unless the server gives structured proof of prompt overflow.

## Decisions

### D1. Bundled Error Discriminator

Decision: keep the current entry gate exactly as narrow as it is today:
`_raise_bundled_status()` only reclassifies an `httpx.HTTPStatusError` when
the response body contains one of `shared._CONTEXT_WINDOW_PATTERNS`.
Non-matching HTTP errors keep raising the raw `HTTPStatusError`.

Within that existing gate, parse the JSON body and treat prompt overflow as
authoritative only when all of these are true:

- the body is a JSON object,
- `error` is a JSON object,
- `error.type == "exceed_context_size_error"`.

That path remains `ContextBudgetExceeded` /
`context_budget_exceeded`.

Every other context-shaped body inside the gate is
`LocalCapacityExhausted` / `local_capacity_exhausted`: absent body, non-JSON
body, missing `error`, missing `error.type`, or `server_error`. This is
intentionally conservative for the pinned bundled server: if the server cannot
prove the prompt was too large, a fitted request is treated as transient
capacity pressure.

Never include `response.text` in a raised exception message. The owner-facing
exception messages stay fixed strings.

### D2. Reason Code And Copy

Decision: add `local_capacity_exhausted`, with a
`LocalCapacityExhausted(LocalProviderError)` subclass in
`solstone/think/providers/local.py`, sibling to `ContextBudgetExceeded`.

Exception message:

- `The local model was busy and could not finish this request. Try again in a moment.`

Readiness/chat copy:

- `_ENTRIES` summary: `the local model was busy and could not finish this request`
- `_ENTRIES` detail: `Try again in a moment.`
- `chat_reasons.js` mirror template: same as the summary, with `action: null`

`operator_detail` needs no custom implementation because
`provider_readiness._operator_detail()` already starts with
`reason_code=<code>` for every mapped reason.

Membership:

- Add to `RUNTIME_REASON_CODES`.
- Add to `_ENTRIES` and `CHAT_REASONS`.
- Do not add to `PROVIDER_LEVEL_CODES`; this is a request/model-level runtime
  condition, like `local_queue_timeout`, and should keep the model component in
  the semantic key.
- Do not add to `_STARTUP_REASON_CODES`; the server is already running and the
  failure is post-admission.
- Do not add to `DETERMINISTIC_FAILURE_REASON_CODES`; this is transient and
  retryable, not a permanent prompt or policy failure.

### D3. Exclusive Retry Signal

Decision: use the provider-specific kwarg `local_exclusive_admission`.

Flow:

- `talents._execute_generate()` passes it only on the capacity retry.
- `models.generate_with_result()` already accepts `**kwargs` and forwards them
  to `provider_mod.run_generate()`.
- `local.run_generate()` and `local.run_agenerate()` pop it alongside
  `inference_retry_index`.

Rules:

- The kwarg never reaches `_build_request_body()` or the HTTP request body.
- The BYO branch ignores it after popping; BYO admission, when configured,
  remains non-exclusive.
- It is not inferred from `inference_retry_index`. Existing incomplete-JSON
  retries pass `inference_retry_index=1` and remain non-exclusive.

### D4. Exclusive Admission

Decision: refactor `LocalPermit` to hold a list of locked files, then extend
the sync and async admission twins with an explicit exclusive mode.

Public shape:

- `acquire_local_slot(capacity, timeout_s, *, exclusive=False, cancel_event=None)`
- `acquire_local_slot_async(capacity, timeout_s, *, exclusive=False)`

`exclusive=False` keeps current one-slot behavior. `exclusive=True` means the
head waiter must hold every `slot-<i>.lock` for `0..capacity-1`.

Hazard controls:

- FIFO stays first: only the oldest live wait ticket may attempt acquisition.
  Two exclusive waiters can never concurrently hold disjoint partial sets.
- Acquisition is all-or-nothing: lock each slot non-blocking; on any busy slot
  or unexpected error, release every lock already acquired before returning or
  raising.
- Waiters never sleep while holding a partial set.
- Timeout and cancellation semantics are unchanged:
  `LocalAdmissionTimeout` / `local_queue_timeout`, ticket cleanup in `finally`.
- `capacity == 1` degenerates to the current single-slot case.

Permit representation:

- Keep `LocalPermit.slot_index` because telemetry reads it as
  `admission_slot`.
- For normal holds, `slot_index` is the acquired slot.
- For exclusive holds, `slot_index` is `0`, representing the first locked slot.
- `release()` is idempotent and closes/unlocks every held file, preserving the
  existing double-release guard behavior.

Telemetry:

- Do not add a new telemetry field. `retry_index=1` plus
  `reason_code=local_capacity_exhausted` identifies the exclusive retry path.
- `admission_slot` reports `permit.slot_index`, so exclusive attempts report
  `0`.

### D5. Sync/Async Parity

Decision: both bundled generate paths share the same discriminator and the same
admission switch.

- `_raise_bundled_status()` remains the single classification helper for sync
  and async HTTP responses.
- `run_generate()` and `run_agenerate()` both pop `local_exclusive_admission`
  and pass it to their sync/async admission twin.
- BYO sync/async paths do not call `_raise_bundled_status()`. Non-confidential
  BYO paths may acquire non-exclusive local admission permits; confidential BYO
  paths remain ungoverned.

### D6. Talent Retry

Decision: extend the existing local-only retry branch in
`talents._execute_generate()` to handle exactly two retryable local failures:

- `IncompleteJSONError` with `reason_code == "incomplete_json_length"`.
- Any exception with `reason_code == "local_capacity_exhausted"`.

Control flow:

1. Make the existing first `generate_with_result()` call unchanged.
2. In the `provider == "local"` exception branch, classify the exception into
   `length_retry` or `capacity_retry`.
3. If neither, re-raise exactly as today.
4. Set `retries = 1`.
5. Build one retry kwargs dict with `inference_retry_index=1`.
6. For length retry, keep the existing temperature floor behavior and do not
   pass `local_exclusive_admission`.
7. For capacity retry, keep the original temperature and pass
   `local_exclusive_admission=True`.
8. Make one second `generate_with_result()` call.
9. If the retry fails, attach `retry_exc.retries = 1` and re-raise.

The local retry branch must not consult `get_backup_provider()` and must not
emit fallback events. Cloud fallback behavior stays in the non-local branch.

## Implementation Sequence

1. Add `LocalCapacityExhausted`, reason-code registration, readiness/chat copy,
   and tests for the registration surfaces.
2. Split `_raise_bundled_status()` by structured JSON body while preserving the
   existing prose-pattern entry gate.
3. Refactor `LocalPermit` to hold multiple files and add exclusive acquisition
   under the existing FIFO wait-ticket protocol.
4. Thread `local_exclusive_admission` through `run_generate()` and
   `run_agenerate()` and into admission.
5. Add the local capacity retry branch in `talents._execute_generate()`.
6. Add and update tests in the order below, keeping sync/async parity green.

## Test Plan

### `tests/test_local.py`

- Update `test_run_generate_bundled_context_rejection_backstop` to use the
  authoritative 400 body with `error.type == "exceed_context_size_error"`,
  `n_prompt_tokens`, and `n_ctx`; expect `ContextBudgetExceeded`. This passed
  on the pre-fix tree too and pins preserved behavior.
- Repoint the alt-phrasing test to the 500 `server_error` body with
  `Context size has been exceeded.`; expect `LocalCapacityExhausted` and
  `local_capacity_exhausted`. Failed on the pre-fix tree because it raised
  `ContextBudgetExceeded`.
- Add a fallback case for a context-pattern body that is missing authoritative
  structure; expect `LocalCapacityExhausted`. Failed on the pre-fix tree for
  the same reason.
- Add async parity for the 500 capacity body through fake `httpx.AsyncClient`;
  expect `LocalCapacityExhausted`. Failed on the pre-fix tree because the
  shared helper misclassified it.
- Add telemetry assertion for bundled generate failure: captured
  `record_local_inference` row has `reason_code == "local_capacity_exhausted"`.
  Failed on the pre-fix tree with `context_budget_exceeded`.
- Keep the no-POST fitter rejection test as-is: `local_budget.fit_contents()`
  still raises `ContextBudgetExceeded` before any HTTP request when preserved
  content cannot fit.

### `tests/test_local_admission.py`

- Exclusive acquisition blocks while a normal single-slot holder is active, then
  succeeds after release. Failed on the pre-fix tree because no exclusive mode
  existed.
- Exclusive acquisition with `capacity == 1` behaves like normal one-slot
  acquisition. Failed on the pre-fix tree because no exclusive mode existed.
- Exclusive timeout releases any partially acquired locks before sleeping or
  timing out. Drive this by holding slot 1, leaving slot 0 free, then attempting
  exclusive acquire; after timeout, a normal waiter must be able to acquire
  slot 0. Failed on the pre-fix tree because no all-slot primitive existed.
- Exclusive permit releases all locks on exception. Acquire exclusive, raise
  inside the context manager, then acquire two normal permits at capacity 2.
  Failed on the pre-fix tree because `LocalPermit` only represented one file.

### `tests/test_talent_fallback.py`

- Add local capacity retry test where both first and retry calls raise
  `LocalCapacityExhausted`; assert exactly two calls, second call has
  `inference_retry_index=1` and `local_exclusive_admission=True`, no fallback is
  consulted, and the emitted error event has `retries == 1`. Failed on the
  pre-fix tree because local non-length errors re-raised without retry.
- Update existing incomplete-JSON retry assertions to prove the retry has
  `inference_retry_index=1` but no `local_exclusive_admission`. This guards
  against inferring exclusivity from retry index.

### Reason-Code Gates

- `tests/test_chat_reasons.py`: add `local_capacity_exhausted` to
  `EXPECTED_CODES` and local runtime rendering expectations.
- `tests/test_provider_state.py`: add it to the `RUNTIME_REASON_CODES` expected
  set.
- `tests/test_provider_readiness_presenter.py`: include
  `LocalCapacityExhausted().reason_code` in local runtime exception
  registration coverage.

## Risks And Open Questions

- The discriminator intentionally depends on llama.cpp `b9291` structure. If a
  future pin changes the structured overflow body, tests should fail and the
  discriminator should be updated with that pin.
- Exclusive retry serializes all bundled local generate work while it runs. The
  retry is capped at one attempt and only follows observed capacity pressure.
- `admission_slot=0` is lossy for all-slot holds, but adding telemetry schema
  now is unnecessary; `retry_index` and `reason_code` identify the path.
- If exclusive retry cannot obtain all slots before the existing timeout, the
  resulting failure remains `local_queue_timeout`, which accurately describes
  pre-admission waiting.
