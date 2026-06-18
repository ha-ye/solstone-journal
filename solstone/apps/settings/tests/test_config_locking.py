# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import threading
from pathlib import Path

from solstone.convey import create_app
from solstone.think.utils import get_config


def _client(journal_path: Path):
    app = create_app(str(journal_path))
    app.config["TESTING"] = True
    return app.test_client()


def test_settings_config_routes_serialize_concurrent_writes(settings_env, monkeypatch):
    journal_path, _config = settings_env(
        {
            "setup": {"completed_at": "2026-05-09T00:00:00Z"},
            "describe": {"max_extractions": 10},
            "transcribe": {"backend": "parakeet", "enrich": True},
        }
    )
    config_path = journal_path / "config" / "journal.json"
    client_a = _client(journal_path)
    client_b = _client(journal_path)

    import solstone.think.journal_io.atomic as atomic_module

    real_replace = atomic_module.os.replace
    gate_lock = threading.Lock()
    replace_count = 0
    first_in_replace = threading.Event()
    second_replaced = threading.Event()
    release_first = threading.Event()

    def gated_replace(src, dst):
        nonlocal replace_count
        if Path(dst) != config_path:
            return real_replace(src, dst)

        with gate_lock:
            replace_count += 1
            current_replace = replace_count

        if current_replace == 1:
            first_in_replace.set()
            if not release_first.wait(timeout=10):
                raise TimeoutError("timed out waiting to release first config replace")
            return real_replace(src, dst)

        if current_replace == 2:
            real_replace(src, dst)
            second_replaced.set()
            return None

        return real_replace(src, dst)

    monkeypatch.setattr(atomic_module.os, "replace", gated_replace)

    errors: list[Exception] = []

    def update_vision() -> None:
        try:
            response = client_a.put(
                "/app/settings/api/vision",
                json={"max_extractions": 42},
            )
            assert response.status_code == 200
        except Exception as exc:  # pragma: no cover - surfaced by assertion below
            errors.append(exc)

    def update_transcribe() -> None:
        try:
            response = client_b.put(
                "/app/settings/api/config",
                json={
                    "section": "transcribe",
                    "data": {"backend": "whisper"},
                },
            )
            assert response.status_code == 200
        except Exception as exc:  # pragma: no cover - surfaced by assertion below
            errors.append(exc)

    thread_a = threading.Thread(target=update_vision)
    thread_b = threading.Thread(target=update_transcribe)
    thread_b_started = False

    thread_a.start()
    try:
        assert first_in_replace.wait(timeout=5)
        thread_b.start()
        thread_b_started = True
        second_replaced.wait(timeout=2)
        release_first.set()
        thread_a.join(timeout=10)
        thread_b.join(timeout=10)
    finally:
        release_first.set()
        thread_a.join(timeout=10)
        if thread_b_started:
            thread_b.join(timeout=10)

    assert errors == []
    assert not thread_a.is_alive()
    assert not thread_b.is_alive()

    final_config = get_config()
    assert final_config["describe"]["max_extractions"] == 42
    assert final_config["transcribe"]["backend"] == "whisper"
