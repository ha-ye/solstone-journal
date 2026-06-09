# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from solstone.think import start
from solstone.think.app_supervised import FLAG, SELECTOR_ENV
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


@pytest.mark.parametrize("selector", ["flag", "env"])
def test_app_supervised_start_skips_reconcile_but_refreshes_version_marker_artifacts(
    selector, monkeypatch, tmp_path
):
    marker = tmp_path / ".last-start-version"
    marker.write_text("old-version\n", encoding="utf-8")
    _patch_marker(monkeypatch, marker)
    monkeypatch.delenv(SELECTOR_ENV, raising=False)
    argv = ["journal", "start"]
    if selector == "flag":
        argv.append(FLAG)
    else:
        monkeypatch.setenv(SELECTOR_ENV, "1")
    monkeypatch.setattr(sys, "argv", argv)

    calls: list[str] = []
    reconcile = MagicMock(return_value=Reconciled(False, None, None, None))
    supervisor = MagicMock()
    monkeypatch.setattr(start, "reconcile_installed_unit", reconcile)
    monkeypatch.setattr(
        start, "_install_current_wrappers", lambda: calls.append("wrappers")
    )
    monkeypatch.setattr(start, "_refresh_skill_links", lambda: calls.append("skills"))
    monkeypatch.setattr("solstone.think.supervisor.main", supervisor)

    start.main()

    reconcile.assert_not_called()
    assert calls == ["wrappers", "skills"]
    assert marker.read_text(encoding="utf-8") == f"{start.solstone.__version__}\n"
    supervisor.assert_called_once_with()


def test_default_start_reconciles_both_sites_with_stale_marker(monkeypatch, tmp_path):
    marker = tmp_path / ".last-start-version"
    marker.write_text("old-version\n", encoding="utf-8")
    _patch_marker(monkeypatch, marker)
    monkeypatch.delenv(SELECTOR_ENV, raising=False)
    monkeypatch.setattr(sys, "argv", ["journal", "start"])

    reconcile = MagicMock(return_value=Reconciled(False, None, None, None))
    supervisor = MagicMock()
    monkeypatch.setattr(start, "reconcile_installed_unit", reconcile)
    monkeypatch.setattr(start, "_install_current_wrappers", lambda: None)
    monkeypatch.setattr(start, "_refresh_skill_links", lambda: None)
    monkeypatch.setattr("solstone.think.supervisor.main", supervisor)

    start.main()

    assert reconcile.call_count == 2
    supervisor.assert_called_once_with()


@pytest.mark.skipif(sys.platform != "linux", reason="linux reconcile regression")
def test_default_start_reconciles_both_sites_with_stale_marker_on_linux(
    monkeypatch, tmp_path
):
    marker = tmp_path / ".last-start-version"
    marker.write_text("old-version\n", encoding="utf-8")
    _patch_marker(monkeypatch, marker)
    monkeypatch.delenv(SELECTOR_ENV, raising=False)
    monkeypatch.setattr(sys, "argv", ["journal", "start"])

    reconcile = MagicMock(return_value=Reconciled(False, None, None, None))
    monkeypatch.setattr(start, "reconcile_installed_unit", reconcile)
    monkeypatch.setattr(start, "_install_current_wrappers", lambda: None)
    monkeypatch.setattr(start, "_refresh_skill_links", lambda: None)
    monkeypatch.setattr("solstone.think.supervisor.main", MagicMock())

    start.main()

    assert reconcile.call_count == 2


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
