# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from solstone.think import start
from solstone.think.service import Reconciled


def _patch_marker(monkeypatch: pytest.MonkeyPatch, marker: Path) -> None:
    monkeypatch.setattr(start, "_version_marker_path", lambda: marker)


def test_start_reconcile_idempotent_no_rewrite(monkeypatch, tmp_path):
    marker = tmp_path / ".last-start-version"
    marker.write_text(f"{start.solstone.__version__}\n", encoding="utf-8")
    _patch_marker(monkeypatch, marker)
    reconcile = MagicMock(return_value=Reconciled(False, None, None, None))
    supervisor = MagicMock()
    monkeypatch.setattr(start, "reconcile_installed_unit", reconcile)
    monkeypatch.setattr("solstone.think.supervisor.main", supervisor)

    start.main()

    reconcile.assert_called_once_with()
    supervisor.assert_called_once_with()


def test_start_version_marker_mismatch_triggers_refresh(monkeypatch, tmp_path):
    marker = tmp_path / ".last-start-version"
    marker.write_text("old-version\n", encoding="utf-8")
    _patch_marker(monkeypatch, marker)
    calls: list[str] = []
    monkeypatch.setattr(
        start, "_install_current_wrappers", lambda: calls.append("wrappers")
    )
    monkeypatch.setattr(
        start,
        "reconcile_installed_unit",
        lambda: calls.append("reconcile") or Reconciled(False, None, None, None),
    )
    monkeypatch.setattr(start, "_refresh_skill_links", lambda: calls.append("skills"))

    start._refresh_for_version_marker()

    assert calls == ["wrappers", "reconcile", "skills"]
    assert marker.read_text(encoding="utf-8") == f"{start.solstone.__version__}\n"


def test_start_version_marker_match_is_noop(monkeypatch, tmp_path):
    marker = tmp_path / ".last-start-version"
    marker.write_text(f"{start.solstone.__version__}\n", encoding="utf-8")
    _patch_marker(monkeypatch, marker)
    monkeypatch.setattr(
        start,
        "_install_current_wrappers",
        lambda: pytest.fail("wrappers should not refresh"),
    )
    monkeypatch.setattr(
        start,
        "reconcile_installed_unit",
        lambda: pytest.fail("reconcile should not refresh"),
    )
    monkeypatch.setattr(
        start,
        "_refresh_skill_links",
        lambda: pytest.fail("skills should not refresh"),
    )

    start._refresh_for_version_marker()


def test_start_invokes_supervisor(monkeypatch, tmp_path):
    marker = tmp_path / ".last-start-version"
    marker.write_text(f"{start.solstone.__version__}\n", encoding="utf-8")
    _patch_marker(monkeypatch, marker)
    monkeypatch.setattr(
        start,
        "reconcile_installed_unit",
        lambda: Reconciled(False, None, None, None),
    )
    supervisor = MagicMock()
    monkeypatch.setattr("solstone.think.supervisor.main", supervisor)

    start.main()

    supervisor.assert_called_once_with()


def test_start_reconcile_failure_exits_nonzero(monkeypatch, tmp_path):
    marker = tmp_path / ".last-start-version"
    marker.write_text(f"{start.solstone.__version__}\n", encoding="utf-8")
    _patch_marker(monkeypatch, marker)
    monkeypatch.setattr(
        start,
        "reconcile_installed_unit",
        MagicMock(side_effect=OSError("boom")),
    )

    with pytest.raises(SystemExit) as exc_info:
        start.main()

    assert exc_info.value.code == 1


def test_start_skill_refresh_error_exits_nonzero(monkeypatch, tmp_path):
    marker = tmp_path / ".last-start-version"
    marker.write_text("old-version\n", encoding="utf-8")
    _patch_marker(monkeypatch, marker)
    monkeypatch.setattr(
        start,
        "reconcile_installed_unit",
        lambda: Reconciled(False, None, None, None),
    )
    monkeypatch.setattr(start, "_install_current_wrappers", lambda: None)
    monkeypatch.setattr(
        start,
        "_refresh_skill_links",
        MagicMock(side_effect=RuntimeError("skill refresh failed")),
    )

    with pytest.raises(SystemExit) as exc_info:
        start.main()

    assert exc_info.value.code == 1
    assert marker.read_text(encoding="utf-8") == "old-version\n"
