# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Fixture builders for bundled provider state tests."""

from __future__ import annotations

from copy import deepcopy

from solstone.think.providers.bundled import PINS

BUNDLED_STATES = (
    "not-enabled",
    "enabling",
    "installed-no-key",
    "key-validating",
    "valid",
    "invalid-key",
    "install-failed",
    "disabled",
)

ENV_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}


def bundled_provider_config(provider: str, state: str) -> dict:
    """Return a complete journal config for a bundled provider state."""

    pin = PINS[provider]
    record = {
        "state": state,
        "last_transition_at": "2026-05-20T00:00:00+00:00",
        "sdk_spec": pin["sdk_spec"],
        "install_error": None,
    }
    if provider == "openai":
        record["codex_version"] = pin["codex_version"]
        artifact = next(iter(pin["codex_artifacts"].values()))
        record["codex_artifact"] = artifact["filename"]
        record["codex_sha256"] = artifact["sha256"]
    if state in {
        "installed-no-key",
        "key-validating",
        "valid",
        "invalid-key",
        "disabled",
    }:
        record["binary_path"] = f"/tmp/solstone-test/{provider}"
    if state == "install-failed":
        record["install_error"] = "network: timeout"

    config = {
        "identity": {"name": "Test User"},
        "setup": {"completed_at": 1},
        "convey": {"trust_localhost": True},
        "env": {},
        "providers": {
            "auth": {
                "anthropic": "api_key",
                "openai": "api_key",
            },
            "key_validation": {},
            "bundled": {provider: record},
        },
    }
    if state in {"key-validating", "valid", "invalid-key"}:
        config["env"][ENV_KEYS[provider]] = "test-key"
    if state == "valid":
        config["providers"]["key_validation"][provider] = {
            "valid": True,
            "timestamp": "2026-05-20T00:00:00+00:00",
        }
    elif state == "invalid-key":
        config["providers"]["key_validation"][provider] = {
            "valid": False,
            "error": "bad key",
            "timestamp": "2026-05-20T00:00:00+00:00",
        }
    return deepcopy(config)
