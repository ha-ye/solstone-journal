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
    errors: list[Exception] = []
    malformed: list[object] = []
    done = threading.Event()

    def reader() -> None:
        while not done.is_set():
            try:
                with path.open(encoding="utf-8") as handle:
                    payload = json.load(handle)
            except json.JSONDecodeError as exc:
                errors.append(exc)
                continue
            value = payload.get("thinking_surfaces")
            if value not in {"always", "on_tap", "never"}:
                malformed.append(value)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for index in range(200):
            save_chat_config({"thinking_surfaces": "always" if index % 2 else "on_tap"})
    finally:
        done.set()
        thread.join(timeout=5)

    assert errors == []
    assert malformed == []
