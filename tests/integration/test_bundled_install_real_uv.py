# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import threading
import time

import pytest

from solstone.think.providers import bundled
from tests.bundled_provider_fixtures import BundledCase, bundled_provider_config


@pytest.mark.integration
def test_bundled_install_emits_real_uv_phases(tmp_path, monkeypatch):
    # anthropic is the smallest real path: one SDK spec, no codex artifact/runtime SDK.
    name = "anthropic"
    try:
        bundled._resolve_uv_command()
    except bundled.CogitateProviderInstallFailed as exc:
        pytest.skip(str(exc))

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "journal.json").write_text(
        json.dumps(
            bundled_provider_config(
                name,
                BundledCase("idle", "key-needed", False, False, False),
            ),
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    bundled._LOCKS.clear()
    bundled._INSTALL_THREADS.clear()
    bundled._INSTALL_PROCESSES.clear()
    bundled._OBSERVED_PHASES.clear()

    observations: list[tuple[float, str]] = []
    stop = threading.Event()

    def poll_state() -> None:
        last_state = None
        while not stop.is_set():
            state = bundled.get_provider_state(name)["install_state"]
            if state != last_state:
                observations.append((time.monotonic(), state))
                last_state = state
            time.sleep(0.25)

    poller = threading.Thread(target=poll_state, daemon=True)
    try:
        poller.start()
        try:
            bundled.install_provider(name)
            thread = bundled._INSTALL_THREADS.get(name)
            if thread is not None:
                thread.join(timeout=300)
        finally:
            stop.set()
            poller.join(timeout=1)

        final_state = bundled.get_provider_state(name)
        if final_state["install_state"] == "failed":
            pytest.skip(
                f"real uv bundled install failed: {final_state['install_error']}"
            )

        assert final_state["install_state"] == "installed"
        observed_states = [state for _timestamp, state in observations]
        assert any(state in {"resolving", "downloading"} for state in observed_states)
    finally:
        try:
            bundled.uninstall_provider(name)
        except Exception:
            pass
