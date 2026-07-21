# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Pure helpers for exact Google model pinning and guidance."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

GOOGLE_ALIAS_PIN_MAP = {
    "gemini-flash-latest": "gemini-3.5-flash",
    "gemini-flash-lite-latest": "gemini-3.1-flash-lite",
}
GOOGLE_PRO_ALIAS = "gemini-pro-latest"
GOOGLE_PROVIDER = "google"
GOOGLE_MODEL_RESOLUTION_TARGETS_FIELD = "google_model_resolution_targets"
GOOGLE_PRO_ALIAS_SLOT_PATHS = {
    "active": "providers.active.model",
    "remembered": "providers.byo_models.google",
    "confidential_prior": "services.confidential.prior_active.model",
}
GOOGLE_PRO_ALIAS_SLOT_TOKENS = tuple(GOOGLE_PRO_ALIAS_SLOT_PATHS)
THINKING_BYO_MODEL_HREF = "/app/thinking/#byo-setup"

ChangedModelField = tuple[str, str, str]


def _google_profile_model(profile: Any) -> str | None:
    if not isinstance(profile, Mapping):
        return None
    if profile.get("provider") != GOOGLE_PROVIDER:
        return None
    model = profile.get("model")
    return model if isinstance(model, str) else None


def _pin_profile_model(profile: Any, path: str) -> ChangedModelField | None:
    if not isinstance(profile, dict):
        return None
    old_model = _google_profile_model(profile)
    if old_model not in GOOGLE_ALIAS_PIN_MAP:
        return None
    new_model = GOOGLE_ALIAS_PIN_MAP[old_model]
    profile["model"] = new_model
    return (path, old_model, new_model)


def pin_google_model_aliases(config: dict[str, Any]) -> list[ChangedModelField]:
    """Pin byte-exact Google alias slots in place and return changed fields."""

    changed: list[ChangedModelField] = []
    providers = config.get("providers")
    if isinstance(providers, dict):
        active = _pin_profile_model(providers.get("active"), "providers.active.model")
        if active is not None:
            changed.append(active)

        byo_models = providers.get("byo_models")
        if isinstance(byo_models, dict):
            old_model = byo_models.get(GOOGLE_PROVIDER)
            if old_model in GOOGLE_ALIAS_PIN_MAP:
                new_model = GOOGLE_ALIAS_PIN_MAP[old_model]
                byo_models[GOOGLE_PROVIDER] = new_model
                changed.append(("providers.byo_models.google", old_model, new_model))

    services = config.get("services")
    confidential = services.get("confidential") if isinstance(services, dict) else None
    if isinstance(confidential, dict):
        prior = _pin_profile_model(
            confidential.get("prior_active"),
            "services.confidential.prior_active.model",
        )
        if prior is not None:
            changed.append(prior)

    return changed


def read_google_pro_alias_slots(config: Mapping[str, Any]) -> list[str]:
    """Return slot tokens whose config fields hold the exact Google Pro alias."""

    slots: list[str] = []
    providers = config.get("providers")
    if isinstance(providers, Mapping):
        if _google_profile_model(providers.get("active")) == GOOGLE_PRO_ALIAS:
            slots.append("active")

        byo_models = providers.get("byo_models")
        if (
            isinstance(byo_models, Mapping)
            and byo_models.get(GOOGLE_PROVIDER) == GOOGLE_PRO_ALIAS
        ):
            slots.append("remembered")

    services = config.get("services")
    confidential = (
        services.get("confidential") if isinstance(services, Mapping) else None
    )
    if isinstance(confidential, Mapping):
        if _google_profile_model(confidential.get("prior_active")) == GOOGLE_PRO_ALIAS:
            slots.append("confidential_prior")

    return slots


def read_google_pro_alias_paths(config: Mapping[str, Any]) -> list[str]:
    """Return config field paths that hold the exact Google Pro alias."""

    return [
        GOOGLE_PRO_ALIAS_SLOT_PATHS[slot]
        for slot in read_google_pro_alias_slots(config)
    ]


def read_google_exact_model_advisory(
    config: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return owner guidance when a saved Google Pro alias needs manual choice."""

    slots = read_google_pro_alias_slots(config)
    if not slots:
        return None
    return {
        "id": "choose_exact_gemini_model",
        "heading": "choose an exact Gemini model",
        GOOGLE_MODEL_RESOLUTION_TARGETS_FIELD: slots,
        "action": {
            "label": "choose model",
            "href": THINKING_BYO_MODEL_HREF,
        },
    }
