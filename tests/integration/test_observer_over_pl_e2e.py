# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import time
from pathlib import Path

import pytest

from solstone.observe.observer_client import ObserverClient
from solstone.think.link.ca import cert_fingerprint
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


def test_observer_over_pl_upload_roundtrip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_journal = tmp_path / "journal"
    tmp_journal.mkdir()
    config_home = tmp_path / "config-home"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_journal))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    label = "pytest-observer-pl"
    with (
        running_convey_server(tmp_journal) as base_url,
        running_link_service(tmp_journal),
    ):
        identity = pair_observer(base_url, label)
        write_bundle(config_home, label, identity)
        write_observer_config(tmp_journal, label)

        segment_file = tmp_path / "audio.flac"
        segment_file.write_bytes(b"observer over pl")
        client = ObserverClient("pytest-pl")
        try:
            result = client.upload_segment(
                "20250103",
                "120000_300",
                [segment_file],
                meta={"stream": "pytest-pl", "host": "pytest-pl"},
            )
            assert result.success is True
            prefix = cert_fingerprint(identity["client_cert"]).replace("sha256:", "")[
                :16
            ]
            _wait_until(
                lambda: (
                    tmp_journal
                    / "apps"
                    / "observer"
                    / "observers"
                    / prefix
                    / "hist"
                    / "20250103.jsonl"
                ).exists()
            )
        finally:
            client.stop()


def _wait_until(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("timed out waiting for condition")
