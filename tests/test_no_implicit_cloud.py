# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

from solstone.think import models, talents
from solstone.think.models import (
    CLAUDE_SONNET_4,
    GEMINI_FLASH,
    GPT_5_MINI,
    LOCAL_MODEL,
    NO_BRAIN_PROVIDER,
    AttestationFailedError,
    NoBrainConfiguredError,
    is_local_provider_needed,
    resolve_provider,
)
from solstone.think.providers import get_provider_module


@pytest.fixture(autouse=True)
def _clear_confidential_transport_state():
    from solstone.think.services import spp, spp_transport

    spp.delete_attestation_state()
    spp_transport.teardown_confidential_transport()
    yield
    spp.delete_attestation_state()
    spp_transport.teardown_confidential_transport()


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


def _confidential_config(*, provider_pins: bool = True) -> dict:
    config: dict = {
        "env": {
            "GOOGLE_API_KEY": "test-google-key",
            "ANTHROPIC_API_KEY": "test-anthropic-key",
            "OPENAI_API_KEY": "test-openai-key",
        },
        "services": {
            "confidential": {
                "enabled_at": "2026-05-24T00:00:00Z",
                "account_id": "acct-test",
                "endpoint_url": "https://spp.example.test",
                "served_model_id": "confidential-model",
                "credential_created_at": "2026-05-24T00:00:00Z",
                "credential_fingerprint_sha256": "fingerprint",
                "prior_generate_provider": "google",
                "prior_cogitate_provider": "openai",
                "prior_local_endpoint": None,
            }
        },
    }
    if provider_pins:
        config["providers"] = {
            "generate": {"provider": "google", "backup": "openai"},
            "cogitate": {"provider": "openai", "backup": "anthropic"},
        }
    return config


def _install_failing_confidential_transport(
    monkeypatch: pytest.MonkeyPatch,
    reason_code: str = "gateway_unreachable",
) -> Mock:
    from solstone.think.services import spp, spp_transport
    from solstone.think.services.spp_attest.ratls.channel import RatlsChannelError

    spp.delete_attestation_state()
    spp_transport.teardown_confidential_transport()
    monkeypatch.setattr(models, "_CONFIDENTIAL_ATTESTATION_VERIFIER", None)
    establish = Mock(side_effect=RatlsChannelError(reason_code))
    monkeypatch.setattr(spp_transport, "establish_attested_channel", establish)
    return establish


def _assert_attestation_failed(
    exc: AttestationFailedError,
    reason_code: str = "gateway_unreachable",
) -> None:
    assert exc.reason_code == "attestation_failed"
    assert f"({reason_code})" in exc.detail


def test_unconfigured_journal_resolves_to_no_brain(tmp_path, monkeypatch):
    _empty_journal(tmp_path, monkeypatch)

    for agent_type in ("generate", "cogitate"):
        provider, model = resolve_provider(agent_type)

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


def test_no_brain_configured_error_is_not_retried(tmp_path, monkeypatch):
    _empty_journal(tmp_path, monkeypatch)
    mocks = _cloud_call_mocks(monkeypatch)

    with pytest.raises(NoBrainConfiguredError):
        talents.prepare_config({"name": "chat"})

    for mock in mocks:
        mock.assert_not_called()


def test_confidential_generate_stops_before_any_provider_dispatch(
    tmp_path,
    monkeypatch,
):
    _empty_journal(tmp_path, monkeypatch)
    _write_journal_config(tmp_path, _confidential_config())
    establish = _install_failing_confidential_transport(monkeypatch)
    mocks = _cloud_call_mocks(monkeypatch)
    httpx_post = Mock(side_effect=AssertionError("local endpoint call attempted"))
    httpx_get = Mock(side_effect=AssertionError("endpoint probe attempted"))
    monkeypatch.setattr("httpx.post", httpx_post)
    monkeypatch.setattr("httpx.get", httpx_get)

    with pytest.raises(AttestationFailedError) as generate_exc:
        models.generate("hello", "any.context")
    _assert_attestation_failed(generate_exc.value)

    with pytest.raises(AttestationFailedError) as result_exc:
        models.generate_with_result("hello", "any.context")
    _assert_attestation_failed(result_exc.value)

    with pytest.raises(AttestationFailedError) as async_exc:
        asyncio.run(models.agenerate("hello", "any.context"))
    _assert_attestation_failed(async_exc.value)

    for mock in mocks:
        mock.assert_not_called()
    httpx_post.assert_not_called()
    httpx_get.assert_not_called()
    assert establish.call_count == 3


def test_confidential_cogitate_stops_before_any_provider_dispatch(
    tmp_path,
    monkeypatch,
):
    _empty_journal(tmp_path, monkeypatch)
    _write_journal_config(tmp_path, _confidential_config())
    establish = _install_failing_confidential_transport(monkeypatch)
    mocks = _cloud_call_mocks(monkeypatch)
    build_llm = Mock(side_effect=AssertionError("llm build attempted"))
    monkeypatch.setattr("solstone.think.providers.openhands._build_llm", build_llm)
    httpx_post = Mock(side_effect=AssertionError("local endpoint call attempted"))
    httpx_get = Mock(side_effect=AssertionError("endpoint probe attempted"))
    monkeypatch.setattr("httpx.post", httpx_post)
    monkeypatch.setattr("httpx.get", httpx_get)

    with pytest.raises(AttestationFailedError) as exc_info:
        asyncio.run(
            talents._execute_with_tools(
                {"provider": "google"},
                lambda _event: None,
            )
        )

    _assert_attestation_failed(exc_info.value)
    build_llm.assert_not_called()
    for mock in mocks:
        mock.assert_not_called()
    httpx_post.assert_not_called()
    httpx_get.assert_not_called()
    establish.assert_called_once()


def test_confidential_readiness_probe_fails_closed_before_endpoint_get(
    tmp_path,
    monkeypatch,
):
    from solstone.think.providers import state
    from solstone.think.providers.local_endpoint import (
        probe_local_endpoint,
        resolve_local_endpoint,
    )
    from solstone.think.services.spp_transport import confidential_probe_status

    _empty_journal(tmp_path, monkeypatch)
    config = _confidential_config()
    config.setdefault("providers", {})["local"] = {
        "endpoint_url": "https://spp.example.test/v1",
        "served_model_id": "confidential-model",
        "credential": "confidential-credential",
    }
    _write_journal_config(tmp_path, config)
    httpx_get = Mock(side_effect=AssertionError("endpoint probe attempted"))
    monkeypatch.setattr("httpx.get", httpx_get)

    endpoint = resolve_local_endpoint()
    assert confidential_probe_status() == (False, "attestation_not_yet_verified")
    assert probe_local_endpoint(endpoint) == (False, "attestation_not_yet_verified")
    status = state.local_status_dict()

    assert status["configured"] is True
    assert status["generate_ready"] is False
    assert status["cogitate_ready"] is False
    assert status["issues"] == ["local_endpoint_unreachable"]
    httpx_get.assert_not_called()

    config.pop("services", None)
    _write_journal_config(tmp_path, config)
    assert confidential_probe_status() is None


def test_confidential_attestation_error_is_non_retryable(tmp_path, monkeypatch):
    _empty_journal(tmp_path, monkeypatch)
    _write_journal_config(tmp_path, _confidential_config())
    establish = _install_failing_confidential_transport(monkeypatch)
    mocks = _cloud_call_mocks(monkeypatch)

    from solstone.think.services.spp_transport import verify_confidential_attestation

    assert (
        models._confidential_attestation_verifier() is verify_confidential_attestation
    )

    with pytest.raises(AttestationFailedError) as exc_info:
        asyncio.run(
            talents._execute_with_tools(
                {"provider": "google", "type": "cogitate"},
                lambda _event: None,
            )
        )
    _assert_attestation_failed(exc_info.value)

    for mock in mocks:
        mock.assert_not_called()
    establish.assert_called_once()


def test_confidential_gate_keys_on_provenance_not_provider_resolution(
    tmp_path,
    monkeypatch,
):
    _empty_journal(tmp_path, monkeypatch)
    config = _confidential_config(provider_pins=False)
    config["env"] = {"GOOGLE_API_KEY": "stray-google-key"}
    _write_journal_config(tmp_path, config)
    establish = _install_failing_confidential_transport(monkeypatch)
    mocks = _cloud_call_mocks(monkeypatch)
    httpx_post = Mock(side_effect=AssertionError("local endpoint call attempted"))
    monkeypatch.setattr("httpx.post", httpx_post)

    with pytest.raises(AttestationFailedError) as exc_info:
        models.generate("hello", "any.context")

    _assert_attestation_failed(exc_info.value)
    for mock in mocks:
        mock.assert_not_called()
    httpx_post.assert_not_called()
    establish.assert_called_once()


def test_confidential_stt_chokepoint_blocks_cloud_audio_egress(
    tmp_path,
    monkeypatch,
):
    _empty_journal(tmp_path, monkeypatch)
    _write_journal_config(tmp_path, _confidential_config(provider_pins=False))
    audio = np.zeros(16000, dtype=np.float32)
    gemini_transcribe = Mock(side_effect=AssertionError("audio egress attempted"))
    revai_transcribe = Mock(side_effect=AssertionError("audio egress attempted"))
    parakeet_transcribe = Mock(return_value=[])
    monkeypatch.setattr(
        "solstone.observe.transcribe.gemini.transcribe",
        gemini_transcribe,
    )
    monkeypatch.setattr(
        "solstone.observe.transcribe.revai.transcribe",
        revai_transcribe,
    )
    monkeypatch.setattr(
        "solstone.observe.transcribe.parakeet.transcribe",
        parakeet_transcribe,
    )

    from solstone.observe.transcribe import (
        ConfidentialAudioEgressError,
        transcribe,
    )

    with pytest.raises(ConfidentialAudioEgressError):
        transcribe("gemini", audio, 16000, {})
    with pytest.raises(ConfidentialAudioEgressError):
        transcribe("revai", audio, 16000, {})

    gemini_transcribe.assert_not_called()
    revai_transcribe.assert_not_called()
    assert transcribe("parakeet", audio, 16000, {}) == []
    parakeet_transcribe.assert_called_once()


def test_none_provider_module_fails_closed(tmp_path, monkeypatch):
    _empty_journal(tmp_path, monkeypatch)

    with pytest.raises(NoBrainConfiguredError):
        get_provider_module(NO_BRAIN_PROVIDER)

    assert not (tmp_path / "config" / "journal.json").exists()


@pytest.mark.parametrize(
    ("agent_type", "env_key", "expected_provider", "expected_model"),
    [
        ("generate", "GOOGLE_API_KEY", "google", GEMINI_FLASH),
        ("generate", "ANTHROPIC_API_KEY", "anthropic", CLAUDE_SONNET_4),
        ("generate", "OPENAI_API_KEY", "openai", GPT_5_MINI),
        ("cogitate", "GOOGLE_API_KEY", "google", GEMINI_FLASH),
        ("cogitate", "ANTHROPIC_API_KEY", "anthropic", CLAUDE_SONNET_4),
        ("cogitate", "OPENAI_API_KEY", "openai", GPT_5_MINI),
    ],
)
def test_key_presence_grandfathers_existing_installs(
    tmp_path,
    monkeypatch,
    agent_type: str,
    env_key: str,
    expected_provider: str,
    expected_model: str,
):
    _empty_journal(tmp_path, monkeypatch)
    original = _write_journal_config(tmp_path, {"env": {env_key: "test-key"}})

    provider, model = resolve_provider(agent_type)

    assert provider == expected_provider
    assert model == expected_model
    assert (tmp_path / "config" / "journal.json").read_text(
        encoding="utf-8"
    ) == original


def test_model_only_config_uses_key_selected_provider(tmp_path, monkeypatch):
    _empty_journal(tmp_path, monkeypatch)
    _write_journal_config(
        tmp_path,
        {
            "env": {"GOOGLE_API_KEY": "test-key"},
            "providers": {"generate": {"model": "gemini-custom"}},
        },
    )

    assert resolve_provider("generate") == ("google", "gemini-custom")


def test_explicit_provider_does_not_fall_through_to_keyed_provider(
    tmp_path, monkeypatch
):
    _empty_journal(tmp_path, monkeypatch)
    _write_journal_config(
        tmp_path,
        {
            "env": {"GOOGLE_API_KEY": "test-key"},
            "providers": {"generate": {"provider": "anthropic"}},
        },
    )
    google = Mock(side_effect=AssertionError("google dispatched"))
    monkeypatch.setattr("solstone.think.providers.google.run_generate", google)

    assert resolve_provider("generate") == ("anthropic", CLAUDE_SONNET_4)
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY not found"):
        models.generate("hello", "any.context")
    google.assert_not_called()


def test_accepted_grandfather_divergence_lite_context_now_uses_brain_model(
    tmp_path, monkeypatch
):
    _empty_journal(tmp_path, monkeypatch)
    _write_journal_config(tmp_path, {"env": {"GOOGLE_API_KEY": "test-key"}})

    assert resolve_provider("generate") == ("google", GEMINI_FLASH)


def test_implicit_local_when_runtime_ready(tmp_path, monkeypatch):
    _empty_journal(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "solstone.think.providers.state.local_runtime_ready", lambda: True
    )

    provider, model = resolve_provider("generate")

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

    provider, model = resolve_provider("generate")

    assert provider == "local"
    assert provider != "google"
    assert model == LOCAL_MODEL
    assert model != "gemini-flash-lite-latest"
