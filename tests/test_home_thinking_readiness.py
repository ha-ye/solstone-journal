# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from solstone.apps.home.thinking_readiness import (
    _generate_path_blocked,
    _thinking_blocked,
)
from solstone.convey.readiness_snapshot import unavailable_snapshot
from solstone.think.journal_io.errors import LockTimeout


@pytest.mark.parametrize(
    ("generate_view", "expected"),
    [
        ({"status": "blocked", "reason_code": "provider_key_missing"}, True),
        (
            {"status": "unhealthy", "reason_code": "local_endpoint_unreachable"},
            True,
        ),
        ({"status": "unhealthy", "reason_code": "local_server_unhealthy"}, True),
        ({"status": "blocked", "reason_code": "provider_quota_exceeded"}, True),
        ({"status": "ready", "reason_code": None}, False),
        ({"status": "unknown", "reason_code": None}, False),
        ({"status": "unhealthy", "reason_code": "local_model_installing"}, False),
        ({"status": "unhealthy", "reason_code": "local_model_loading"}, False),
    ],
)
def test_generate_path_blocked_from_generate_interface(
    generate_view: dict,
    expected: bool,
) -> None:
    assert _generate_path_blocked(generate_view) is expected


def test_generate_path_blocked_ignores_cogitate_interface() -> None:
    snapshot = {
        "interfaces": {
            "generate": {"status": "ready", "reason_code": None},
            "cogitate": {
                "status": "blocked",
                "reason_code": "provider_key_missing",
            },
        }
    }

    assert _generate_path_blocked(snapshot["interfaces"]["generate"]) is False


def test_generate_path_blocked_biases_false_when_unavailable() -> None:
    assert (
        _generate_path_blocked(unavailable_snapshot()["interfaces"].get("generate"))
        is False
    )


def test_thinking_blocked_uses_fresh_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    from solstone.think.awareness import update_state

    update_state("thinking_readiness", {"blocked": True, "checked_at": time.time()})

    with patch(
        "solstone.apps.home.thinking_readiness.build_readiness_snapshot"
    ) as mock_snapshot:
        assert _thinking_blocked() is True

    mock_snapshot.assert_not_called()


def test_thinking_blocked_recomputes_and_writes_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    blocked_snapshot = {
        "interfaces": {
            "generate": {
                "status": "blocked",
                "reason_code": "provider_key_missing",
            }
        }
    }

    with patch(
        "solstone.apps.home.thinking_readiness.build_readiness_snapshot",
        return_value=blocked_snapshot,
    ) as mock_snapshot:
        assert _thinking_blocked() is True

    mock_snapshot.assert_called_once_with()

    from solstone.think.awareness import get_current

    cached = get_current()["thinking_readiness"]
    assert cached["blocked"] is True
    assert "checked_at" in cached


def test_thinking_blocked_returns_computed_value_when_cache_write_is_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    blocked_snapshot = {
        "interfaces": {
            "generate": {
                "status": "blocked",
                "reason_code": "provider_key_missing",
            }
        }
    }

    with (
        patch(
            "solstone.apps.home.thinking_readiness.build_readiness_snapshot",
            return_value=blocked_snapshot,
        ),
        patch(
            "solstone.apps.home.thinking_readiness.update_state",
            side_effect=LockTimeout(Path("x"), 1.0),
        ),
    ):
        assert _thinking_blocked() is True


def test_thinking_blocked_biases_false_on_recompute_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    with patch(
        "solstone.apps.home.thinking_readiness.build_readiness_snapshot",
        side_effect=Exception("boom"),
    ):
        assert _thinking_blocked() is False
