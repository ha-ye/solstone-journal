# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json

from solstone.apps.settings import routes, transcribe_resource
from solstone.apps.settings.install_copy import (
    STT_AUTO_SWITCH_NOTICE,
    STT_DETECTED_MEMORY_UNKNOWN,
    STT_FORCE_LOCAL_HINT,
    STT_LOCAL_UNSUPPORTED,
    STT_NO_KEY_RECOVERY,
)
from solstone.convey import create_app

RESOURCE_KEYS = {
    "min_ram_gb",
    "available_memory_gb",
    "requirement",
    "detected",
    "auto_switched",
    "needs_setup",
    "notice",
    "force_local_hint",
}


def _payload(monkeypatch, *, available_bytes, floor_bytes, google_key, configured):
    monkeypatch.setattr(
        transcribe_resource, "read_available_bytes", lambda: available_bytes
    )
    monkeypatch.setattr(
        transcribe_resource, "stt_local_floor_bytes", lambda: floor_bytes
    )
    monkeypatch.setattr(transcribe_resource, "local_stt_backend", lambda: "parakeet")
    return transcribe_resource.get_transcribe_resource_payload(
        google_key_present=google_key,
        configured_backend=configured,
    )


def _client(journal_path):
    app = create_app(str(journal_path))
    app.config["TESTING"] = True
    return app.test_client()


def _ready_journal(settings_env):
    journal_path, config = settings_env()
    config["setup"] = {"completed_at": "2026-05-23T00:00:00Z"}
    config.setdefault("convey", {})["trust_localhost"] = True
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
        google_key=False,
        configured=None,
    )

    assert set(payload) == RESOURCE_KEYS
    assert payload["min_ram_gb"] == 4
    assert payload["available_memory_gb"] == 8.0
    assert payload["auto_switched"] is False
    assert payload["needs_setup"] is False


def test_transcribe_resource_unknown_memory(monkeypatch):
    payload = _payload(
        monkeypatch,
        available_bytes=None,
        floor_bytes=4 * 1024**3,
        google_key=True,
        configured=None,
    )

    assert payload["available_memory_gb"] is None
    assert payload["detected"] == STT_DETECTED_MEMORY_UNKNOWN
    assert payload["auto_switched"] is True


def test_transcribe_resource_auto_switch_notice(monkeypatch):
    payload = _payload(
        monkeypatch,
        available_bytes=2 * 1024**3,
        floor_bytes=4 * 1024**3,
        google_key=True,
        configured=None,
    )

    assert payload["auto_switched"] is True
    assert payload["needs_setup"] is False
    assert payload["notice"] == STT_AUTO_SWITCH_NOTICE
    assert payload["force_local_hint"] == STT_FORCE_LOCAL_HINT


def test_transcribe_resource_no_key_recovery(monkeypatch):
    payload = _payload(
        monkeypatch,
        available_bytes=2 * 1024**3,
        floor_bytes=4 * 1024**3,
        google_key=False,
        configured=None,
    )

    assert payload["auto_switched"] is False
    assert payload["needs_setup"] is True
    assert payload["notice"] == STT_NO_KEY_RECOVERY
    assert payload["force_local_hint"] == ""


def test_transcribe_resource_configured_backend_has_no_auto_flags(monkeypatch):
    payload = _payload(
        monkeypatch,
        available_bytes=2 * 1024**3,
        floor_bytes=4 * 1024**3,
        google_key=True,
        configured="parakeet",
    )

    assert payload["auto_switched"] is False
    assert payload["needs_setup"] is False
    assert payload["notice"] == ""
    assert payload["force_local_hint"] == ""


def test_transcribe_resource_unsupported_platform(monkeypatch):
    payload = _payload(
        monkeypatch,
        available_bytes=8 * 1024**3,
        floor_bytes=None,
        google_key=False,
        configured=None,
    )

    assert payload["min_ram_gb"] is None
    assert payload["requirement"] == STT_LOCAL_UNSUPPORTED
    assert payload["needs_setup"] is True


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
