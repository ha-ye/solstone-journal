# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from solstone.think import models
from solstone.think.models import (
    CLAUDE_SONNET_4,
    GEMINI_FLASH,
    GPT_5_MINI,
    LOCAL_MODEL,
    NO_BRAIN_PROVIDER,
    NoBrainConfiguredError,
    get_backup_provider,
    is_local_provider_needed,
    resolve_provider,
)
from solstone.think.providers import get_provider_module


def _empty_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    for key in ("GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def _cloud_call_mocks(monkeypatch: pytest.MonkeyPatch) -> list[Mock]:
    mocks: list[Mock] = []
    targets = [
        ("solstone.think.providers.openhands", "run_generate"),
        ("solstone.think.providers.openhands", "run_agenerate"),
        ("solstone.think.providers.openhands", "run_cogitate"),
        ("solstone.think.providers.google", "run_generate"),
        ("solstone.think.providers.google", "run_agenerate"),
        ("solstone.think.providers.openai", "run_generate"),
        ("solstone.think.providers.openai", "run_agenerate"),
        ("solstone.think.providers.anthropic", "run_generate"),
        ("solstone.think.providers.anthropic", "run_agenerate"),
    ]
    for module_name, attr in targets:
        mock = Mock(side_effect=AssertionError("cloud call attempted"))
        monkeypatch.setattr(f"{module_name}.{attr}", mock)
        mocks.append(mock)
    return mocks


def _write_journal_config(tmp_path: Path, config: dict) -> str:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(config)
    (config_dir / "journal.json").write_text(payload, encoding="utf-8")
    return payload


def test_unconfigured_journal_resolves_to_no_brain(tmp_path, monkeypatch):
    _empty_journal(tmp_path, monkeypatch)

    for agent_type in ("generate", "cogitate"):
        provider, model = resolve_provider("any.context", agent_type)

        assert provider == NO_BRAIN_PROVIDER
        assert provider != "google"
        assert model == ""

    assert not (tmp_path / "config" / "journal.json").exists()


def test_unconfigured_execution_stops_before_cloud(tmp_path, monkeypatch):
    _empty_journal(tmp_path, monkeypatch)
    mocks = _cloud_call_mocks(monkeypatch)

    with pytest.raises(NoBrainConfiguredError):
        models.generate("hello", "any.context")

    for mock in mocks:
        mock.assert_not_called()
    assert not (tmp_path / "config" / "journal.json").exists()


def test_none_provider_module_and_backup_fail_closed(tmp_path, monkeypatch):
    _empty_journal(tmp_path, monkeypatch)

    with pytest.raises(NoBrainConfiguredError):
        get_provider_module(NO_BRAIN_PROVIDER)

    assert get_backup_provider("generate") is None
    assert not (tmp_path / "config" / "journal.json").exists()


@pytest.mark.parametrize(
    ("env_key", "expected_provider", "expected_model"),
    [
        ("GOOGLE_API_KEY", "google", GEMINI_FLASH),
        ("ANTHROPIC_API_KEY", "anthropic", CLAUDE_SONNET_4),
        ("OPENAI_API_KEY", "openai", GPT_5_MINI),
    ],
)
def test_key_presence_grandfathers_existing_installs(
    tmp_path,
    monkeypatch,
    env_key: str,
    expected_provider: str,
    expected_model: str,
):
    _empty_journal(tmp_path, monkeypatch)
    original = _write_journal_config(tmp_path, {"env": {env_key: "test-key"}})

    provider, model = resolve_provider("any.context", "generate")

    assert provider == expected_provider
    assert model == expected_model
    assert (tmp_path / "config" / "journal.json").read_text(
        encoding="utf-8"
    ) == original


def test_implicit_local_when_runtime_ready(tmp_path, monkeypatch):
    _empty_journal(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "solstone.think.providers.state.local_runtime_ready", lambda: True
    )

    provider, model = resolve_provider("any.context", "generate")

    assert provider == "local"
    assert model == LOCAL_MODEL
    assert is_local_provider_needed() is True
    assert not (tmp_path / "config" / "journal.json").exists()


def test_explicit_local_type_default_neutralizes_cloud_context_pin(
    tmp_path,
    monkeypatch,
):
    _empty_journal(tmp_path, monkeypatch)
    _write_journal_config(
        tmp_path,
        {
            "providers": {
                "generate": {"provider": "local"},
                "contexts": {
                    "talent.timeline.segment_summary": {
                        "provider": "google",
                        "model": "gemini-flash-lite-latest",
                    },
                },
            },
        },
    )

    provider, model = resolve_provider("talent.timeline.segment_summary", "generate")

    assert provider == "local"
    assert provider != "google"
    assert model == LOCAL_MODEL
    assert model != "gemini-flash-lite-latest"
