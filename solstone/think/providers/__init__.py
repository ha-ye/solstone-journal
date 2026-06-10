# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""AI provider backends for think.

This package contains provider-specific implementations for LLM generation
and agent execution. Effective provider modules expose:

- run_generate(): Sync text generation, returns GenerateResult
- run_agenerate(): Async text generation, returns GenerateResult
- run_cogitate(): Tool-calling execution with event streaming

GenerateResult is a TypedDict with: text, usage, finish_reason, thinking.
The wrapper functions in think.models handle token logging and JSON validation.

Available providers:
- google: Google Gemini models
- openai: OpenAI GPT models
- anthropic: Anthropic Claude models
- local: bundled on-device llama-server models
"""

import shutil
from importlib import import_module
from types import ModuleType
from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# Provider Registry
# ---------------------------------------------------------------------------
# Central registry of supported providers and their module paths.
# All registered provider module targets must implement:
#   - run_generate(contents, model, ...) -> GenerateResult
#   - run_agenerate(contents, model, ...) -> GenerateResult
#   - run_cogitate(config, on_event) -> str
# ---------------------------------------------------------------------------

PROVIDER_REGISTRY: Dict[str, str] = {
    "google": "solstone.think.providers.openhands",
    "openai": "solstone.think.providers.openhands",
    "anthropic": "solstone.think.providers.openhands",
    "local": "solstone.think.providers.local",
}

# ---------------------------------------------------------------------------
# Provider Metadata
# ---------------------------------------------------------------------------
# Display labels, environment variable names, and CLI metadata where applicable.
# Used by settings UI, provider status, and agent health checks.
# ---------------------------------------------------------------------------

PROVIDER_METADATA: Dict[str, Dict[str, Any]] = {
    "google": {
        "label": "Google (Gemini)",
        "env_key": "GOOGLE_API_KEY",
        "vertex_env_keys": [
            "GOOGLE_GENAI_USE_VERTEXAI",
            "GOOGLE_APPLICATION_CREDENTIALS",
        ],
    },
    "openai": {
        "label": "OpenAI (GPT)",
        "env_key": "OPENAI_API_KEY",
    },
    "anthropic": {
        "label": "Anthropic (Claude)",
        "env_key": "ANTHROPIC_API_KEY",
    },
    "local": {
        "label": "Local (on-device)",
        "env_key": "",
    },
}


def managed_provider_env_keys() -> set[str]:
    """Return the set of managed provider API-key environment-variable names.

    Derived from ``PROVIDER_METADATA``: every non-empty ``env_key`` across the
    registered providers (currently GOOGLE_API_KEY, OPENAI_API_KEY,
    ANTHROPIC_API_KEY; ``local`` has no key). These are exactly the keys for which
    journal config's ``env`` section is the authoritative and exclusive source — a
    managed key absent from journal config is stripped from ``os.environ`` at CLI
    startup (see :func:`solstone.think.utils.setup_cli`) so a shell-set value is
    never used. Vertex/ADC auth vars (``vertex_env_keys``) are deliberately excluded:
    they are file/ADC credentials a user may legitimately set in the shell.
    """
    return {m["env_key"] for m in PROVIDER_METADATA.values() if m.get("env_key")}


def get_provider_module(provider: str) -> ModuleType:
    """Get the provider module for the given provider name.

    Parameters
    ----------
    provider
        Provider name (e.g., "google", "openai", "anthropic").

    Returns
    -------
    ModuleType
        The provider module with run_generate, run_agenerate, and run_cogitate functions.

    Raises
    ------
    ValueError
        If the provider is not registered.
    """
    if provider not in PROVIDER_REGISTRY:
        valid = ", ".join(sorted(PROVIDER_REGISTRY.keys()))
        raise ValueError(f"Unknown provider: {provider!r}. Valid providers: {valid}")

    return import_module(PROVIDER_REGISTRY[provider])


def get_provider_list() -> List[Dict[str, Any]]:
    """Get list of providers with metadata for UI display.

    Returns
    -------
    List[Dict[str, Any]]
        List of provider info dicts, each containing:
        - name: Provider identifier (e.g., "google")
        - label: Display label (e.g., "Google (Gemini)")
        - env_key: Environment variable for API key
    """
    providers = []
    for name in PROVIDER_REGISTRY:
        meta = PROVIDER_METADATA.get(name, {"label": name, "env_key": ""})
        provider = {
            "name": name,
            "label": meta.get("label", name),
            "env_key": meta.get("env_key", ""),
        }
        if "vertex_env_keys" in meta:
            provider["vertex_env_keys"] = meta["vertex_env_keys"]
        providers.append(provider)
    return providers


def build_provider_status(
    providers_list: List[Dict[str, Any]] | None = None,
    vertex_creds_configured: bool = False,
) -> Dict[str, Dict[str, Any]]:
    """Build per-provider readiness status.

    Parameters
    ----------
    providers_list
        Output of get_provider_list().
    vertex_creds_configured
        Whether Vertex AI credentials are configured (for Google).

    Returns
    -------
    Dict[str, Dict[str, Any]]
        Keyed by provider name. Each entry has readiness fields and issues.
        Local readiness also includes cogitate_cli and cogitate_cli_found.
    """
    if providers_list is None:
        providers_list = get_provider_list()

    status = {}
    for provider in providers_list:
        from solstone.think.providers import state as provider_state

        name = provider["name"]
        env_key = provider.get("env_key", "")
        meta = PROVIDER_METADATA.get(name, {})
        cogitate_cli = meta.get("cogitate_cli", "")
        issues: list[str] = []

        if name == "local":
            status[name] = provider_state.local_status_dict()
            continue
        elif name in {"google", "anthropic", "openai"}:
            configured = provider_state.cloud_key_configured(env_key)
            status[name] = {
                "provider": name,
                "configured": configured,
                "generate_ready": configured,
                "cogitate_ready": configured,
                "issues": [] if configured else [f"{env_key} not set"],
            }
            continue
        else:
            configured = provider_state.cloud_key_configured(env_key)
            if not configured and env_key:
                issues.append(f"{env_key} not set")

        cogitate_cli_found = bool(shutil.which(cogitate_cli)) if cogitate_cli else False
        if cogitate_cli and not cogitate_cli_found:
            install_cmd = meta.get("cogitate_cli_install")
            if install_cmd:
                issues.append(
                    f"{cogitate_cli} CLI not found on PATH — run: {install_cmd}"
                )
            else:
                issues.append(f"{cogitate_cli} CLI not found on PATH")
        cogitate_ready = configured and cogitate_cli_found

        generate_ready = configured

        status[name] = {
            "configured": configured,
            "generate_ready": generate_ready,
            "cogitate_ready": cogitate_ready,
            "cogitate_cli": cogitate_cli,
            "cogitate_cli_found": cogitate_cli_found,
            "issues": issues,
        }
    return status


def get_provider_models(provider: str) -> list[dict]:
    """Get available models for a provider.

    Parameters
    ----------
    provider
        Provider name (e.g., "google", "openai", "anthropic").

    Returns
    -------
    list[dict]
        List of raw model info objects returned by the provider API.

    Raises
    ------
    ValueError
        If the provider is not registered.
    """
    module = get_provider_module(provider)
    return module.list_models(provider)


def validate_key(provider: str, api_key: str) -> dict:
    """Validate an API key for a provider.

    Parameters
    ----------
    provider
        Provider name (e.g., "google", "openai", "anthropic").
    api_key
        The API key string to validate.

    Returns
    -------
    dict
        {"valid": True} or {"valid": False, "error": "..."}.

    Raises
    ------
    ValueError
        If the provider is not registered.
    """
    module = get_provider_module(provider)
    return module.validate_key(provider, api_key)


__all__ = [
    "PROVIDER_REGISTRY",
    "PROVIDER_METADATA",
    "get_provider_module",
    "get_provider_list",
    "build_provider_status",
    "get_provider_models",
    "validate_key",
    "managed_provider_env_keys",
]
