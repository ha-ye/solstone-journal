# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from solstone.think.models import AttestationStaleError
from solstone.think.services import spp, spp_transport
from solstone.think.services.spp_attest.cadence import AttestationSession


class _FakeChannel:
    def __init__(
        self,
        verdict: object,
        last_used_monotonic: float | None = None,
        epoch: int | None = None,
    ) -> None:
        self.verdict = verdict
        self.tls = object()
        self.last_used_monotonic = (
            time.monotonic() if last_used_monotonic is None else last_used_monotonic
        )
        self.epoch = spp_transport._EPOCH if epoch is None else epoch
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeLocal:
    def __init__(self) -> None:
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


@pytest.fixture(autouse=True)
def _clear_transport_state():
    spp.clear_attestation_state()
    spp_transport.teardown_confidential_transport()
    yield
    spp.clear_attestation_state()
    spp_transport.teardown_confidential_transport()


def _write_confidential_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    config = {
        "services": {
            "confidential": {
                "enabled_at": "2026-05-24T00:00:00Z",
                "account_id": "acct-test",
                "endpoint_url": "https://spp.example.test:9443",
                "served_model_id": "confidential-model",
                "credential_created_at": "2026-05-24T00:00:00Z",
                "credential_fingerprint_sha256": "fingerprint",
                "prior_generate_provider": "google",
                "prior_cogitate_provider": "openai",
                "prior_local_endpoint": None,
            }
        }
    }
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "journal.json").write_text(json.dumps(config), encoding="utf-8")
    return config["services"]["confidential"]


def _patch_listener(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_verify_confidential_attestation_reuses_then_raises_stale_once_then_reestablishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    block = _write_confidential_config(tmp_path, monkeypatch)
    _patch_listener(monkeypatch)
    verdict = object()

    def fake_establish(*_args, **kwargs):
        return _FakeChannel(verdict, epoch=kwargs["epoch"])

    establish = Mock(side_effect=fake_establish)
    monkeypatch.setattr(spp_transport, "establish_attested_channel", establish)

    spp_transport.verify_confidential_attestation(block)
    spp_transport.verify_confidential_attestation(block)

    assert establish.call_count == 1
    assert spp.get_attestation_state().session is not None

    spp.record_attestation_verified(_stale_session(verdict))
    with pytest.raises(AttestationStaleError):
        spp_transport.verify_confidential_attestation(block)

    state = spp.get_attestation_state()
    assert state.session is not None
    assert state.session.status(datetime.now(timezone.utc)) == "stale"
    assert establish.call_count == 1

    spp_transport.verify_confidential_attestation(block)

    assert establish.call_count == 2
    assert spp.get_attestation_state().session is not None
    assert (
        spp.get_attestation_state().session.status(datetime.now(timezone.utc))
        == "verified"
    )


def test_confidential_egress_base_url_returns_forwarder_not_configured_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    block = _write_confidential_config(tmp_path, monkeypatch)
    _patch_listener(monkeypatch)

    def fake_establish(*_args, **kwargs):
        return _FakeChannel(object(), epoch=kwargs["epoch"])

    establish = Mock(side_effect=fake_establish)
    monkeypatch.setattr(spp_transport, "establish_attested_channel", establish)

    base_url = spp_transport.confidential_egress_base_url(block["endpoint_url"])

    assert base_url == "http://127.0.0.1:4567"
    assert base_url != block["endpoint_url"]
    assert establish.call_args.args[0].host == "spp.example.test"
    assert establish.call_args.args[0].port == 9443


def test_confidential_probe_status_reads_state_without_attestation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_confidential_config(tmp_path, monkeypatch)
    establish = Mock(side_effect=AssertionError("attestation ceremony attempted"))
    monkeypatch.setattr(spp_transport, "establish_attested_channel", establish)

    assert spp_transport.confidential_probe_status() == (
        False,
        "attestation_not_yet_verified",
    )

    now = datetime.now(timezone.utc)
    spp.record_attestation_verified(
        AttestationSession(
            verdict=object(),
            started_at=now,
            tpm_heartbeat_at=now,
            gpu_reattest_at=now,
        )
    )
    assert spp_transport.confidential_probe_status(now) == (True, None)

    spp.record_attestation_verified(_stale_session(object()))
    assert spp_transport.confidential_probe_status() == (False, "attestation_stale")

    spp.record_attestation_failed("gateway_unreachable")
    assert spp_transport.confidential_probe_status() == (False, "attestation_failed")
    establish.assert_not_called()


def test_pooled_channels_older_than_idle_limit_are_discarded() -> None:
    stale = _FakeChannel(object(), last_used_monotonic=0.0)
    fresh = _FakeChannel(object(), last_used_monotonic=119.0)
    spp_transport._POOL[:] = [stale, fresh]

    spp_transport._discard_idle_locked(121.0)

    assert stale.closed is True
    assert fresh.closed is False
    assert spp_transport._POOL == [fresh]


def test_teardown_closes_checked_out_channel_and_prevents_repool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_listener(monkeypatch)
    verdict = object()
    now = datetime.now(timezone.utc)
    spp.record_attestation_verified(
        AttestationSession(
            verdict=verdict,
            started_at=now,
            tpm_heartbeat_at=now,
            gpu_reattest_at=now,
        )
    )
    channel = _FakeChannel(verdict)
    spp_transport._POOL[:] = [channel]
    local = _FakeLocal()

    def fake_pump(_local, _tls):
        assert channel in spp_transport._ACTIVE
        with spp_transport._LOCK:
            spp_transport._teardown_locked()
        return "local_closed"

    monkeypatch.setattr(spp_transport, "_pump", fake_pump)

    spp_transport._handle_loopback_connection(local)

    assert local.closed is True
    assert channel.closed is True
    assert channel not in spp_transport._POOL
    assert channel not in spp_transport._ACTIVE


def test_wrong_epoch_channel_is_not_checked_out() -> None:
    stale_epoch_channel = _FakeChannel(object(), epoch=spp_transport._EPOCH - 1)
    spp_transport._POOL[:] = [stale_epoch_channel]

    assert spp_transport._borrow_channel_locked(time.monotonic()) is None
    assert stale_epoch_channel.closed is True
    assert spp_transport._POOL == []
