# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import ast
import asyncio
import importlib
import json
import time
from datetime import datetime, timedelta, timezone
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
from solstone.think.services.spp_attest.cadence import AttestationSession


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        if base is None:
            return None
        return f"{base}.{node.attr}"
    return None


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


class _FakeChannel:
    def __init__(self, verdict: object, epoch: int | None = None) -> None:
        from solstone.think.services import spp_transport

        self.verdict = verdict
        self.tls = object()
        self.last_used_monotonic = time.monotonic()
        self.epoch = spp_transport._EPOCH if epoch is None else epoch
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeListener:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _AliveThread:
    def is_alive(self) -> bool:
        return True


def _patch_confidential_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    from solstone.think.services import spp_transport

    def fake_start_listener() -> None:
        spp_transport._LISTENER = _FakeListener()
        spp_transport._LISTENER_THREAD = _AliveThread()
        spp_transport._FORWARDER_BASE_URL = "http://127.0.0.1:4567"

    monkeypatch.setattr(spp_transport, "_start_listener_locked", fake_start_listener)


def _stale_session(verdict: object) -> AttestationSession:
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    return AttestationSession(
        verdict=verdict,
        started_at=old,
        tpm_heartbeat_at=old,
        gpu_reattest_at=old,
    )


def _add_local_endpoint(config: dict) -> None:
    config.setdefault("providers", {})["local"] = {
        "endpoint_url": "https://spp.example.test/v1",
        "served_model_id": "confidential-model",
        "credential": "confidential-credential",
    }


def _stt_audio() -> np.ndarray:
    return np.zeros(16000, dtype=np.float32)


def _install_stt_backend_mocks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    gemini_result=AssertionError("gemini audio egress attempted"),
    revai_result=AssertionError("revai audio egress attempted"),
    parakeet_result=AssertionError("parakeet dispatch attempted"),
    confidential_result=AssertionError("confidential dispatch attempted"),
) -> dict[str, Mock]:
    targets = {
        "gemini": (
            "solstone.observe.transcribe.gemini.transcribe",
            gemini_result,
        ),
        "revai": (
            "solstone.observe.transcribe.revai.transcribe",
            revai_result,
        ),
        "parakeet": (
            "solstone.observe.transcribe.parakeet.transcribe",
            parakeet_result,
        ),
        "confidential": (
            "solstone.observe.transcribe.confidential.transcribe",
            confidential_result,
        ),
    }
    mocks: dict[str, Mock] = {}
    for name, (target, result) in targets.items():
        if result is None:
            continue
        mock = (
            Mock(side_effect=result)
            if isinstance(result, BaseException)
            else Mock(return_value=result)
        )
        monkeypatch.setattr(target, mock)
        mocks[name] = mock
    return mocks


def test_stt_backend_dispatch_chokepoint_is_exclusive() -> None:
    from solstone.observe.transcribe import BACKEND_REGISTRY

    repo_root = Path(__file__).resolve().parents[1]
    solstone_root = repo_root / "solstone"
    package_dispatcher = Path("observe/transcribe/__init__.py")
    backend_module_paths = set(BACKEND_REGISTRY.values())
    backend_module_names = {
        module_path.rsplit(".", maxsplit=1)[-1] for module_path in backend_module_paths
    }
    violations: list[str] = []

    for path in sorted(solstone_root.rglob("*.py")):
        relative = path.relative_to(solstone_root)
        if relative == package_dispatcher:
            continue

        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        backend_aliases = set(backend_module_paths)
        backend_function_aliases: set[str] = set()
        get_backend_aliases = {"get_backend"}
        get_backend_results: set[str] = set()
        package_aliases = {"solstone.observe.transcribe"}

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    target = alias.asname or alias.name
                    if alias.name in backend_module_paths:
                        backend_aliases.add(target)
                    elif alias.name == "solstone.observe.transcribe":
                        package_aliases.add(target)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == "solstone.observe.transcribe":
                    for alias in node.names:
                        target = alias.asname or alias.name
                        if alias.name in backend_module_names:
                            backend_aliases.add(target)
                        elif alias.name == "get_backend":
                            get_backend_aliases.add(target)
                elif module in backend_module_paths:
                    for alias in node.names:
                        if alias.name == "transcribe":
                            backend_function_aliases.add(alias.asname or alias.name)

        def is_get_backend_call(call: ast.Call) -> bool:
            name = _dotted_name(call.func)
            if name in get_backend_aliases:
                return True
            if name in {f"{alias}.get_backend" for alias in package_aliases}:
                return True
            return name == "solstone.observe.transcribe.get_backend"

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                if is_get_backend_call(node.value):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            get_backend_results.add(target.id)
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and isinstance(node.value, ast.Call)
                and is_get_backend_call(node.value)
            ):
                get_backend_results.add(node.target.id)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in backend_function_aliases:
                violations.append(
                    f"{relative.as_posix()}:{node.lineno} direct backend "
                    f"transcribe() import bypasses the STT egress chokepoint"
                )
                continue
            if not isinstance(func, ast.Attribute) or func.attr != "transcribe":
                continue
            receiver = _dotted_name(func.value)
            if receiver in backend_aliases or receiver in get_backend_results:
                violations.append(
                    f"{relative.as_posix()}:{node.lineno} calls "
                    f"{receiver}.transcribe() outside the package dispatcher"
                )
                continue
            if isinstance(func.value, ast.Call) and is_get_backend_call(func.value):
                violations.append(
                    f"{relative.as_posix()}:{node.lineno} calls "
                    "get_backend(...).transcribe() outside the package dispatcher"
                )

    assert not violations, (
        "Raw-audio STT backend dispatch must pass through "
        "solstone.observe.transcribe.transcribe() so the deny-by-default "
        "confidential egress gate is the single chokepoint. Move dispatch "
        "through the package function instead of calling backend transcribe() "
        "directly:\n" + "\n".join(violations)
    )


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


def test_confidential_stt_attestation_failure_blocks_remote_audio_egress(
    tmp_path,
    monkeypatch,
):
    _empty_journal(tmp_path, monkeypatch)
    config = _confidential_config(provider_pins=False)
    _add_local_endpoint(config)
    _write_journal_config(tmp_path, config)
    establish = _install_failing_confidential_transport(monkeypatch)
    mocks = _install_stt_backend_mocks(
        monkeypatch,
        parakeet_result=[],
        confidential_result=None,
    )
    httpx_post = Mock(side_effect=AssertionError("audio egress attempted"))
    monkeypatch.setattr("httpx.post", httpx_post)

    from solstone.observe.transcribe import (
        BACKEND_REGISTRY,
        ConfidentialAudioEgressError,
        ConfidentialTranscribeDeferral,
        transcribe,
    )

    with pytest.raises(ConfidentialTranscribeDeferral) as confidential_exc:
        transcribe("confidential", _stt_audio(), 16000, {})
    assert confidential_exc.value.reason_code == "attestation_unreachable"

    with pytest.raises(ConfidentialAudioEgressError):
        transcribe("gemini", _stt_audio(), 16000, {})
    with pytest.raises(ConfidentialAudioEgressError):
        transcribe("revai", _stt_audio(), 16000, {})
    monkeypatch.setitem(
        BACKEND_REGISTRY,
        "future-remote",
        "solstone.observe.transcribe.gemini",
    )
    with pytest.raises(ConfidentialAudioEgressError):
        transcribe("future-remote", _stt_audio(), 16000, {})

    assert transcribe("parakeet", _stt_audio(), 16000, {}) == []
    mocks["gemini"].assert_not_called()
    mocks["revai"].assert_not_called()
    mocks["parakeet"].assert_called_once()
    httpx_post.assert_not_called()
    establish.assert_called_once()


def test_confidential_stt_stale_session_defers_before_egress(tmp_path, monkeypatch):
    from solstone.think.services import spp, spp_transport

    _empty_journal(tmp_path, monkeypatch)
    config = _confidential_config(provider_pins=False)
    _add_local_endpoint(config)
    _write_journal_config(tmp_path, config)
    spp_transport._LISTENER = _FakeListener()
    spp_transport._LISTENER_THREAD = _AliveThread()
    spp_transport._FORWARDER_BASE_URL = "http://127.0.0.1:4567"
    spp.record_attestation_verified(_stale_session(object()))
    mocks = _install_stt_backend_mocks(monkeypatch, confidential_result=None)
    httpx_post = Mock(side_effect=AssertionError("audio egress attempted"))
    monkeypatch.setattr("httpx.post", httpx_post)

    from solstone.observe.transcribe import ConfidentialTranscribeDeferral, transcribe

    with pytest.raises(ConfidentialTranscribeDeferral) as exc_info:
        transcribe("confidential", _stt_audio(), 16000, {})

    assert exc_info.value.reason_code == "attestation_stale"
    mocks["gemini"].assert_not_called()
    mocks["revai"].assert_not_called()
    mocks["parakeet"].assert_not_called()
    httpx_post.assert_not_called()


def test_confidential_stt_setting_off_gate_blocks_confidential_only(
    tmp_path,
    monkeypatch,
):
    _empty_journal(tmp_path, monkeypatch)
    config = _confidential_config(provider_pins=False)
    config["transcribe"] = {"confidential_audio": False}
    _add_local_endpoint(config)
    _write_journal_config(tmp_path, config)
    mocks = _install_stt_backend_mocks(monkeypatch, parakeet_result=[])
    httpx_post = Mock(side_effect=AssertionError("audio egress attempted"))
    monkeypatch.setattr("httpx.post", httpx_post)

    from solstone.observe.transcribe import (
        ConfidentialAudioEgressError,
        ConfidentialTranscribeDeferral,
        transcribe,
    )

    with pytest.raises(ConfidentialTranscribeDeferral) as exc_info:
        transcribe("confidential", _stt_audio(), 16000, {})
    assert exc_info.value.reason_code == "confidential_audio_disabled"
    with pytest.raises(ConfidentialAudioEgressError):
        transcribe("gemini", _stt_audio(), 16000, {})
    with pytest.raises(ConfidentialAudioEgressError):
        transcribe("revai", _stt_audio(), 16000, {})

    assert transcribe("parakeet", _stt_audio(), 16000, {}) == []
    mocks["confidential"].assert_not_called()
    mocks["gemini"].assert_not_called()
    mocks["revai"].assert_not_called()
    mocks["parakeet"].assert_called_once()
    httpx_post.assert_not_called()


def test_confidential_stt_lane_inactive_refuses_confidential_without_passthrough(
    tmp_path,
    monkeypatch,
):
    _empty_journal(tmp_path, monkeypatch)
    _write_journal_config(
        tmp_path,
        {"env": {"GOOGLE_API_KEY": "test-google-key", "REVAI_ACCESS_TOKEN": "revai"}},
    )
    mocks = _install_stt_backend_mocks(
        monkeypatch,
        gemini_result=["gemini-dispatched"],
        revai_result=["revai-dispatched"],
    )
    httpx_post = Mock(side_effect=AssertionError("passthrough URL posted to"))
    monkeypatch.setattr("httpx.post", httpx_post)

    from solstone.observe.transcribe import ConfidentialTranscribeDeferral, transcribe

    with pytest.raises(ConfidentialTranscribeDeferral) as exc_info:
        transcribe("confidential", _stt_audio(), 16000, {})
    assert exc_info.value.reason_code == "confidential_lane_inactive"
    assert transcribe("gemini", _stt_audio(), 16000, {}) == ["gemini-dispatched"]
    assert transcribe("revai", _stt_audio(), 16000, {}) == ["revai-dispatched"]

    mocks["confidential"].assert_not_called()
    mocks["gemini"].assert_called_once()
    mocks["revai"].assert_called_once()
    httpx_post.assert_not_called()


def test_confidential_stt_posts_only_to_verified_forwarder(tmp_path, monkeypatch):
    _empty_journal(tmp_path, monkeypatch)
    config = _confidential_config(provider_pins=False)
    _add_local_endpoint(config)
    _write_journal_config(tmp_path, config)
    _patch_confidential_listener(monkeypatch)

    from solstone.think.services import spp_transport

    establish = Mock(
        side_effect=lambda *_args, **kwargs: _FakeChannel(
            object(), epoch=kwargs["epoch"]
        )
    )
    monkeypatch.setattr(spp_transport, "establish_attested_channel", establish)
    captured: dict = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Mock(
            status_code=200,
            json=Mock(
                return_value={
                    "text": "hello.",
                    "words": [{"word": "hello.", "start": 0.0, "end": 0.5}],
                }
            ),
        )

    monkeypatch.setattr("httpx.post", fake_post)

    from solstone.observe.transcribe import transcribe

    statements = transcribe("confidential", _stt_audio(), 16000, {})

    assert statements
    assert captured["url"] == "http://127.0.0.1:4567/v1/audio/transcriptions"
    assert "spp.example.test" not in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer confidential-credential"
    assert captured["headers"]["x-sol-device"] == "fingerprint"
    establish.assert_called_once()


def test_confidential_stt_toggle_off_selection_is_immediate(tmp_path, monkeypatch):
    _empty_journal(tmp_path, monkeypatch)
    config = _confidential_config(provider_pins=False)
    _write_journal_config(tmp_path, config)
    transcribe_main = importlib.import_module("solstone.observe.transcribe.main")
    monkeypatch.setattr(transcribe_main, "read_available_bytes", lambda: 1 * 1024**3)
    monkeypatch.setattr(transcribe_main, "stt_local_floor_bytes", lambda: 4 * 1024**3)
    monkeypatch.setattr(transcribe_main, "local_stt_backend", lambda: "parakeet")
    from solstone.think.utils import get_config

    args = type("Args", (), {"backend": None})()
    assert (
        transcribe_main.resolve_default_backend(
            args, get_config().get("transcribe", {})
        )
        == "confidential"
    )

    config["transcribe"] = {"confidential_audio": False}
    _write_journal_config(tmp_path, config)

    assert (
        transcribe_main.resolve_default_backend(
            args, get_config().get("transcribe", {})
        )
        == "parakeet"
    )


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
