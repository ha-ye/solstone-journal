# Provider Architecture

This guide describes the provider behavior that ships today. The core code paths
are `solstone/think/models.py`, `solstone/think/talents.py`,
`solstone/think/providers/__init__.py`, `solstone/think/providers/openhands.py`,
and `solstone/think/providers/local.py`.

For the broader think pipeline, see `docs/THINK.md`.

## Active Brain Resolution

Solstone runs one active provider/model pair per interface:

- `generate` for single-shot model calls and generators.
- `cogitate` for tool-using OpenHands runs.

`solstone/think/models.py` owns active-brain resolution. `resolve_provider()`
checks `providers.<interface>.provider` first, then managed cloud-key presence in
the grandfathered `google` -> `anthropic` -> `openai` order, then local runtime
readiness, then the no-brain state. `providers.<interface>.model` can pin the
model for that interface. Without a model pin, each provider has one default
model in `DEFAULT_MODEL_BY_PROVIDER`.

`resolve_provider()` intentionally ignores retired routing keys: `tier`,
`backup`, and `providers.models`. `providers.contexts` no longer steers
provider/model routing either. Its `disabled` and `extract` fields are still live
talent metadata because `solstone/think/talent.py` merges exactly those fields
from `providers.contexts.<context>`.

## Provider Registry

`solstone/think/providers/__init__.py` is the registry. Cloud provider names
`google`, `openai`, and `anthropic` all resolve to the OpenHands facade module,
`solstone.think.providers.openhands`; `local` resolves to
`solstone.think.providers.local`.

The effective provider modules expose the interface that `models.py` and
`talents.py` call:

- `run_generate()` returns a `GenerateResult`.
- `run_agenerate()` returns a `GenerateResult` from async callers.
- `run_cogitate()` runs a tool-using conversation and emits events.

The cloud vendor leaf modules `solstone/think/providers/google.py`,
`solstone/think/providers/openai.py`, and
`solstone/think/providers/anthropic.py` implement generate/agenerate only. They
are not cogitate providers. The OpenHands facade owns cloud cogitate.

## Generate Dispatch

`solstone/think/models.py` resolves the active generate brain, prepares the
provider-facing schema, calls `provider_mod.run_generate()` or
`provider_mod.run_agenerate()`, and logs usage centrally.

For cloud generate, `provider_mod` is the OpenHands facade. The facade keeps
`run_generate()` and `run_agenerate()` as redispatchers: at call time it imports
the vendor leaf from `_GENERATE_MODULES` in
`solstone/think/providers/openhands.py` and calls the matching leaf function.
This keeps vendor-specific transport behavior in the leaf modules while leaving
the registry stable.

The native cloud leaves remain intentionally thin transport adapters. Solstone
keeps them because the OpenAI-compatible surfaces do not provide full parity for
the behavior the generate path needs, including strict/constrained schema support
and native-provider response details. Anthropic, OpenAI, and Google each keep
their provider-specific generate implementation in their own module.

For local generate, `solstone/think/providers/local.py` owns both bundled-local
and configured endpoint traffic. Bundled local posts to the supervisor-owned
loopback llama-server. A configured local endpoint posts to the owner-supplied
OpenAI-compatible URL resolved by `solstone/think/providers/local_endpoint.py`.

## Cogitate Dispatch

`solstone/think/talents.py` runs cogitate talents through
`_execute_with_tools()`. It resolves the provider module through
`solstone/think/providers/__init__.py` and calls `run_cogitate()`.

For cloud cogitate, `solstone/think/providers/openhands.py` builds and runs the
OpenHands conversation. For local cogitate,
`solstone/think/providers/local.py` performs local readiness/admission work and
then delegates to `openhands.run_cogitate()` with a local OpenAI-compatible
configuration. This is why local cogitate failures are classified in
`local.py`, not by a separate local tool-calling engine.

## Local Lanes

The owner-facing provider key is `local`. It has three runtime lanes:

- Bundled local: `solstone/think/providers/local_server.py` manages the
  loopback llama-server process and `solstone/think/providers/local.py` sends
  OpenAI-compatible requests to it.
- BYO local endpoint: `solstone/think/providers/local_endpoint.py` activates the
  endpoint only when both `providers.local.endpoint_url` and
  `providers.local.served_model_id` are present. Optional
  `providers.local.credential` supplies the bearer token. Optional
  `providers.local.parallel_slots` governs non-confidential BYO admission.
- Confidential endpoint: `solstone/think/services/spp_transport.py` gates
  confidential egress and `solstone/think/providers/local.py` uses that transport
  before provider dispatch.

Bundled local, BYO URL, and confidential local traffic all use an
OpenAI-compatible request shape. The difference is where the request is sent and
which readiness, admission, and attestation gates run first.

## Local Admission and Capacity

The `local` provider has one shared admission boundary for governed local lanes:
the supervisor-owned Qwen server and non-confidential OpenAI-compatible endpoint
overrides. The confidential-processing lane (`services.confidential` present)
and every cloud provider bypass this boundary. Bundled-local inference telemetry
remains bundled-only.

Capacity remains explicit and intentionally small:

| Runtime profile | Serving capacity | Evidence |
|---|---:|---|
| Linux floor | 1 | supervisor `ServerTier`; live `/props.total_slots` wins |
| Linux capable (at least 16 GiB tiering VRAM) | 2 | supervisor `ServerTier`; live `/props.total_slots` wins |
| Apple mlx-vlm local backend | 1 | conservative explicit default; mlx-vlm on darwin exposes neither `/props` nor `ServerTier` (`solstone/think/providers/local_server.py:185`) |
| Non-confidential BYO endpoint | configured | journal config `providers.local.parallel_slots`; resolved by `_configured_byo_parallel_slots()` (`solstone/think/providers/local_endpoint.py:68`) after `resolve_local_endpoint()` selects BYO (`solstone/think/providers/local_endpoint.py:84`) |

For bundled local, the provider memoizes capacity once per process. It first
reads live `/props.total_slots`, then the persisted `health/local.ctx` launch
profile, then uses one slot when neither source is available. The supervisor
remains the configuration owner:
changing a Linux profile's `parallel_slots` changes both
`llama-server --parallel` and provider admission after journal processes restart.
Apple stays at one until that runtime exposes a stable capacity contract and a
separate measurement justifies raising it.

Admission uses one `flock` file per slot under
`health/local-inference-admission/`. This coordinates independent journal
processes without a scheduler service or in-memory queue. Waiting async calls
are cancellation-safe; exceptions and cancellation release acquired locks;
process exit releases kernel locks. Queue time consumes the caller's existing
provider deadline, so waiting cannot silently extend a request beyond its
configured timeout. Cogitate holds one parent permit across model turns, but
temporarily yields that permit while the OpenHands `sol` tool runs a nested
`sol` child process. The parent reacquires through the same FIFO admission pool
before any further model request; failure to reacquire is a terminal
`local_queue_timeout`.

Every bundled-local attempt appends a content-free JSON record to
`health/local-inference/YYYYMMDD.jsonl`. These files follow the configured
`retention.journal_logs.days` policy. Records contain request id, timestamp,
kind, provider, logical model, runtime profile, serving capacity, evidence
source, admission slot, client queue wait, timing, token counts, retry index,
finish reason, outcome, timeout/cancellation flags, and a safe reason code on
failure. Records never contain prompt text, generated text, messages, schemas,
images, endpoint URLs, or credentials.

## Honest Failure Semantics

Provider failure is not a routing signal. Solstone does not silently switch to
another provider when the active brain fails.

- Quota failures are recorded by
  `solstone/think/providers/state.py::record_quota_failure()` in
  `health/talents.json` under the active journal root with provider, model, interface,
  `provider_quota_exceeded`, and `reset_at_ms`.
- Local endpoint reachability and contract failures are classified by
  `solstone/think/providers/local_endpoint.py` and
  `solstone/think/providers/local.py`.
- Local retry is deliberately narrow in `solstone/think/talents.py`: generate
  retries once only for `incomplete_json_length` or
  `local_capacity_exhausted`, and it retries the same local provider.
- Segment deferral is represented in health JSONL, not by provider switching.
  `solstone/think/pipeline_health.py` folds segment progress, and
  `solstone/think/thinking.py` selects sensed-but-not-fully-thought segments for
  repair.

If the local runtime, model files, RAM gate, loopback server, BYO endpoint, or
confidential attestation is not ready, Solstone surfaces that recovery reason
instead of falling back to a cloud provider.

## Live Configuration Keys

Provider configuration lives in `config/journal.json` under the active journal
root; the canonical reader/writer is `solstone/think/journal_config.py`.

Live routing keys:

- `providers.generate.provider`
- `providers.generate.model`
- `providers.cogitate.provider`
- `providers.cogitate.model`

Live local endpoint keys:

- `providers.local.endpoint_url`
- `providers.local.served_model_id`
- `providers.local.credential`
- `providers.local.parallel_slots`

Live Google backend keys:

- `providers.google_backend`, read by Google provider code for Gemini Developer
  API versus Vertex behavior.
- Vertex/ADC credential settings used by `solstone/apps/thinking/routes.py` and
  `solstone/apps/thinking/vertex_credentials.py`.

Live managed key storage:

- `env.GOOGLE_API_KEY`
- `env.ANTHROPIC_API_KEY`
- `env.OPENAI_API_KEY`

Retired for provider/model routing:

- `tier`
- `backup`
- `providers.models`
- `providers.contexts.<context>.provider`
- `providers.contexts.<context>.model`

`providers.contexts.<context>.disabled` and
`providers.contexts.<context>.extract` remain live talent metadata. Do not remove
those fields as if the whole `providers.contexts` block were inert.

## Adding or Changing Providers

Provider changes should start from the current registry and active-brain model,
not from the retired tier/backup system:

1. Update `solstone/think/providers/__init__.py` for provider identity and
   metadata.
2. Implement the effective module surface that the registry points to.
3. If the provider is cloud generate-only, keep cogitate on the OpenHands facade
   and add a vendor leaf only for generate/agenerate.
4. Add one default model in `solstone/think/models.py`.
5. Add focused provider tests and lane-honesty tests under `tests/`.
6. Update `docs/THINK.md`, `docs/CORTEX.md`, and this file.
