# Provider Implementation Guide

Guide for implementing new AI providers in the think module.

For a high-level overview of the think module, see [THINK.md](THINK.md).

## Required Exports

Each provider module in `solstone/think/providers/` must export three functions:

| Function | Purpose |
|----------|---------|
| `run_generate()` | Synchronous text generation, returns `GenerateResult` |
| `run_agenerate()` | Asynchronous text generation, returns `GenerateResult` |
| `run_cogitate()` | Tool-calling execution |

See `solstone/think/providers/__init__.py` for the canonical export list and `solstone/think/providers/google.py` as a reference implementation.

Each provider module must also define `__all__` exporting these three functions.

## API Key Handling

API keys are configured in the ``env`` section of ``journal/config/journal.json``. At process startup, ``setup_cli()`` loads these into ``os.environ``. Providers read keys from ``os.environ`` — no ``.env`` files or ``dotenv`` are involved.

**Naming convention:** `{PROVIDER}_API_KEY` (e.g., `GOOGLE_API_KEY`, `OPENAI_API_KEY`)

**Implementation pattern:**
```python
api_key = os.getenv("MYPROVIDER_API_KEY")
if not api_key:
    raise ValueError("MYPROVIDER_API_KEY not found in environment")
```

**Client caching:** Providers typically cache client instances as module-level singletons to enable connection reuse:
```python
_client = None

def _get_client():
    global _client
    if _client is None:
        api_key = os.getenv("MYPROVIDER_API_KEY")
        if not api_key:
            raise ValueError("MYPROVIDER_API_KEY not found in environment")
        _client = MyProviderClient(api_key=api_key)
    return _client
```

**Settings app integration:** Add your provider to `PROVIDER_METADATA` in `solstone/think/providers/__init__.py` with `label` and `env_key` fields. The settings UI dynamically builds provider dropdowns from the registry. Add corresponding API key UI fields in `solstone/apps/settings/workspace.html` for owner configuration.

## run_generate() / run_agenerate()

These functions handle direct LLM text generation. The unified API in `solstone/think/models.py` routes requests to provider-specific implementations and handles token logging and JSON validation centrally.

**Function signature:**
```python
from solstone.think.providers.shared import GenerateResult

def run_generate(
    contents: Union[str, List[Any]],
    model: str,
    temperature: float = 0.3,
    max_output_tokens: int = 8192 * 2,
    system_instruction: Optional[str] = None,
    json_output: bool = False,
    thinking_budget: Optional[int] = None,
    timeout_s: Optional[float] = None,
    **kwargs: Any,
) -> GenerateResult:
```

The `run_agenerate()` function has the same signature but is `async`.

**Return type - GenerateResult:**
```python
class GenerateResult(TypedDict, total=False):
    text: Required[str]           # Response text
    usage: Optional[dict]         # Normalized usage dict
    finish_reason: Optional[str]  # Normalized: "stop", "max_tokens", etc.
    thinking: Optional[list]      # List of thinking block dicts
```

**Parameter details:**

| Parameter | Notes |
|-----------|-------|
| `contents` | String, list of strings, or list with mixed content. For vision-capable providers (currently Google only), can include PIL Image objects. Other providers stringify non-text content. |
| `model` | Already resolved by routing - providers don't need to handle model selection. |
| `max_output_tokens` | Response token limit. Note: Google internally adds `thinking_budget` to this for total budget calculation. |
| `system_instruction` | System prompt. Providers handle this per their API (separate field, prepended message, etc.). |
| `json_output` | Request JSON response. Google uses `response_mime_type`, Anthropic/OpenAI use response format or system instruction. |
| `thinking_budget` | Token budget for reasoning/thinking. Must be `> 0` to enable; `None` or `0` means no thinking. Google and Anthropic use this directly. OpenAI ignores `thinking_budget` — instead, reasoning effort is controlled via model name suffixes (e.g., `"gpt-5.2-high"`). Valid suffixes: `-none`, `-low`, `-medium`, `-high`, `-xhigh`. Without a suffix, `reasoning_effort` is omitted and OpenAI uses the model default. Note: `run_cogitate()` always enables thinking regardless of this parameter. |
| `timeout_s` | Request timeout in seconds. Convert to provider's expected format (e.g., Google uses milliseconds internally). |
| `**kwargs` | Absorb unknown kwargs for forward compatibility. Provider-specific options (e.g., `cached_content` for Google) pass through here. |

**Key responsibilities:**
- Accept the common parameter set shown above
- Return `GenerateResult` with text, usage, finish_reason, and thinking
- Normalize `finish_reason` to standard values: `"stop"`, `"max_tokens"`, `"safety"`, etc.
- Handle provider-specific response parsing

**Note:** Token logging and JSON validation are handled by the wrapper in `solstone/think/models.py`, not by providers.

**Important:** Providers should gracefully ignore unsupported parameters rather than raising errors.

### Structured-output schema preparation

Canonical generation schemas may contain bounds such as `maxItems` and
`maxLength`; `maxItems` reaches local grammar generation, while string
constraints do not. The wrapper in `solstone/think/models.py` prepares a
provider-facing copy with `solstone/think/schema_prep.py` before calling a
provider. `STRICT_UNSUPPORTED_KEYWORDS` is the single support matrix for strict
cloud providers. Local receives a canonical copy from `schema_prep.py`, then the
local provider drops string constraints request-side before llama.cpp grammar
generation while keeping array bounds. Response validation still uses the
canonical schema: `generate()` raises on violations, while
`generate_with_result()` records `schema_validation` instead.

Use `make check-schema-bounds` to run the bounds ratchet for canonical schemas.
Use `make eval-schemas` to run the opt-in local llama.cpp structured-output
eval harness; it requires `journal install-provider local` and a running local
server via `journal start` or `journal service start`.

## run_cogitate()

Handles tool-calling execution.

```python
async def run_cogitate(
    config: Dict[str, Any],
    on_event: Optional[Callable[[dict], None]] = None,
) -> str:
```

**Config dict fields** (see `solstone/think/agents.py` `main_async()` for routing logic):
- `prompt`: User's input (required)
- `model`: Model identifier
- `max_tokens`: Output token limit
- `system_instruction`: System instruction (journal.md for agents)
- `extra_context`: Runtime context (facets, insights list, datetime) as first user message
- `user_instruction`: Agent-specific prompt as second user message
- `tools`: Optional list of allowed tool names
- `use_id`, `name`: Identity for logging and tool calls
- `session_id`: solstone-owned session ID for conversation continuation; Google cogitate history is stored under `journal/.cache/cogitate-history/`
- `chat_id`: Chat ID for reverse lookup from agent to chat

**Event emission:**

Providers must emit events via the `on_event` callback. See `solstone/think/providers/shared.py` for TypedDict definitions:

| Event | When |
|-------|------|
| `StartEvent` | Agent run begins |
| `ToolStartEvent` | Tool invocation starts |
| `ToolEndEvent` | Tool invocation completes |
| `ThinkingEvent` | Reasoning/thinking content available |
| `FinishEvent` | Agent run completes successfully |
| `ErrorEvent` | Error occurs |

Use `JSONEventCallback` from `solstone/think/providers/shared.py` to wrap the callback and auto-add timestamps.

**Finish event format:**

The `finish` event must include the result text and should include usage for token tracking:
```python
callback.emit({
    "event": "finish",
    "result": final_text,
    "usage": usage_dict,  # Same format as token logging
    "ts": int(time.time() * 1000),
})
```

**Error handling pattern:**

All providers must follow this pattern to prevent duplicate error reporting:
```python
try:
    # ... agent logic ...
except Exception as exc:
    callback.emit({
        "event": "error",
        "error": str(exc),
        "trace": traceback.format_exc(),
    })
    setattr(exc, "_evented", True)  # Prevents duplicate reporting
    raise
```

**Tool integration:**

Invoke tools via `sol call <module> <command> [args...]` commands.
Providers should route tool calls through the configured command path and
honor `config["tools"]` allowlists when present.


**Conversation continuation:**

When `session_id` is provided, use the provider's native continuation mechanism
or a solstone-owned history file where the provider has no durable session
handle. The `session_id` is reused for all subsequent continuations within the
same chat.

## Token Logging

Token logging is handled centrally by the wrapper in `solstone/think/models.py`. Providers return usage data in their `GenerateResult`, and the wrapper calls `log_token_usage()`.

**Usage dict format:**

Providers normalize usage into the unified schema defined by `USAGE_KEYS` in `solstone/think/providers/shared.py`. Each provider's `_extract_usage()` is responsible for mapping API-specific field names to these canonical keys. `log_token_usage()` passes through known keys — it does **not** re-normalize.

```python
usage_dict = {
    "input_tokens": 1500,            # Required
    "output_tokens": 500,            # Required
    "total_tokens": 2000,            # Required (computed if missing)
    "cached_tokens": 800,            # Optional: cache hits
    "reasoning_tokens": 200,         # Optional: thinking/reasoning tokens
    "cache_creation_tokens": 100,    # Optional: cache creation cost
    "requests": 1,                   # Optional: request count
}
```

**Key points:**
- Return usage in `GenerateResult["usage"]` - wrapper handles logging
- For `run_cogitate()`, include usage in the `finish` event

## Bundled-local admission and inference telemetry

The `local` provider has one shared admission boundary for the supervisor-owned
Qwen server. It applies only when `providers.local` resolves to the bundled
loopback runtime. A configured OpenAI-compatible endpoint and every cloud
provider bypass this boundary.

Capacity remains explicit and intentionally small:

| Runtime profile | Serving capacity | Evidence |
|---|---:|---|
| Linux floor | 1 | supervisor `ServerTier`; live `/props.total_slots` wins |
| Linux capable (at least 16 GiB tiering VRAM) | 2 | supervisor `ServerTier`; live `/props.total_slots` wins |
| Apple MLX | 1 | conservative explicit fallback because mlx-vlm 0.6.2 does not advertise a slot limit |

The provider memoizes the capacity once per process. It first reads live
`/props.total_slots`, then the persisted `health/local.ctx` launch tier, then
falls back to one. The supervisor remains the configuration owner: changing a
Linux tier's `parallel_slots` changes both `llama-server --parallel` and provider
admission after the journal processes restart. Apple stays at one until that
runtime exposes a stable capacity contract and a separate measurement justifies
raising it.

Admission uses one `flock` file per slot under
`health/local-inference-admission/`. This coordinates independent journal
processes without a scheduler service or in-memory queue. Waiting async calls
are cancellation-safe; exceptions and cancellation release acquired locks;
process exit releases kernel locks. Queue time consumes the caller's existing
provider deadline, so waiting cannot silently extend a request beyond its
configured timeout. Cogitate holds one permit for its run because the OpenHands
SDK owns its internal multi-turn HTTP calls; this is conservative and avoids an
uncontrolled second path to the same server.

Every bundled-local attempt appends a content-free JSON record to
`health/local-inference/YYYYMMDD.jsonl`. These files follow the configured
`retention.journal_logs.days` policy. Records contain:

- request id, timestamp, generate/cogitate kind, provider, logical model, and
  runtime profile;
- serving capacity, evidence source, admission slot, and client queue wait;
- client wall time plus server prompt-evaluation, generation, and total timing
  when the response exposes them;
- prompt/generated token counts, reused prompt tokens, prompt-cache cold/warm
  state, and selected server slot when exposed;
- retry index, finish reason, outcome, timeout/cancellation flags, and a safe
  reason code on failure.

Records never contain prompt text, generated text, messages, schemas, images,
endpoint URLs, or credentials. A successful `GenerateResult` also carries the
same record as `inference`; callers that already retain the full result can use
it without rereading the log. Fields unavailable from a runtime remain null or
`unknown` rather than being inferred.

Run the synthetic journal-shaped benchmark against an isolated server:

```bash
python scripts/benchmark_local_inference_admission.py \
  --endpoint http://127.0.0.1:8080 --slots 2 --concurrency 10 \
  --requests 30 --mode baseline
python scripts/benchmark_local_inference_admission.py \
  --endpoint http://127.0.0.1:8080 --slots 2 --concurrency 10 \
  --requests 30 --mode admitted
```

It reports throughput, latency P50/P95/P99, explicit queue wait, residual opaque
wait, failures, and NVIDIA peak memory/utilization when `nvidia-smi` is present.
The payloads are fixed synthetic text/JSON requests and never read a journal.

The implementation gate on `fedora.local` used a fresh b9957 server with two
slots, ten producers, and twelve mixed requests for each side. Baseline versus
admitted results were 0.1291 versus 0.1296 requests/s, P95 latency 78.71 versus
77.22 seconds, P99 80.28 versus 77.30 seconds, and identical 4,652 MiB peak GPU
memory. Most importantly, P95 wait hidden inside the server fell from 65.56
seconds to 1.22 seconds; the admitted run reported 62.38 seconds as explicit
client queue wait. The boundary preserved throughput, slightly improved the
tail, and made the backlog observable without changing model work.

Rollback is one code revert plus a journal-process restart. The lock files hold
no state and may remain on disk; removing the provider calls to
`acquire_local_slot*()` immediately restores server-side queueing. Telemetry
JSONL files are ordinary operational logs and can remain until retention prunes
them.

## Context & Routing

Context strings determine provider and model selection. Providers receive already-resolved models, but understanding the system helps:

**Context naming convention:**
- Talent configs (agents/generators): `talent.{source}.{name}` where source is `system` or app name
  - System: `talent.system.meetings`, `talent.system.default`
  - App: `talent.entities.observer`, `talent.chat.helper`
- Other contexts: `{module}.{feature}[.{operation}]`
  - Examples: `observe.describe.frame`, `app.chat.title`

**Dynamic discovery:** All context metadata (tier/label/group) is defined in prompt .md files via YAML frontmatter:
- Prompt files: Listed in `PROMPT_PATHS` in `solstone/think/models.py` - add `context`, `tier`, `label`, `group` fields
- Categories: `solstone/observe/categories/*.md` - add `tier`, `label`, `group` fields
- System talent: `solstone/talent/*.md` - add `tier`, `label`, `group` fields in frontmatter
- App talent: `solstone/apps/*/talent/*.md` - add `tier`, `label`, `group` fields in frontmatter

All contexts are discovered at runtime. Use `get_context_registry()` to get the complete context map.

**Resolution** (handled by `solstone/think/models.py` `resolve_provider(context, agent_type)`):
1. Exact match in journal.json `providers.contexts`
2. Glob pattern match (fnmatch) with specificity ranking
3. Dynamic context registry (discovered prompts, categories, talent configs)
4. Type-specific default (from `providers.generate` or `providers.cogitate`)
5. System defaults from `TYPE_DEFAULTS`

Providers don't implement routing - they receive the resolved model.

## Configuration

Provider configuration lives in `journal.json` under the `providers` key.

**Structure:**
```
providers:
  generate:
    provider: <provider-name>
    tier: <1|2|3>
    backup: <provider-name>
  cogitate:
    provider: <provider-name>
    tier: <1|2|3>
    backup: <provider-name>
  contexts:
    <context-pattern>:
      provider: <provider-name>
      model: <explicit-model>  # OR
      tier: <1|2|3>            # tier-based resolution
  models:
    <provider-name>:
      "<tier>": "<model-override>"
```

The `generate` section controls text generation (analysis, extraction, transcription).
The `cogitate` section controls tool-calling agents (interactive chat, daily briefings).
Each section has its own provider, tier, and backup provider.

**Tier system:**
- 1 = PRO (most capable)
- 2 = FLASH (balanced)
- 3 = LITE (fast/cheap)

See `tests/fixtures/journal/config/journal.json` for a complete example and `solstone/think/models.py` `PROVIDER_DEFAULTS` for tier-to-model mappings.

## Testing

**Required test coverage:**

**Unit tests** in `tests/test_<provider>.py`:
- Mock API responses
- Test parameter handling
- Test error cases

See existing test files for patterns:
- `tests/test_google.py`, `tests/test_openai.py`, `tests/test_anthropic.py`

## Batch Processing

The `Batch` class in `solstone/think/batch.py` automatically works with all providers via the unified `agenerate()` API in `solstone/think/models.py`. No provider-specific batch implementation is needed - just ensure your `run_agenerate()` works correctly.

## OpenAI-Compatible Providers

For providers with OpenAI-compatible APIs (e.g., DigitalOcean, Azure OpenAI, local LLMs), you can leverage the OpenAI SDK with a custom base URL:

```python
from openai import OpenAI

client = OpenAI(
    api_key=os.getenv("MYPROVIDER_API_KEY"),
    base_url="https://api.myprovider.com/v1",
)
```

This allows reusing much of the OpenAI provider's patterns for request/response handling.

The bundled local provider (`solstone/think/providers/local.py`) is OpenAI-
compatible over a loopback llama-server runtime, while still requiring no API
key.

## Local (On-device) Provider

The ``local`` provider installs pinned llama.cpp ``llama-server`` and GGUF
artifacts on demand, then serves requests on ``127.0.0.1`` through the
OpenAI-compatible ``/v1`` surface. Key differences from cloud providers:

- **No API key required.** ``validate_key()`` performs a loopback health check
  through a tiny local generation request.
- **Bundled runtime.** Settings installs the pinned llama-server binary plus the
  selected GGUF model under the journal cache. The current local model is the
  vision-capable unified VLM ``local/qwen3.5-4b`` from
  ``unsloth/Qwen3.5-4B-GGUF``: ``Qwen3.5-4B-Q4_K_M.gguf`` (2740937888 bytes,
  8 GiB minimum RAM) with ``mmproj-F16.gguf``. v1 ships macOS arm64 Metal and
  Linux Vulkan slices plus a CUDA OCI image. CUDA image pulls verify the pinned
  image signature before any blob download. The Vulkan slice is vendor-neutral:
  AMD, NVIDIA, and Intel hardware GPUs can work when they expose a real Vulkan
  device. CPU or software Vulkan devices such as llvmpipe, lavapipe, and
  SwiftShader are rejected, with no CPU fallback.
- **Install fit checks.** Installers render a platform/RAM/disk/GPU fit report
  before first download unless artifacts are already installed. Local RAM
  shortfalls warn but do not block; unsupported platform and insufficient disk
  for known-size artifacts block before download.
- **Linux GPU override:** operators can set
  ``providers.bundled.local.vulkan_device_index`` to a raw Vulkan physical-device
  index when auto-selection chooses the wrong GPU. The override is still gated:
  absent, CPU, virtual, software, or out-of-range indices fail readiness.
- **Missing GPU recovery:** local setup or retry does not fix a missing hardware
  GPU. Choose a configured cloud provider instead.
- **Model prefix convention:** Models use the ``local/`` prefix
  (for example, ``local/qwen3.5-4b``).
- **Cogitate through OpenHands.** Cogitate uses the OpenHands + LiteLLM facade
  with ``base_url=http://127.0.0.1:<port>/v1`` and ``api_key=EMPTY``. Generate
  uses the provider's direct loopback client. Both paths send
  ``chat_template_kwargs.enable_thinking=false`` to llama-server.
- **No cloud fallback.** If the local runtime, model files, RAM gate, or
  loopback server are not ready, the local provider surfaces that recovery
  reason instead of silently falling back to a cloud provider.

## MLX (Local, Apple Silicon) Provider

The ``mlx`` provider (`solstone/think/providers/mlx.py`) runs vision/generate
on-device on Apple Silicon via the MLX framework — used for the screen-analysis
path with nothing sent to a cloud provider. It surfaces in Settings → Providers
as **"MLX (Local, Apple Silicon)"**.

- **Generate-only, no cogitate.** ``run_generate()`` / ``run_agenerate()`` are
  implemented; ``run_cogitate()`` raises — MLX is vision/generate-only in v1.
  Configure a cloud provider for cogitate (tool-using) agents.
- **No API key.** ``env_key`` is empty and ``validate_key()`` always returns
  ``{"valid": True}`` — availability is gated on platform + RAM, not a secret.
- **Availability gating.** ``is_mlx_available()`` requires Apple Silicon plus the
  ``mlx``/``mlx-vlm`` packages. MLX installs also run the shared fit report and
  block before download when platform, package, RAM, or known-size disk checks
  fail.
- **Model registry (`_MLX_MODEL_REGISTRY`).** Pinned by repo + revision:
  ``qwen3.5:9b`` (`mlx-community/Qwen3.5-9B-MLX-8bit`, ≥16 GB) and
  ``gemma-4-26b-a4b-it-mlx-4bit`` (`mlx-community/gemma-4-26b-a4b-it-4bit`, ≥24 GB,
  with a ``post_load`` hook that constrains the Gemma 4 vision tower to the
  screenshot-faithful patch budget).
- **On-demand snapshot.** The pinned snapshot downloads in the background on first
  enable; a missing snapshot raises ``ModelSnapshotMissingError`` rather than
  silently degrading. Loaded models are cached at module level.

## Checklist for New Providers

**Core implementation:**
1. Create `solstone/think/providers/<name>.py` with `__all__ = ["run_generate", "run_agenerate", "run_cogitate"]`
2. Implement `run_generate()`, `run_agenerate()`, `run_cogitate()` following signatures above
3. Import `GenerateResult` from `think.providers.shared` and return it from generate functions

**Model constants** in `solstone/think/models.py`:
4. Add model constants using the pattern `{PROVIDER}_{TIER}` (e.g., `DO_LLAMA_70B`, `DO_MISTRAL_NEMO`)
   - Existing examples: `GEMINI_FLASH`, `GPT_5`, `CLAUDE_SONNET_4`
5. Add provider tier mappings to `PROVIDER_DEFAULTS` dict
6. Update `get_model_provider()` to detect your models by prefix (critical for cost tracking)

**Registry:**
7. Add provider to `PROVIDER_REGISTRY` in `solstone/think/providers/__init__.py`
8. Add routing case in `solstone/think/agents.py` `main_async()` (around line 331)

**Settings UI:**
9. Add provider to `PROVIDER_METADATA` in `solstone/think/providers/__init__.py` with `label` and `env_key`
10. Add API key UI field in `solstone/apps/settings/workspace.html`

**Testing:**
11. Create unit tests in `tests/test_<name>.py`
12. Add test contexts to `tests/fixtures/journal/config/journal.json`

**Documentation:**
14. Update `solstone/think/providers/__init__.py` docstring
15. Update `docs/THINK.md` providers table
16. Update `docs/CORTEX.md` valid provider values
