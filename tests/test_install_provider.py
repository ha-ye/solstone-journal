# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import sys

from solstone.think import install_provider


def test_install_provider_local_prints_install_status(monkeypatch, capsys):
    calls = []
    parakeet_calls = []

    def install_local():
        calls.append(True)
        return {"name": "local", "install_state": "installed"}

    def install_parakeet():
        parakeet_calls.append(True)
        return {"name": "parakeet", "install_state": "installed"}

    monkeypatch.setattr(sys, "argv", ["journal install-provider", "local"])
    monkeypatch.setattr(install_provider.local_install, "install_local", install_local)
    monkeypatch.setattr(
        install_provider.parakeet_install,
        "install_parakeet",
        install_parakeet,
    )

    assert install_provider.main() == 0

    assert calls == [True]
    assert parakeet_calls == []
    assert json.loads(capsys.readouterr().out) == {
        "name": "local",
        "install_state": "installed",
    }


def test_install_provider_parakeet_prints_disclosure_and_status(monkeypatch, capsys):
    calls = []

    def install_parakeet():
        calls.append(True)
        return {"name": "parakeet", "install_state": "installed"}

    monkeypatch.setattr(sys, "argv", ["journal install-provider", "parakeet"])
    monkeypatch.setattr(
        install_provider.parakeet_install,
        "install_parakeet",
        install_parakeet,
    )

    assert install_provider.main() == 0

    captured = capsys.readouterr()
    assert calls == [True]
    assert json.loads(captured.out) == {
        "name": "parakeet",
        "install_state": "installed",
    }
    assert install_provider.PARAKEET_DOWNLOAD_DISCLOSURE in captured.err
    assert "github.com" in captured.err
    assert "huggingface.co" in captured.err
    banned = {"capture", "watch", "record", "monitor", "track", "collect"}
    assert not (banned & set(captured.err.lower().split()))


def test_install_provider_unsupported_rejects_without_install(monkeypatch, capsys):
    local_calls = []
    parakeet_calls = []

    def install_local():
        local_calls.append(True)
        return {"name": "local", "install_state": "installed"}

    def install_parakeet():
        parakeet_calls.append(True)
        return {"name": "parakeet", "install_state": "installed"}

    monkeypatch.setattr(sys, "argv", ["journal install-provider", "foo"])
    monkeypatch.setattr(install_provider.local_install, "install_local", install_local)
    monkeypatch.setattr(
        install_provider.parakeet_install,
        "install_parakeet",
        install_parakeet,
    )

    assert install_provider.main() == 2

    captured = capsys.readouterr()
    assert local_calls == []
    assert parakeet_calls == []
    assert "unsupported provider 'foo'" in captured.err
    assert "local" in captured.err
    assert "parakeet" in captured.err
