# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from solstone.apps.observer.utils import revoke_observer_record
from solstone.observe import observer_client as observer_client_module
from solstone.observe.observer_client import ObserverClient
from tests.integration.observer_pl_helpers import (
    pair_observer,
    write_bundle,
    write_observer_config,
)
from tests.link.live_helpers import (
    running_convey_server,
    running_link_service,
    skip_unless_live_relay,
)

pytestmark = pytest.mark.integration
skip_unless_live_relay()


def test_revoke_pl_observer_tls_reconnect_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tmp_journal = tmp_path / "journal"
    tmp_journal.mkdir()
    config_home = tmp_path / "config-home"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_journal))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    label = "pytest-observer-pl-revoke"
    with (
        running_convey_server(tmp_journal) as base_url,
        running_link_service(tmp_journal),
    ):
        identity = pair_observer(base_url, label)
        write_bundle(config_home, label, identity)
        write_observer_config(tmp_journal, label)

        first_segment = tmp_path / "first.flac"
        first_segment.write_bytes(b"observer over pl before revoke")
        first_client = ObserverClient("pytest-pl")
        try:
            result = first_client.upload_segment(
                "20250103",
                "120000_300",
                [first_segment],
                meta={"stream": "pytest-pl", "host": "pytest-pl"},
            )
            assert result.success is True
        finally:
            first_client.stop()

        revoke_observer_record(label)
        monkeypatch.setattr(observer_client_module, "RETRY_BACKOFF", [0])
        caplog.set_level(logging.WARNING, logger="solstone.observe.observer_client")

        second_segment = tmp_path / "second.flac"
        second_segment.write_bytes(b"observer over pl after revoke")
        second_client = ObserverClient("pytest-pl")
        try:
            result = second_client.upload_segment(
                "20250103",
                "120500_300",
                [second_segment],
                meta={"stream": "pytest-pl", "host": "pytest-pl"},
            )
            assert result.success is False
        finally:
            second_client.stop()

    log_text = caplog.text.lower()
    assert "upload rejected (403)" not in log_text
    assert "pl upload attempt" in log_text
    assert any(
        marker in log_text
        for marker in (
            "tls",
            "handshake",
            "all pl dial attempts failed",
            "connection",
        )
    )
