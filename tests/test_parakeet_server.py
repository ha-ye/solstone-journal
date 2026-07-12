# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from solstone.think import parakeet_readiness
from solstone.think.providers import parakeet_server


class _Response:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


def _install_fake_httpx(monkeypatch: pytest.MonkeyPatch, get):
    fake_httpx = types.SimpleNamespace(get=get)
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)
    return fake_httpx


def test_placement_record_round_trips_and_clears(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))

    assert parakeet_server.read_parakeet_placement() is None
    parakeet_server.clear_parakeet_placement()
    parakeet_server.write_parakeet_placement("gpu")
    assert parakeet_server.read_parakeet_placement() == "gpu"
    parakeet_server.write_parakeet_placement("cpu")
    assert parakeet_server.read_parakeet_placement() == "cpu"
    parakeet_server.clear_parakeet_placement()
    assert parakeet_server.read_parakeet_placement() is None


def test_placement_record_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    with pytest.raises(ValueError, match="invalid parakeet placement"):
        parakeet_server.write_parakeet_placement("vulkan")

    path = tmp_path / "journal" / "health" / "parakeet-cpp.placement"
    path.parent.mkdir(parents=True)
    path.write_text("vulkan")
    assert parakeet_server.read_parakeet_placement() is None


def test_no_port_probe_failed_and_connect_not_ready(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(parakeet_server, "read_service_port", lambda _service: None)

    assert parakeet_server.probe_state() == (parakeet_server.STATE_FAILED, "no port")
    with pytest.raises(parakeet_server.ParakeetServerNotReady) as exc_info:
        parakeet_server.connect()

    assert exc_info.value.reason_code == "parakeet_server_not_ready"
    assert exc_info.value.retry_reason == "no_port"


def test_connect_returns_info_when_health_ready(monkeypatch: pytest.MonkeyPatch):
    observed_urls = []

    def fake_get(url, timeout):
        observed_urls.append((url, timeout))
        return _Response(200, "{}")

    monkeypatch.setattr(parakeet_server, "read_service_port", lambda _service: 4567)
    _install_fake_httpx(monkeypatch, fake_get)

    info = parakeet_server.connect()

    assert observed_urls == [("http://127.0.0.1:4567/health", 1.0)]
    assert info == parakeet_server.ParakeetServerInfo(
        model_id=parakeet_readiness.PARAKEET_CPP_MODEL_FILENAME,
        port=4567,
        base_url="http://127.0.0.1:4567",
        state=parakeet_server.STATE_READY,
    )


@pytest.mark.parametrize(
    ("response", "expected_detail"),
    [
        (_Response(500, "broken"), "HTTP 500: broken"),
        (_Response(503, "loading model"), "HTTP 503: loading model"),
    ],
)
def test_non_200_health_is_failed_not_loading(
    monkeypatch: pytest.MonkeyPatch, response: _Response, expected_detail: str
):
    monkeypatch.setattr(parakeet_server, "read_service_port", lambda _service: 4567)
    _install_fake_httpx(monkeypatch, lambda _url, timeout: response)

    assert parakeet_server.probe_state() == (
        parakeet_server.STATE_FAILED,
        expected_detail,
    )
    with pytest.raises(parakeet_server.ParakeetServerNotReady) as exc_info:
        parakeet_server.connect()

    assert exc_info.value.reason_code == "parakeet_server_not_ready"
    assert exc_info.value.retry_reason == "server_not_ready"


@pytest.mark.parametrize("error", [ConnectionError("refused"), TimeoutError("slow")])
def test_connection_errors_are_not_ready(
    monkeypatch: pytest.MonkeyPatch, error: Exception
):
    def fake_get(_url, timeout):
        raise error

    monkeypatch.setattr(parakeet_server, "read_service_port", lambda _service: 4567)
    _install_fake_httpx(monkeypatch, fake_get)

    state, detail = parakeet_server.probe_state()
    assert state == parakeet_server.STATE_FAILED
    assert detail == str(error)
    with pytest.raises(parakeet_server.ParakeetServerNotReady) as exc_info:
        parakeet_server.connect()

    assert exc_info.value.retry_reason == "server_not_ready"
