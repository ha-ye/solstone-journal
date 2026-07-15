# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import argparse
import asyncio
import fcntl
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _patch_health_journal(monkeypatch, providers_cli, tmp_path):
    monkeypatch.setattr(providers_cli, "get_journal", lambda: str(tmp_path))
    monkeypatch.setattr(
        "solstone.think.providers.state.get_journal",
        lambda: str(tmp_path),
    )


def _args(**overrides):
    values = {
        "provider": None,
        "interface": None,
        "model": None,
        "json": False,
        "timeout": 1,
        "targeted": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_run_check_writes_health_file(tmp_path, monkeypatch):
    import solstone.think.providers_cli as providers_cli

    monkeypatch.setattr(
        "solstone.think.providers.PROVIDER_REGISTRY", {"fake": object()}
    )
    monkeypatch.setattr(
        "solstone.think.models.default_model_for_provider",
        lambda provider: f"{provider}-model",
    )
    _patch_health_journal(monkeypatch, providers_cli, tmp_path)
    monkeypatch.setattr(
        providers_cli,
        "_check_generate",
        lambda *_args: ("ok", "ok", None),
    )

    async def mock_check_cogitate(*_args):
        return "ok", "ok", None

    monkeypatch.setattr(providers_cli, "_check_cogitate", mock_check_cogitate)

    with pytest.raises(SystemExit) as exc_info:
        asyncio.run(providers_cli._run_check(_args()))

    assert exc_info.value.code == 0
    payload = json.loads((tmp_path / "health" / "talents.json").read_text())
    assert datetime.fromisoformat(payload["checked_at"]).tzinfo is not None
    assert payload["summary"] == {"total": 2, "passed": 2, "skipped": 0, "failed": 0}
    assert all("tier" not in row for row in payload["results"])
    assert all("reused_from" not in row for row in payload["results"])


def test_run_check_partial_failure_exits_one(tmp_path, monkeypatch):
    import solstone.think.providers_cli as providers_cli

    monkeypatch.setattr(
        "solstone.think.providers.PROVIDER_REGISTRY", {"fake": object()}
    )
    monkeypatch.setattr(
        "solstone.think.models.default_model_for_provider",
        lambda _provider: "fake-model",
    )
    _patch_health_journal(monkeypatch, providers_cli, tmp_path)
    monkeypatch.setattr(
        providers_cli,
        "_check_generate",
        lambda *_args: ("ok", "ok", None),
    )

    async def mock_check_cogitate(*_args):
        return "fail", "FAIL: timeout", "unknown"

    monkeypatch.setattr(providers_cli, "_check_cogitate", mock_check_cogitate)

    with pytest.raises(SystemExit) as exc_info:
        asyncio.run(providers_cli._run_check(_args()))

    assert exc_info.value.code == 1
    payload = json.loads((tmp_path / "health" / "talents.json").read_text())
    assert payload["summary"] == {"total": 2, "passed": 1, "skipped": 0, "failed": 1}
    assert payload["results"][1]["reason_code"] == "unknown"


def test_run_check_targeted_uses_active_routes(tmp_path, monkeypatch):
    import solstone.think.providers_cli as providers_cli

    _patch_health_journal(monkeypatch, providers_cli, tmp_path)
    monkeypatch.setattr(
        "solstone.think.models.resolve_provider",
        lambda _interface: ("google", "gemini-flash-latest"),
    )
    gen_mock = MagicMock(return_value=("ok", "ok", None))
    monkeypatch.setattr(providers_cli, "_check_generate", gen_mock)

    cog_inner = MagicMock(return_value=("ok", "ok", None))

    async def mock_check_cogitate(*args):
        return cog_inner(*args)

    monkeypatch.setattr(providers_cli, "_check_cogitate", mock_check_cogitate)

    with pytest.raises(SystemExit) as exc_info:
        asyncio.run(providers_cli._run_check(_args(targeted=True, json=True)))

    assert exc_info.value.code == 0
    gen_mock.assert_called_once_with("google", "gemini-flash-latest", 1)
    cog_inner.assert_called_once_with("google", "gemini-flash-latest", 1)
    payload = json.loads((tmp_path / "health" / "talents.json").read_text())
    assert {
        (row["provider"], row["model"], row["interface"]) for row in payload["results"]
    } == {
        ("google", "gemini-flash-latest", "generate"),
        ("google", "gemini-flash-latest", "cogitate"),
    }


def test_run_check_targeted_empty_journal_uses_real_resolution(
    tmp_path, monkeypatch, capsys
):
    import solstone.think.providers_cli as providers_cli
    from solstone.think.models import NO_BRAIN_PROVIDER, resolve_provider

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    for key in ("GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        "solstone.think.providers.state.local_runtime_ready", lambda: False
    )
    _patch_health_journal(monkeypatch, providers_cli, tmp_path)
    gen_mock = MagicMock(side_effect=AssertionError("provider selected"))
    cog_mock = MagicMock(side_effect=AssertionError("provider selected"))
    monkeypatch.setattr(providers_cli, "_check_generate", gen_mock)

    async def mock_check_cogitate(*_args):
        return cog_mock(*_args)

    monkeypatch.setattr(providers_cli, "_check_cogitate", mock_check_cogitate)

    with pytest.raises(SystemExit) as exc_info:
        asyncio.run(providers_cli._run_check(_args(targeted=True, json=True)))

    assert exc_info.value.code == 0
    assert resolve_provider("generate") == (NO_BRAIN_PROVIDER, "")
    assert resolve_provider("cogitate") == (NO_BRAIN_PROVIDER, "")
    gen_mock.assert_not_called()
    cog_mock.assert_not_called()
    payload = json.loads((tmp_path / "health" / "talents.json").read_text())
    assert payload["results"] == []
    assert payload["summary"] == {"total": 0, "passed": 0, "skipped": 0, "failed": 0}
    stdout = capsys.readouterr().out
    assert json.loads(stdout) == {
        "results": [],
        "summary": {"total": 0, "passed": 0, "skipped": 0, "failed": 0},
    }


def test_run_check_targeted_flock_dedup(tmp_path, monkeypatch):
    import solstone.think.providers_cli as providers_cli

    _patch_health_journal(monkeypatch, providers_cli, tmp_path)
    monkeypatch.setattr(
        "solstone.think.models.resolve_provider",
        lambda _interface: ("google", "gemini-flash-latest"),
    )
    lock_dir = tmp_path / "health"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_dir / "recheck.lock", "w", encoding="utf-8")
    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    gen_mock = MagicMock(return_value=("ok", "ok", None))
    monkeypatch.setattr(providers_cli, "_check_generate", gen_mock)

    asyncio.run(providers_cli._run_check(_args(targeted=True)))

    gen_mock.assert_not_called()
    assert not (tmp_path / "health" / "talents.json").exists()
    lock_file.close()


def test_explicit_provider_uses_single_default_model(tmp_path, monkeypatch):
    import solstone.think.providers_cli as providers_cli

    _patch_health_journal(monkeypatch, providers_cli, tmp_path)
    monkeypatch.setattr(
        "solstone.think.providers.PROVIDER_REGISTRY",
        {"fake": object(), "other": object()},
    )
    monkeypatch.setattr(
        "solstone.think.models.default_model_for_provider",
        lambda provider: f"{provider}-default",
    )
    gen_mock = MagicMock(return_value=("ok", "ok", None))
    monkeypatch.setattr(providers_cli, "_check_generate", gen_mock)

    with pytest.raises(SystemExit):
        asyncio.run(
            providers_cli._run_check(
                _args(provider=["fake"], interface="generate", json=True)
            )
        )

    gen_mock.assert_called_once_with("fake", "fake-default", 1)


def test_explicit_provider_can_override_model(tmp_path, monkeypatch):
    import solstone.think.providers_cli as providers_cli

    _patch_health_journal(monkeypatch, providers_cli, tmp_path)
    monkeypatch.setattr(
        "solstone.think.providers.PROVIDER_REGISTRY", {"fake": object()}
    )
    gen_mock = MagicMock(return_value=("ok", "ok", None))
    monkeypatch.setattr(providers_cli, "_check_generate", gen_mock)

    with pytest.raises(SystemExit):
        asyncio.run(
            providers_cli._run_check(
                _args(
                    provider=["fake"],
                    interface="generate",
                    model="custom-model",
                    json=True,
                )
            )
        )

    gen_mock.assert_called_once_with("fake", "custom-model", 1)


def test_check_generate_logs_token_usage(monkeypatch):
    import solstone.think.providers_cli as providers_cli

    fake_module = MagicMock()
    fake_module.run_generate.return_value = {
        "text": "OK",
        "usage": {"input_tokens": 5, "output_tokens": 2},
    }
    monkeypatch.setattr(
        "solstone.think.providers.get_provider_module", lambda _: fake_module
    )
    monkeypatch.setattr(
        "solstone.think.providers.PROVIDER_METADATA",
        {"fake": {"env_key": "FAKE_API_KEY", "label": "Fake Provider"}},
    )
    monkeypatch.setenv("FAKE_API_KEY", "test-key")
    log_mock = MagicMock()
    monkeypatch.setattr("solstone.think.models.log_token_usage", log_mock)

    status, msg, reason_code = providers_cli._check_generate("fake", "fake-model", 30)

    assert status == "ok"
    assert msg == "OK"
    assert reason_code is None
    log_mock.assert_called_once_with(
        model="fake-model",
        usage={"input_tokens": 5, "output_tokens": 2},
        context="health.check.generate",
        type="generate",
    )


def test_missing_env_key_returns_skip(monkeypatch):
    import solstone.think.providers_cli as providers_cli

    monkeypatch.setattr(
        "solstone.think.providers.PROVIDER_METADATA",
        {"fake": {"env_key": "FAKE_API_KEY", "label": "Fake Provider"}},
    )
    monkeypatch.delenv("FAKE_API_KEY", raising=False)

    status, msg, reason_code = providers_cli._check_generate("fake", "fake-model", 30)
    assert status == "skip"
    assert reason_code == "provider_key_missing"
    assert "Fake Provider not configured" in msg
    assert "FAKE_API_KEY" in msg


def test_check_cogitate_cloud_configured_runs_without_install_skip(monkeypatch):
    import solstone.think.providers_cli as providers_cli

    class FakeModule:
        @staticmethod
        async def run_cogitate(*_args, **_kwargs):
            return "OK"

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(
        "solstone.think.providers.get_provider_module",
        lambda _provider: FakeModule,
    )

    status, msg, reason_code = asyncio.run(
        providers_cli._check_cogitate("anthropic", "claude-sonnet-4-6", 30)
    )

    assert (status, msg) == ("ok", "OK")
    assert reason_code is None


def test_check_cogitate_local_missing_runtime_names_local_install_hint(monkeypatch):
    import solstone.think.providers_cli as providers_cli

    monkeypatch.setattr(
        providers_cli,
        "_provider_status",
        lambda _name: {
            "configured": True,
            "cogitate_ready": False,
            "issues": ["model_missing"],
        },
    )
    monkeypatch.setattr(
        "solstone.think.providers.state.readiness_for_provider",
        lambda *_args: type("FakeState", (), {"reason_code": "local_model_missing"})(),
    )
    monkeypatch.setattr(
        "solstone.think.providers.local_endpoint.resolve_local_endpoint",
        lambda: SimpleNamespace(is_bundled=True),
    )

    status, msg, reason_code = asyncio.run(
        providers_cli._check_cogitate("local", "local/qwen3.5-4b", 30)
    )

    assert status == "skip"
    assert reason_code == "local_model_missing"
    assert "journal install-provider local" in msg


def test_check_cogitate_local_endpoint_unreachable_uses_endpoint_reason(monkeypatch):
    import solstone.think.providers_cli as providers_cli

    monkeypatch.setattr(
        providers_cli,
        "_provider_status",
        lambda _name: {
            "configured": True,
            "cogitate_ready": False,
            "issues": ["local_endpoint_unreachable"],
        },
    )
    monkeypatch.setattr(
        "solstone.think.providers.state.readiness_for_provider",
        lambda *_args: type(
            "FakeState", (), {"reason_code": "local_endpoint_unreachable"}
        )(),
    )
    monkeypatch.setattr(
        "solstone.think.providers.local_endpoint.resolve_local_endpoint",
        lambda: SimpleNamespace(is_bundled=False),
    )

    status, msg, reason_code = asyncio.run(
        providers_cli._check_cogitate("local", "local/qwen3.5-4b", 30)
    )

    assert status == "skip"
    assert msg == "local_endpoint_unreachable"
    assert reason_code == "local_endpoint_unreachable"
    assert "journal install-provider local" not in msg


def test_cortex_start_emits_providers_check(tmp_path):
    from solstone.think.cortex import CortexService

    cortex = CortexService(journal_path=str(tmp_path))
    cortex.callosum = MagicMock()
    cortex.callosum.start.return_value = None
    cortex.shutdown_requested.set()

    with patch("solstone.think.cortex.threading.Thread") as mock_thread:
        mock_thread.return_value = MagicMock()
        with patch("solstone.think.cortex.time.sleep", return_value=None):
            cortex.start()

    cortex.callosum.emit.assert_any_call(
        "supervisor", "request", cmd=["journal", "providers", "check"]
    )
