# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

import pytest

from solstone.think.facets import create_facet, update_facet


def test_facet_json_atomic_failure_preserves_existing_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    slug = create_facet("Home Reno")
    facet_json = tmp_path / "facets" / slug / "facet.json"
    original = facet_json.read_bytes()

    def fail_replace(_src: str, _dst: str) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("solstone.think.journal_io.atomic.os.replace", fail_replace)

    with pytest.raises(OSError):
        update_facet(slug, title="New Home Reno")

    assert facet_json.read_bytes() == original
    assert list(facet_json.parent.glob(".tmp_*")) == []
