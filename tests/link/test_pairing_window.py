# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

import pytest

from solstone.think.link import window
from solstone.think.link.nonces import NONCE_TTL_SECONDS, NonceStore
from solstone.think.link.paths import nonces_path
from solstone.think.link.window import read_posture, window_open
from tests.link.certless_helpers import write_config


def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    journal = tmp_path / "journal"
    journal.mkdir()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    return journal


def test_read_posture_exact_match_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _journal(tmp_path, monkeypatch)

    write_config(journal)
    assert read_posture() == "direct"

    for link_cfg in (
        {},
        {"posture": 123},
        {"posture": "relay"},
        {"posture": "spl "},
    ):
        write_config(journal, link=link_cfg)
        assert read_posture() == "direct"

    write_config(journal, link={"posture": "spl"})
    assert read_posture() == "spl"


@pytest.mark.parametrize("posture", ["spl", "direct"])
def test_window_open_requires_live_unused_nonce_in_any_posture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    posture: str,
) -> None:
    journal = _journal(tmp_path, monkeypatch)
    write_config(journal, link={"posture": posture})
    store = NonceStore(nonces_path())

    assert window_open(now=1000) is False

    store.add("live", "phone", now=1000)
    assert window_open(now=1000 + NONCE_TTL_SECONDS - 1) is True

    store.consume("live", now=1001)
    assert window_open(now=1002) is False

    store.add("expired", "phone", now=2000)
    assert window_open(now=2000 + NONCE_TTL_SECONDS) is False


def test_window_open_in_direct_posture_with_live_nonce(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _journal(tmp_path, monkeypatch)
    write_config(journal, link={"posture": "direct"})
    NonceStore(nonces_path()).add("live", "phone", now=1000)

    assert window_open(now=1001) is True


@pytest.mark.parametrize("posture", ["spl", "direct"])
def test_window_open_fail_closed_on_corrupt_nonce_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    posture: str,
) -> None:
    journal = _journal(tmp_path, monkeypatch)
    write_config(journal, link={"posture": posture})
    path = nonces_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert window_open(now=1000) is False


@pytest.mark.parametrize("posture", ["spl", "direct"])
def test_window_open_fail_closed_on_read_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    posture: str,
) -> None:
    journal = _journal(tmp_path, monkeypatch)
    write_config(journal, link={"posture": posture})

    class BrokenNonceStore:
        def __init__(self, _path: Path) -> None:
            pass

        def snapshot(self) -> list[object]:
            raise OSError("nope")

    monkeypatch.setattr(window, "NonceStore", BrokenNonceStore)

    assert window_open(now=1000) is False
    assert "cert-less pairing window read failed" in caplog.text
