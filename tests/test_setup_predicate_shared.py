# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from solstone.convey import create_app


def _write_iso_completed_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    journal = tmp_path / "journal"
    journal.mkdir()
    config_dir = journal / "config"
    config_dir.mkdir()
    (config_dir / "journal.json").write_text(
        json.dumps({"setup": {"completed_at": "2026-04-26T00:00:00Z"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    return journal


def test_access_gate_treats_iso_completed_at_as_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _write_iso_completed_journal(tmp_path, monkeypatch)
    app = create_app(str(journal))
    app.config["TESTING"] = True
    client = app.test_client()

    resp = client.get("/")

    assert resp.status_code == 302
    assert "/init" in resp.headers["Location"]


def test_secure_listener_treats_iso_completed_at_as_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from solstone.convey.secure_listener import runtime as rt
    from solstone.think.link.ca import load_or_generate_ca
    from solstone.think.link.paths import ca_dir

    previous_runtime = rt._runtime
    try:
        rt._runtime = None
        _write_iso_completed_journal(tmp_path, monkeypatch)
        load_or_generate_ca(ca_dir())
        app = SimpleNamespace(config={"SECURE_LISTENER_ENABLED": True})

        rt.start_secure_listener(app)

        assert rt._runtime is None
        assert not getattr(app, "secure_listener_started", False)
    finally:
        rt.stop_all_secure_listener()
        rt._runtime = previous_runtime
