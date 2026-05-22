# Local provider

## D1 Provider identity and registry

Decision: use the literal provider key `local`, registered as `PROVIDER_REGISTRY["local"] = "solstone.think.providers.local"`. Replace the current `ollama` provider entry; do not keep an alias. Use `PROVIDER_METADATA["local"] = {"label": "Local (on-device)", "env_key": "", "cogitate_runtime": "openhands"}` with no `cogitate_cli` or install-command metadata.

Justification: `local` is the owner-facing backend identity. The old `ollama` implementation depended on an external daemon and an OpenCode CLI; the new provider is a bundled llama-server loopback provider and should be selected through the same provider registry without compatibility shims.

Implementation note: update `solstone/think/providers/__init__.py`. `build_provider_status` must remove the Ollama `/api/version` probe and add a `local` branch whose readiness is true only when the pinned llama-server binary is installed, the selected GGUF is present and sha256-verified, and the loopback daemon is healthy. Report distinct issue codes for `binary_missing`, `model_missing`, `server_unhealthy`, and `ram_insufficient`.

## D2 OpenHands facade branch

Decision: add a `local` branch to `solstone/think/providers/openhands.py::_build_llm`. The exact kwargs are `model=f"openai/{local_model_id}"`, `base_url=f"http://127.0.0.1:{port}/v1"`, `api_key="EMPTY"`, `native_tool_calling=False`, `input_cost_per_token=0`, and `chat_template_kwargs={"enable_thinking": False}`.

Justification: llama-server exposes an OpenAI-compatible loopback API, but it does not provide native OpenHands tool calling or cloud API-key semantics. LiteLLM should treat it as an OpenAI-compatible custom endpoint with zero cost.

Implementation note: obtain `port` by calling `solstone.think.providers.local_server.ensure_running(local_model_id)` before constructing the `LLM`. Do not add `local` to `_GENERATE_MODULES` or `_API_KEY_ENV`; `local` generate is owned by `solstone.think.providers.local`, while `local` cogitate delegates through `openhands.run_cogitate`. Add `"local"` to `_KNOWN_MODEL_PREFIXES` so `_prefixed_model` can strip local-prefixed ids when a caller hands one to cloud code, but make the `local` branch return before `_MODEL_PREFIXES` lookup. `local.py::run_cogitate(config, on_event=None)` should import and call `openhands.run_cogitate(config, on_event)` after daemon readiness is established.

## D3 Model-id scheme

Decision: replace `ollama-local/` with `local/`. Ship these model ids: `local/qwen2.5-coder-7b` and `local/qwen3-coder-30b-a3b-q4_k_m`.

Justification: the provider prefix must match the new provider identity and should not preserve an Ollama-shaped namespace. Model ids stay stable across binary releases and map to pinned GGUF artifacts.

Implementation note: in `solstone/think/models.py`, add `get_model_provider()` handling for `local/`, add `local` to the zero-cost provider set in `calc_token_cost`, and replace `PROVIDER_DEFAULTS["ollama"]` with `PROVIDER_DEFAULTS["local"]`. Define model specs with fields `model_id`, `repo`, `filename`, `revision`, `sha256`, `size_bytes`, and `min_ram_bytes`. The 7B spec is `Qwen/Qwen2.5-Coder-7B-Instruct-GGUF`, `qwen2.5-coder-7b-instruct-q4_k_m.gguf`, sha256 `509287f78cb4d4cf6b3843734733b914b2c158e43e22a7f4bf5e963800894d3c`, size `4683073536`. The 30B spec is `giladgd/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M-GGUF`, `qwen3-coder-30b-a3b-instruct-q4_k_m.gguf`, sha256 `ab4fc2b27b2043483a9e346c802809dfbe9b775efbeea7ca74dc2fd1aa4a0f71`, size `18556688704`.

## D4 First-slice GGUF default

Decision: ship `Qwen2.5-Coder-7B-Instruct Q4_K_M` as the practical default. Set `LOCAL_LITE = "local/qwen2.5-coder-7b"`, `LOCAL_FLASH = "local/qwen2.5-coder-7b"`, and `LOCAL_PRO = "local/qwen3-coder-30b-a3b-q4_k_m"`.

Justification: the 30B GGUF is about 18.6 GB before runtime overhead and is not a safe default for 16 GB hosts. The 7B GGUF is about 4.68 GB and fits the macOS arm64 and Linux CPU first slice with a reasonable RAM gate.

Implementation note: set `min_ram_bytes` to `12 * 1024**3` for the 7B spec and `32 * 1024**3` for the 30B spec. Default provider selection that uses tier 2 will resolve to the 7B model. Tier 3 may expose the 30B model only when availability checks pass the RAM gate.

## D5 Installer placement and state

Decision: implement local installation with a local-specific core installer plus a settings bootstrap module: `solstone/think/providers/local_install.py` for artifact paths, pins, downloads, extraction, chmod, and verification; `solstone/apps/settings/local_bootstrap.py` for availability/progress state and HTTP route helpers. Do not extend `solstone/think/providers/bundled.py`.

Justification: `bundled.py` delegates Codex binary installation to an external SDK and does not contain reusable tarball download/extract logic. Local needs two sha256-verified artifact classes: GitHub release tarballs for llama-server and Hugging Face LFS GGUF files, which matches the explicit verification pattern in `mlx_bootstrap.py`.

Implementation note: define `LLAMA_SERVER_PINS` as `{artifact_key: {"release_tag": str, "filename": str, "sha256": str, "binary_name": "llama-server"}}`. Pin v1 to `aarch64-apple-darwin` -> `b9291`, `llama-b9291-bin-macos-arm64.tar.gz`, sha256 `0e985f87dd71f96a9cb9ebc3ad26f8388030342d000e7e82d4a38d14913373ff`; and `x86_64-unknown-linux-gnu` -> `b9291`, `llama-b9291-bin-ubuntu-x64.tar.gz`, sha256 `8cb79eb596cc5cc15a6089ceadaa2723e3d75c1e7b37cfb9977ad1d4dc4a41eb`. Store binaries under `<journal>/cache/providers/local/bin/<artifact_key>/<release_tag>/` and models under `<journal>/cache/providers/local/models/<model_id>/`. Extract tarballs with path traversal checks, chmod the `llama-server` binary executable, and on macOS run `xattr -dr com.apple.quarantine <binary>` best-effort after sha256 verification. Persist install state in `providers.bundled.local` because that key already represents bundled provider install state; include fields for `binary_artifact`, `binary_sha256`, `binary_path`, `model_id`, `model_path`, `model_sha256`, `state`, `last_transition_at`, and `install_error`.

## D6 Linux CUDA slice

Decision: v1 ships macOS arm64 and Linux x86_64 CPU only. Linux CUDA is deferred.

Justification: recent llama.cpp releases expose Ubuntu CPU, Vulkan, SYCL, OpenVINO, and ROCm tarballs plus Windows CUDA zips, but no `ubuntu` or `linux` CUDA tarball equivalent was present in the checked release asset set. Building from source would violate the prebuilt zero-install-size constraint for this lode.

Implementation note: do not add a CUDA artifact key in `LLAMA_SERVER_PINS` until an upstream Linux CUDA prebuilt tarball with a published sha256 is available. Add a completion note flagging this as a founder-facing decision point: v1 accepts no Linux CUDA acceleration path; the next decision is whether to wait for upstream, use a container, or own a build pipeline.

## D7 Daemon ownership

Decision: use lazy-start on first `local` generate or cogitate call, not a supervisor-registered always-on service. Implement `solstone/think/providers/local_server.py` as the daemon manager.

Justification: there is no existing lazy subprocess-daemon pattern, and running llama-server all the time would impose memory cost on users who only occasionally choose the local backend. The provider can own a single loopback daemon and reuse existing process and port primitives without adding a new top-level service.

Implementation note: `local_server.ensure_running(model_id: str) -> LocalServerInfo` must reuse `RunnerManagedProcess.spawn()` to launch `[binary_path, "-m", model_path, "--alias", model_id, "--host", "127.0.0.1", "--port", str(port)]`. Allocate the port with `find_available_port()`, persist it with `write_service_port("local", port)`, and reattach by reading `read_service_port("local")` plus probing `/health`. Poll `/health` until HTTP 200; treat HTTP 503 with "Loading model" as `loading`; timeout becomes `model_load_timeout`. Enforce one instance with a process-local lock plus a file lock at `<journal>/health/local-server.lock` so concurrent first calls do not double-spawn. Provide `stop()` using `ManagedProcess.terminate()`. New state names are `idle`, `starting`, `loading`, `ready`, `failed`, and `stopped`; install/bootstrap keeps the MLX-style `idle`, `downloading`, `verifying`, `installed`, `failed`.

## D8 Fallback opt-out

Decision: `local` must never silently fall back to a cloud provider for either `generate` or `cogitate`. Keep MLX behavior unchanged: MLX opts out for `generate` only.

Justification: selecting `local` is an explicit privacy and locality choice. Silent cloud fallback would violate that intent and hide fixable local installation or runtime failures.

Implementation note: in `solstone/think/models.py::get_backup_provider`, return `None` when `primary_provider == "local"` for all agent types, then keep the existing `agent_type == "generate" and primary_provider == "mlx"` branch. This makes the preflight swap in `talents.py` and the on-failure cogitate fallback no-ops because both paths already require a non-empty backup. On local failure, emit or surface a recovery reason instead of setting `config["fallback_from"]`: `binary_missing`, `model_missing`, `server_crashed`, `model_load_failed`, `port_conflict`, or `ram_insufficient`. Update `tests/test_talent_fallback.py` so `local` expects `None` for both generate and cogitate; remove the old Ollama-to-Anthropic expectation.

## D9 Migration command

Decision: implement the migration as a manual settings maintenance module at `solstone/apps/settings/maint/_migrate_ollama_to_local.py`, with a CLI wrapper `sol call settings providers migrate-ollama-to-local [--commit] [--json]`.

Justification: app `maint/` scripts are auto-discovered and run without flags, but this data-format migration must dry-run by default and require `--commit` per L5. The leading underscore keeps the shared migration implementation under settings maint while preventing accidental automatic execution.

Implementation note: the dry-run prints a JSON-compatible report of every planned rewrite and exits without writing. `--commit` rewrites `providers.generate.provider`, `providers.generate.backup`, `providers.cogitate.provider`, and `providers.cogitate.backup` values from `ollama` to `local`; moves `providers.models.ollama` to `providers.models.local`, filling missing local tier keys and reporting conflicts; moves `providers.auth.ollama` to `providers.auth.local`; moves `providers.key_validation.ollama` to `providers.key_validation.local`; updates every `providers.contexts.*.provider == "ollama"` to `local`; and rewrites model strings with these exact mappings: `ollama-local/qwen3.5:2b` -> `local/qwen2.5-coder-7b`, `ollama-local/qwen3.5:9b` -> `local/qwen2.5-coder-7b`, `ollama-local/qwen3.5:35b-a3b-bf16` -> `local/qwen3-coder-30b-a3b-q4_k_m`, and other `ollama-local/<name>` values -> `local/<name>` with an `unsupported_model` warning. Do not touch `providers.api_keys`. Do not rewrite `providers.contexts` map keys because those are owner-defined context patterns; only rewrite their values. Running twice must produce an empty rewrite report.

## D10 Failure taxonomy and copy

Decision: use one local failure taxonomy shared by provider status, bootstrap routes, provider CLI, and UI.

Justification: local has more failure modes than a cloud API key provider, but the UI should still present the same recovery vocabulary as MLX bootstrap and bundled providers: install, verify, retry, and choose another configured provider manually.

Implementation note: map failures as follows. `binary_missing`: action `install_local_runtime`, copy "Local runtime is not installed." `gguf_missing`: action `install_local_model`, copy "Local model files are not installed." `server_crashed`: action `restart_local_runtime`, copy "Local runtime stopped unexpectedly." `model_load_failed`: action `retry_model_load`, copy "Local model could not be loaded." `model_load_timeout`: action `retry_model_load`, copy "Local model is still loading." `port_conflict`: action `restart_local_runtime`, copy "Local runtime port is unavailable." `ram_insufficient`: action `choose_smaller_model`, copy "This computer does not have enough memory for the selected local model." No message should offer automatic cloud fallback.

## D11 Settings, CLI, and UI surface

Decision: rename all Ollama settings surfaces to Local and add a Local bootstrap region modeled on MLX.

Justification: the old UI was an external Ollama/OpenCode readiness check. Local needs install, model availability, and daemon readiness controls that match the new bundled runtime.

Implementation note: replace `/api/providers/ollama/status` and `get_ollama_provider_status` with `/api/providers/local/status` and `get_local_provider_status`. Add `/api/local/models`, `/api/local/availability`, `/api/local/bootstrap`, and `/api/local/bootstrap/status` mirroring `/api/mlx/*`. In `workspace.html`, rename `#ollamaCogitateStatus*` to `#localCogitateStatus*`, `.ollama-command-row` to `.local-command-row`, `ollamaStatusLoading` to `localStatusLoading`, `OLLAMA_OPENCODE_*` to `LOCAL_RUNTIME_*`, `renderOllamaCogitateStatus` to `renderLocalCogitateStatus`, and `recheckOllamaStatus` to `recheckLocalStatus`. Add `.local-bootstrap-region` and `.local-progress-shell` modeled on `.mlx-bootstrap-region` and `.mlx-progress-shell`. Update `chat_reasons.py` and `chat_reasons.js` display names from `"ollama": "Ollama"` to `"local": "Local"`. In `providers_cli.py`, include `local` in the OpenHands-backed cogitate check set, remove Ollama-specific skip copy, and emit Local readiness copy from the D10 taxonomy.

## D12 Importability and zero-fetch rule

Decision: `solstone.think.providers.local`, `solstone.think.providers.local_server`, `solstone.think.providers.local_install`, and `solstone.think.providers.openhands` must import without llama-server, GGUF files, llama.cpp, OpenHands, LiteLLM, or Hugging Face network access present.

Justification: provider registration and settings pages must work before the owner enables the local backend. Imports must not trigger downloads, subprocess starts, or optional SDK imports.

Implementation note: keep OpenHands/LiteLLM imports inside `openhands` functions, keep Hugging Face and HTTP download imports inside bootstrap/install functions, and keep daemon startup inside `ensure_running()`. Nothing fetches or installs until the owner runs the Local bootstrap/install action.

## Deferred decisions and completion notes

Decision: record these completion notes with the implementation PR.

Justification: they capture resolved scope boundaries and the one founder-facing risk that should not be rediscovered during implementation.

Implementation note: B2 is resolved by shipping tier 1 and tier 2 on `local/qwen2.5-coder-7b`, with tier 3 on the 30B GGUF gated at 32 GiB RAM. B3 is resolved by lazy-start provider ownership in `local_server.py`, not supervisor startup registration. B4 is deferred to v1.1: add a non-gating warning constant named `LOCAL_BOOTSTRAP_DISK_WARNING_THRESHOLD_BYTES` before downloading large models. B5 is deferred: local vision is not in v1; image contents passed to local generate should fail loudly with an unsupported-capability error. D6 remains the explicit CUDA decision point: v1 ships no Linux CUDA slice.

## Implementation sequence

Decision: implement in this order: provider identity, facade, models, fallback; then install, daemon, bootstrap; then settings UI and CLI; then migration; then tests, baselines, and docs.

Justification: provider identity and model resolution are prerequisites for every route and test. Daemon/bootstrap depend on model specs and install paths. UI and migration should target stable provider/status APIs.

Implementation note: first update `providers/__init__.py`, `models.py`, `openhands.py`, `local.py`, and fallback tests. Next add `local_install.py`, `local_server.py`, and `apps/settings/local_bootstrap.py`. Then update settings routes, workspace IDs/classes/JS, `providers_cli.py`, and chat reason display names. Then add the manual migration wrapper and update fixtures/baselines. Finish with provider, fallback, migration, settings route, workspace, and baseline tests.
