# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import io
import json
import logging
import threading
import time
from datetime import datetime

import pytest

from solstone.apps.observer import auth_rejection_log
from solstone.apps.observer.auth_rejection_log import (
    AUTH_REJECTION_QUIET_CLOSE_SECONDS,
    AUTH_REJECTION_TRACKED_PREFIX_CAP,
    AUTH_REJECTION_WINDOW_SECONDS,
    KEYLESS_AUTH_REQUIRED_PREFIX,
)
from solstone.apps.observer.utils import load_observer, save_observer
from solstone.apps.support.diagnostics import (
    _bounded_redacted_text,
    collect_recent_errors,
)
from solstone.convey.reasons import (
    AUTH_KEY_INVALID,
    AUTH_REQUIRED,
    FEATURE_UNAVAILABLE,
    PL_REVOKED,
)
from solstone.think.runner import DailyLogWriter, _format_log_line

LOGGER_NAME = auth_rejection_log.__name__


@pytest.fixture(autouse=True)
def reset_auth_rejection_log():
    auth_rejection_log.reset_auth_rejection_state_for_tests()
    yield
    auth_rejection_log.reset_auth_rejection_state_for_tests()


def _auth_records(caplog):
    return [
        record
        for record in caplog.records
        if record.name == LOGGER_NAME
        and record.getMessage().startswith("observer_auth_rejection")
    ]


def _auth_messages(caplog) -> list[str]:
    return [record.getMessage() for record in _auth_records(caplog)]


def _set_clock(monkeypatch, clock: dict[str, int]) -> None:
    monkeypatch.setattr(auth_rejection_log, "now_ms", lambda: clock["now"])


def _tick_rejection(
    *,
    surface: str = "observer_ingest_event",
    reason=AUTH_KEY_INVALID,
    prefix: str | None = "badkey12",
) -> None:
    auth_rejection_log.sweep_auth_rejection_bursts()
    auth_rejection_log.record_auth_rejection(
        surface=surface,
        reason=reason,
        attempted_prefix=prefix,
    )


def _create_observer(env, name: str) -> str:
    resp = env.client.post(
        "/app/observer/api/create",
        json={"name": name},
        content_type="application/json",
    )
    assert resp.status_code == 200
    return resp.get_json()["key"]


def _post_event(env, key: str | None, payload: dict | None = None):
    headers = {"Authorization": f"Bearer {key}"} if key is not None else {}
    return env.client.post(
        "/app/observer/ingest/event",
        headers=headers,
        json=payload or {"tract": "observe", "event": "status"},
    )


def test_ac2_error_routes_to_support_recent_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    clock = {"now": 1_800_000_000_000}
    _set_clock(monkeypatch, clock)
    rendered_stream = io.StringIO()
    handler = logging.StreamHandler(rendered_stream)
    handler.setFormatter(logging.Formatter(logging.BASIC_FORMAT))
    logger = logging.getLogger(LOGGER_NAME)
    old_level = logger.level
    old_propagate = logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    try:
        for index in range(7):
            clock["now"] = 1_800_000_000_000 + index * 1_000
            auth_rejection_log.record_auth_rejection(
                surface="observer_ingest_event",
                reason=AUTH_KEY_INVALID,
                attempted_prefix="badkey12",
            )
        clock["now"] += AUTH_REJECTION_QUIET_CLOSE_SECONDS * 1000
        auth_rejection_log.sweep_auth_rejection_bursts()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        logger.propagate = old_propagate

    rendered_lines = rendered_stream.getvalue().splitlines()
    rendered = next(line for line in rendered_lines if line.startswith("ERROR:"))
    assert rendered.startswith(f"ERROR:{LOGGER_NAME}:")
    writer = DailyLogWriter(
        "1800000000000",
        "convey",
        day=datetime.now().strftime("%Y%m%d"),
    )
    try:
        writer.write(_format_log_line("convey", "stderr", rendered))
    finally:
        writer.close()

    errors = collect_recent_errors()
    assert any(
        "observer_auth_rejection_burst_closed" in entry["message"]
        and "active_count=7" in entry["message"]
        for entry in errors
    )


def test_ac3_first_in_scope_rejection_warns_without_full_key(observer_env, caplog):
    env = observer_env()
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    full_key = "badkey1234567890-full-secret"

    resp = _post_event(env, full_key)

    assert resp.status_code == 401
    messages = _auth_messages(caplog)
    assert len(messages) == 1
    assert "observer_auth_rejection " in messages[0]
    assert "surface=observer_ingest_event" in messages[0]
    assert f"reason_code={AUTH_KEY_INVALID.code}" in messages[0]
    assert "key_prefix=badkey12" in messages[0]
    assert full_key not in messages[0]


def test_ac4_twenty_rejections_inside_one_window_emit_one_record(
    monkeypatch,
    caplog,
):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    clock = {"now": 1_800_000_000_000}
    _set_clock(monkeypatch, clock)

    for index in range(20):
        clock["now"] = 1_800_000_000_000 + index * 1_000
        _tick_rejection()

    assert len(_auth_records(caplog)) == 1


def test_ac5_continuous_burst_rewarns_once_per_window(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    clock = {"now": 1_800_000_000_000}
    _set_clock(monkeypatch, clock)
    windows = 25
    start = 1_800_000_000_000
    end = start + (windows - 1) * AUTH_REJECTION_WINDOW_SECONDS * 1000

    for current in range(start, end + 1, 100_000):
        clock["now"] = current
        _tick_rejection()

    records = _auth_records(caplog)
    assert (
        len([record for record in records if record.levelno == logging.WARNING])
        == windows
    )
    assert not [record for record in records if record.levelno >= logging.ERROR]


def test_ac6_concurrent_rejections_keep_exact_count(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    clock = {"now": 1_800_000_000_000, "sleep": True}

    def slow_now():
        if clock["sleep"]:
            time.sleep(0.01)
        return clock["now"]

    monkeypatch.setattr(auth_rejection_log, "now_ms", slow_now)

    barrier = threading.Barrier(8)
    calls_remaining = 12
    calls_lock = threading.Lock()

    def worker():
        nonlocal calls_remaining
        barrier.wait()
        while True:
            with calls_lock:
                if calls_remaining <= 0:
                    return
                calls_remaining -= 1
            auth_rejection_log.record_auth_rejection(
                surface="observer_ingest_event",
                reason=AUTH_KEY_INVALID,
                attempted_prefix="racekey1",
            )

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    clock["sleep"] = False
    clock["now"] += AUTH_REJECTION_QUIET_CLOSE_SECONDS * 1000
    auth_rejection_log.sweep_auth_rejection_bursts()

    messages = _auth_messages(caplog)
    assert any(
        "observer_auth_rejection_burst_closed" in message
        and "active_count=12" in message
        for message in messages
    )


def test_ac7_quiet_close_uses_stored_count(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    clock = {"now": 1_800_000_000_000}
    _set_clock(monkeypatch, clock)

    for index in range(7):
        clock["now"] = 1_800_000_000_000 + index * 1_000
        _tick_rejection(prefix="closekey")

    clock["now"] += AUTH_REJECTION_QUIET_CLOSE_SECONDS * 1000
    auth_rejection_log.sweep_auth_rejection_bursts()

    messages = _auth_messages(caplog)
    assert any(
        "observer_auth_rejection_burst_closed" in message
        and "active_count=7" in message
        and "surface=observer_ingest_event" in message
        for message in messages
    )


def test_ac8_empty_sweep_and_post_flush_sweep_emit_nothing(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    clock = {"now": 1_800_000_000_000}
    _set_clock(monkeypatch, clock)

    auth_rejection_log.sweep_auth_rejection_bursts()
    assert _auth_records(caplog) == []

    _tick_rejection(prefix="emptychk")
    caplog.clear()
    clock["now"] += AUTH_REJECTION_QUIET_CLOSE_SECONDS * 1000
    auth_rejection_log.sweep_auth_rejection_bursts()
    assert len(_auth_records(caplog)) == 1

    caplog.clear()
    auth_rejection_log.sweep_auth_rejection_bursts()
    assert _auth_records(caplog) == []


def test_ac9_successful_ingest_events_emit_no_records(observer_env, caplog):
    env = observer_env()
    key = _create_observer(env, "success-no-records")
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)

    for _ in range(20):
        resp = _post_event(env, key)
        assert resp.status_code == 200

    assert _auth_records(caplog) == []


def test_ac10_second_burst_starts_from_zero(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    clock = {"now": 1_800_000_000_000}
    _set_clock(monkeypatch, clock)

    for index in range(5):
        clock["now"] = 1_800_000_000_000 + index * 1_000
        _tick_rejection(prefix="secondb1")
    clock["now"] += AUTH_REJECTION_QUIET_CLOSE_SECONDS * 1000
    auth_rejection_log.sweep_auth_rejection_bursts()
    assert auth_rejection_log.tracked_auth_rejection_bucket_count_for_tests() == 0

    caplog.clear()
    clock["now"] += 1_000
    for index in range(4):
        clock["now"] += 1_000
        _tick_rejection(prefix="secondb1")
    clock["now"] += AUTH_REJECTION_QUIET_CLOSE_SECONDS * 1000
    auth_rejection_log.sweep_auth_rejection_bursts()

    messages = _auth_messages(caplog)
    assert any("active_count=4" in message for message in messages)
    assert not any("active_count=9" in message for message in messages)


def test_ac11_keyless_and_unknown_key_prefixes(observer_env, caplog):
    env = observer_env()
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)

    missing = _post_event(env, None)
    assert missing.status_code == 401
    assert missing.get_json()["reason_code"] == "auth_required"

    unknown_key = "unknown123456789"
    invalid = _post_event(env, unknown_key)
    assert invalid.status_code == 401
    assert invalid.get_json()["reason_code"] == "auth_key_invalid"

    messages = "\n".join(_auth_messages(caplog))
    assert f"reason_code={AUTH_REQUIRED.code}" in messages
    assert f"key_prefix={KEYLESS_AUTH_REQUIRED_PREFIX}" in messages
    assert f"reason_code={AUTH_KEY_INVALID.code}" in messages
    assert "key_prefix=unknown1" in messages
    assert unknown_key not in messages


def test_ac12_revoked_and_disabled_keep_status_body_and_warn(observer_env, caplog):
    env = observer_env()
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)

    revoked_key = _create_observer(env, "revoked-auth-log")
    revoked = load_observer(revoked_key)
    revoked["revoked"] = True
    revoked["revoked_at"] = 1_800_000_000_000
    assert save_observer(revoked)

    revoked_resp = _post_event(env, revoked_key)
    assert revoked_resp.status_code == 403
    assert revoked_resp.get_json()["reason_code"] == "pl_revoked"
    assert revoked_resp.get_json()["detail"] == "Observer revoked"

    disabled_key = _create_observer(env, "disabled-auth-log")
    disabled = load_observer(disabled_key)
    disabled["enabled"] = False
    assert save_observer(disabled)

    disabled_resp = _post_event(env, disabled_key)
    assert disabled_resp.status_code == 403
    assert disabled_resp.get_json()["reason_code"] == "feature_unavailable"
    assert disabled_resp.get_json()["detail"] == "Observer disabled"

    messages = "\n".join(_auth_messages(caplog))
    assert f"reason_code={PL_REVOKED.code}" in messages
    assert f"key_prefix={revoked_key[:8]}" in messages
    assert f"reason_code={FEATURE_UNAVAILABLE.code}" in messages
    assert f"key_prefix={disabled_key[:8]}" in messages


def test_ac13_cap_refuses_overflow_and_does_not_scale_with_distinct_keys(
    monkeypatch,
    caplog,
):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    clock = {"now": 1_800_000_000_000}
    _set_clock(monkeypatch, clock)
    distinct = AUTH_REJECTION_TRACKED_PREFIX_CAP + 16

    for _ in range(2):
        for index in range(distinct):
            _tick_rejection(prefix=f"{index:08x}")

    warnings = [
        record for record in _auth_records(caplog) if record.levelno == logging.WARNING
    ]
    assert auth_rejection_log.tracked_auth_rejection_bucket_count_for_tests() == (
        AUTH_REJECTION_TRACKED_PREFIX_CAP
    )
    assert len(warnings) == AUTH_REJECTION_TRACKED_PREFIX_CAP
    assert len(warnings) < distinct

    clock["now"] += AUTH_REJECTION_QUIET_CLOSE_SECONDS * 1000
    auth_rejection_log.sweep_auth_rejection_bursts()
    assert auth_rejection_log.tracked_auth_rejection_bucket_count_for_tests() == 0

    caplog.clear()
    clock["now"] += 1
    _tick_rejection(prefix="realkey1")
    assert auth_rejection_log.tracked_auth_rejection_bucket_count_for_tests() == 1
    assert len(_auth_records(caplog)) == 1


def test_ac14_invalid_key_body_is_sorted_json_and_unchanged(observer_env):
    env = observer_env()

    resp = _post_event(env, "invalid-body-key")

    expected = {
        "detail": "Invalid key",
        "error": "I couldn't verify that key.",
        "reason_code": "auth_key_invalid",
    }
    assert resp.status_code == 401
    assert resp.get_json() == expected
    expected_body = json.dumps(expected, sort_keys=True, separators=(",", ":")) + "\n"
    assert resp.get_data(as_text=True) == expected_body


def test_ac15_out_of_scope_rejections_do_not_account(observer_env, caplog):
    env = observer_env()
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    expected = {
        "detail": "Invalid key",
        "error": "I couldn't verify that key.",
        "reason_code": "auth_key_invalid",
    }

    health = env.client.post(
        "/app/observer/health",
        headers={"Authorization": "Bearer out-of-scope-key"},
        json={"status": "bad"},
    )
    manifest = env.client.get(
        "/app/observer/ingest/manifest",
        headers={"Authorization": "Bearer out-of-scope-key"},
    )

    assert health.status_code == 401
    assert health.get_json() == expected
    assert manifest.status_code == 401
    assert manifest.get_json() == expected
    assert _auth_records(caplog) == []


def test_ac16_request_json_payload_never_appears_in_records(observer_env, caplog):
    env = observer_env()
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    sentinel = "payload-sentinel-74f8c3f8e9d0"

    resp = _post_event(
        env,
        "payload-key-123456789",
        payload={"tract": "observe", "event": "status", "secret_note": sentinel},
    )

    assert resp.status_code == 401
    messages = "\n".join(_auth_messages(caplog))
    assert "observer_auth_rejection" in messages
    assert sentinel not in messages


def test_extra_redaction_vocabulary_survives_support_redaction():
    warning = (
        "WARNING:solstone.apps.observer.auth_rejection_log:"
        "observer_auth_rejection surface=observer_ingest_event "
        "reason_code=auth_key_invalid key_prefix=abc12345 "
        "first_ts=1 latest_ts=2"
    )
    error = (
        "ERROR:solstone.apps.observer.auth_rejection_log:"
        "observer_auth_rejection_burst_closed surface=observer_ingest_event "
        "reason_code=auth_key_invalid key_prefix=abc12345 "
        "first_ts=1 latest_ts=2 active_count=7"
    )

    redacted_warning = _bounded_redacted_text(warning, limit=500)
    redacted_error = _bounded_redacted_text(error, limit=500)

    assert "<path>" not in redacted_warning
    assert "<path>" not in redacted_error
    assert "surface=observer_ingest_event" in redacted_warning
    assert "reason_code=auth_key_invalid" in redacted_error
    assert "key_prefix=abc12345" in redacted_error


def test_extra_global_sweep_closes_other_bucket(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    clock = {"now": 1_800_000_000_000}
    _set_clock(monkeypatch, clock)

    _tick_rejection(prefix="oldkey01")
    clock["now"] += AUTH_REJECTION_QUIET_CLOSE_SECONDS * 1000
    _tick_rejection(prefix="newkey01")

    messages = _auth_messages(caplog)
    assert any(
        "observer_auth_rejection_burst_closed" in message for message in messages
    )
    assert any("key_prefix=oldkey01" in message for message in messages)
    assert any("key_prefix=newkey01" in message for message in messages)


def test_extra_no_close_before_quiet_threshold(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    clock = {"now": 1_800_000_000_000}
    _set_clock(monkeypatch, clock)

    _tick_rejection(prefix="quiet001")
    caplog.clear()
    clock["now"] += AUTH_REJECTION_QUIET_CLOSE_SECONDS * 1000 - 1
    auth_rejection_log.sweep_auth_rejection_bursts()

    assert _auth_records(caplog) == []
