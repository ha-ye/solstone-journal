# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import logging
import os
from datetime import datetime

import pytest

from solstone.think.processing import (
    AWAITING_ANALYSIS_TEMPLATE,
    DEFAULT_PROCESSING,
    DISPLAY_POWERSAVE_UNAVAILABLE,
    DRAIN_STATE_NO_CONDITION,
    DRAIN_STATE_NO_ENGINE,
    DRAIN_STATE_REALTIME,
    DRAIN_STATE_WAITING,
    DRAIN_STATE_WINDOW_OPEN,
    ConditionState,
    DisplayPowersaveReading,
    DisplayPowersaveSettings,
    GateSettings,
    GateState,
    ProcessingSettings,
    TimeWindowSettings,
    derive_drain_state,
    evaluate_display_powersave,
    evaluate_drain_gate,
    evaluate_time_window,
    format_awaiting_analysis,
    parse_processing_settings,
    read_last_drained_at,
    validate_processing_update,
)

EXPECTED_DEFAULT = {
    "mode": "realtime",
    "gate": {
        "time_window": {"enabled": True, "start": "02:00", "end": "06:00"},
        "display_powersave": {"enabled": False},
    },
}


def _time_window(
    *,
    enabled: bool = True,
    start: str = "02:00",
    end: str = "06:00",
) -> TimeWindowSettings:
    return TimeWindowSettings(enabled=enabled, start=start, end=end)


def _settings(
    *,
    mode: str = "deferred",
    time_window: TimeWindowSettings | None = None,
    display_powersave: DisplayPowersaveSettings | None = None,
) -> ProcessingSettings:
    return ProcessingSettings(
        mode=mode,
        gate=GateSettings(
            time_window=time_window or _time_window(),
            display_powersave=display_powersave
            or DisplayPowersaveSettings(enabled=False),
        ),
    )


@pytest.mark.parametrize("raw", [None, {}])
def test_parse_processing_settings_defaults(raw: object) -> None:
    settings = parse_processing_settings(raw, strict=False)

    assert settings == DEFAULT_PROCESSING
    assert settings.to_dict() == EXPECTED_DEFAULT


def test_parse_processing_settings_missing_section_is_quiet(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)

    settings = parse_processing_settings(None, strict=False)

    assert settings == DEFAULT_PROCESSING
    assert caplog.records == []


def test_strict_validation_rejects_bad_mode() -> None:
    with pytest.raises(
        ValueError,
        match="processing mode must be realtime or deferred",
    ):
        parse_processing_settings({"mode": "batch"}, strict=True)


def test_strict_validation_rejects_non_bool_enabled() -> None:
    with pytest.raises(
        ValueError,
        match="gate.time_window.enabled has invalid value: 'yes'",
    ):
        parse_processing_settings(
            {"gate": {"time_window": {"enabled": "yes"}}},
            strict=True,
        )


def test_strict_validation_rejects_bad_start() -> None:
    with pytest.raises(
        ValueError,
        match="start must be HH:MM 24-hour time, got '25:99'",
    ):
        parse_processing_settings(
            {"gate": {"time_window": {"start": "25:99"}}},
            strict=True,
        )


def test_strict_validation_rejects_bad_end() -> None:
    with pytest.raises(ValueError, match="end must be HH:MM 24-hour time"):
        parse_processing_settings(
            {"gate": {"time_window": {"end": "6:00"}}},
            strict=True,
        )


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"extra": True}, "processing.extra is not a recognized setting"),
        ({"gate": {"extra": True}}, "gate.extra is not a recognized setting"),
        (
            {"gate": {"time_window": {"extra": True}}},
            "gate.time_window.extra is not a recognized setting",
        ),
        (
            {"gate": {"display_powersave": {"extra": True}}},
            "gate.display_powersave.extra is not a recognized setting",
        ),
    ],
)
def test_strict_validation_rejects_unknown_keys(
    raw: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        parse_processing_settings(raw, strict=True)


def test_validate_processing_update_allows_deferred_without_conditions() -> None:
    settings = validate_processing_update(
        {},
        {
            "mode": "deferred",
            "gate": {
                "time_window": {"enabled": False},
                "display_powersave": {"enabled": False},
            },
        },
    )

    assert settings.mode == "deferred"
    assert settings.gate.time_window.enabled is False
    assert settings.gate.display_powersave.enabled is False


def test_validate_processing_update_preserves_nested_defaults() -> None:
    settings = validate_processing_update(
        DEFAULT_PROCESSING.to_dict(),
        {"mode": "deferred"},
    )

    assert settings.mode == "deferred"
    assert settings.gate.time_window.start == "02:00"
    assert settings.gate.time_window.end == "06:00"


def test_evaluate_time_window_standard_window_boundaries() -> None:
    window = _time_window(start="02:00", end="06:00")

    assert evaluate_time_window(window, datetime(2026, 1, 1, 3, 30)).open is True
    assert evaluate_time_window(window, datetime(2026, 1, 1, 1, 59)).open is False
    assert evaluate_time_window(window, datetime(2026, 1, 1, 2, 0)).open is True
    assert evaluate_time_window(window, datetime(2026, 1, 1, 6, 0)).open is False


def test_evaluate_time_window_midnight_wrap() -> None:
    window = _time_window(start="22:00", end="06:00")

    assert evaluate_time_window(window, datetime(2026, 1, 1, 23, 0)).open is True
    assert evaluate_time_window(window, datetime(2026, 1, 1, 5, 0)).open is True
    assert evaluate_time_window(window, datetime(2026, 1, 1, 12, 0)).open is False


def test_evaluate_time_window_zero_width_is_closed() -> None:
    window = _time_window(start="02:00", end="02:00")

    assert evaluate_time_window(window, datetime(2026, 1, 1, 2, 0)).open is False


def test_evaluate_drain_gate_uses_or_composition() -> None:
    assert (
        evaluate_drain_gate(
            _settings(
                time_window=_time_window(enabled=True, start="02:00", end="06:00"),
                display_powersave=DisplayPowersaveSettings(enabled=True),
            ),
            datetime(2026, 1, 1, 3, 0),
            DISPLAY_POWERSAVE_UNAVAILABLE,
        ).open
        is True
    )

    assert (
        evaluate_drain_gate(
            _settings(
                time_window=_time_window(enabled=False, start="02:00", end="06:00"),
                display_powersave=DisplayPowersaveSettings(enabled=True),
            ),
            datetime(2026, 1, 1, 3, 0),
            DISPLAY_POWERSAVE_UNAVAILABLE,
        ).open
        is False
    )


def test_evaluate_display_powersave_disabled_ignores_reading() -> None:
    state = evaluate_display_powersave(
        DisplayPowersaveSettings(enabled=False),
        DisplayPowersaveReading(available=True, asleep=True, debounced=True),
    )

    assert state.enabled is False
    assert state.available is False
    assert state.open is False


def test_evaluate_display_powersave_enabled_uses_reading() -> None:
    asleep = evaluate_display_powersave(
        DisplayPowersaveSettings(enabled=True),
        DisplayPowersaveReading(available=True, asleep=True, debounced=True),
    )
    awake = evaluate_display_powersave(
        DisplayPowersaveSettings(enabled=True),
        DisplayPowersaveReading(available=True, asleep=False, debounced=False),
    )
    undetectable = evaluate_display_powersave(
        DisplayPowersaveSettings(enabled=True),
        DISPLAY_POWERSAVE_UNAVAILABLE,
    )

    assert asleep == ConditionState(enabled=True, available=True, open=True)
    assert awake == ConditionState(enabled=True, available=True, open=False)
    assert undetectable == ConditionState(enabled=True, available=False, open=False)


def test_evaluate_drain_gate_opens_on_debounced_display_powersave() -> None:
    gate = evaluate_drain_gate(
        _settings(
            time_window=_time_window(enabled=False),
            display_powersave=DisplayPowersaveSettings(enabled=True),
        ),
        datetime(2026, 1, 1, 12, 0),
        DisplayPowersaveReading(available=True, asleep=True, debounced=True),
    )

    assert gate.open is True


def test_display_powersave_undetectable_has_no_active_condition() -> None:
    settings = _settings(
        time_window=_time_window(enabled=False),
        display_powersave=DisplayPowersaveSettings(enabled=True),
    )
    gate = evaluate_drain_gate(
        settings,
        datetime(2026, 1, 1, 12, 0),
        DISPLAY_POWERSAVE_UNAVAILABLE,
    )

    assert gate.open is False
    assert derive_drain_state(settings, gate, False) == DRAIN_STATE_NO_CONDITION


def test_derive_drain_state_tokens() -> None:
    open_gate = GateState(
        open=True,
        conditions={"time_window": ConditionState(True, True, True)},
    )
    waiting_gate = GateState(
        open=False,
        conditions={"time_window": ConditionState(True, True, False)},
    )
    unavailable_gate = GateState(
        open=False,
        conditions={
            "time_window": ConditionState(False, True, False),
            "display_powersave": ConditionState(True, False, False),
        },
    )

    assert derive_drain_state(_settings(mode="realtime"), open_gate, False) == (
        DRAIN_STATE_REALTIME
    )
    assert derive_drain_state(_settings(mode="deferred"), open_gate, False) == (
        DRAIN_STATE_WINDOW_OPEN
    )
    assert derive_drain_state(_settings(mode="deferred"), waiting_gate, False) == (
        DRAIN_STATE_WAITING
    )
    assert derive_drain_state(_settings(mode="deferred"), unavailable_gate, False) == (
        DRAIN_STATE_NO_CONDITION
    )
    assert derive_drain_state(_settings(mode="realtime"), open_gate, True) == (
        DRAIN_STATE_NO_ENGINE
    )


def test_format_awaiting_analysis_uses_fixed_template() -> None:
    assert format_awaiting_analysis(1) == AWAITING_ANALYSIS_TEMPLATE.format(count=1)


def test_read_last_drained_at_returns_none_without_markers(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    assert read_last_drained_at() is None


def test_read_last_drained_at_returns_latest_marker_mtime(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    older = tmp_path / "chronicle" / "20260101" / "health" / "daily.updated"
    newer = tmp_path / "chronicle" / "20260102" / "health" / "daily.updated"
    older.parent.mkdir(parents=True)
    newer.parent.mkdir(parents=True)
    older.write_text("", encoding="utf-8")
    newer.write_text("", encoding="utf-8")
    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_700_000_500, 1_700_000_500))

    assert read_last_drained_at() == 1_700_000_500_000
