# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from solstone.apps.chat.config import load_chat_config, save_chat_config


def _journal(tmp_path: Path, monkeypatch) -> Path:
    journal = tmp_path / "journal"
    journal.mkdir()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    return journal


def test_load_chat_config_defaults_when_file_missing(tmp_path, monkeypatch):
    _journal(tmp_path, monkeypatch)

    assert load_chat_config()["thinking_surfaces"] == "on_tap"


def test_save_chat_config_round_trips_and_preserves_other_keys(tmp_path, monkeypatch):
    journal = _journal(tmp_path, monkeypatch)
    path = journal / "config" / "chat.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "thinking_surfaces": "on_tap",
                "other": {"kept": True},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    saved = save_chat_config({"thinking_surfaces": "always"})

    assert saved["thinking_surfaces"] == "always"
    assert load_chat_config()["thinking_surfaces"] == "always"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "thinking_surfaces": "always",
        "other": {"kept": True},
    }


def test_load_chat_config_malformed_value_warns_and_defaults(
    tmp_path, monkeypatch, caplog
):
    journal = _journal(tmp_path, monkeypatch)
    path = journal / "config" / "chat.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"thinking_surfaces": "bogus"}\n', encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="solstone.apps.chat.config"):
        config = load_chat_config()

    assert config["thinking_surfaces"] == "on_tap"
    assert "invalid chat thinking_surfaces value" in caplog.text


def test_save_chat_config_invalid_update_warns_and_preserves_existing(
    tmp_path, monkeypatch, caplog
):
    _journal(tmp_path, monkeypatch)
    save_chat_config({"thinking_surfaces": "always"})

    with caplog.at_level(logging.WARNING, logger="solstone.apps.chat.config"):
        saved = save_chat_config({"thinking_surfaces": "bogus"})

    assert saved["thinking_surfaces"] == "always"
    assert "dropping invalid chat thinking_surfaces value" in caplog.text


def test_save_chat_config_atomic_under_concurrent_reads(tmp_path, monkeypatch):
    journal = _journal(tmp_path, monkeypatch)
    path = journal / "config" / "chat.json"
    save_chat_config({"thinking_surfaces": "on_tap"})

    import solstone.think.journal_io.atomic as atomic_module

    real_replace = atomic_module.os.replace
    replace_ready = threading.Event()
    allow_replace = threading.Event()

    def gated_replace(src, dst):
        if Path(dst) == path:
            replace_ready.set()
            if not allow_replace.wait(timeout=5):
                raise TimeoutError("timed out waiting to release chat config replace")
        return real_replace(src, dst)

    monkeypatch.setattr(atomic_module.os, "replace", gated_replace)

    errors: list[Exception] = []

    def writer() -> None:
        try:
            save_chat_config({"thinking_surfaces": "always"})
        except Exception as exc:  # pragma: no cover - surfaced by assertion below
            errors.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert replace_ready.wait(timeout=5)
        for _ in range(100):
            with path.open(encoding="utf-8") as handle:
                assert json.load(handle)["thinking_surfaces"] == "on_tap"
        allow_replace.set()
        thread.join(timeout=5)
    finally:
        allow_replace.set()
        thread.join(timeout=5)

    assert errors == []
    assert not thread.is_alive()
    assert load_chat_config()["thinking_surfaces"] == "always"
