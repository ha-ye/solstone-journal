# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from solstone.think.providers import bundled
from tests.bundled_provider_fixtures import (
    BUNDLED_STATES,
    bundled_provider_config,
)


@pytest.fixture(autouse=True)
def reset_bundled_locks():
    bundled._LOCKS.clear()


@pytest.fixture
def journal_config(tmp_path, monkeypatch):
    def _write(config: dict) -> Path:
        config_path = tmp_path / "config" / "journal.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
        return config_path

    return _write


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
@pytest.mark.parametrize("state", BUNDLED_STATES)
def test_fixture_states_compose_contract(journal_config, provider, state):
    journal_config(bundled_provider_config(provider, state))

    contract = bundled.get_provider_state(provider)

    assert contract["name"] == provider
    assert contract["state"] == state
    assert "actions" in contract
    assert "issues" in contract


def test_install_provider_persists_enabling_and_starts_one_thread(
    journal_config,
    monkeypatch,
):
    journal_config(bundled_provider_config("anthropic", "not-enabled"))
    started = []
    monkeypatch.setattr(
        bundled,
        "_start_thread",
        lambda target, args: started.append((target, args)),
    )

    state = bundled.install_provider("anthropic")

    assert state["state"] == "enabling"
    assert len(started) == 1

    second = bundled.install_provider("anthropic")

    assert second["state"] == "enabling"
    assert len(started) == 1


def test_install_provider_installed_no_key_is_noop(journal_config, monkeypatch):
    journal_config(bundled_provider_config("anthropic", "installed-no-key"))
    started = []
    monkeypatch.setattr(
        bundled,
        "_start_thread",
        lambda target, args: started.append((target, args)),
    )

    state = bundled.install_provider("anthropic")

    assert state["state"] == "installed-no-key"
    assert started == []


def test_install_provider_retries_install_failed(journal_config, monkeypatch):
    journal_config(bundled_provider_config("openai", "install-failed"))
    started = []
    monkeypatch.setattr(
        bundled,
        "_start_thread",
        lambda target, args: started.append((target, args)),
    )

    state = bundled.install_provider("openai")

    assert state["state"] == "enabling"
    assert state["install_error"] is None
    assert len(started) == 1


def test_stuck_enabling_allows_install_retry(journal_config, monkeypatch):
    config = bundled_provider_config("openai", "enabling")
    old = datetime.now(timezone.utc) - timedelta(
        seconds=bundled.STUCK_ENABLING_SECONDS + 60
    )
    config["providers"]["bundled"]["openai"]["last_transition_at"] = old.isoformat()
    journal_config(config)
    started = []
    monkeypatch.setattr(
        bundled,
        "_start_thread",
        lambda target, args: started.append((target, args)),
    )

    before = bundled.get_provider_state("openai")
    after = bundled.install_provider("openai")

    assert before["stuck_enabling"] is True
    assert after["state"] == "enabling"
    assert after["stuck_enabling"] is False
    assert len(started) == 1


def test_install_thread_success_transitions_to_valid(journal_config, monkeypatch):
    config = bundled_provider_config("anthropic", "enabling")
    config["env"]["ANTHROPIC_API_KEY"] = "test-key"
    config["providers"]["key_validation"]["anthropic"] = {"valid": True}
    journal_config(config)
    monkeypatch.setattr(bundled, "_run_uv_pip_install", lambda sdk_spec: None)
    monkeypatch.setattr(
        bundled,
        "_resolve_anthropic_binary_via_subprocess",
        lambda: Path("/tmp/claude"),
    )

    bundled._install_thread("anthropic")

    state = bundled.get_provider_state("anthropic")
    assert state["state"] == "valid"
    assert state["binary_path"] == "/tmp/claude"


def test_validate_key_thread_persists_result(journal_config, monkeypatch):
    config = bundled_provider_config("openai", "installed-no-key")
    config["env"]["OPENAI_API_KEY"] = "test-key"
    journal_config(config)
    monkeypatch.setattr(
        bundled,
        "_validate_provider_key",
        lambda name: {"valid": True},
    )

    bundled._validate_thread("openai")

    state = bundled.get_provider_state("openai")
    assert state["state"] == "valid"
    assert state["key_validation"]["valid"] is True
    assert "timestamp" in state["key_validation"]


def test_validate_key_thread_persists_human_error(journal_config, monkeypatch):
    config = bundled_provider_config("openai", "key-validating")
    config["env"]["OPENAI_API_KEY"] = "test-key"
    journal_config(config)

    def fail(_name):
        raise RuntimeError("provider rejected key")

    monkeypatch.setattr(bundled, "_validate_provider_key", fail)

    bundled._validate_thread("openai")

    state = bundled.get_provider_state("openai")
    assert state["state"] == "invalid-key"
    assert state["key_validation"]["error"] == "provider rejected key"
    assert "provider rejected key" in state["issues"]


def test_validate_key_returns_key_validating_immediately(journal_config, monkeypatch):
    config = bundled_provider_config("anthropic", "installed-no-key")
    config["env"]["ANTHROPIC_API_KEY"] = "test-key"
    journal_config(config)
    started = []
    monkeypatch.setattr(
        bundled,
        "_start_thread",
        lambda target, args: started.append((target, args)),
    )

    state = bundled.validate_key("anthropic")

    assert state["state"] == "key-validating"
    assert len(started) == 1


def test_validate_key_not_enabled_requires_install(journal_config, monkeypatch):
    journal_config(bundled_provider_config("anthropic", "not-enabled"))
    started = []
    monkeypatch.setattr(
        bundled,
        "_start_thread",
        lambda target, args: started.append((target, args)),
    )

    with pytest.raises(bundled.CogitateProviderNotInstalled) as exc_info:
        bundled.validate_key("anthropic")

    assert "sol call settings providers install anthropic" in str(exc_info.value)
    assert started == []


def test_uninstall_during_install_raises(journal_config):
    config = bundled_provider_config("anthropic", "enabling")
    config["providers"]["bundled"]["anthropic"]["last_transition_at"] = datetime.now(
        timezone.utc
    ).isoformat()
    journal_config(config)

    with pytest.raises(bundled.CogitateProviderInstallInFlight):
        bundled.uninstall_provider("anthropic")


def test_uninstall_not_enabled_is_noop(journal_config, monkeypatch):
    config_path = journal_config(bundled_provider_config("anthropic", "not-enabled"))
    calls = []
    monkeypatch.setattr(
        bundled,
        "_run_uv_pip_uninstall",
        lambda sdk_spec: calls.append(sdk_spec),
    )

    state = bundled.uninstall_provider("anthropic")

    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert state["state"] == "not-enabled"
    assert calls == []
    assert persisted["providers"]["bundled"]["anthropic"]["state"] == "not-enabled"


def test_uninstall_preserves_keys_auth_and_env(journal_config, monkeypatch):
    config = bundled_provider_config("anthropic", "valid")
    config["env"]["ANTHROPIC_API_KEY"] = "test-key"
    config["providers"]["auth"]["anthropic"] = "api_key"
    config["providers"]["key_validation"]["anthropic"] = {
        "valid": True,
        "timestamp": "2026-05-20T00:00:00+00:00",
    }
    config_path = journal_config(config)
    monkeypatch.setattr(bundled, "_run_uv_pip_uninstall", lambda sdk_spec: None)

    state = bundled.uninstall_provider("anthropic")

    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert state["state"] == "not-enabled"
    assert persisted["env"]["ANTHROPIC_API_KEY"] == "test-key"
    assert persisted["providers"]["auth"]["anthropic"] == "api_key"
    assert persisted["providers"]["key_validation"]["anthropic"]["valid"] is True


def test_resolve_bundled_binary_success(journal_config):
    journal_config(bundled_provider_config("openai", "valid"))

    assert bundled.resolve_bundled_binary("openai") == Path("/tmp/solstone-test/openai")


def test_resolve_bundled_binary_missing_has_install_hint(journal_config):
    journal_config(bundled_provider_config("anthropic", "not-enabled"))

    with pytest.raises(bundled.CogitateProviderNotInstalled) as exc_info:
        bundled.resolve_bundled_binary("anthropic")

    assert "sol call settings providers install anthropic" in str(exc_info.value)


def test_uv_install_error_categorization(monkeypatch):
    monkeypatch.setattr(
        bundled.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            1,
            stdout="",
            stderr="timed out connecting to pypi",
        ),
    )

    with pytest.raises(bundled.CogitateProviderInstallFailed) as exc_info:
        bundled._run_uv_pip_install("claude-agent-sdk==0.2.82")

    assert str(exc_info.value).startswith("network:")


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("network connection failed", "codex binary download: network"),
        ("sha256 mismatch", "codex binary download: sha256 mismatch"),
        (
            "unsupported platform triple",
            "codex binary download: unsupported platform triple",
        ),
        ("archive missing", "codex binary download: other: archive missing"),
    ],
)
def test_codex_install_error_categorization(monkeypatch, stderr, expected):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0],
            1,
            stdout="",
            stderr=stderr,
        )

    monkeypatch.setattr(bundled.subprocess, "run", fake_run)

    with pytest.raises(bundled.CogitateProviderInstallFailed) as exc_info:
        bundled._run_codex_install("rust-v0.131.0", "", "")

    assert str(exc_info.value) == expected


def test_unsupported_provider_raises():
    with pytest.raises(bundled.UnsupportedBundledProvider):
        bundled.get_provider_state("google")
