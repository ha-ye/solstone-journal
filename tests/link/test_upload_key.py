# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from solstone.think.link.paths import upload_private_key_path
from solstone.think.link.upload_key import (
    load_or_generate_upload_key,
    load_upload_key,
)


def test_load_upload_key_raises_when_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))

    with pytest.raises(FileNotFoundError):
        load_upload_key()


def test_load_or_generate_upload_key_mints_once_with_private_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))

    first = load_or_generate_upload_key()
    path = upload_private_key_path()
    second = load_or_generate_upload_key()
    loaded = load_upload_key()

    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert second.public_spki_der == first.public_spki_der
    assert loaded.public_spki_der == first.public_spki_der
    assert (
        second.private_key.private_numbers().private_value
        == first.private_key.private_numbers().private_value
    )
