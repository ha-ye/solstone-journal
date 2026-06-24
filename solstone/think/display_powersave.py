# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Display powersave detection and debounced drain-gate monitoring."""

from __future__ import annotations

import ctypes
import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from solstone.think.processing import (
    DISPLAY_POWERSAVE_UNAVAILABLE,
    DisplayPowersaveReading,
)

logger = logging.getLogger(__name__)

DISPLAY_ASLEEP = "asleep"
DISPLAY_AWAKE = "awake"
DISPLAY_UNDETECTABLE = "undetectable"

REASON_HEADLESS = "headless"
REASON_UNREADABLE = "unreadable"
REASON_UNSUPPORTED_PLATFORM = "unsupported_platform"
REASON_NO_WINDOW_SERVER = "no_window_server"

DISPLAY_DEBOUNCE_S = 120.0
DRM_SYSFS_ROOT = Path("/sys/class/drm")

_OFF_DPMS = frozenset({"Off", "Standby", "Suspend"})
_MACOS_DISPLAY_OFF_MARKER = "Display is turned off"
_MACOS_DISPLAY_ON_MARKER = "Display is turned on"
_PMSET_TIMEOUT_S = 5.0
_CORE_GRAPHICS_PATH = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"


@dataclass(frozen=True)
class DisplayReading:
    state: str
    reason: str | None


def _asleep() -> DisplayReading:
    return DisplayReading(DISPLAY_ASLEEP, None)


def _awake() -> DisplayReading:
    return DisplayReading(DISPLAY_AWAKE, None)


def _undetectable(reason: str) -> DisplayReading:
    return DisplayReading(DISPLAY_UNDETECTABLE, reason)


def read_display_power(
    *,
    platform: str = sys.platform,
    sysfs_root: Path = DRM_SYSFS_ROOT,
) -> DisplayReading:
    """Return the current display power state, or an undetectable reason."""
    try:
        if platform.startswith("linux"):
            return _read_linux(sysfs_root)
        if platform == "darwin":
            return _read_macos()
        return _undetectable(REASON_UNSUPPORTED_PLATFORM)
    except Exception:
        logger.debug("display powersave detection failed", exc_info=True)
        return _undetectable(REASON_UNREADABLE)


def _read_linux(sysfs_root: Path) -> DisplayReading:
    try:
        connectors = list(sysfs_root.glob("card*-*"))
    except OSError:
        return _undetectable(REASON_UNREADABLE)
    if not connectors:
        return _undetectable(REASON_UNREADABLE)

    considered = 0
    any_unreadable = False
    for connector in connectors:
        status = _read_attr(connector, "status")
        enabled = _read_attr(connector, "enabled")
        if status != "connected" or enabled != "enabled":
            continue
        dpms = _read_attr(connector, "dpms")
        considered += 1
        if dpms == "On":
            return _awake()
        if dpms not in _OFF_DPMS:
            any_unreadable = True

    if considered == 0:
        return _undetectable(REASON_HEADLESS)
    if any_unreadable:
        return _undetectable(REASON_UNREADABLE)
    return _asleep()


def _read_attr(path: Path, name: str) -> str | None:
    try:
        return (path / name).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _core_graphics():
    return ctypes.CDLL(_CORE_GRAPHICS_PATH)


def _macos_main_display_id() -> int:
    core_graphics = _core_graphics()
    core_graphics.CGMainDisplayID.restype = ctypes.c_uint32
    return int(core_graphics.CGMainDisplayID())


def _macos_display_is_asleep(display_id: int) -> bool:
    core_graphics = _core_graphics()
    core_graphics.CGDisplayIsAsleep.argtypes = [ctypes.c_uint32]
    core_graphics.CGDisplayIsAsleep.restype = ctypes.c_bool
    return bool(core_graphics.CGDisplayIsAsleep(display_id))


def _macos_pmset_log() -> str:
    try:
        completed = subprocess.run(
            ["pmset", "-g", "log"],
            capture_output=True,
            text=True,
            timeout=_PMSET_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return completed.stdout


def _read_macos() -> DisplayReading:
    main = _macos_main_display_id()
    if main == 0:
        return _parse_macos_log(_macos_pmset_log())
    if _macos_display_is_asleep(main):
        return _asleep()
    return _awake()


def _parse_macos_log(text: str) -> DisplayReading:
    last: str | None = None
    for line in text.splitlines():
        if _MACOS_DISPLAY_OFF_MARKER in line:
            last = DISPLAY_ASLEEP
        elif _MACOS_DISPLAY_ON_MARKER in line:
            last = DISPLAY_AWAKE
    if last == DISPLAY_ASLEEP:
        return _asleep()
    if last == DISPLAY_AWAKE:
        return _awake()
    return _undetectable(REASON_NO_WINDOW_SERVER)


class DisplayPowersaveMonitor:
    def __init__(self, *, debounce_s: float = DISPLAY_DEBOUNCE_S):
        self._debounce_s = debounce_s
        self._snapshot = DISPLAY_POWERSAVE_UNAVAILABLE
        self._asleep_since: float | None = None
        self._ever_detected = False

    def poll(
        self,
        *,
        now: float,
        read: Callable[[], DisplayReading] = read_display_power,
    ) -> DisplayPowersaveReading:
        reading = read()
        if reading.state == DISPLAY_ASLEEP:
            if self._asleep_since is None:
                self._asleep_since = now
            snapshot = DisplayPowersaveReading(
                available=True,
                asleep=True,
                debounced=(now - self._asleep_since) >= self._debounce_s,
            )
        elif reading.state == DISPLAY_AWAKE:
            self._asleep_since = None
            snapshot = DisplayPowersaveReading(
                available=True,
                asleep=False,
                debounced=False,
            )
        else:
            self._asleep_since = None
            snapshot = DISPLAY_POWERSAVE_UNAVAILABLE

        if snapshot.available:
            self._ever_detected = True
        self._snapshot = snapshot
        return snapshot

    def last(self) -> DisplayPowersaveReading:
        return self._snapshot

    def capability_known(self) -> bool:
        return self._ever_detected

    def reset(self) -> None:
        self._snapshot = DISPLAY_POWERSAVE_UNAVAILABLE
        self._asleep_since = None
        self._ever_detected = False


_MONITOR = DisplayPowersaveMonitor()


def poll_display_powersave(now: float) -> DisplayPowersaveReading:
    return _MONITOR.poll(now=now)


def last_display_powersave() -> DisplayPowersaveReading:
    return _MONITOR.last()


def display_powersave_detectable() -> bool:
    return _MONITOR.capability_known()


def reset_display_powersave_monitor() -> None:
    _MONITOR.reset()
