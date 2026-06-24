# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from solstone.think import display_powersave as dp
from solstone.think.processing import DISPLAY_POWERSAVE_UNAVAILABLE


@pytest.fixture(autouse=True)
def reset_display_monitor():
    dp.reset_display_powersave_monitor()
    yield
    dp.reset_display_powersave_monitor()


def _connector(
    root: Path,
    name: str,
    *,
    status: str | None,
    enabled: str | None,
    dpms: str | None,
) -> Path:
    path = root / name
    path.mkdir(parents=True)
    for attr_name, value in {
        "status": status,
        "enabled": enabled,
        "dpms": dpms,
    }.items():
        if value is not None:
            (path / attr_name).write_text(value, encoding="utf-8")
    return path


def _read_linux(root: Path):
    return dp.read_display_power(platform="linux", sysfs_root=root)


def _reader(state: str, reason: str | None = None):
    return lambda: dp.DisplayReading(state, reason)


def test_read_linux_returns_awake_when_any_considered_connector_is_on(
    tmp_path: Path,
) -> None:
    _connector(
        tmp_path,
        "card1-HDMI-A-2",
        status="connected",
        enabled="enabled",
        dpms="On",
    )

    assert _read_linux(tmp_path) == dp.DisplayReading(dp.DISPLAY_AWAKE, None)


@pytest.mark.parametrize("dpms", ["Off", "Standby", "Suspend"])
def test_read_linux_returns_asleep_when_all_considered_connectors_are_off_states(
    tmp_path: Path,
    dpms: str,
) -> None:
    _connector(
        tmp_path,
        "card1-HDMI-A-2",
        status="connected",
        enabled="enabled",
        dpms=dpms,
    )

    assert _read_linux(tmp_path) == dp.DisplayReading(dp.DISPLAY_ASLEEP, None)


def test_read_linux_ignores_disconnected_and_disabled_stale_dpms(
    tmp_path: Path,
) -> None:
    _connector(
        tmp_path,
        "card1-HDMI-A-2",
        status="disconnected",
        enabled="enabled",
        dpms="On",
    )
    _connector(
        tmp_path,
        "card1-DP-1",
        status="connected",
        enabled="disabled",
        dpms="On",
    )

    assert _read_linux(tmp_path) == dp.DisplayReading(
        dp.DISPLAY_UNDETECTABLE,
        dp.REASON_HEADLESS,
    )


def test_read_linux_mixed_on_and_off_returns_awake(tmp_path: Path) -> None:
    _connector(
        tmp_path,
        "card1-HDMI-A-2",
        status="connected",
        enabled="enabled",
        dpms="Off",
    )
    _connector(
        tmp_path,
        "card1-DP-1",
        status="connected",
        enabled="enabled",
        dpms="On",
    )

    assert _read_linux(tmp_path) == dp.DisplayReading(dp.DISPLAY_AWAKE, None)


def test_read_linux_returns_unreadable_for_missing_or_unknown_considered_dpms(
    tmp_path: Path,
) -> None:
    _connector(
        tmp_path,
        "card1-HDMI-A-2",
        status="connected",
        enabled="enabled",
        dpms="Off",
    )
    _connector(
        tmp_path,
        "card1-DP-1",
        status="connected",
        enabled="enabled",
        dpms=None,
    )

    assert _read_linux(tmp_path) == dp.DisplayReading(
        dp.DISPLAY_UNDETECTABLE,
        dp.REASON_UNREADABLE,
    )


def test_read_linux_on_wins_over_unreadable_sibling(tmp_path: Path) -> None:
    _connector(
        tmp_path,
        "card1-HDMI-A-2",
        status="connected",
        enabled="enabled",
        dpms=None,
    )
    _connector(
        tmp_path,
        "card1-DP-1",
        status="connected",
        enabled="enabled",
        dpms="On",
    )

    assert _read_linux(tmp_path) == dp.DisplayReading(dp.DISPLAY_AWAKE, None)


def test_read_linux_returns_headless_when_no_connected_enabled_connectors(
    tmp_path: Path,
) -> None:
    _connector(
        tmp_path,
        "card1-HDMI-A-2",
        status="disconnected",
        enabled="enabled",
        dpms="Off",
    )

    assert _read_linux(tmp_path) == dp.DisplayReading(
        dp.DISPLAY_UNDETECTABLE,
        dp.REASON_HEADLESS,
    )


def test_read_linux_returns_unreadable_when_no_connectors(tmp_path: Path) -> None:
    assert _read_linux(tmp_path) == dp.DisplayReading(
        dp.DISPLAY_UNDETECTABLE,
        dp.REASON_UNREADABLE,
    )


def test_read_linux_returns_unreadable_when_root_missing(tmp_path: Path) -> None:
    assert _read_linux(tmp_path / "missing") == dp.DisplayReading(
        dp.DISPLAY_UNDETECTABLE,
        dp.REASON_UNREADABLE,
    )


def test_read_linux_returns_unreadable_when_glob_raises() -> None:
    class BrokenRoot:
        def glob(self, _pattern):
            raise OSError("no drm")

    assert dp.read_display_power(platform="linux", sysfs_root=BrokenRoot()) == (
        dp.DisplayReading(dp.DISPLAY_UNDETECTABLE, dp.REASON_UNREADABLE)
    )


def test_read_display_power_wraps_detector_exception_as_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connector(
        tmp_path,
        "card1-HDMI-A-2",
        status="connected",
        enabled="enabled",
        dpms="On",
    )

    def fail_read_attr(_path: Path, _name: str) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(dp, "_read_attr", fail_read_attr)

    assert _read_linux(tmp_path) == dp.DisplayReading(
        dp.DISPLAY_UNDETECTABLE,
        dp.REASON_UNREADABLE,
    )


def test_read_display_power_dispatches_linux_and_darwin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dp,
        "_read_linux",
        lambda root: dp.DisplayReading(dp.DISPLAY_AWAKE, None),
    )
    monkeypatch.setattr(
        dp,
        "_read_macos",
        lambda: dp.DisplayReading(dp.DISPLAY_ASLEEP, None),
    )

    assert dp.read_display_power(platform="linux", sysfs_root=tmp_path).state == (
        dp.DISPLAY_AWAKE
    )
    assert dp.read_display_power(platform="darwin", sysfs_root=tmp_path).state == (
        dp.DISPLAY_ASLEEP
    )


def test_read_display_power_returns_unsupported_platform(tmp_path: Path) -> None:
    assert dp.read_display_power(platform="win32", sysfs_root=tmp_path) == (
        dp.DisplayReading(dp.DISPLAY_UNDETECTABLE, dp.REASON_UNSUPPORTED_PLATFORM)
    )


def test_parse_macos_log_uses_last_display_marker() -> None:
    text = "\n".join(
        [
            "2026-01-01 Display is turned off",
            "2026-01-01 Display is turned on",
        ]
    )

    assert dp._parse_macos_log(text) == dp.DisplayReading(dp.DISPLAY_AWAKE, None)


def test_parse_macos_log_without_marker_returns_no_window_server() -> None:
    assert dp._parse_macos_log("unrelated line") == dp.DisplayReading(
        dp.DISPLAY_UNDETECTABLE,
        dp.REASON_NO_WINDOW_SERVER,
    )


@pytest.mark.parametrize(
    ("asleep", "expected_state"),
    [(True, dp.DISPLAY_ASLEEP), (False, dp.DISPLAY_AWAKE)],
)
def test_read_macos_uses_coregraphics_sleep_state_when_main_display_exists(
    monkeypatch: pytest.MonkeyPatch,
    asleep: bool,
    expected_state: str,
) -> None:
    monkeypatch.setattr(dp, "_macos_main_display_id", lambda: 1)
    monkeypatch.setattr(dp, "_macos_display_is_asleep", lambda display_id: asleep)

    assert dp._read_macos() == dp.DisplayReading(expected_state, None)


def test_read_macos_falls_back_to_pmset_log_when_main_display_is_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dp, "_macos_main_display_id", lambda: 0)
    monkeypatch.setattr(dp, "_macos_pmset_log", lambda: "Display is turned off")

    assert dp._read_macos() == dp.DisplayReading(dp.DISPLAY_ASLEEP, None)


def test_macos_pmset_log_contains_timeout_and_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["pmset", "-g", "log"], timeout=1)

    monkeypatch.setattr(dp.subprocess, "run", timeout)
    assert dp._macos_pmset_log() == ""

    def oserror(*_args, **_kwargs):
        raise OSError("pmset missing")

    monkeypatch.setattr(dp.subprocess, "run", oserror)
    assert dp._macos_pmset_log() == ""


def test_monitor_debounces_only_after_threshold_from_injected_now() -> None:
    monitor = dp.DisplayPowersaveMonitor(debounce_s=120.0)

    first = monitor.poll(now=0.0, read=_reader(dp.DISPLAY_ASLEEP))
    before_threshold = monitor.poll(now=119.0, read=_reader(dp.DISPLAY_ASLEEP))
    after_threshold = monitor.poll(now=240.0, read=_reader(dp.DISPLAY_ASLEEP))

    assert first.debounced is False
    assert before_threshold.debounced is False
    assert after_threshold.debounced is True


def test_monitor_wake_closes_immediately_and_resets_since() -> None:
    monitor = dp.DisplayPowersaveMonitor(debounce_s=120.0)
    monitor.poll(now=0.0, read=_reader(dp.DISPLAY_ASLEEP))
    monitor.poll(now=240.0, read=_reader(dp.DISPLAY_ASLEEP))

    awake = monitor.poll(now=241.0, read=_reader(dp.DISPLAY_AWAKE))
    next_asleep = monitor.poll(now=242.0, read=_reader(dp.DISPLAY_ASLEEP))

    assert awake.available is True
    assert awake.asleep is False
    assert awake.debounced is False
    assert next_asleep.debounced is False


def test_monitor_undetectable_resets_snapshot_and_debounce() -> None:
    monitor = dp.DisplayPowersaveMonitor(debounce_s=120.0)
    monitor.poll(now=0.0, read=_reader(dp.DISPLAY_ASLEEP))
    undetectable = monitor.poll(
        now=60.0,
        read=_reader(dp.DISPLAY_UNDETECTABLE, dp.REASON_UNREADABLE),
    )
    next_asleep = monitor.poll(now=180.0, read=_reader(dp.DISPLAY_ASLEEP))

    assert undetectable == DISPLAY_POWERSAVE_UNAVAILABLE
    assert next_asleep.debounced is False


def test_monitor_capability_known_is_sticky_until_reset() -> None:
    monitor = dp.DisplayPowersaveMonitor(debounce_s=120.0)

    assert monitor.capability_known() is False
    monitor.poll(now=0.0, read=_reader(dp.DISPLAY_AWAKE))
    assert monitor.capability_known() is True
    monitor.poll(
        now=1.0,
        read=_reader(dp.DISPLAY_UNDETECTABLE, dp.REASON_UNREADABLE),
    )
    assert monitor.capability_known() is True
    monitor.reset()
    assert monitor.capability_known() is False


def test_singleton_last_and_reset_are_non_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = dp._MONITOR.poll(now=0.0, read=_reader(dp.DISPLAY_AWAKE))
    read = MagicMock(side_effect=AssertionError("last should not poll"))
    monkeypatch.setattr(dp, "read_display_power", read)

    assert dp.last_display_powersave() == snapshot
    read.assert_not_called()
    dp.reset_display_powersave_monitor()
    assert dp.last_display_powersave() == DISPLAY_POWERSAVE_UNAVAILABLE
