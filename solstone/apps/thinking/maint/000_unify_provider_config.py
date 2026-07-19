# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Collapse legacy thinking provider routing into one active brain profile."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from solstone.think.journal_config import (
    JournalConfigMutation,
    mutate_journal_config,
)
from solstone.think.models import DEFAULT_MODEL_BY_PROVIDER
from solstone.think.utils import get_journal

SUPPORTED_PROVIDERS = frozenset(DEFAULT_MODEL_BY_PROVIDER)
CLOUD_KEY_ORDER = (
    ("GOOGLE_API_KEY", "google"),
    ("ANTHROPIC_API_KEY", "anthropic"),
    ("OPENAI_API_KEY", "openai"),
)
RETIRED_PROVIDER_FIELDS = (
    "generate",
    "cogitate",
    "tier",
    "backup",
    "models",
    "google_backend",
    "vertex_credentials",
)


def _profile(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    provider = value.get("provider")
    if not isinstance(provider, str) or provider not in SUPPORTED_PROVIDERS:
        return None
    model = value.get("model")
    if not isinstance(model, str) or not model.strip():
        model = DEFAULT_MODEL_BY_PROVIDER[provider]
    return {"provider": provider, "model": model.strip()}


def _choose_active(
    config: dict[str, Any], providers: dict[str, Any]
) -> dict[str, str] | None:
    for field in ("active", "cogitate", "generate"):
        profile = _profile(providers.get(field))
        if profile is not None:
            return profile

    env = config.get("env", {})
    if not isinstance(env, dict):
        env = {}
    for env_var, provider in CLOUD_KEY_ORDER:
        if env.get(env_var):
            return {
                "provider": provider,
                "model": DEFAULT_MODEL_BY_PROVIDER[provider],
            }
    return {
        "provider": "local",
        "model": DEFAULT_MODEL_BY_PROVIDER["local"],
    }


def migrate(config: dict[str, Any], journal: Path) -> bool:
    changed = False
    providers = config.get("providers")
    if not isinstance(providers, dict):
        providers = {}
        config["providers"] = providers
        changed = True

    active = _choose_active(config, providers)
    if providers.get("active") != active:
        if active is None:
            providers.pop("active", None)
        else:
            providers["active"] = active
        changed = True

    legacy_contexts = providers.get("contexts")
    if isinstance(legacy_contexts, dict) and legacy_contexts:
        overrides = config.get("talent_overrides")
        if not isinstance(overrides, dict):
            overrides = {}
        for context, value in legacy_contexts.items():
            if not isinstance(value, dict):
                continue
            supported = {
                key: value[key] for key in ("disabled", "extract") if key in value
            }
            if supported and overrides.get(context) != supported:
                overrides[context] = supported
                changed = True
        if overrides and config.get("talent_overrides") != overrides:
            config["talent_overrides"] = overrides
            changed = True
    if "contexts" in providers:
        providers.pop("contexts")
        changed = True

    services = config.get("services")
    confidential = services.get("confidential") if isinstance(services, dict) else None
    if isinstance(confidential, dict):
        if "prior_active" not in confidential:
            prior_provider = confidential.get(
                "prior_cogitate_provider"
            ) or confidential.get("prior_generate_provider")
            confidential["prior_active"] = (
                {
                    "provider": prior_provider,
                    "model": DEFAULT_MODEL_BY_PROVIDER[prior_provider],
                }
                if prior_provider in SUPPORTED_PROVIDERS
                else None
            )
            changed = True
        for field in ("prior_generate_provider", "prior_cogitate_provider"):
            if field in confidential:
                confidential.pop(field)
                changed = True

    for field in RETIRED_PROVIDER_FIELDS:
        if field in providers:
            providers.pop(field)
            changed = True

    validation = providers.get("key_validation")
    if isinstance(validation, dict):
        services = config.get("service_key_validation")
        if not isinstance(services, dict):
            services = {}
        for key in ("revai", "plaud"):
            if key in validation:
                if services.get(key) != validation[key]:
                    services[key] = validation[key]
                validation.pop(key)
                changed = True
        if services:
            if config.get("service_key_validation") != services:
                config["service_key_validation"] = services
                changed = True
        if "google_vertex" in validation:
            validation.pop("google_vertex")
            changed = True
        if not validation:
            providers.pop("key_validation")
            changed = True

    return changed


def main() -> None:
    journal = Path(get_journal())
    result = mutate_journal_config(
        lambda config: JournalConfigMutation(
            changed=migrate(config, journal),
            value=None,
        ),
        journal_path=journal,
    )
    vertex_credentials = journal / ".config" / "vertex-credentials.json"
    removed_credentials = False
    if vertex_credentials.exists() or vertex_credentials.is_symlink():
        vertex_credentials.unlink()
        removed_credentials = True
    if not result.changed and not removed_credentials:
        print("Thinking provider config already unified.")
        return
    print("Unified thinking provider config and removed retired provider settings.")


if __name__ == "__main__":
    main()
