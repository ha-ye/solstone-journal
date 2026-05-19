# MLX provider

## D1. Provider registry key

Decision: use `mlx`.

Justification: this is a single lowercase provider token and matches the existing registry style: `google`, `openai`, `anthropic`, and `ollama`.

Implementation note: register it only as a provider key; do not introduce alternate aliases.

## D2. Module path

Decision: implement the provider in `solstone/think/providers/mlx.py`.

Justification: every first-class provider has one module under `solstone/think/providers/`, and the registry points directly to that module path.

Implementation note: the module must expose the same public provider functions as the existing provider modules.

## D3. `build_provider_status` branch

Decision: do not add a `build_provider_status` branch for `mlx` in this lode.

Justification: the default branch in `solstone/think/providers/__init__.py` marks providers with an empty `env_key` as `configured=False` and `generate_ready=False`. This under-reports readiness on healthy Apple hosts, but it is quiet and not visibly broken. The Settings UI and status lode is the explicit next lode and owns accurate platform/package readiness.

Implementation note: `PROVIDER_METADATA["mlx"]["env_key"]` should be `""`. Avoid adding platform checks or package checks to `build_provider_status` here.

## D4. `get_model_provider` branch for bare `qwen3.5:9b`

Decision: add a specific `get_model_provider` branch for the bare Qwen 3.5 9B model identifier.

Justification: returning `unknown` is not equivalent to zero cost. `calc_token_cost` returns `None` for unknown providers, and the tokens app skips unknown providers entirely. MLX usage should appear in token summaries as a local `$0` provider line item, not disappear.

Implementation note: add the exact-match branch before prefix matches, because it is more specific. Extend the existing Ollama zero-cost short-circuit in `calc_token_cost` so `mlx` also returns zero-cost data.

## D5. Schema validation placement

Decision: keep schema validation central in `solstone/think/models.py`.

Justification: `generate_with_result` already calls `_validate_schema` and populates `result["schema_validation"]`. Provider-level schema validation would create a parallel truth source and diverge from the Gemini pattern.

Implementation note: the MLX provider may use constrained decoding with `build_json_schema_logits_processor` to improve output shape, but it should still return text verbatim and let central validation surface the final advisory result.

## D6. `run_cogitate` exception type

Decision: raise `RuntimeError` from MLX `run_cogitate`.

Justification: this matches Ollama's runtime dependency failure pattern and describes an environment/capability constraint, not a programming error. A named subclass is unnecessary for one unsupported provider method.

Implementation note: the message must contain the substrings `vision` and `v1`.

## D7. `mlx-vlm` cache shape

Decision: cache `(model, processor, config)` at module scope, where `config = model.config`.

Justification: `mlx_vlm.load` returns `(model, processor)`, not a 3-tuple, while `apply_chat_template` requires a separate config argument. A module-level single-process cache avoids repeated model loads without adding eviction complexity.

Implementation note: cache for the process lifetime only. Do not copy or serialize model objects.

## D8. Chat-template and image placement

Decision: let `mlx_vlm.apply_chat_template` construct Qwen 3.5 image placement.

Justification: for Qwen 3.5, `mlx-vlm` maps the model to `MessageFormat.LIST_WITH_IMAGE_FIRST`, producing image content items before text for the user message. Reconstructing that logic locally would duplicate upstream behavior.

Implementation note: walk `contents`, collect `PIL.Image` instances by reference, concatenate text parts with blank lines, and build either messages or an inline system-prefixed prompt depending on the verified `apply_chat_template` signature. Call `apply_chat_template` with `num_images=len(images)`, `enable_thinking=False`, and `add_generation_prompt=True`, then pass the templated prompt plus the original image objects to `mlx_vlm.generate`.

## D9. `temperature`, `max_output_tokens`, and `thinking_budget`

Decision: pass `temperature` as `temperature`, pass `max_output_tokens` as `max_tokens`, and ignore `thinking_budget`.

Justification: `mlx-vlm` uses `max_tokens` for output length, while the Solstone provider interface uses `max_output_tokens`. This provider is vision/generate-only and should not emit thinking blocks.

Implementation note: hard-code `enable_thinking=False` in chat-template calls. Silently ignore `thinking_budget`.

## D10. `finish_reason` derivation

Decision: derive `finish_reason` only from fields exposed by `mlx_vlm.GenerationResult`.

Justification: the prep research confirmed the result object has token statistics, but did not pin a stop-reason field. Fabricating `"stop"` would overstate what the SDK reported.

Implementation note: if the result exposes a stop reason or an unambiguous token-count-vs-limit signal, normalize to `"stop"` or `"max_tokens"`. If nothing is exposed, return `None` and add a one-line comment explaining that MLX does not expose a finish reason.

## D11. `usage` derivation

Decision: populate normalized usage only when `GenerationResult` exposes token counts.

Justification: Solstone providers return normalized usage using `input_tokens`, `output_tokens`, and `total_tokens`. `mlx-vlm.GenerationResult` likely exposes `prompt_tokens`, `generation_tokens`, and `total_tokens`, which map directly.

Implementation note: if token fields are absent, return `usage=None`. Do not estimate token counts from text.

## D12. Model identifier constants

Decision: add `QWEN_35_9B = "qwen3.5:9b"` plus `MLX_PRO`, `MLX_FLASH`, and `MLX_LITE`, all pointing to that model.

Justification: this mirrors the existing tier constant pattern while preserving a single actual MLX model for now. The tier indirection stays useful for settings and provider checks even before MLX has differentiated tiers.

Implementation note: place these near the existing Ollama constants in `solstone/think/models.py`.

## D13. HuggingFace pinned snapshot

Decision: pin the MLX model to `mlx-community/Qwen3.5-9B-MLX-8bit` at revision `84f7c2deea248d8df56240f88102def51c7ed5d6`.

Justification: a pinned revision makes model bootstrap and runtime behavior reproducible. `mlx_vlm.load` accepts `revision`, so the provider can request the exact snapshot.

Implementation note: define `MLX_MODEL_REPO` and `MLX_MODEL_REVISION` in the provider module and pass `revision=MLX_MODEL_REVISION` during lazy load.

## D14. `ModelSnapshotMissingError`

Decision: define `ModelSnapshotMissingError` as a named subclass of `RuntimeError`.

Justification: the bootstrap UI needs to distinguish a missing local model snapshot from other load failures. All other load errors should pass through unchanged so real bugs are not hidden.

Implementation note: export the class in `__all__`. Its string form must contain the exact lowercase sentinel `model snapshot not present`. Catch HuggingFace `LocalEntryNotFoundError` and a narrow `OSError` shape that clearly means the snapshot is not cached; do not broad-except around model loading.

## D15. `is_mlx_available()`

Decision: add `is_mlx_available() -> tuple[bool, str]` with four ordered failure reasons.

Justification: MLX support depends on host platform, CPU architecture, memory, and package availability. Returning a boolean plus reason gives future UI code a stable diagnostic surface without importing backend packages at module import time.

Implementation note: check in this order, first failure wins: `not running on macOS`, `not running on Apple Silicon`, `insufficient RAM (need 16 GB, have {n} GB)`, and `mlx-vlm package not installed`. Happy path is `(True, "")`.

## D16. Module-level imports

Decision: keep module-level imports to stdlib, project imports, and `psutil`.

Justification: non-Apple hosts must be able to import the provider module without installing Apple-only backend packages. This also keeps provider registration safe on every platform.

Implementation note: import `mlx_vlm` and `huggingface_hub` only inside functions that need them.

## D17. `__all__`

Decision: export the provider public surface and bootstrap-facing constants/errors.

Justification: other provider modules expose their expected public functions through `__all__`, and the bootstrap lode will need access to MLX availability and snapshot details.

Implementation note: include at minimum `run_generate`, `run_agenerate`, `run_cogitate`, `list_models`, `validate_key`, `is_mlx_available`, `ModelSnapshotMissingError`, `MLX_MODEL_REPO`, `MLX_MODEL_REVISION`, and `QWEN_35_9B` if re-imported from `models.py`.

## D18. `list_models` and `validate_key`

Decision: `list_models()` returns `[QWEN_35_9B]`, and `validate_key(_key)` reports success without requiring a key.

Justification: MLX is a local provider with one pinned model and no API key. This should match registry expectations while avoiding fake credential semantics.

Implementation note: follow the existing provider return shapes. Other providers return dicts from `validate_key`, so MLX should use the same shape rather than a bare boolean.

## D19. `run_agenerate`

Decision: implement `run_agenerate` by delegating to `run_generate` with `asyncio.to_thread`.

Justification: `mlx-vlm` generation is synchronous. Running it in a thread gives async callers the required provider interface without pretending there is a native async SDK path.

Implementation note: add a one-line comment at the delegation site.

## D20. `get_backup_provider` branch

Decision: for `agent_type == "generate"` and primary provider `mlx`, return `None` regardless of configured backup.

Justification: MLX is intended as a deliberate local generate choice; automatic generate fallback would hide missing model/bootstrap issues and make local-vs-cloud behavior harder to reason about. Cogitate fallback behavior should remain unchanged.

Implementation note: place the branch after resolving `primary_provider` and `backup`, before the existing same-primary check.

## D21. `pyproject.toml`

Decision: add `mlx-vlm` and `mlx` as top-level dependencies with Darwin arm64 PEP 508 markers.

Justification: these are provider runtime dependencies, not optional extras, but non-Apple platforms must skip installation entirely.

Implementation note: add them near existing AI provider dependencies. Use version ranges `mlx-vlm>=0.5.0,<1` and `mlx>=0.31.2,<1`, both gated by `sys_platform == 'darwin' and platform_machine == 'arm64'`.

## D22. `run_cogitate` signature

Decision: match the existing provider `run_cogitate(config, on_event=None)` signature and raise immediately.

Justification: `solstone/think/talents.py` calls providers through the shared `run_cogitate` interface. MLX does not support tool-using cogitate agents in this lode.

Implementation note: raise `RuntimeError` with the message: `MLX provider does not support cogitate in v1 — it is vision/generate-only. Configure a cloud provider for cogitate agents.` The required substrings are `vision` and `v1`.

## D23. Test file location

Decision: put MLX provider tests in `tests/test_providers_mlx.py` and extend `tests/test_talent_fallback.py` for the backup regression.

Justification: this mirrors the focused provider test file pattern used by Google, while keeping `get_backup_provider` coverage beside the existing fallback cases.

Implementation note: provider tests should cover module import without `mlx_vlm`, availability reasons, lazy-load cache shape, snapshot-missing translation, generate result shape, schema logits processor wiring, async delegation, unsupported cogitate, list/validate shapes, and model/provider token-cost visibility.
