# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json

from solstone.apps.settings import routes, transcribe_resource
from solstone.apps.settings.install_copy import (
    STT_DETECTED_MEMORY_UNKNOWN,
    STT_LOCAL_UNSUPPORTED,
    STT_NO_LOCAL_STT_RECOVERY,
)
from solstone.convey import create_app

RESOURCE_KEYS = {
    "min_ram_gb",
    "available_memory_gb",
    "requirement",
    "detected",
    "needs_setup",
    "notice",
}


def _payload(
    monkeypatch,
    *,
    available_bytes,
    floor_bytes,
    configured,
    confidential=False,
    confidential_audio=True,
):
    monkeypatch.setattr(
        transcribe_resource, "read_available_bytes", lambda: available_bytes
    )
    monkeypatch.setattr(
        transcribe_resource, "stt_local_floor_bytes", lambda: floor_bytes
    )
    monkeypatch.setattr(transcribe_resource, "local_stt_backend", lambda: "parakeet")
    return transcribe_resource.get_transcribe_resource_payload(
        configured_backend=configured,
        confidential_lane_active=confidential,
        confidential_audio=confidential_audio,
    )


def _client(journal_path):
    app = create_app(str(journal_path))
    app.config["TESTING"] = True
    return app.test_client()


def _ready_journal(settings_env):
    journal_path, config = settings_env()
    config["setup"] = {"completed_at": 1700000000000}
    (journal_path / "config" / "journal.json").write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )
    return journal_path


def test_transcribe_resource_payload_shape(monkeypatch):
    payload = _payload(
        monkeypatch,
        available_bytes=8 * 1024**3,
        floor_bytes=4 * 1024**3,
        configured=None,
        confidential=False,
    )

    assert set(payload) == RESOURCE_KEYS
    assert payload["min_ram_gb"] == 4
    assert payload["available_memory_gb"] == 8.0
    assert payload["needs_setup"] is False
    assert payload["notice"] == ""


def test_transcribe_resource_unknown_memory(monkeypatch):
    payload = _payload(
        monkeypatch,
        available_bytes=None,
        floor_bytes=4 * 1024**3,
        configured=None,
        confidential=False,
    )

    assert payload["available_memory_gb"] is None
    assert payload["detected"] == STT_DETECTED_MEMORY_UNKNOWN
    assert payload["needs_setup"] is True
    assert payload["notice"] == STT_NO_LOCAL_STT_RECOVERY


def test_transcribe_resource_below_floor_surfaces_local_setup(monkeypatch):
    payload = _payload(
        monkeypatch,
        available_bytes=2 * 1024**3,
        floor_bytes=4 * 1024**3,
        configured=None,
        confidential=False,
    )

    assert payload["needs_setup"] is True
    assert payload["notice"] == STT_NO_LOCAL_STT_RECOVERY


def test_transcribe_resource_configured_backend_has_no_auto_flags(monkeypatch):
    payload = _payload(
        monkeypatch,
        available_bytes=2 * 1024**3,
        floor_bytes=4 * 1024**3,
        configured="parakeet",
        confidential=False,
    )

    assert payload["needs_setup"] is False
    assert payload["notice"] == ""


def test_transcribe_resource_unsupported_platform(monkeypatch):
    payload = _payload(
        monkeypatch,
        available_bytes=8 * 1024**3,
        floor_bytes=None,
        configured=None,
        confidential=False,
    )

    assert payload["min_ram_gb"] is None
    assert payload["requirement"] == STT_LOCAL_UNSUPPORTED
    assert payload["needs_setup"] is True
    assert payload["notice"] == STT_NO_LOCAL_STT_RECOVERY


def test_transcribe_resource_confidential_low_memory_stays_local(monkeypatch):
    payload = _payload(
        monkeypatch,
        available_bytes=2 * 1024**3,
        floor_bytes=4 * 1024**3,
        configured=None,
        confidential=True,
    )

    assert payload["needs_setup"] is False
    assert payload["notice"] == ""


def test_transcribe_resource_confidential_disabled_low_memory_uses_local(monkeypatch):
    payload = _payload(
        monkeypatch,
        available_bytes=2 * 1024**3,
        floor_bytes=4 * 1024**3,
        configured=None,
        confidential=True,
        confidential_audio=False,
    )

    assert payload["needs_setup"] is False
    assert payload["notice"] == ""


def test_transcribe_route_includes_resource_block(settings_env, monkeypatch):
    journal_path = _ready_journal(settings_env)
    monkeypatch.setattr(
        routes.transcribe_resource,
        "get_transcribe_resource_payload",
        lambda **_kwargs: transcribe_resource.fallback_transcribe_resource_payload(),
    )
    client = _client(journal_path)

    response = client.get("/app/settings/api/transcribe")

    assert response.status_code == 200
    payload = response.get_json()
    assert set(payload["resource"]) == RESOURCE_KEYS


def test_transcribe_route_passes_confidential_lane_flag(settings_env, monkeypatch):
    journal_path, config = settings_env()
    config["setup"] = {"completed_at": 1700000000000}
    config.setdefault("services", {})["confidential"] = {
        "enabled_at": "2026-05-24T00:00:00Z"
    }
    config.setdefault("providers", {})["local"] = {
        "endpoint_url": "https://spp.example.test/v1",
        "served_model_id": "confidential-model",
        "credential": "confidential-credential",
    }
    (journal_path / "config" / "journal.json").write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )
    captured = {}

    def capture_payload(**kwargs):
        captured.update(kwargs)
        return transcribe_resource.fallback_transcribe_resource_payload()

    monkeypatch.setattr(
        routes.transcribe_resource,
        "get_transcribe_resource_payload",
        capture_payload,
    )
    client = _client(journal_path)

    response = client.get("/app/settings/api/transcribe")

    assert response.status_code == 200
    assert captured["confidential_lane_active"] is True
    assert captured["confidential_audio"] is True


def test_transcribe_route_uses_resource_fallback_on_assembly_error(
    settings_env, monkeypatch
):
    journal_path = _ready_journal(settings_env)

    def raise_error(**_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        routes.transcribe_resource,
        "get_transcribe_resource_payload",
        raise_error,
    )
    client = _client(journal_path)

    response = client.get("/app/settings/api/transcribe")

    assert response.status_code == 200
    assert response.get_json()["resource"] == (
        transcribe_resource.fallback_transcribe_resource_payload()
    )
